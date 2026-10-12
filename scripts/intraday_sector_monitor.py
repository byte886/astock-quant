#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中全板块异动实时监测器（B 方案）
=====================================================================
目标：盘中实时监控所有概念+行业板块，L1 抓突发 / L2 确认主线，
     状态机分级去重，微信主动推送，联动持仓/候选池，自动留痕复盘。

数据来源（4 路，全部内置降级，任一挂掉不崩）：
  1. 东财 push2 板块快照（curl 子进程）：板块涨幅/量比/涨速/成交额。
     —— AkShare 封装的 stock_board_concept_spot_em 当前会被东财关连接
       （requests TLS 指纹被拦），故这里用 curl 直连原始接口。
  2. 腾讯个股快照（AkShare stock_zh_a_spot_tx）：全市场涨速 speed / 量比 lb /
     涨跌幅 / 主力净流入 zljlr / 换手率，抗封，作为盘中高频主力与聚合兜底。
  3. 东财涨停池（AkShare stock_zt_pool_em）：涨停家数、连板数、所属行业。
  4. 新浪板块（AkShare stock_sector_spot）：板块涨幅/成交额备份。

触发口径（阈值见 CONFIG，可用环境变量覆盖）：
  L1（满足任一）：板块涨速>=SPEED_L1（尾盘 5min>=SPEED_LATE）
                  或 涨幅>CHG_L1 且 涨停>=ZT_L1
  L2（同时满足）：涨幅>CHG_L1 且 涨停>=ZT_L2 且 量比>=LB_L2 且 上涨占比>=UP_RATIO
  强信号（记录）：板块涨停数 / 全市场涨停数 >= CONCENTRATION

状态机：每板块 state ∈ {NONE,L1,L2}，当天每级只推一次；L1->L2 升级再推；
        条件全消失后重新放量突破可再推一次 L1，L2 当天不重复。

用法：
  常驻： python intraday_sector_monitor.py
  试跑： python intraday_sector_monitor.py --once        # 单次扫描一轮（盘后测试用）
         python intraday_sector_monitor.py --build-map  # 仅重建板块成分映射
环境变量（均可选；缺失则降级只留痕不推送）：
  WECOM_BOT_WEBHOOK   企业微信群机器人 webhook
  SERVERCHAN_KEY      Server酱 SendKey
  HOLDINGS_CSV        持仓表路径（默认 config/holdings.csv）
"""
import os, sys, csv, json, time, subprocess, fcntl, traceback

# 国内行情必须直连，禁止任何代理（豆包沙箱会注入 DOUBAO_OFFICE_*_FORWARD_PROXY，
# launchd 进程继承后访问东财/同花顺会被代理劫持报 ProxyError；import akshare 前清干净）
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

from pathlib import Path
from datetime import datetime, time as dtime

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "data" / "_workspace"
ALERT_DIR = ROOT / "data" / "intraday_alerts"
SNAP_DIR = WORKSPACE / "snapshots"   # 每轮全板块快照，供事后逐分钟复盘
DIFF_DIR = WORKSPACE / "source_diff"  # 东财 vs 腾讯 两源口径偏差对账样本
for _d in (WORKSPACE, ALERT_DIR, SNAP_DIR, DIFF_DIR):
    _d.mkdir(parents=True, exist_ok=True)
LOG = WORKSPACE / "intraday_monitor.log"

# ---------------- 配置（环境变量可覆盖） ----------------
def _f(name, default):
    v = os.environ.get(name)
    return float(v) if v is not None else default

CHG_L1      = _f("MON_CHG_L1", 2.5)      # L1/L2 涨幅阈值 %
SPEED_L1    = _f("MON_SPEED_L1", 1.5)    # L1 涨速阈值 %（10min）
SPEED_LATE  = _f("MON_SPEED_LATE", 1.0)  # 尾盘(14:30后)涨速阈值 %（5min）
ZT_L1       = int(_f("MON_ZT_L1", 3))    # L1 涨停家数
ZT_L2       = int(_f("MON_ZT_L2", 5))    # L2 涨停家数
LB_L2       = _f("MON_LB_L2", 1.5)       # L2 量比
UP_RATIO    = _f("MON_UP_RATIO", 0.6)    # L2 上涨家数占比
CONCENTRATION = _f("MON_CONC", 0.15)     # 强信号：板块涨停占全市场比
STEP_ZT       = int(_f("MON_STEP_ZT", 2))   # "走强"台阶：涨停数较上次预警增加>=此数才再报，否则静默
RELAPSE_GRACE_MIN = float(_f("MON_RELAPSE_GRACE_MIN", 10))  # 消退宽限(分钟)：掉出阈值后该时长内回来算同一波(静默)，超过才算新一波(再度启动)
# 本次运行生效的阈值快照（写进每条预警，便于日后按"当时标准"追溯）
THRESHOLD_SNAP = (f"chg>{CHG_L1}%|ztL1>={ZT_L1}|ztL2>={ZT_L2}|"
                  f"lb>={LB_L2}|speed>{SPEED_L1}/尾盘{SPEED_LATE}")
POLL_NORMAL = int(_f("MON_POLL_NORMAL", 60))   # 常规轮询秒
POLL_LATE   = int(_f("MON_POLL_LATE", 30))     # 尾盘轮询秒
POLL_CLOSED = int(_f("MON_POLL_CLOSED", 60))    # 判休市后醒来周期（s）：每分钟醒一次判断是否开盘，开盘后≤1分钟上岗
HB_STALL_SEC = int(_f("MON_HB_STALL_SEC", 1800))  # 盘中心跳看门狗：交易时段连续无成功扫描超过该秒数即告警（默认30分钟，冷却同阈值）
RECONCILE_INTERVAL_SEC = int(_f("MON_RECONCILE_INTERVAL_SEC", 1800))  # 东财可用时双源对账采样间隔（默认30分钟；东财被限自然不采、零开销）

EM_HEADERS = ["-H", "Referer: https://quote.eastmoney.com/",
              "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"]


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def num(x):
    """东财字段 '-' / None 安全转 float。"""
    try:
        if x in ("-", "", None):
            return None
        return float(x)
    except Exception:
        return None


def normalize_code(c):
    c = str(c).strip().split(".")[0].zfill(6)
    if c.startswith("920"):
        # 北交所 920 新号段：必须先于下方 "9" 判断，否则会被误归为 sh（ISSUES I16）
        return "bj" + c
    if c.startswith(("60", "68", "9")):
        return "sh" + c
    if c.startswith(("00", "30", "20")):
        return "sz" + c
    if c.startswith(("8", "4")):
        return "bj" + c
    return "sz" + c


# ---------------- 取数：东财 push2（curl 直连） ----------------
def em_boards(kind="concept"):
    """返回 [{code:BKxx,name,chg,amount,lb,speed}]。失败返回 None。"""
    fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
    fields = "f2,f3,f6,f10,f12,f14,f22,f104,f105"
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1000&po=1&np=1"
           f"&fltt=2&invt=2&fs={fs}&fields={fields}")
    try:
        out = subprocess.run(["curl", "-s", "--retry", "2", "-m", "15", *EM_HEADERS, url],
                             capture_output=True, text=True, timeout=20).stdout
        d = json.loads(out)
        rows = []
        for r in d["data"]["diff"]:
            rows.append({
                "code": r.get("f12"), "name": r.get("f14"),
                "chg": num(r.get("f3")), "amount": num(r.get("f6")),
                "lb": num(r.get("f10")), "speed": num(r.get("f22")),
                "up": num(r.get("f104")), "down": num(r.get("f105")),
            })
        return rows
    except Exception as e:
        log(f"[em_boards:{kind}] 失败（降级）：{type(e).__name__}")
        return None


# ---------------- 取数：腾讯个股快照（AkShare） ----------------
def tx_spot():
    """返回 DataFrame（code=sh600519, speed,lb,zdf,zljlr,turnover,stock_type...）。失败 None。"""
    try:
        import akshare as ak
        return ak.stock_zh_a_spot_tx()
    except Exception as e:
        log(f"[tx_spot] 失败：{type(e).__name__}")
        return None


# ---------------- 取数：东财涨停池（AkShare datacenter） ----------------
def zt_pool(date):
    """返回 list[dict(code,name,lianban,industry)]。失败 None。"""
    try:
        import akshare as ak
        df = ak.stock_zt_pool_em(date=date)
        out = []
        for _, r in df.iterrows():
            out.append({
                "code": normalize_code(r["代码"]),
                "name": r.get("名称"),
                "lianban": r.get("连板数", 1),
                "industry": r.get("所属行业"),
            })
        return out
    except Exception as e:
        log(f"[zt_pool:{date}] 失败：{type(e).__name__}")
        return None


# ---------------- 取数：新浪板块（备份） ----------------
def sina_boards(indicator="概念"):
    try:
        import akshare as ak
        df = ak.stock_sector_spot(indicator=indicator)
        rows = []
        for _, r in df.iterrows():
            rows.append({"name": r.get("板块"), "chg": num(r.get("涨跌幅")),
                         "amount": num(r.get("总成交额"))})
        return rows
    except Exception as e:
        log(f"[sina_boards:{indicator}] 失败：{type(e).__name__}")
        return None


# ---------------- 板块成分映射（盘前构建，当日缓存） ----------------
def _collect_sina_indicator(c2b, indicator, kind):
    """收集单个新浪指标（概念/新浪行业）下每个板块的成分，原地并入 c2b。"""
    import akshare as ak
    try:
        spot = ak.stock_sector_spot(indicator=indicator)
    except Exception as e:
        log(f"[sina_map:{indicator}] 板块列表失败：{type(e).__name__}")
        return
    for _, r in spot.iterrows():
        label, nm = r.get("label"), r.get("板块")
        if not label or not nm:
            continue
        try:
            df = ak.stock_sector_detail(sector=str(label))
            codecol = "code" if "code" in df.columns else df.columns[0]
            for c in df[codecol]:
                c2b.setdefault(normalize_code(c), set()).add(f"{kind}:{nm}")
        except Exception:
            continue
        time.sleep(0.05)


def _sina_sector_map():
    """东财成分被限时用新浪板块成分兜底（独立可用，概念175+行业49，单个约0.3s）。
    返回 {code: set(kind:板块名)}；失败/空返回 {}。"""
    c2b = {}
    for indicator, kind in [("概念", "concept"), ("新浪行业", "industry")]:
        _collect_sina_indicator(c2b, indicator, kind)
    return c2b


def _em_board_names(kind):
    """取东财板块名列表（行业/概念）；失败返回 []。"""
    import akshare as ak
    try:
        names = ak.stock_board_industry_name_em() if kind == "industry" \
            else ak.stock_board_concept_name_em()
        col = "板块名称" if "板块名称" in names.columns else names.columns[1]
        return names[col].tolist()
    except Exception as e:
        log(f"[build_map:{kind}] 东财板块名列表失败（回退新浪）：{type(e).__name__}")
        return []


def _collect_em_cons(c2b, kind, fn, names):
    """遍历东财板块名，把每个板块成分并入 c2b。"""
    for nm in names:
        try:
            df = fn(symbol=nm)
            codecol = "代码" if "代码" in df.columns else df.columns[1]
            for c in df[codecol]:
                c2b.setdefault(normalize_code(c), set()).add(f"{kind}:{nm}")
        except Exception:
            continue
        time.sleep(0.1)


def _cached_sector_map(today):
    """读取当日板块映射缓存；非空才返回，无缓存/空缓存/损坏返回 None（须重建）。"""
    cache = WORKSPACE / f"sector_map_{today}.json"
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data if data else None
    except Exception:
        return None


def build_sector_map(today):
    """构建 code -> set((kind,board_name)) 映射，缓存到本地 json。
    优先东财成分（最全 504 概念）；东财被限/不完整时追加新浪板块成分兜底。
    空缓存视为无效（东财被限时曾写入 '{}' 的空映射，须重建）。"""
    cached = _cached_sector_map(today)
    if cached is not None:
        return cached
    import akshare as ak
    c2b = {}
    ok = False
    # 行业成分（少而稳，优先东财）
    for kind, fn in [("industry", ak.stock_board_industry_cons_em),
                     ("concept", ak.stock_board_concept_cons_em)]:
        names = _em_board_names(kind)
        if names:
            ok = True
        _collect_em_cons(c2b, kind, fn, names)
    if not ok or len(c2b) < 100:
        log("[build_map] 东财成分不完整，追加新浪板块成分兜底")
        try:
            c2b.update(_sina_sector_map())
        except Exception as e:
            log(f"[build_map] 新浪兜底失败：{type(e).__name__}")
    out = {k: sorted(v) for k, v in c2b.items()}
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log(f"[build_map] 完成：{len(out)} 只股票映射 -> {cache.name}")
    return out


# ---------------- 腾讯个股 spot 聚合板块（东财被限时的完整兜底） ----------------
def _is_zt_stock(zdf, code):
    """个股是否触及涨停（按代码前缀区分 20cm / 10cm 涨跌停幅度）。

    腾讯 spot 返回的 code 带市场前缀（sz300308 / sh688808 / bj830xxx / bj920xxx），
    判定 20cm 前需先剥离 sh/sz/bj 前缀，否则创业/科创/北交票会落到 10cm 的 9.9%
    分支而被误计涨停（ISSUES I16）。北交所实际 30% 涨跌幅当前按 20cm 桶处理（已知简化）。
    """
    import pandas as pd
    if zdf is None or pd.isna(zdf):
        return False
    c = str(code)
    if len(c) > 6 and c[:2] in ("sh", "sz", "bj"):  # 剥离市场前缀，回到 6 位裸码
        c = c[2:]
    if c.startswith(("30", "68", "8", "4", "92")):  # 创业/科创/北交 20cm
        return zdf >= 19.9
    return zdf >= 9.9


def _new_agg_bucket():
    return {"chg_sum": 0.0, "n": 0, "zt": 0, "lb_sum": 0.0,
            "speed_sum": 0.0, "up": 0, "down": 0}


def _accumulate_price(a, zdf, code):
    """把一只个股的涨幅累加到板块桶（家数、涨跌家数、涨停数）。"""
    import pandas as pd
    if zdf is None or pd.isna(zdf):
        return
    a["chg_sum"] += zdf
    a["n"] += 1
    if zdf > 0:
        a["up"] += 1
    elif zdf < 0:
        a["down"] += 1
    if _is_zt_stock(zdf, code):
        a["zt"] += 1


def _accumulate_volume(a, lb, sp):
    """把一只个股的量比/涨速累加到板块桶。"""
    import pandas as pd
    if lb is not None and not pd.isna(lb):
        a["lb_sum"] += lb
    if sp is not None and not pd.isna(sp):
        a["speed_sum"] += sp


def _accumulate_tx_row(board_agg, code, tags, row):
    """把一行腾讯个股数据，按其所属成分标签累加进对应板块桶。"""
    zdf = row.get("zdf")
    for tag in tags:
        _, _, nm = tag.partition(":")
        a = board_agg.setdefault(nm, _new_agg_bucket())
        _accumulate_price(a, zdf, code)
        _accumulate_volume(a, row.get("lb"), row.get("speed"))


def _boards_from_buckets(board_agg):
    """把板块桶聚合成输出行（等权算术平均）；空桶跳过，无输出返回 None。"""
    out = []
    for nm, a in board_agg.items():
        if a["n"] == 0:
            continue
        out.append({
            "code": None, "name": nm,
            "chg": a["chg_sum"] / a["n"],
            "zt": a["zt"],
            "lb": (a["lb_sum"] / a["n"]) if a["lb_sum"] else None,
            "speed": (a["speed_sum"] / a["n"]) if a["speed_sum"] else None,
            "up": a["up"], "down": a["down"],
        })
    return out or None


def tx_board_agg(mapping, tx_df):
    """用腾讯个股 spot + 板块成分映射聚合出板块行情，补齐东财 push2 缺失时
    新浪兜底没有的 涨停数/量比/涨速/上涨占比（L1/L2 完整触发）。
    返回 [{code,name,chg,zt,lb,speed,up,down}]；映射或 spot 不可用返回 None。"""
    import pandas as pd
    if not mapping or tx_df is None or tx_df.empty:
        return None
    try:
        tx_df = tx_df.copy()
        for col in ("zdf", "lb", "speed"):
            tx_df[col] = pd.to_numeric(tx_df[col], errors="coerce")
        board_agg = {}  # 板块名 -> 聚合桶
        for _, row in tx_df.iterrows():
            code = row["code"]
            tags = mapping.get(code)
            if not tags:
                continue
            _accumulate_tx_row(board_agg, code, tags, row)
        return _boards_from_buckets(board_agg)
    except Exception as e:
        log(f"[tx_agg] 聚合失败：{type(e).__name__}")
        return None


# ---------------- 持仓 / 候选 ----------------
def _row_code_name(row):
    """从持仓/候选 CSV 一行取规范化代码与名称；空代码返回 (None, None)。"""
    code = normalize_code(row.get("code") or row.get("代码") or "")
    if not code:
        return None, None
    return code, (row.get("name") or row.get("名称") or "")


def load_csv_codes(path):
    p = Path(path)
    codes = {}
    if not p.exists():
        return codes
    try:
        with open(p, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        if lines:
            for row in csv.DictReader(lines):
                code, name = _row_code_name(row)
                if code:
                    codes[code] = name
    except Exception as e:
        log(f"[load_csv {p.name}] {e}")
    return codes


# ---------------- 推送 ----------------
def _push_wecom(body, title):
    """企业微信群机器人推送；成功返回 True，未配置/失败返回 False。"""
    webhook = os.environ.get("WECOM_BOT_WEBHOOK")
    if not webhook:
        return False
    try:
        import requests
        resp = requests.post(webhook, json={"msgtype": "markdown",
                      "markdown": {"content": body}}, timeout=8)
        try:
            rj = resp.json()
            ec, emsg = rj.get("errcode"), rj.get("errmsg")
        except Exception:
            ec, emsg = None, (resp.text or "")[:80]
        if ec == 0:
            log(f"[push:wecom] 发送成功：{title}（errcode=0, HTTP{resp.status_code}）")
            return True
        log(f"[push:wecom] 发送失败：HTTP{resp.status_code} errcode={ec} errmsg={emsg}")
    except Exception as e:
        log(f"[push:wecom] 发送异常：{type(e).__name__}: {e}")
    return False


def _push_serverchan(title, content):
    """Server酱推送；成功返回 True，未配置/失败返回 False。"""
    key = os.environ.get("SERVERCHAN_KEY")
    if not key:
        return False
    try:
        import requests
        resp = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                      data={"title": title, "desp": content}, timeout=8)
        try:
            code = resp.json().get("code")
        except Exception:
            code = None
        if code == 0:
            log(f"[push:serverchan] 发送成功：{title}（code=0, HTTP{resp.status_code}）")
            return True
        log(f"[push:serverchan] 发送失败：HTTP{resp.status_code} resp={(resp.text or '')[:100]}")
    except Exception as e:
        log(f"[push:serverchan] 发送异常：{type(e).__name__}: {e}")
    return False


def push(title, content):
    body = f"**{title}**\n{content}"
    wecom_ok = _push_wecom(body, title)
    chan_ok = _push_serverchan(title, content)  # 两通道都尝试，不因一方成功而短路
    pushed = wecom_ok or chan_ok
    if not pushed:
        if os.environ.get("WECOM_BOT_WEBHOOK") or os.environ.get("SERVERCHAN_KEY"):
            log(f"[push] 已配置通道但均未成功送达：{title}")
        else:
            log(f"[NO-PUSH] 未配置 webhook，仅留痕：{title}")
    return pushed


def append_alert(row):
    p = ALERT_DIR / f"{datetime.now():%Y-%m-%d}.csv"
    exists = p.exists()
    cols = ["时间", "级别", "板块类型", "板块代码", "板块名", "涨幅%", "涨停数",
            "涨速%", "量比", "上涨占比", "主力净流入(元)", "命中持仓", "命中候选",
            "阈值快照", "消息"]
    with open(p, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if not exists:
            w.writeheader()
        w.writerow(row)


# ---------------- 交易时段 ----------------
# ---------------- 每轮板块快照（逐分钟复盘用，不参与触发） ----------------
SNAP_HEADER = ["时间", "数据源", "板块代码", "板块名", "涨幅%", "涨停数",
               "涨速%", "量比", "上涨家数", "下跌家数"]

def save_snapshot(now, boards, source):
    """把本轮全板块指标追加到当日快照 CSV；只用于事后复盘，失败仅记日志、绝不影响扫描主流程。"""
    if not boards:
        return
    try:
        f = SNAP_DIR / f"{now:%Y-%m-%d}.csv"
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        rows = [[ts, source, b.get("code") or "", b.get("name"), b.get("chg"),
                 b.get("zt"), b.get("speed"), b.get("lb"), b.get("up"), b.get("down")]
                for b in boards]
        new_file = not f.exists()
        with open(f, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(SNAP_HEADER)
            w.writerows(rows)
    except Exception as e:
        log(f"[snapshot] 快照写入失败（不影响监测）：{type(e).__name__}: {e}")


# ---------------- 双源口径对账（仅东财可用时低频采样，不参与触发） ----------------
DIFF_HEADER = ["时间", "板块名", "东财涨幅%", "腾讯涨幅%", "涨幅差(东-腾)",
               "东财涨停", "腾讯涨停", "东财上涨", "腾讯上涨", "东财下跌", "腾讯下跌"]

def _in_critical_band(ec, tc):
    """两源涨幅任一落在 2.0~3.0% 触发线附近。"""
    return (2.0 <= ec <= 3.0) or (2.0 <= tc <= 3.0)


def _reconcile_pair(e, tx_map, ts):
    """对齐一对东财/腾讯板块；返回 (row, abs_diff, zt_mismatch, band_flag)。
    无法对齐返回 None；band_flag 仅在 2.5% 临界带内给出 True/False，否则 None。"""
    t = tx_map.get(e.get("name"))
    ec, tc = e.get("chg"), (t or {}).get("chg")
    if not t or ec is None or tc is None:
        return None
    d = ec - tc
    ez = e.get("zt") or 0
    tz = t.get("zt") or 0
    band_flag = None
    if _in_critical_band(ec, tc):
        band_flag = (ec > CHG_L1) != (tc > CHG_L1)
    row = [ts, e.get("name"), round(ec, 3), round(tc, 3), round(d, 3),
           ez, tz, e.get("up"), t.get("up"), e.get("down"), t.get("down")]
    return row, abs(d), (ez != tz), band_flag


def _collect_reconcile_rows(em_list, tx_map, ts):
    """遍历东财板块按名对齐腾讯，返回 (rows, abs_diffs, zt_mismatch, band_cross)。"""
    rows, abs_diffs, band = [], [], []
    zt_mismatch = 0
    for e in em_list:
        got = _reconcile_pair(e, tx_map, ts)
        if got is None:
            continue
        row, ad, ztmm, band_flag = got
        rows.append(row)
        abs_diffs.append(ad)
        if ztmm:
            zt_mismatch += 1
        if band_flag is not None:
            band.append(band_flag)
    return rows, abs_diffs, zt_mismatch, band


def _reconcile_summary(abs_diffs, zt_mismatch, band):
    """生成双源对账的汇总日志行（abs_diffs 需已排序）。"""
    n = len(abs_diffs)
    med = abs_diffs[n // 2]
    p90 = abs_diffs[min(n - 1, int(n * 0.9))]
    within = sum(1 for x in abs_diffs if x <= 0.3) / n * 100
    cross = sum(1 for x in band if x)
    return (f"[reconcile] 双源对账：配对 {n} 板块，涨幅|差|中位 {med:.2f}/P90 {p90:.2f}/最大 {abs_diffs[-1]:.2f} 个百分点，"
            f"|差|≤0.3pct 占 {within:.0f}%；涨停家数不一致 {zt_mismatch} 个；"
            f"2.5%临界带跨阈值分歧 {cross}/{len(band)}")


def reconcile_sources(now, em_boards_data, smap):
    """东财板块可用时，额外拉一次腾讯个股聚合，按板块名对齐两源指标并落盘，
    累积"官方加权 vs 成分等权"偏差样本，供 scripts/reconcile_source_diff.py 盘后量化。
    纯观测：任何异常只记日志，绝不影响信号主流程。"""
    try:
        tx_list = tx_board_agg(smap, tx_spot())
        if not tx_list:
            log("[reconcile] 腾讯聚合为空，跳过本轮双源对账")
            return
        tx_map = {b["name"]: b for b in tx_list}
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        rows, abs_diffs, zt_mismatch, band = _collect_reconcile_rows(em_boards_data, tx_map, ts)
        if not rows:
            log("[reconcile] 两源板块名无配对（成分映射口径可能不一致），跳过")
            return
        f = DIFF_DIR / f"{now:%Y-%m-%d}.csv"
        new_file = not f.exists()
        with open(f, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(DIFF_HEADER)
            w.writerows(rows)
        abs_diffs.sort()
        log(_reconcile_summary(abs_diffs, zt_mismatch, band))
    except Exception as e:
        log(f"[reconcile] 对账失败（不影响监测）：{type(e).__name__}: {e}")


TRADE_CAL = WORKSPACE / "trade_cal.json"

def load_trade_dates():
    """加载 A 股交易日历（新浪接口，本地缓存7天）。
    返回 set['YYYY-MM-DD']；拉取失败返回 None（降级为只按周末判断）。"""
    if TRADE_CAL.exists():
        try:
            age = time.time() - TRADE_CAL.stat().st_mtime
            if age < 7 * 86400:
                return set(json.load(open(TRADE_CAL)))
        except Exception:
            pass
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        dates = sorted(set(cal["trade_date"].astype(str).tolist()))
        json.dump(dates, open(TRADE_CAL, "w"))
        return set(dates)
    except Exception as e:
        print(f"[{datetime.now():%F %T}] 交易日历拉取失败，降级仅按周末判断: {e}", file=sys.stderr)
        return None


def is_trading_time(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    cal = load_trade_dates()
    if cal is not None and now.strftime("%Y-%m-%d") not in cal:
        return False  # 法定节假日（中秋/国庆等），不开市不扫描
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


# ---------------- 主扫描一轮 ----------------
class Monitor:
    def __init__(self):
        self.states = {}   # board_key -> "NONE"/"L1"/"L2"
        self.holdings = load_csv_codes(os.environ.get(
            "HOLDINGS_CSV", str(ROOT / "config" / "holdings.csv")))
        self.pool = load_csv_codes(str(ROOT / "config" / "pool3_constituents.csv"))
        self.hint_shown = False   # 当天是否已提示过"未配置持仓"
        self.last_reconcile = None  # 上次双源对账采样时间（东财可用时限频）
        self._load_states()       # 恢复当日板块状态（重启不把持续板块当新启动、保留次数/波次）

    # ---------- 板块状态机持久化（治"重启失忆"）----------
    BOARD_TIME_FIELDS = ("ts", "lost_ts", "last_fire_ts", "wave_start_ts", "prev_wave_end_ts")

    def _board_state_path(self, day=None):
        day = day or datetime.now().strftime("%Y-%m-%d")
        return WORKSPACE / f"board_states_{day}.json"

    def _load_states(self):
        """启动时读回当天板块状态；跨天文件自然不读。"""
        try:
            p = self._board_state_path()
            if not p.exists():
                return
            raw = json.loads(p.read_text(encoding="utf-8"))
            for k, v in raw.items():
                for fld in self.BOARD_TIME_FIELDS:
                    if v.get(fld):
                        try:
                            v[fld] = datetime.fromisoformat(v[fld])
                        except Exception:
                            v[fld] = None
                self.states[k] = v
            log(f"[状态机] 已恢复当日板块状态 {len(self.states)} 个（重启不重复推送、保留次数/波次）")
        except Exception:
            log("[状态机] 恢复当日状态失败，按空状态启动：\n" + traceback.format_exc())

    def _save_states(self):
        """每轮成功扫描后落盘；只存真正触发过/在波的板块，避免写几百个空状态。"""
        try:
            out = {}
            for k, v in self.states.items():
                if not (v.get("fire_count", 0) or v.get("active") or v.get("wave", 0)):
                    continue
                out[k] = {kk: (vv.isoformat() if isinstance(vv, datetime) else vv)
                          for kk, vv in v.items()}
            self._board_state_path().write_text(
                json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _sina_fallback_boards():
        """腾讯/映射不可用时的新浪板块保底（名称匹配，缺涨停/量比）。"""
        sina = sina_boards("概念") or []
        sina += sina_boards("新浪行业") or []
        boards = [{"code": None, "name": b["name"], "chg": b["chg"],
                   "amount": b["amount"], "lb": None, "speed": None} for b in sina]
        return boards, ("sina" if boards else None)

    def _fetch_boards(self, today):
        """取板块行情，按 东财→腾讯聚合→新浪兜底 降级；返回 (boards, source)。"""
        em_c = em_boards("concept")
        em_i = em_boards("industry")
        boards = (em_c or []) + (em_i or [])
        if boards:
            return boards, "eastmoney"
        # 东财被限流 -> 优先腾讯个股 spot 聚合（维度完整，可触发 L1/L2）
        log("[scan] 东财板块无数据，尝试腾讯 spot 聚合兜底")
        try:
            smap_now = build_sector_map(today)
        except Exception:
            smap_now = {}
        tx_boards = tx_board_agg(smap_now, tx_spot())
        if tx_boards:
            log(f"[scan] 腾讯聚合兜底成功：{len(tx_boards)} 板块（维度完整）")
            return tx_boards, "tencent_agg"
        # 腾讯/映射不可用 -> 新浪板块保底（名称匹配，缺涨停/量比）
        log("[scan] 腾讯聚合兜底失败，启用新浪板块兜底")
        return self._sina_fallback_boards()

    @staticmethod
    def _build_zt_index(zt):
        """由涨停池建 行业涨停计数、涨停代码集合、涨停总数。"""
        zt_by_industry, zt_codes = {}, set()
        for z in zt:
            zt_codes.add(z["code"])
            if z.get("industry"):
                zt_by_industry[z["industry"]] = zt_by_industry.get(z["industry"], 0) + 1
        return zt_by_industry, zt_codes, len(zt)

    @staticmethod
    def _build_concept_zt(zt_codes, smap):
        """由涨停代码反查所属概念，统计每概念涨停数。"""
        zt_by_concept = {}
        for c in zt_codes:
            for tag in smap.get(c, []):
                kind, _, nm = tag.partition(":")
                if kind == "concept":
                    zt_by_concept[nm] = zt_by_concept.get(nm, 0) + 1
        return zt_by_concept

    @staticmethod
    def _fill_zt(boards, zt_by_industry, zt_by_concept):
        """东财板块无现成涨停数，用涨停池按行业/概念归集补齐。"""
        for b in boards:
            if b.get("zt") is None:
                b["zt"] = zt_by_industry.get(b["name"], 0) or zt_by_concept.get(b["name"], 0)

    def _maybe_reconcile(self, now, source, boards, smap):
        """东财可用时低频做一次双源口径对账（纯观测，不影响信号）。"""
        if source != "eastmoney":
            return
        if (self.last_reconcile is not None
                and (now - self.last_reconcile).total_seconds() < RECONCILE_INTERVAL_SEC):
            return
        reconcile_sources(now, boards, smap)
        self.last_reconcile = now

    def scan_once(self):
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        late = now.time() >= dtime(14, 30)

        boards, source = self._fetch_boards(today)

        zt = zt_pool(today) or []
        zt_by_industry, zt_codes, n_zt = self._build_zt_index(zt)

        # 板块成分映射（概念归集涨停用）
        try:
            smap = build_sector_map(today)
        except Exception:
            smap = {}
        zt_by_concept = self._build_concept_zt(zt_codes, smap)

        if not boards:
            log("[scan] 全部数据源均不可用，本轮无板块数据")
            return None
        # 快照与评估共用同一份补齐后的涨停数
        self._fill_zt(boards, zt_by_industry, zt_by_concept)
        save_snapshot(now, boards, source)

        for b in boards:
            self._eval_board(b, zt_by_industry, zt_by_concept, n_zt, late, today, now)

        self._maybe_reconcile(now, source, boards, smap)
        self._save_states()
        return source

    # ============================================================
    # 板块状态机：把单板块一轮评估拆成"分级 → 宽限/波次 → 文案 → 推送落盘"的小步骤
    # 每个方法职责单一（圈复杂度≤8），编排方法 _eval_board 只负责串联，不含业务分支细节
    # ============================================================

    @staticmethod
    def _empty_board_state():
        """一个从未达标过的板块的初始状态。"""
        return {"wave": 0, "active": False, "lvl": "NONE",
                "fired_l1": False, "fired_l2": False, "fire_count": 0,
                "peak_zt": 0, "lost_ts": None, "trough_chg": None,
                "prev_wave_end_ts": None, "last_fire_ts": None,
                "last_fire_lvl": None, "last_fire_zt": None,
                "last_fire_chg": None, "last_fire_lb": None}

    @staticmethod
    def _up_ratio(up, down):
        """板块内上涨家数占比；家数缺失或合计为 0 时返回 None。"""
        if up and down is not None and (up + down) > 0:
            return up / (up + down)
        return None

    @staticmethod
    def _meets_l1(chg, zt_n, speed, speed_thr):
        """一级（L1）触发：涨速达标，或涨幅+涨停数达标。返回 (涨速命中, 涨幅命中, 非L2基础级别)。"""
        hit_speed = speed is not None and speed >= speed_thr
        hit_chg = chg > CHG_L1 and zt_n >= ZT_L1
        return hit_speed, hit_chg, ("L1" if (hit_speed or hit_chg) else "NONE")

    @staticmethod
    def _meets_l2(chg, zt_n, lb, up_ratio):
        """二级（L2）触发：涨幅+涨停数达标，且量比、上涨占比同时达标。"""
        price_ok = chg > CHG_L1 and zt_n >= ZT_L2
        breadth_ok = ((lb is None or lb >= LB_L2)
                      and (up_ratio is None or up_ratio >= UP_RATIO))
        return bool(price_ok and breadth_ok)

    @staticmethod
    def _classify_signal(b, zt_by_industry, zt_by_concept, late):
        """提取板块字段并判定当下级别，返回信号 dict；chg 缺失返回 None。"""
        chg = b["chg"]
        if chg is None:
            return None
        zt_n = b.get("zt")
        if zt_n is None:
            zt_n = zt_by_industry.get(b["name"], 0) or zt_by_concept.get(b["name"], 0)
        speed, lb = b.get("speed"), b.get("lb")
        up_ratio = Monitor._up_ratio(b.get("up"), b.get("down"))
        speed_thr = SPEED_LATE if late else SPEED_L1
        hit_l1_speed, hit_l1_chg, base_lvl = Monitor._meets_l1(chg, zt_n, speed, speed_thr)
        l2 = Monitor._meets_l2(chg, zt_n, lb, up_ratio)
        cur_lvl = "L2" if l2 else base_lvl
        return {"name": b["name"], "raw_code": b.get("code") or "",
                "code": b.get("code") or b["name"],
                "chg": chg, "zt": zt_n, "speed": speed, "lb": lb,
                "up": b.get("up"), "down": b.get("down"), "up_ratio": up_ratio,
                "speed_thr": speed_thr, "hit_l1_speed": hit_l1_speed,
                "hit_l1_chg": hit_l1_chg, "l2": l2, "cur_lvl": cur_lvl}

    @staticmethod
    def _concentration(sig, st, n_zt):
        """计算主线占比、上一轮占比、以及'本轮是否首次成为主线'。"""
        zt_n = sig["zt"]
        conc_ratio = (zt_n / n_zt) if (n_zt and zt_n) else None
        prev_conc = (st or {}).get("conc") or 0.0
        first_conc = (conc_ratio is not None and conc_ratio >= CONCENTRATION
                      and prev_conc < CONCENTRATION)
        return conc_ratio, prev_conc, first_conc

    @staticmethod
    def _snapshot_fields(sig, conc_ratio, prev_conc, now):
        """每轮落盘的行情快照（供重启恢复与消退期追踪）。"""
        return {"chg": sig["chg"], "zt": sig["zt"], "lb": sig["lb"],
                "speed": sig["speed"],
                "conc": conc_ratio if conc_ratio is not None else prev_conc,
                "ts": now}

    def _settle_while_inactive(self, st, sig, snap, now):
        """情形一：当下不达标——进入/维持/结束消退宽限，此情形永不推送。"""
        if st is None:
            st = self._empty_board_state()
        if st.get("active"):
            if st.get("lost_ts") is None:
                st["lost_ts"] = now
            if sig["chg"] is not None:
                tc = st.get("trough_chg")
                st["trough_chg"] = sig["chg"] if tc is None else min(tc, sig["chg"])
            if (now - st["lost_ts"]).total_seconds() / 60.0 >= RELAPSE_GRACE_MIN:
                # 消退超过宽限：本波彻底结束，此后再达标算"新一波"
                st["active"] = False
                st["lvl"] = "NONE"
                st["prev_wave_end_ts"] = now
                st["prev_wave_peak_zt"] = st.get("peak_zt", 0)
                st["lost_ts"] = None
        st.update(snap)
        return st

    @staticmethod
    def _close_expired_grace(st, now):
        """健壮性：上一波掉出阈值、消退已满宽限此刻才恢复（可能错过中间轮或刚重启），先结算上一波。"""
        if (st and st.get("active") and st.get("lost_ts") is not None and
                (now - st["lost_ts"]).total_seconds() / 60.0 >= RELAPSE_GRACE_MIN):
            st["active"] = False
            st["lvl"] = "NONE"
            st["prev_wave_end_ts"] = now
            st["prev_wave_peak_zt"] = st.get("peak_zt", 0)
            st["lost_ts"] = None

    def _begin_new_wave(self, st, sig, now):
        """非活跃→达标：当天首波，或上一波消退满宽限后的再度放量。"""
        old = st or {}
        wave = int(old.get("wave", 0)) + 1
        relaunch_gap = None
        if old.get("last_fire_ts") is not None:
            relaunch_gap = round((now - old["last_fire_ts"]).total_seconds() / 60.0, 1)
        cur_lvl, zt_n = sig["cur_lvl"], sig["zt"]
        st = {"wave": wave, "active": True, "lvl": cur_lvl,
              "fired_l1": cur_lvl == "L1", "fired_l2": cur_lvl == "L2",
              "fire_count": int(old.get("fire_count", 0)),
              "last_fire_ts": old.get("last_fire_ts"),  # 仅用于算"距上次预警N分钟"
              "last_fire_lvl": old.get("last_fire_lvl"),
              # 跨波不沿用上一波指标做台阶对比（新一波从谷底重新起步，比峰值会误导为走弱）
              "last_fire_zt": None, "last_fire_chg": None, "last_fire_lb": None,
              "peak_zt": zt_n, "lost_ts": None, "trough_chg": None,
              "trough_at_relaunch": old.get("trough_chg"),
              "wave_start_ts": now,
              "prev_wave_end_ts": old.get("prev_wave_end_ts")}
        stage_tag = "首次启动" if wave == 1 else f"再度启动·第{wave}波"
        return st, stage_tag, relaunch_gap

    def _advance_active_wave(self, st, sig, first_conc):
        """活跃波（含宽限期内刚恢复）：清消退计时，仅在升级/成主线/走强台阶时再报；否则返回 None（静默）。"""
        st["lost_ts"] = None
        st["trough_chg"] = None
        cur_lvl, zt_n = sig["cur_lvl"], sig["zt"]
        prev_lvl = st.get("lvl")
        if prev_lvl == "L1" and cur_lvl == "L2" and not st.get("fired_l2"):
            return cur_lvl, "升级 L1→L2"
        if first_conc:
            return cur_lvl, "首次成为市场主线"
        if zt_n - int(st.get("last_fire_zt") or 0) >= STEP_ZT:
            return cur_lvl, "走强一步"
        return None

    @staticmethod
    def _delta_zt(bf_zt, zt_n):
        if bf_zt is None:
            return None
        dz = zt_n - int(bf_zt)
        return f"涨停 {int(bf_zt)}→{zt_n} 家（{dz:+d}）" if dz else None

    @staticmethod
    def _delta_chg(bf_chg, chg):
        if bf_chg is not None and abs(chg - bf_chg) >= 0.5:
            return f"涨幅 {bf_chg:+.2f}% → {chg:+.2f}%"
        return None

    @staticmethod
    def _delta_lb(bf_lb, lb):
        if bf_lb is not None and lb is not None and abs(lb - bf_lb) >= 0.2:
            return f"量比 {bf_lb:.2f} → {lb:.2f}"
        return None

    @staticmethod
    def _format_deltas(st, sig):
        """与'上一次收到的预警'相比的增量行（首次启动无对比基准则为空）。"""
        parts = [Monitor._delta_zt(st.get("last_fire_zt"), sig["zt"]),
                 Monitor._delta_chg(st.get("last_fire_chg"), sig["chg"]),
                 Monitor._delta_lb(st.get("last_fire_lb"), sig["lb"])]
        deltas = [d for d in parts if d]
        return ("变化：" + "；".join(deltas)) if deltas else ""

    @staticmethod
    def _format_reasons(sig):
        """命中条件说明（写进消息，让用户一眼看到为什么报）。"""
        if sig["l2"]:
            return [f"满足L2：涨幅>{CHG_L1}% 且 涨停≥{ZT_L2} 且 量比≥{LB_L2} 且 上涨占比>{UP_RATIO*100:.0f}%"]
        reasons = []
        if sig["hit_l1_speed"]:
            reasons.append(f"涨速≥{sig['speed_thr']}%")
        if sig["hit_l1_chg"]:
            reasons.append(f"涨幅>{CHG_L1}% 且 涨停≥{ZT_L1}")
        return reasons

    @staticmethod
    def _format_tag_extra(stage_tag, fire_no, relaunch_gap, trough):
        """次数/波次/距上次预警：一眼看清这是今天第几次、为什么又发。"""
        if stage_tag and stage_tag.startswith("再度启动"):
            bits = [f"今日第{fire_no}次"]
            if relaunch_gap is not None:
                gap_txt = int(relaunch_gap) if float(relaunch_gap).is_integer() else relaunch_gap
                bits.append(f"距上次预警{gap_txt}分钟")
            extra = f"（{'，'.join(bits)}）"
            if trough is not None:
                extra += f"\n上一波消退期涨幅最低曾至 {trough:+.2f}%，本次为重新放量"
            return extra
        return f"（今日第{fire_no}次）" if fire_no > 1 else ""

    @staticmethod
    def _concentration_line(conc_ratio):
        if conc_ratio is None:
            return ""
        if conc_ratio >= CONCENTRATION:
            return f"\n⭐ 当前市场主线（占今日涨停 {conc_ratio*100:.0f}%，≥{CONCENTRATION*100:.0f}%）"
        return f"\n占今日涨停 {conc_ratio*100:.0f}%（主线阈值≥{CONCENTRATION*100:.0f}%，暂非主线）"

    def _hold_hint(self):
        """持仓标签，以及'未配置持仓'的一次性提示。"""
        hold_tags = [f"持仓{len(self.holdings)}"] if self.holdings else []
        hint_line = ""
        if not self.holdings and not self.hint_shown:
            self.hint_shown = True
            hint_line = ("\n💡 未配置持仓：想让板块爆发时提醒你手里的票？"
                         "可在 config/holdings.csv 填入持仓代码即可开启")
        return hold_tags, hint_line

    @staticmethod
    def _metrics_line(sig, n_zt, hold_tags):
        """消息正文中的行情指标行。"""
        chg, zt_n = sig["chg"], sig["zt"]
        speed, lb, up_ratio = sig["speed"], sig["lb"], sig["up_ratio"]
        line = f"涨幅 {chg:+.2f}% ｜ 涨停 {zt_n} 家"
        if speed is not None:
            line += f" ｜ 涨速 {speed:+.2f}%"
        if lb is not None:
            line += f" ｜ 量比 {lb:.2f}"
        if up_ratio is not None:
            line += f" ｜ 上涨占比 {up_ratio*100:.0f}%"
        if n_zt:
            line += f" ｜ 全市场涨停 {n_zt}"
        if hold_tags:
            line += f" ｜ {'/'.join(hold_tags)}"
        return line

    def _build_alert_message(self, sig, n_zt, conc_ratio, level, stage_tag,
                             fire_no, relaunch_gap, st, now):
        delta_line = self._format_deltas(st, sig)
        reasons = self._format_reasons(sig)
        hold_tags, hint_line = self._hold_hint()
        conc_line = self._concentration_line(conc_ratio)
        tag_extra = self._format_tag_extra(stage_tag, fire_no, relaunch_gap,
                                           st.get("trough_at_relaunch"))
        head = now.strftime("%H:%M")
        msg = (f"[{head}] {stage_tag} · {level}{tag_extra}\n"
               + (delta_line + "\n" if delta_line else "")
               + f"触发：{' ＋ '.join(reasons)}\n"
               + self._metrics_line(sig, n_zt, hold_tags)
               + conc_line + hint_line)
        title = f"{'🔴' if level == 'L2' else '🟠'} {sig['name']} · {stage_tag}"
        return title, msg

    def _emit_alert(self, key, sig, n_zt, conc_ratio, level, stage_tag,
                    relaunch_gap, st, snap, now):
        """本轮确定推送：计数 → 组装消息 → 推送/落台账 → 回写状态。"""
        st["fire_count"] = int(st.get("fire_count", 0)) + 1
        fire_no = st["fire_count"]
        if level == "L2":
            st["fired_l2"] = True
        if level == "L1":
            st["fired_l1"] = True
        chg, zt_n, speed, lb, up_ratio = (sig["chg"], sig["zt"], sig["speed"],
                                          sig["lb"], sig["up_ratio"])
        title, msg = self._build_alert_message(sig, n_zt, conc_ratio, level,
                                               stage_tag, fire_no, relaunch_gap, st, now)
        push(title, msg)
        append_alert({
            "时间": datetime.now().strftime("%H:%M:%S"), "级别": level,
            "板块类型": "概念/行业", "板块代码": sig["raw_code"], "板块名": sig["name"],
            "涨幅%": round(chg, 2), "涨停数": zt_n,
            "涨速%": round(speed, 2) if speed is not None else "",
            "量比": round(lb, 2) if lb is not None else "",
            "上涨占比": round(up_ratio * 100) if up_ratio is not None else "", "主力净流入(元)": "",
            "命中持仓": ",".join(self.holdings.values()) if level else "",
            "命中候选": "", "阈值快照": THRESHOLD_SNAP, "消息": msg,
        })
        st.update({"lvl": level, "last_fire_ts": now, "last_fire_lvl": level,
                   "last_fire_zt": zt_n, "last_fire_chg": chg,
                   "last_fire_lb": lb,
                   "peak_zt": max(int(st.get("peak_zt") or 0), zt_n)})
        st.update(snap)
        self.states[key] = st
        log(f"[{level}|{stage_tag}|第{fire_no}次|W{st.get('wave')}] {sig['name']} {msg}")

    def _eval_board(self, b, zt_by_industry, zt_by_concept, n_zt, late, today, now):
        """编排：对单个板块做一轮评估，只负责串联各单一职责步骤，不含具体判定/文案细节。"""
        sig = self._classify_signal(b, zt_by_industry, zt_by_concept, late)
        if sig is None:
            return
        key = sig["code"]
        st = self.states.get(key)
        conc_ratio, prev_conc, first_conc = self._concentration(sig, st, n_zt)
        snap = self._snapshot_fields(sig, conc_ratio, prev_conc, now)

        # 情形一：当下不达标 → 进入/维持/结束"消退宽限"，永不推送
        if sig["cur_lvl"] == "NONE":
            self.states[key] = self._settle_while_inactive(st, sig, snap, now)
            return

        # 情形二：当下达标（先健壮结算可能已满宽限的上一波）
        self._close_expired_grace(st, now)
        relaunch_gap = None
        if not st or not st.get("active"):
            st, stage_tag, relaunch_gap = self._begin_new_wave(st, sig, now)
            level = sig["cur_lvl"]
        else:
            decision = self._advance_active_wave(st, sig, first_conc)
            if decision is None:
                # 同一波、无实质台阶（含宽限期内抖动回来）：静默，只刷新快照
                st["lvl"] = sig["cur_lvl"]
                st["peak_zt"] = max(int(st.get("peak_zt") or 0), sig["zt"])
                st.update(snap)
                self.states[key] = st
                return
            level, stage_tag = decision

        self._emit_alert(key, sig, n_zt, conc_ratio, level, stage_tag,
                         relaunch_gap, st, snap, now)


def single_instance():
    """单例：fcntl 文件锁，已运行则退出。"""
    lock = WORKSPACE / ".intraday_monitor.lock"
    fp = open(lock, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("已有监测实例在运行，退出。")
        sys.exit(0)
    return fp


STATE_FILE = WORKSPACE / "monitor_state.json"   # 记录最近一次正常扫描时间，用于停机补检


def load_last_scan():
    """读取最近一次正常扫描时间（'%Y-%m-%d %H:%M:%S'）；无记录返回 None。"""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_scan")
    except Exception:
        return None
    return None


def save_last_scan(ts_str=None):
    """写入最近一次正常扫描时间；默认取当前时间。"""
    try:
        ts = ts_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATE_FILE.write_text(json.dumps({"last_scan": ts}, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


class _RunState:
    """主循环可变状态（替代 main 内 nonlocal 闭包变量）。"""
    __slots__ = ("last_success", "last_hb", "down_alerted", "selfcheck_done")

    def __init__(self, last_success):
        self.last_success = last_success
        self.last_hb = None        # 上次宕机告警时间（冷却用）
        self.down_alerted = False  # 是否处于"已告警未恢复"
        self.selfcheck_done = False  # 交易时段首轮启动自检是否已做


def _mark_down(st, title, msg, now):
    push(title, msg)
    st.last_hb = now
    st.down_alerted = True


def _watchdog(st, now):
    """交易时段连续 HB_STALL_SEC 秒没有一次成功扫描 -> 心跳告警；同一次宕机按阈值冷却。"""
    if not is_trading_time(now):
        return
    stall = (now - st.last_success).total_seconds()
    if stall >= HB_STALL_SEC and (st.last_hb is None
                                  or (now - st.last_hb).total_seconds() >= HB_STALL_SEC):
        _mark_down(st, "⚠️ 盘中监控心跳告警",
                   f"已连续约 {int(stall // 60)} 分钟没有完成一次成功扫描，"
                   f"期间板块异动可能漏推，请立即检查。\n日志：data/_workspace/intraday_monitor.log",
                   now)


def _parse_last_success(prev_state):
    """从持久化的上次扫描时间恢复 last_success；解析失败用当前时间。"""
    if prev_state:
        try:
            return datetime.strptime(prev_state, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now()


def _gap_check(m, st, prev_state):
    """距上次正常扫描超过阈值且当前在交易时段，立即补检一次，避免停机窗口漏推。"""
    gap_min = 0.0
    if prev_state:
        try:
            gap_min = (datetime.now()
                       - datetime.strptime(prev_state, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
        except Exception:
            gap_min = 0.0
    if gap_min < 5 or not is_trading_time():
        return
    log(f"[停机补检] 距上次扫描 {gap_min:.0f} 分钟（上次 {prev_state}），中间未实时监测，"
        f"立即补检当前市场状态")
    try:
        src = m.scan_once()
        if src:
            save_last_scan()
            st.last_success = datetime.now()
            st.selfcheck_done = True
        else:
            _mark_down(st, "⚠️ 盘中监控启动自检失败",
                       "停机补检时所有板块数据源均不可用，监控可能无法产出信号，请立即检查网络/数据源。",
                       datetime.now())
            st.selfcheck_done = True
    except Exception:
        log("停机补检异常：\n" + traceback.format_exc())
        _mark_down(st, "⚠️ 盘中监控启动自检异常",
                   "停机补检首轮扫描即抛异常，期间可能漏推，请立即检查日志 data/_workspace/intraday_monitor.log",
                   datetime.now())
        st.selfcheck_done = True


def _poll_once(m, st):
    """主循环单轮：交易时段扫描并处理成功/失败/异常，返回本轮应 sleep 的秒数。"""
    now = datetime.now()
    try:
        if not is_trading_time(now):
            return POLL_CLOSED
        src = m.scan_once()
        if src:
            save_last_scan()
            st.last_success = now
            if st.down_alerted:
                push("✅ 盘中监控已恢复", f"扫描恢复正常（本轮数据源：{src}）。")
                st.down_alerted = False
        elif not st.selfcheck_done:
            # 交易时段首轮所有数据源都拿不到 -> 立即高声量告警，不必等心跳
            _mark_down(st, "⚠️ 盘中监控启动自检失败",
                       "首轮扫描所有板块数据源均不可用，监控可能无法产出信号，请立即检查网络/数据源。",
                       now)
        st.selfcheck_done = True
        late = now.time() >= dtime(14, 30)
        return POLL_LATE if late else POLL_NORMAL
    except Exception:
        log("主循环异常：\n" + traceback.format_exc())
        if not st.selfcheck_done:
            _mark_down(st, "⚠️ 盘中监控启动自检异常",
                       "首轮扫描即抛异常，期间可能漏推，请立即检查日志 data/_workspace/intraday_monitor.log",
                       now)
        st.selfcheck_done = True
        return POLL_NORMAL


def main():
    once = "--once" in sys.argv
    build_only = "--build-map" in sys.argv
    if build_only:
        build_sector_map(datetime.now().strftime("%Y%m%d"))
        return
    single_instance()
    m = Monitor()
    log(f"启动 板块异动监测  CHG>{CHG_L1}% L1涨停>={ZT_L1} L2涨停>={ZT_L2} "
        f"常规{POLL_NORMAL}s/尾盘{POLL_LATE}s once={once}")
    if once:
        m.scan_once()
        save_last_scan()
        log("单次扫描结束。")
        return
    # 最近一次"成功扫描"时间：优先读持久化记录（跨进程/跨天也准确），供心跳看门狗判断
    _prev_state = load_last_scan()
    st = _RunState(_parse_last_success(_prev_state))
    _gap_check(m, st, _prev_state)

    while True:
        secs = _poll_once(m, st)
        time.sleep(secs)
        _watchdog(st, datetime.now())


if __name__ == "__main__":
    main()
