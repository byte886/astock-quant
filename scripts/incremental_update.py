#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一增量更新 worker（T13 全量下载收尾 + T35 双轨第2步「增量直写 Parquet」）
=================================================================================
为什么需要它：
- 全量历史下完后，行情每天仍在产生。本 worker 让数据「每天保鲜」，且增量同时写
  CSV（一手底稿）与 Parquet（读取加速），消除 ADR-004 里「CSV 已更新、Parquet 没转」
  的短暂不一致 —— 即 T35。

四类标的（数据源 / 路径 / 唯一键见 KINDS）：
- stock_daily / stock_minute：baostock，前复权(adj=2)
- etf_daily：AkShare 东财(qfq)，东财断连回退新浪
- etf_minute：baostock（复刻 download_etf.py 的原始调用，口径与历史一致）

调度（--daemon 常驻，由 launchd 管理，单例 + 崩溃自愈）：
  1. 盘后(交易日 15:05 后)先更新「核心」：个股=中证800、ETF=config/core_etfs.csv
  2. 再把全市场日线补到当天（分批、断点、游标）
  3. 全市场 5 分钟线「滚动追赶」：核心每天必更，其余按游标循环轮转、逐步逼近最新
  交易时段(9:25-15:05)避让 baostock；非交易日只做分钟滚动（无新数据则快速 unchanged）。

用法：
  python scripts/incremental_update.py --daemon                 # 常驻（launchd 用）
  python scripts/incremental_update.py --once --scope core      # 单次：核心四类
  python scripts/incremental_update.py --once --kinds stock_daily --codes sh.600000
  python scripts/incremental_update.py --once --kinds etf_daily --scope all
"""
# --- 必须在 import akshare 前清掉豆包注入的代理（否则国内行情被代理劫持，监测器踩过的坑）---
import os
for _k in list(os.environ):
    if "proxy" in _k.lower():
        del os.environ[_k]

import sys
import json
import time
import fcntl
import signal
import random
import argparse
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import csv_to_parquet as c2p
import baostock as bs

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data/raw"
WS = ROOT / "data/_workspace"
STATE_FILE = WS / "incremental_worker_state.json"
LOCK_FILE = WS / "incremental_worker.lock"
LOG_FILE = WS / "incremental_worker.log"

STOCK_D_FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
                  "turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST")
MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"

# kind -> 存储根 / baostock频率 / 数据源 / 复权 / 字段 / 去重唯一键 / 单只超时 / 间隔
KINDS = {
    "stock_daily":  dict(root="daily",       freq="d", src="bs",  adj="2",
                         fields=STOCK_D_FIELDS, key="date", timeout=45, iv=(1.0, 2.0)),
    "stock_minute": dict(root="minute",      freq="5", src="bs",  adj="2",
                         fields=MINUTE_FIELDS, key="time", timeout=90, iv=(2.0, 3.0)),
    "etf_daily":    dict(root="etf/daily",   freq="d", src="ak",  adj="qfq",
                         fields=None, key="date", timeout=60, iv=(1.5, 2.5)),
    "etf_minute":   dict(root="etf/minute",  freq="5", src="bs",  adj="2",
                         fields=MINUTE_FIELDS, key="time", timeout=90, iv=(2.0, 3.0)),
}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    WS.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Timeout(Exception):
    pass


@contextmanager
def time_limit(seconds):
    def handler(signum, frame):
        raise Timeout(f"超过 {seconds}s")
    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ---------- 名单 ----------
def csi800_codes():
    p = WS / "csi800_daily_status.json"
    st = json.loads(p.read_text())
    return sorted(set(st.get("completed", [])) | set(st.get("failed", {}).keys()))


def core_etf_codes():
    return pd.read_csv(ROOT / "config/core_etfs.csv")["code"].astype(str).tolist()


def all_stock_codes():
    df = pd.read_csv(WS / "stock_list.csv")
    s = df["code"].astype(str)
    return sorted(s[~s.str.startswith("sz.399")].tolist())


def all_etf_codes():
    return sorted(pd.read_csv(WS / "etf_list.csv")["code"].astype(str).tolist())


def codes_for(kind, scope):
    if kind.startswith("stock"):
        return csi800_codes() if scope == "core" else all_stock_codes()
    return core_etf_codes() if scope == "core" else all_etf_codes()


# ---------- 交易日历（复用监测器缓存；失败降级只判周末）----------
def is_trading_day(d: datetime):
    cal = WS / "trade_cal.json"
    if cal.exists():
        try:
            j = json.loads(cal.read_text())
            dates = j if isinstance(j, list) else j.get("dates", j.get("trade_dates", []))
            return d.strftime("%Y-%m-%d") in set(map(str, dates))
        except Exception:
            pass
    return d.weekday() < 5


# ---------- 路径 / 读取 ----------
def csv_path(kind, code):
    mkt = code.split(".")[0]
    return RAW / KINDS[kind]["root"] / mkt / f"{code}.csv"


def read_existing_date(kind, code):
    cp = csv_path(kind, code)
    pp = cp.with_suffix(".parquet")
    try:
        if pp.exists():
            d = pd.read_parquet(pp, columns=["date"])["date"]
            return pd.to_datetime(d, errors="coerce").max()
        if cp.exists():
            d = pd.read_csv(cp, usecols=["date"])["date"]
            return pd.to_datetime(d, errors="coerce").max()
    except Exception:
        return None
    return None


# ---------- 拉取 ----------
_bs_ok = False


def bs_ensure():
    global _bs_ok
    if not _bs_ok:
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败 {lg.error_msg}")
        _bs_ok = True


def bs_fetch(kind, code, start, end):
    cfg = KINDS[kind]
    bs_ensure()
    rs = bs.query_history_k_data_plus(
        code, cfg["fields"], start_date=start, end_date=end,
        frequency=cfg["freq"], adjustflag=cfg["adj"])
    if rs.error_code != "0":
        global _bs_ok
        _bs_ok = False
        raise RuntimeError(f"baostock {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=cfg["fields"].split(","))


_EM_RENAME = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "amount", "振幅": "amplitude", "涨跌幅": "pctChg",
    "涨跌额": "change", "换手率": "turn",
}


def ak_etf_daily(code, start_yyyymmdd, end_yyyymmdd):
    import akshare as ak
    sym = code.split(".")[1]
    df = None
    try:
        df = ak.fund_etf_hist_em(symbol=sym, period="daily",
                                 start_date=start_yyyymmdd, end_date=end_yyyymmdd,
                                 adjust="qfq")
        if df is not None and len(df):
            df = df.rename(columns=_EM_RENAME)
    except Exception:
        df = None
    if df is None or not len(df):
        s = ak.fund_etf_hist_sina(symbol=code.replace(".", ""))  # 新浪回退
        if s is None or not len(s):
            return pd.DataFrame()
        s = s.copy()
        s["amount"] = pd.NA
        df = s
    df["date"] = df["date"].astype(str)
    df["code"] = code
    return df


# ---------- 单只增量：合并去重 → 同写 CSV + Parquet ----------
def update_one(kind, code, lookback_days=3):
    cfg = KINDS[kind]
    cp = csv_path(kind, code)
    ld = read_existing_date(kind, code)
    today = datetime.now()
    start = (ld - timedelta(days=lookback_days)) if ld is not None else today - timedelta(days=9000)
    with time_limit(cfg["timeout"]):
        if cfg["src"] == "bs":
            new = bs_fetch(kind, code, start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
        else:
            new = ak_etf_daily(code, start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
    if len(new):
        new = new[new["close"].astype(str).str.len() > 0]
    if not len(new):
        return "unchanged"
    base = pd.read_csv(cp, dtype=c2p.READ_STR, low_memory=False) if cp.exists() else None
    key = cfg["key"]
    if base is not None:
        comb = pd.concat([base, new], ignore_index=True, sort=False)
        target_cols = list(base.columns)
    else:
        comb = new
        target_cols = list(new.columns)
    comb = (comb.drop_duplicates(subset=key, keep="last")
                .sort_values(key).reindex(columns=target_cols).reset_index(drop=True))
    cp.parent.mkdir(parents=True, exist_ok=True)
    comb.to_csv(cp, index=False, encoding="utf-8")  # CSV 底稿
    typed = c2p.typeify(comb, "")
    tmp = cp.with_suffix(f".parquet.tmp-{os.getpid()}")
    typed.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    os.replace(tmp, cp.with_suffix(".parquet"))      # 直写 Parquet（T35）
    return "updated"


def run_codes(kind, codes, max_fail_log=8):
    out = {"updated": 0, "unchanged": 0, "failed": {}}
    iv = KINDS[kind]["iv"]
    for code in codes:
        try:
            out[update_one(kind, code)] += 1
        except Exception as e:
            out["failed"][code] = str(e)[:120]
        time.sleep(random.uniform(*iv))
    if out["failed"]:
        log(f"  {kind} 失败 {len(out['failed'])}：{list(out['failed'].items())[:max_fail_log]}")
    return out


# ---------- 状态 ----------
def fresh_state():
    return {"core_done": {}, "daily_all_done": {},
            "daily_cursor": {"stock_daily": 0, "etf_daily": 0},
            "roll_cursor": {"stock_minute": 0, "etf_minute": 0}, "last_run": None}


def load_state():
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text())
        for k, v in fresh_state().items():
            st.setdefault(k, v)
        return st
    return fresh_state()


def save_state(st):
    st["last_run"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- daemon ----------
def acquire_lock():
    WS.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("另一个 incremental worker 已在运行，本进程退出（单例）")
        sys.exit(0)
    return fd


def market_open(now, td):
    t = now.hour * 60 + now.minute
    return td and 9 * 60 + 25 <= t <= 15 * 60 + 5


def daemon(batch):
    acquire_lock()
    log("增量 worker 启动（daemon，单例）")
    while True:
        now = datetime.now()
        tdate = now.strftime("%Y-%m-%d")
        td = is_trading_day(now)
        data_ready = td and (now.hour > 15 or (now.hour == 15 and now.minute >= 5))
        st = load_state()
        did_something = False

        # 1) 核心四类（盘后数据齐、当天未完成）
        if data_ready:
            for kind in ("stock_daily", "stock_minute", "etf_daily", "etf_minute"):
                if st["core_done"].get(kind) == tdate:
                    continue
                codes = codes_for(kind, "core")
                log(f"[核心] {kind} {len(codes)} 只 ...")
                r = run_codes(kind, codes)
                log(f"[核心] {kind} 更新{r['updated']} 无新{r['unchanged']} 失败{len(r['failed'])}")
                if not r["failed"]:
                    st["core_done"][kind] = tdate
                did_something = True
                save_state(st)

        # 2) 全市场日线（分批游标，断点续传）
        if data_ready:
            for kind in ("stock_daily", "etf_daily"):
                if st["daily_all_done"].get(kind) == tdate:
                    continue
                allc = codes_for(kind, "all")
                cur = st["daily_cursor"].get(kind, 0)
                seg = allc[cur:cur + batch]
                if not seg:
                    st["daily_all_done"][kind] = tdate
                    st["daily_cursor"][kind] = 0
                    save_state(st)
                    continue
                log(f"[全市场日线] {kind} {cur}/{len(allc)} 本批{len(seg)} ...")
                r = run_codes(kind, seg)
                st["daily_cursor"][kind] = cur + len(seg)
                if r["failed"]:  # 失败不推进对应位置：下轮从批首重试（保守，可能重复但幂等）
                    st["daily_cursor"][kind] = cur
                log(f"[全市场日线] {kind} 推进到 {st['daily_cursor'][kind]}/{len(allc)}")
                did_something = True
                save_state(st)

        # 3) 全市场分钟滚动（仅交易日的非盘中时段：盘后→整夜→次日盘前）。
        # 非交易日不跑——全量历史已齐，滚动只为补每个交易日的新数据，节假日无新数据，
        # 且 baostock 周末常挂起（W3）。
        if td and not market_open(now, td):
            for kind in ("stock_minute", "etf_minute"):
                allc = codes_for(kind, "all")
                cur = st["roll_cursor"].get(kind, 0) % len(allc)
                seg = allc[cur:cur + batch]
                r = run_codes(kind, seg)
                nxt = cur + len(seg)
                if nxt >= len(allc):
                    nxt = 0
                st["roll_cursor"][kind] = nxt
                log(f"[分钟滚动] {kind} {cur}→{nxt}/{len(allc)} "
                    f"更{r['updated']} 无新{r['unchanged']} 败{len(r['failed'])}")
                did_something = True
                save_state(st)

        save_state(st)
        time.sleep(20 if did_something else 300)


# ---------- once ----------
def once(kinds_sel, scope, codes_sel):
    acquire_lock()
    for kind in kinds_sel:
        codes = codes_sel or codes_for(kind, scope)
        log(f"[once] {kind} scope={scope} {len(codes)} 只")
        r = run_codes(kind, codes)
        log(f"[once] {kind} 更新{r['updated']} 无新{r['unchanged']} 失败{len(r['failed'])}")
        if r["failed"]:
            sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="统一增量更新 worker")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--kinds", default="stock_daily,stock_minute,etf_daily,etf_minute")
    ap.add_argument("--scope", default="core", choices=["core", "all"])
    ap.add_argument("--codes", default="")
    ap.add_argument("--batch", type=int, default=40)
    a = ap.parse_args()
    kinds_sel = [k for k in a.kinds.split(",") if k in KINDS]
    codes_sel = [c.strip() for c in a.codes.split(",") if c.strip()]
    if a.daemon:
        daemon(a.batch)
    else:
        once(kinds_sel, a.scope, codes_sel)


if __name__ == "__main__":
    main()
