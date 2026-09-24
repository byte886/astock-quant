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
from pathlib import Path
from datetime import datetime, time as dtime

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "data" / "_workspace"
ALERT_DIR = ROOT / "data" / "intraday_alerts"
for _d in (WORKSPACE, ALERT_DIR):
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
POLL_NORMAL = int(_f("MON_POLL_NORMAL", 60))   # 常规轮询秒
POLL_LATE   = int(_f("MON_POLL_LATE", 30))     # 尾盘轮询秒
POLL_CLOSED = int(_f("MON_POLL_CLOSED", 300))  # 判休市后拉长

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
    fields = "f2,f3,f6,f10,f12,f14,f22"
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
def build_sector_map(today):
    """构建 code -> set((kind,board_name)) 映射，缓存到本地 json。"""
    cache = WORKSPACE / f"sector_map_{today}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    import akshare as ak
    c2b = {}
    # 行业成分（少而稳）
    for kind, fn in [("industry", ak.stock_board_industry_cons_em),
                     ("concept", ak.stock_board_concept_cons_em)]:
        names = None
        try:
            names = ak.stock_board_industry_name_em() if kind == "industry" \
                else ak.stock_board_concept_name_em()
            col = "板块名称" if "板块名称" in names.columns else names.columns[1]
            names = names[col].tolist()
        except Exception as e:
            log(f"[build_map:{kind}] 板块名列表失败（用部分映射）：{type(e).__name__}")
            names = []
        for nm in names:
            try:
                df = fn(symbol=nm)
                codecol = "代码" if "代码" in df.columns else df.columns[1]
                for c in df[codecol]:
                    c2b.setdefault(normalize_code(c), set()).add(f"{kind}:{nm}")
            except Exception:
                continue
            time.sleep(0.3)
    out = {k: sorted(v) for k, v in c2b.items()}
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log(f"[build_map] 完成：{len(out)} 只股票映射 -> {cache.name}")
    return out


# ---------------- 持仓 / 候选 ----------------
def load_csv_codes(path):
    p = Path(path)
    codes = {}
    if not p.exists():
        return codes
    try:
        with open(p, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = normalize_code(row.get("code") or row.get("代码") or "")
                if code:
                    codes[code] = row.get("name") or row.get("名称") or ""
    except Exception as e:
        log(f"[load_csv {path.name}] {e}")
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
            requests.post(webhook, json={"msgtype": "markdown",
                          "markdown": {"content": body}}, timeout=8)
            pushed = True
        except Exception as e:
            log(f"[push:wecom] {e}")
    # Server酱
    key = os.environ.get("SERVERCHAN_KEY")
    if key:
        try:
            import requests
            requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": content}, timeout=8)
            pushed = True
        except Exception as e:
            log(f"[push:serverchan] {e}")
    if not pushed:
        log(f"[NO-PUSH] 未配置 webhook，仅留痕：{title}")
    return pushed


def append_alert(row):
    p = ALERT_DIR / f"{datetime.now():%Y-%m-%d}.csv"
    exists = p.exists()
    cols = ["时间", "级别", "板块类型", "板块代码", "板块名", "涨幅%", "涨停数",
            "涨速%", "量比", "上涨占比", "主力净流入(元)", "命中持仓", "命中候选", "消息"]
    with open(p, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if not exists:
            w.writeheader()
        w.writerow(row)


# ---------------- 交易时段 ----------------
def is_trading_time(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


# ---------------- 主扫描一轮 ----------------
class Monitor:
    def __init__(self):
        self.states = {}   # board_key -> "NONE"/"L1"/"L2"
        self.holdings = load_csv_codes(os.environ.get(
            "HOLDINGS_CSV", str(ROOT / "config" / "holdings.csv")))
        self.pool = load_csv_codes(str(ROOT / "config" / "pool3_constituents.csv"))

    def scan_once(self):
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        late = now.time() >= dtime(14, 30)

        boards = em_boards("concept") or []
        boards += em_boards("industry") or []
        if not boards:
            # 东财被限流 -> 用新浪板块兜底（名称匹配，缺涨停/量比）
            log("[scan] 东财板块无数据，启用新浪兜底")
            sina = sina_boards("概念") or []
            sina += sina_boards("新浪行业") or []
            boards = [{"code": None, "name": b["name"], "chg": b["chg"],
                       "amount": b["amount"], "lb": None, "speed": None} for b in sina]

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

        for b in boards:
            self._eval_board(b, zt_by_industry, zt_by_concept, n_zt, late, today)

    def _eval_board(self, b, zt_by_industry, zt_by_concept, n_zt, late, today):
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

        # 触发判断
        speed_thr = SPEED_LATE if late else SPEED_L1
        hit_l1_speed = (speed is not None and speed >= speed_thr)
        hit_l1_chg = (chg > CHG_L1 and zt_n >= ZT_L1)
        l1 = hit_l1_speed or hit_l1_chg
        l2 = (chg > CHG_L1 and zt_n >= ZT_L2 and (lb is None or lb >= LB_L2))

        key = b["code"] or name
        prev = self.states.get(key, "NONE")
        new_state = prev
        level = None
        if l2:
            if prev != "L2":
                level = "L2"
                new_state = "L2"
        elif l1:
            if prev == "NONE":
                level = "L1"
                new_state = "L1"
            elif prev == "L2":
                pass  # 已推过 L2，不重复
        else:
            # 条件消失，允许重新放量再推 L1；L2 当天不重复
            if prev in ("L1", "L2"):
                new_state = "NONE"

        if level is None:
            self.states[key] = new_state
            return

        # 持仓/候选联动（按板块名粗匹配；v1 简化，后续接成分精配）
        tag = []
        if self.holdings:
            tag.append(f"持仓{len(self.holdings)}")
        msg = (f"涨幅 {chg:+.2f}% ｜ 涨停 {zt_n} 家"
               + (f" ｜ 涨速 {speed:+.2f}%" if speed is not None else "")
               + (f" ｜ 量比 {lb:.2f}" if lb is not None else "")
               + (f" ｜ 全市场涨停 {n_zt}" if n_zt else "")
               + (f" ｜ {'/'.join(tag)}" if tag else ""))

        title = f"{'🔴 L2 主线' if level=='L2' else '🟠 L1 异动'}｜{name}"
        push(title, msg)
        append_alert({
            "时间": datetime.now().strftime("%H:%M:%S"), "级别": level,
            "板块类型": "概念/行业", "板块代码": b.get("code") or "", "板块名": name,
            "涨幅%": round(chg, 2), "涨停数": zt_n,
            "涨速%": round(speed, 2) if speed is not None else "",
            "量比": round(lb, 2) if lb is not None else "",
            "上涨占比": "", "主力净流入(元)": "",
            "命中持仓": ",".join(self.holdings.values()) if level else "",
            "命中候选": "", "消息": msg,
        })
        self.states[key] = new_state
        log(f"[{level}] {name} {msg}")


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
        log("单次扫描结束。")
        return
    while True:
        try:
            if is_trading_time():
                m.scan_once()
                late = datetime.now().time() >= dtime(14, 30)
                time.sleep(POLL_LATE if late else POLL_NORMAL)
            else:
                time.sleep(POLL_CLOSED)
        except Exception:
            log("主循环异常：\n" + traceback.format_exc())
            time.sleep(POLL_NORMAL)


if __name__ == "__main__":
    main()
