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
for _d in (WORKSPACE, ALERT_DIR, SNAP_DIR):
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
STEP_ZT       = int(_f("MON_STEP_ZT", 2))   # "走强"台阶：涨停数较上次增加>=此数才再报，否则静默
# 本次运行生效的阈值快照（写进每条预警，便于日后按"当时标准"追溯）
THRESHOLD_SNAP = (f"chg>{CHG_L1}%|ztL1>={ZT_L1}|ztL2>={ZT_L2}|"
                  f"lb>={LB_L2}|speed>{SPEED_L1}/尾盘{SPEED_LATE}")
POLL_NORMAL = int(_f("MON_POLL_NORMAL", 60))   # 常规轮询秒
POLL_LATE   = int(_f("MON_POLL_LATE", 30))     # 尾盘轮询秒
POLL_CLOSED = int(_f("MON_POLL_CLOSED", 60))    # 判休市后醒来周期（s）：每分钟醒一次判断是否开盘，开盘后≤1分钟上岗
HB_STALL_SEC = int(_f("MON_HB_STALL_SEC", 1800))  # 盘中心跳看门狗：交易时段连续无成功扫描超过该秒数即告警（默认30分钟，冷却同阈值）

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
    if c.startswith(("60", "68", "9")):
        return "sh" + c
    if c.startswith(("00", "30", "20")):
        return "sz" + c
    if c.startswith(("8", "4", "92")):
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
def _sina_sector_map():
    """东财成分被限时用新浪板块成分兜底（独立可用，概念175+行业49，单个约0.3s）。
    返回 {code: set(kind:板块名)}；失败/空返回 {}。"""
    import akshare as ak
    c2b = {}
    for indicator, kind in [("概念", "concept"), ("新浪行业", "industry")]:
        try:
            spot = ak.stock_sector_spot(indicator=indicator)
        except Exception as e:
            log(f"[sina_map:{indicator}] 板块列表失败：{type(e).__name__}")
            continue
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
    return c2b


def build_sector_map(today):
    """构建 code -> set((kind,board_name)) 映射，缓存到本地 json。
    优先东财成分（最全 504 概念）；东财被限/不完整时追加新浪板块成分兜底。
    空缓存视为无效（东财被限时曾写入 '{}' 的空映射，须重建）。"""
    cache = WORKSPACE / f"sector_map_{today}.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data:  # 非空才用；空缓存重建
                return data
        except Exception:
            pass
    import akshare as ak
    c2b = {}
    ok = False
    # 行业成分（少而稳，优先东财）
    for kind, fn in [("industry", ak.stock_board_industry_cons_em),
                     ("concept", ak.stock_board_concept_cons_em)]:
        names = None
        try:
            names = ak.stock_board_industry_name_em() if kind == "industry" \
                else ak.stock_board_concept_name_em()
            col = "板块名称" if "板块名称" in names.columns else names.columns[1]
            names = names[col].tolist()
            if names:
                ok = True
        except Exception as e:
            log(f"[build_map:{kind}] 东财板块名列表失败（回退新浪）：{type(e).__name__}")
            names = []
        for nm in names:
            try:
                df = fn(symbol=nm)
                codecol = "代码" if "代码" in df.columns else df.columns[1]
                for c in df[codecol]:
                    c2b.setdefault(normalize_code(c), set()).add(f"{kind}:{nm}")
            except Exception:
                continue
            time.sleep(0.1)
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

        def _is_zt(zdf, code):
            if zdf is None or pd.isna(zdf):
                return False
            c = str(code)
            if c.startswith(("30", "68", "8", "4", "92")):  # 创业/科创/北交 20cm
                return zdf >= 19.9
            return zdf >= 9.9

        for _, row in tx_df.iterrows():
            code = row["code"]
            tags = mapping.get(code)
            if not tags:
                continue
            zdf = row.get("zdf")
            for tag in tags:
                _, _, nm = tag.partition(":")
                a = board_agg.setdefault(nm, {"chg_sum": 0.0, "n": 0, "zt": 0,
                                              "lb_sum": 0.0, "speed_sum": 0.0,
                                              "up": 0, "down": 0})
                if zdf is not None and not pd.isna(zdf):
                    a["chg_sum"] += zdf
                    a["n"] += 1
                    if zdf > 0:
                        a["up"] += 1
                    elif zdf < 0:
                        a["down"] += 1
                    if _is_zt(zdf, code):
                        a["zt"] += 1
                lb, sp = row.get("lb"), row.get("speed")
                if lb is not None and not pd.isna(lb):
                    a["lb_sum"] += lb
                if sp is not None and not pd.isna(sp):
                    a["speed_sum"] += sp
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
    except Exception as e:
        log(f"[tx_agg] 聚合失败：{type(e).__name__}")
        return None


# ---------------- 持仓 / 候选 ----------------
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
                code = normalize_code(row.get("code") or row.get("代码") or "")
                if code:
                    codes[code] = row.get("name") or row.get("名称") or ""
    except Exception as e:
        log(f"[load_csv {p.name}] {e}")
    return codes


# ---------------- 推送 ----------------
def push(title, content):
    pushed = False
    body = f"**{title}**\n{content}"
    # 企业微信群机器人
    webhook = os.environ.get("WECOM_BOT_WEBHOOK")
    if webhook:
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
                pushed = True
            else:
                log(f"[push:wecom] 发送失败：HTTP{resp.status_code} errcode={ec} errmsg={emsg}")
        except Exception as e:
            log(f"[push:wecom] 发送异常：{type(e).__name__}: {e}")
    # Server酱
    key = os.environ.get("SERVERCHAN_KEY")
    if key:
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
                pushed = True
            else:
                log(f"[push:serverchan] 发送失败：HTTP{resp.status_code} resp={(resp.text or '')[:100]}")
        except Exception as e:
            log(f"[push:serverchan] 发送异常：{type(e).__name__}: {e}")
    if not pushed:
        if webhook or key:
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

    def scan_once(self):
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        late = now.time() >= dtime(14, 30)

        em_c = em_boards("concept")
        em_i = em_boards("industry")
        boards = (em_c or []) + (em_i or [])
        source = "eastmoney" if boards else None
        if not boards:
            # 东财被限流 -> 优先腾讯个股 spot 聚合（维度完整，可触发 L1/L2）
            log("[scan] 东财板块无数据，尝试腾讯 spot 聚合兜底")
            try:
                smap_now = build_sector_map(today)
            except Exception:
                smap_now = {}
            tx_now = tx_spot()
            tx_boards = tx_board_agg(smap_now, tx_now)
            if tx_boards:
                boards = tx_boards
                source = "tencent_agg"
                log(f"[scan] 腾讯聚合兜底成功：{len(boards)} 板块（维度完整）")
            else:
                # 腾讯/映射不可用 -> 新浪板块保底（名称匹配，缺涨停/量比）
                log("[scan] 腾讯聚合兜底失败，启用新浪板块兜底")
                sina = sina_boards("概念") or []
                sina += sina_boards("新浪行业") or []
                boards = [{"code": None, "name": b["name"], "chg": b["chg"],
                           "amount": b["amount"], "lb": None, "speed": None} for b in sina]
                source = "sina" if boards else None

        # 涨停池 + 行业归集
        zt = zt_pool(today) or []
        zt_by_industry, zt_codes = {}, set()
        for z in zt:
            zt_codes.add(z["code"])
            if z.get("industry"):
                zt_by_industry[z["industry"]] = zt_by_industry.get(z["industry"], 0) + 1
        n_zt = len(zt)

        # 板块成分映射（概念归集涨停用）
        try:
            smap = build_sector_map(today)
        except Exception:
            smap = {}
        # 由涨停股反查所属概念，统计每概念涨停数
        zt_by_concept = {}
        for c in zt_codes:
            for tag in smap.get(c, []):
                kind, _, nm = tag.partition(":")
                if kind == "concept":
                    zt_by_concept[nm] = zt_by_concept.get(nm, 0) + 1

        if not boards:
            log("[scan] 全部数据源均不可用，本轮无板块数据")
            return None
        # 东财板块无现成涨停数，先用涨停池按行业/概念归集补齐（快照与评估共用同一份）
        for b in boards:
            if b.get("zt") is None:
                b["zt"] = zt_by_industry.get(b["name"], 0) or zt_by_concept.get(b["name"], 0)
        save_snapshot(now, boards, source)

        for b in boards:
            self._eval_board(b, zt_by_industry, zt_by_concept, n_zt, late, today, now)
        return source

    def _eval_board(self, b, zt_by_industry, zt_by_concept, n_zt, late, today, now):
        name = b["name"]
        chg = b["chg"]
        if chg is None:
            return
        # 涨停数：行业按名匹配；概念按概念名
        zt_n = b.get("zt")
        if zt_n is None:
            zt_n = zt_by_industry.get(name, 0) or zt_by_concept.get(name, 0)
        speed = b.get("speed")
        lb = b.get("lb")
        up = b.get("up"); down = b.get("down")
        up_ratio = (up/(up+down)) if (up and down is not None and (up+down)>0) else None

        # 触发判断
        speed_thr = SPEED_LATE if late else SPEED_L1
        hit_l1_speed = (speed is not None and speed >= speed_thr)
        hit_l1_chg = (chg > CHG_L1 and zt_n >= ZT_L1)
        l1 = hit_l1_speed or hit_l1_chg
        l2 = (chg > CHG_L1 and zt_n >= ZT_L2 and (lb is None or lb >= LB_L2)
              and (up_ratio is None or up_ratio >= UP_RATIO))

        key = b["code"] or name
        prev = self.states.get(key) or {}
        prev_lvl = prev.get("lvl", "NONE")
        cur_lvl = "L2" if l2 else ("L1" if l1 else "NONE")
        prev_conc = prev.get("conc") or 0.0

        # 本次主线占比
        conc_ratio = (zt_n / n_zt) if (n_zt and zt_n) else None
        first_conc = (conc_ratio is not None and conc_ratio >= CONCENTRATION
                      and prev_conc < CONCENTRATION)

        level = None      # None = 静默不推；否则 "L1"/"L2"
        stage_tag = ""    # 新启动 / 升级强信号 / 首次达主线 / 走强一步

        if cur_lvl == "NONE":
            # 条件消失：回到 NONE，保留数值快照
            self.states[key] = {"lvl": "NONE", "chg": chg, "zt": zt_n,
                                 "lb": lb, "speed": speed, "conc": prev_conc,
                                 "ts": now}
            return

        if prev_lvl not in ("L1", "L2"):
            level, stage_tag = cur_lvl, "新启动"
        else:
            prev_zt = prev.get("zt") or 0
            if prev_lvl == "L1" and cur_lvl == "L2":
                level, stage_tag = "L2", "升级强信号"
            elif first_conc:
                level, stage_tag = cur_lvl, "首次达主线"
            elif (zt_n or 0) - prev_zt >= STEP_ZT:
                level, stage_tag = cur_lvl, "走强一步"
            else:
                # 与上次相比没有实质台阶：静默，只刷新快照
                self.states[key] = {"lvl": cur_lvl, "chg": chg, "zt": zt_n,
                                    "lb": lb, "speed": speed,
                                    "conc": conc_ratio or prev_conc, "ts": now}
                return

        # 距上次多少分钟
        gap_min = None
        if prev.get("ts") is not None:
            try:
                gap_min = round((now - prev["ts"]).total_seconds() / 60, 1)
            except Exception:
                gap_min = None

        # 与上次相比的增量（让人一眼看出为什么现在又发）
        deltas = []
        if prev.get("zt") is not None:
            dz = (zt_n or 0) - prev.get("zt")
            if dz:
                deltas.append(f"涨停 {prev.get('zt')}→{zt_n} 家（{dz:+d}）")
        if prev.get("chg") is not None and abs(chg - prev.get("chg")) >= 0.5:
            deltas.append(f"涨幅 {prev.get('chg'):+.2f}% → {chg:+.2f}%")
        if prev.get("lb") and lb and abs(lb - prev.get("lb")) >= 0.2:
            deltas.append(f"量比 {prev.get('lb'):.2f} → {lb:.2f}")
        delta_line = ("变化：" + "；".join(deltas)) if deltas else ""

        # 命中条件说明（写进消息，让用户一眼看到为什么报）
        if l2:
            reasons = [f"满足L2：涨幅>{CHG_L1}% 且 涨停≥{ZT_L2} 且 量比≥{LB_L2} 且 上涨占比>{UP_RATIO*100:.0f}%"]
        else:
            reasons = []
            if hit_l1_speed:
                reasons.append(f"涨速≥{speed_thr}%")
            if hit_l1_chg:
                reasons.append(f"涨幅>{CHG_L1}% 且 涨停≥{ZT_L1}")

        hold_tags = []
        if self.holdings:
            hold_tags.append(f"持仓{len(self.holdings)}")
        hint_line = ""
        if not self.holdings and not self.hint_shown:
            self.hint_shown = True
            hint_line = ("\n💡 未配置持仓：想让板块爆发时提醒你手里的票？"
                         "可在 config/holdings.csv 填入持仓代码即可开启")
        conc_line = ""
        if conc_ratio is not None:
            if conc_ratio >= CONCENTRATION:
                conc_line = f"\n⭐ 当前市场主线（占今日涨停 {conc_ratio*100:.0f}%，≥{CONCENTRATION*100:.0f}%）"
            else:
                conc_line = f"\n占今日涨停 {conc_ratio*100:.0f}%（主线阈值≥{CONCENTRATION*100:.0f}%，暂非主线）"

        head = now.strftime("%H:%M")
        if prev_lvl in ("L1", "L2") and gap_min is not None:
            head += f"（距上次 {gap_min} 分钟）"
        elif prev_lvl == "NONE":
            head += "（重新触发）"

        msg = (f"[{head}] {stage_tag}\n"
               + (delta_line + "\n" if delta_line else "")
               + f"触发：{' ＋ '.join(reasons)}\n"
               + f"涨幅 {chg:+.2f}% ｜ 涨停 {zt_n} 家"
               + (f" ｜ 涨速 {speed:+.2f}%" if speed is not None else "")
               + (f" ｜ 量比 {lb:.2f}" if lb is not None else "")
               + (f" ｜ 上涨占比 {up_ratio*100:.0f}%" if up_ratio is not None else "")
               + (f" ｜ 全市场涨停 {n_zt}" if n_zt else "")
               + (f" ｜ {'/'.join(hold_tags)}" if hold_tags else "")
               + conc_line
               + hint_line)

        title = f"{'🔴' if level=='L2' else '🟠'} {name} · {stage_tag}"
        push(title, msg)
        append_alert({
            "时间": datetime.now().strftime("%H:%M:%S"), "级别": level,
            "板块类型": "概念/行业", "板块代码": b.get("code") or "", "板块名": name,
            "涨幅%": round(chg, 2), "涨停数": zt_n,
            "涨速%": round(speed, 2) if speed is not None else "",
            "量比": round(lb, 2) if lb is not None else "",
            "上涨占比": round(up_ratio*100) if up_ratio is not None else "", "主力净流入(元)": "",
            "命中持仓": ",".join(self.holdings.values()) if level else "",
            "命中候选": "", "阈值快照": THRESHOLD_SNAP, "消息": msg,
        })
        self.states[key] = {"lvl": level, "chg": chg, "zt": zt_n, "lb": lb,
                            "speed": speed, "conc": conc_ratio or prev_conc, "ts": now}
        log(f"[{level}|{stage_tag}] {name} {msg}")


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
    last_success = None
    _prev_state = load_last_scan()
    if _prev_state:
        try:
            last_success = datetime.strptime(_prev_state, "%Y-%m-%d %H:%M:%S")
        except Exception:
            last_success = None
    if last_success is None:
        last_success = datetime.now()
    last_hb = None          # 上次宕机告警时间（冷却用）
    down_alerted = False    # 当前是否处于"已告警未恢复"
    selfcheck_done = False  # 交易时段首轮启动自检只做一次

    def mark_down(title, msg, now2):
        nonlocal last_hb, down_alerted
        push(title, msg)
        last_hb, down_alerted = now2, True

    def watchdog(now2):
        # 交易时段连续 HB_STALL_SEC 秒没有一次成功扫描 -> 心跳告警；同一次宕机按该阈值冷却
        if not is_trading_time(now2):
            return
        stall = (now2 - last_success).total_seconds()
        if stall >= HB_STALL_SEC and (last_hb is None
                                      or (now2 - last_hb).total_seconds() >= HB_STALL_SEC):
            mark_down("⚠️ 盘中监控心跳告警",
                      f"已连续约 {int(stall // 60)} 分钟没有完成一次成功扫描，"
                      f"期间板块异动可能漏推，请立即检查。\n日志：data/_workspace/intraday_monitor.log",
                      now2)

    # 停机补检：若距上次正常扫描超过阈值（进程曾停止/重启），且当前在交易时段，
    # 立即补检一次当前市场状态，避免停机窗口期间的异动被整段漏掉。
    gap_min = 0.0
    if _prev_state:
        try:
            gap_min = (datetime.now()
                       - datetime.strptime(_prev_state, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
        except Exception:
            gap_min = 0.0
    if gap_min >= 5 and is_trading_time():
        log(f"[停机补检] 距上次扫描 {gap_min:.0f} 分钟（上次 {_prev_state}），中间未实时监测，"
            f"立即补检当前市场状态")
        try:
            src = m.scan_once()
            if src:
                save_last_scan()
                last_success = datetime.now()
                selfcheck_done = True
            else:
                mark_down("⚠️ 盘中监控启动自检失败",
                          "停机补检时所有板块数据源均不可用，监控可能无法产出信号，请立即检查网络/数据源。",
                          datetime.now())
                selfcheck_done = True
        except Exception:
            log("停机补检异常：\n" + traceback.format_exc())
            mark_down("⚠️ 盘中监控启动自检异常",
                      "停机补检首轮扫描即抛异常，期间可能漏推，请立即检查日志 data/_workspace/intraday_monitor.log",
                      datetime.now())
            selfcheck_done = True

    while True:
        try:
            if is_trading_time():
                src = m.scan_once()
                if src:
                    save_last_scan()
                    last_success = datetime.now()
                    if down_alerted:
                        push("✅ 盘中监控已恢复", f"扫描恢复正常（本轮数据源：{src}）。")
                        down_alerted = False
                elif not selfcheck_done:
                    # 交易时段首轮所有数据源都拿不到 -> 立即高声量告警，不必等心跳
                    mark_down("⚠️ 盘中监控启动自检失败",
                              "首轮扫描所有板块数据源均不可用，监控可能无法产出信号，请立即检查网络/数据源。",
                              datetime.now())
                selfcheck_done = True
                late = datetime.now().time() >= dtime(14, 30)
                time.sleep(POLL_LATE if late else POLL_NORMAL)
            else:
                time.sleep(POLL_CLOSED)
        except Exception:
            log("主循环异常：\n" + traceback.format_exc())
            if not selfcheck_done:
                mark_down("⚠️ 盘中监控启动自检异常",
                          "首轮扫描即抛异常，期间可能漏推，请立即检查日志 data/_workspace/intraday_monitor.log",
                          datetime.now())
            selfcheck_done = True
            time.sleep(POLL_NORMAL)
        finally:
            watchdog(datetime.now())


if __name__ == "__main__":
    main()
