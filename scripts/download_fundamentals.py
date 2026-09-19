#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中证800 财务数据下载（ROE / 净利润增速 / 分红 / 历史成分股）
============================================================
为多因子选股（价值=股息率、成长=YOYNI+ROE门槛）提供 PIT 基本面数据。

输出：
  data/raw/fundamentals/profit/{code}.csv    季度盈利能力（roeAvg 等），2012Q1起
  data/raw/fundamentals/growth/{code}.csv    季度成长能力（YOYNI 等）
  data/raw/fundamentals/dividend/{code}.csv  年度分红除权（每股现金分红）
  data/raw/index_constituents/hs300_YYYY-MM.csv / zz500_YYYY-MM.csv  逐月历史成分

特性：断点续传（已存在跳过）、限速、失败重试、独立状态。
用法：
  python scripts/download_fundamentals.py --limit 5      # 小批量测试
  python scripts/download_fundamentals.py                # 全量
  python scripts/download_fundamentals.py --constituents # 只下历史成分股
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

ROOT = Path(__file__).parent.parent
FUND = ROOT / "data/raw/fundamentals"
CONS = ROOT / "data/raw/index_constituents"
WORKSPACE = ROOT / "data/_workspace"
STATUS_FILE = WORKSPACE / "fundamentals_status.json"

START_YEAR = 2012          # 分红/财务起始（回测2015起，预留连续3年分红窗口）
QUARTERS = [(y, q) for y in range(START_YEAR, datetime.now().year + 1)
            for q in (1, 2, 3, 4)]


def get_csi800_codes():
    hs, zz = set(), set()
    rs = bs.query_hs300_stocks()
    while rs.next():
        hs.add(rs.get_row_data()[1])
    rs = bs.query_zz500_stocks()
    while rs.next():
        zz.add(rs.get_row_data()[1])
    return sorted(c for c in (hs | zz) if "688" not in c)


def fetch_table(query_fn, **kw):
    for attempt in range(3):
        try:
            rs = query_fn(**kw)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code != "0":
                time.sleep(1.0)
                continue
            return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame()
        except Exception:
            time.sleep(1.5)
    return pd.DataFrame()


def download_one(code):
    """下载单只股票的 profit/growth/dividend，返回 (ok, n_rows)"""
    profits, growths, divs = [], [], []
    for y, q in QUARTERS:
        if datetime(y, q * 3, 28) > datetime.now() and q >= 3:
            pass  # 未来季度仍尝试（baostock返回空）
        p = fetch_table(bs.query_profit_data, code=code, year=y, quarter=q)
        if not p.empty:
            profits.append(p)
        g = fetch_table(bs.query_growth_data, code=code, year=y, quarter=q)
        if not g.empty:
            growths.append(g)
        time.sleep(random.uniform(0.05, 0.15))
    for y in range(START_YEAR, datetime.now().year + 1):
        d = fetch_table(bs.query_dividend_data, code=code, year=str(y), yearType="report")
        if not d.empty:
            divs.append(d)
    if not profits and not divs:
        return False, 0
    if profits:
        df = pd.concat(profits, ignore_index=True)
        df.to_csv(FUND / "profit" / f"{code}.csv", index=False)
    if growths:
        df = pd.concat(growths, ignore_index=True)
        df.to_csv(FUND / "growth" / f"{code}.csv", index=False)
    if divs:
        df = pd.concat(divs, ignore_index=True)
        df.to_csv(FUND / "dividend" / f"{code}.csv", index=False)
    n = sum(len(x) for x in profits)
    return True, n


def download_constituents():
    """逐月下载历史成分股（2014-12起，每月末）"""
    CONS.mkdir(parents=True, exist_ok=True)
    months = []
    for y in range(2014, datetime.now().year + 1):
        for m in range(1, 13):
            if datetime(y, m, 1) <= datetime.now():
                months.append(f"{y}-{m:02d}")
    for kind, fn in [("hs300", bs.query_hs300_stocks),
                     ("zz500", bs.query_zz500_stocks)]:
        for ym in months:
            out = CONS / f"{kind}_{ym}.csv"
            if out.exists():
                continue
            # 用该月最后一天取成分（date 参数）
            y, m = int(ym[:4]), int(ym[5:7])
            import calendar
            last = calendar.monthrange(y, m)[1]
            date = f"{y}-{m:02d}-{last:02d}"
            try:
                rs = fn(date=date)
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    pd.DataFrame(rows, columns=rs.fields).to_csv(out, index=False)
                    print(f"{kind} {ym}: {len(rows)}只")
            except Exception as e:
                print(f"{kind} {ym} 失败: {e}")
            time.sleep(0.2)
    print("历史成分股下载完成")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--constituents", action="store_true")
    args = ap.parse_args()

    for d in ["profit", "growth", "dividend"]:
        (FUND / d).mkdir(parents=True, exist_ok=True)

    bs.login()
    if args.constituents:
        download_constituents()
        bs.logout()
        return

    codes = get_csi800_codes()
    if args.limit:
        codes = codes[:args.limit]
    print(f"待下载财务数据：{len(codes)}只")

    st = {"completed": [], "failed": {}}
    if STATUS_FILE.exists():
        st = json.loads(STATUS_FILE.read_text())
    done = set(st.get("completed", []))

    t0 = time.time()
    for i, code in enumerate(codes):
        if code in done:
            continue
        # 三个文件都在才算完成
        if (FUND / "profit" / f"{code}.csv").exists() and \
           (FUND / "dividend" / f"{code}.csv").exists():
            done.add(code)
            continue
        try:
            ok, n = download_one(code)
            if ok:
                done.add(code)
                st["failed"].pop(code, None)
                print(f"[{i+1}/{len(codes)}] {code} ✓ {n}季")
            else:
                st["failed"][code] = "无数据"
                print(f"[{i+1}/{len(codes)}] {code} ✗ 无数据")
        except Exception as e:
            st["failed"][code] = str(e)[:80]
            print(f"[{i+1}/{len(codes)}] {code} ✗ {e}")
        st["completed"] = sorted(done)
        if (i + 1) % 10 == 0:
            st["updated_at"] = datetime.now().isoformat()
            STATUS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
            rate = (i + 1) / (time.time() - t0)
            eta = (len(codes) - i - 1) / rate
            print(f"  --- 进度 {len(done)}/{len(codes)}, {rate:.1f}只/秒, 预计剩余{eta/60:.0f}分钟 ---")

    st["completed"] = sorted(done)
    st["updated_at"] = datetime.now().isoformat()
    STATUS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"\n完成 {len(done)}/{len(codes)}，失败 {len(st['failed'])}")
    bs.logout()


if __name__ == "__main__":
    main()
