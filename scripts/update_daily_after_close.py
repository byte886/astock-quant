#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘后日线增量更新（策略宇宙）
============================
每个交易日盘后（16:00 后）跑一次，把策略实际需要的标的日线补到最新交易日：
- 默认宇宙 = 中证800剔科创板（多因子/模拟盘，约 706 只，名单取 csi800_daily_status.json）
- 个股日线文件 data/raw/daily/{sh,sz}/{code}.csv，字段与全量下载完全一致
- 逐只读 csv 最后日期，从次自然日拉到今天，追加后按 date 去重排序，幂等可重跑
- 不碰全市场 4889 只下载（T13，launchd 另跑）；本脚本只补策略宇宙，跑得快

前置：全市场 launchd 下载任务建议先暂停（避免同 IP 并发被 baostock 限流）：
  launchctl unload ~/Library/LaunchAgents/com.astock-quant.download-data.plist
  launchctl unload ~/Library/LaunchAgents/com.astock-quant.download-monitor.plist
跑完再 load 恢复（T27.2 后由盘后自动化统一编排）。

用法：
  python scripts/update_daily_after_close.py              # 补中证800全部
  python scripts/update_daily_after_close.py --codes sh.600000,sz.000001  # 只补指定
  python scripts/update_daily_after_close.py --lookback 10  # 强制回看天数（防漏）
"""

import sys
import json
import time
import random
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import baostock as bs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = PROJECT_ROOT / "data/raw/daily"
WORKSPACE = PROJECT_ROOT / "data/_workspace"
CSI_STATUS = WORKSPACE / "csi800_daily_status.json"
LOG_FILE = WORKSPACE / "incremental_daily.log"
STATE_FILE = WORKSPACE / "incremental_daily_status.json"

FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
          "turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST")


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_universe():
    st = json.loads(CSI_STATUS.read_text())
    return sorted(set(st.get("completed", [])) | set(st.get("failed", {}).keys()))


def daily_path(code):
    mkt = "sh" if code.startswith("sh.") else "sz"
    return DAILY_DIR / mkt / f"{code}.csv"


def last_date_in_csv(path):
    """返回 csv 最后交易日（datetime）；文件不存在返回 None。"""
    if not path.exists():
        return None
    try:
        tail = pd.read_csv(path, usecols=["date"])
        if tail.empty:
            return None
        return pd.to_datetime(tail["date"].iloc[-1])
    except Exception:
        return None


def fetch_increment(code, start, end):
    """拉 [start, end] 日线（前复权），返回 DataFrame；无数据返回空 DataFrame。"""
    rs = bs.query_history_k_data_plus(
        code, FIELDS,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        frequency="d", adjustflag="2")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=FIELDS.split(","))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=str, default="", help="逗号分隔指定代码，默认中证800宇宙")
    ap.add_argument("--lookback", type=int, default=1,
                    help="在 csv 最后日期基础上多回看的自然日数（去重兜底，默认1）")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or load_universe()
    today = datetime.now()
    log("=" * 60)
    log(f"盘后增量开始：{len(codes)} 只，截至 {today:%Y-%m-%d}")

    lg = bs.login()
    if lg.error_code != "0":
        log(f"❌ baostock 登录失败：{lg.error_msg}")
        sys.exit(2)

    updated, unchanged, failed = [], [], {}
    for i, code in enumerate(codes):
        path = daily_path(code)
        last = last_date_in_csv(path)
        start = (last + timedelta(days=1 - args.lookback)) if last else \
                today - timedelta(days=4000)  # 新票直接全量
        try:
            new = fetch_increment(code, start, today)
            # 只保留实际交易日（有收盘价的行）
            if len(new):
                new = new[new["close"].astype(str).str.len() > 0]
            if len(new) == 0:
                unchanged.append(code)
            else:
                if path.exists():
                    old = pd.read_csv(path)
                    n_before = len(old)
                    combined = pd.concat([old, new], ignore_index=True) \
                        .drop_duplicates(subset=["date"], keep="last") \
                        .sort_values("date").reset_index(drop=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    n_before = 0
                    combined = new.sort_values("date").reset_index(drop=True)
                combined.to_csv(path, index=False, encoding="utf-8")
                updated.append(code)
                if (i + 1) <= 5 or (i + 1) % 100 == 0:
                    log(f"[{i+1}/{len(codes)}] {code} +{len(combined)-n_before} 行 → {combined['date'].iloc[-1]}")
        except Exception as e:
            failed[code] = str(e)[:100]
            log(f"[{i+1}/{len(codes)}] {code} ❌ {e}")
        time.sleep(random.uniform(1.0, 2.0))

    bs.logout()

    # 状态落盘
    latest = {}
    for code in set(updated) | set(unchanged):
        d = last_date_in_csv(daily_path(code))
        if d is not None:
            latest[code] = d.strftime("%Y-%m-%d")
    state = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "universe_size": len(codes),
        "updated": len(updated),
        "unchanged": len(unchanged),
        "failed": failed,
        "max_date": max(latest.values()) if latest else None,
        "min_date": min(latest.values()) if latest else None,
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"完成：更新 {len(updated)}，无新数据 {len(unchanged)}，失败 {len(failed)}；"
        f"宇宙最新日 {state['max_date']}，最旧末日 {state['min_date']}")
    if failed:
        log(f"失败明细：{list(failed.items())[:10]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
