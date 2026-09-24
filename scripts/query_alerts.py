#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史预警查询
=====================================================================
读取 data/intraday_alerts/ 下所有按日 CSV，按日期 / 级别 / 板块名筛选。
每条记录自带"阈值快照"，可追溯当时是按什么标准报的。

用法：
  query_alerts.py                      # 汇总全部预警
  query_alerts.py --date 2026-09-25    # 只看某天
  query_alerts.py --level L2           # 只看主线
  query_alerts.py --board 海峡         # 板块名模糊搜
  query_alerts.py --board 海峡 --level L2
"""
import argparse, glob, sys
from pathlib import Path

try:
    import pandas as pd
except Exception:
    print("需要 pandas：.venv/bin/python", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
ALERT_DIR = ROOT / "data" / "intraday_alerts"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="日期 YYYY-MM-DD，只看某天的文件")
    ap.add_argument("--level", help="L1 或 L2")
    ap.add_argument("--board", help="板块名关键词，模糊匹配")
    a = ap.parse_args()

    files = sorted(glob.glob(str(ALERT_DIR / "*.csv")))
    if a.date:
        files = [f for f in files if a.date.replace("-", "") in Path(f).stem]
    if not files:
        print(f"（{ALERT_DIR} 下没有符合条件的预警记录）")
        return

    df = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in files],
                   ignore_index=True)
    if a.level:
        df = df[df["级别"] == a.level]
    if a.board:
        df = df[df["板块名"].astype(str).str.contains(a.board, na=False)]

    if df.empty:
        print("没有匹配的记录。")
        return

    print(f"共 {len(df)} 条预警（涉及 {len(files)} 个交易日）\n")
    show = ["时间", "级别", "板块名", "涨幅%", "涨停数", "阈值快照"]
    print(df[show].to_string(index=False))
    # 附完整消息
    print("\n--- 完整消息 ---")
    for _, r in df.iterrows():
        print(f"[{r['时间']}] {r['板块名']}：\n{r['消息']}\n")


if __name__ == "__main__":
    main()
