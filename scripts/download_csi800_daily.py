#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中证800（沪深300+中证500，排除科创板688）日线优先下载
======================================================
多因子选股（价值/成长）只需要中证800约706只的日线（月度调仓无需分钟线）。
全市场4889只下载慢，本脚本优先把策略真正需要的706只下齐，数据量小、速度快。

- 输出：data/raw/daily/{sh,sz}/{code}.csv（与全量下载同目录、同字段，幂等）
- 前复权（adjustflag=2），1990至今
- 独立状态文件 data/_workspace/csi800_daily_status.json，不干扰全量下载服务
- 限速 1.0~2.0 秒随机，断点续传
"""

import baostock as bs
import pandas as pd
import os
import json
import time
import random
import argparse
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent
DAILY_DIR = PROJECT_ROOT / "data/raw/daily"
WORKSPACE = PROJECT_ROOT / "data/_workspace"
STATUS_FILE = WORKSPACE / "csi800_daily_status.json"

FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
          "turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST")
START_DATE = "2010-01-01"   # 多因子回测从2010起足够，减小体积


def get_csi800():
    hs, zz = set(), set()
    rs = bs.query_hs300_stocks()
    while rs.next():
        hs.add(rs.get_row_data()[1])
    rs = bs.query_zz500_stocks()
    while rs.next():
        zz.add(rs.get_row_data()[1])
    codes = sorted(c for c in (hs | zz) if "688" not in c)
    return codes


def load_status():
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text())
    return {"completed": [], "failed": {}}


def save_status(st):
    st["updated_at"] = datetime.now().isoformat()
    STATUS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true", help="只重试失败的")
    args = ap.parse_args()

    bs.login()
    codes = get_csi800()
    print(f"中证800(排除科创板)：{len(codes)}只")

    st = load_status()
    done = set(st["completed"])

    todo = codes
    if args.retry_failed:
        todo = sorted(st["failed"].keys())
        print(f"重试失败标的：{len(todo)}只")

    for i, code in enumerate(todo):
        if code in done and not args.retry_failed:
            continue
        mkt = "sh" if code.startswith("sh.") else "sz"
        out = DAILY_DIR / mkt / f"{code}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.stat().st_size > 5000 and not args.retry_failed:
            done.add(code)
            continue
        try:
            rs = bs.query_history_k_data_plus(
                code, FIELDS,
                start_date=START_DATE, end_date=datetime.now().strftime("%Y-%m-%d"),
                frequency="d", adjustflag="2")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if len(rows) < 200:
                st["failed"][code] = f"仅{len(rows)}行"
                print(f"[{i+1}/{len(todo)}] {code} 数据不足({len(rows)}行)")
            else:
                pd.DataFrame(rows, columns=FIELDS.split(",")).to_csv(out, index=False)
                done.add(code)
                st["failed"].pop(code, None)
                print(f"[{i+1}/{len(todo)}] {code} ✓ {len(rows)}行")
        except Exception as e:
            st["failed"][code] = str(e)[:100]
            print(f"[{i+1}/{len(todo)}] {code} ✗ {e}")
        st["completed"] = sorted(done)
        if (i + 1) % 20 == 0:
            save_status(st)
        time.sleep(random.uniform(1.0, 2.0))

    save_status(st)
    print(f"\n完成：{len(done)}/{len(codes)}，失败{len(st['failed'])}")
    bs.logout()


if __name__ == "__main__":
    main()
