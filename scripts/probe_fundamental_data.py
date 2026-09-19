#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子选股 · 财务数据可得性探测
================================
验证 baostock 能否提供：
  1. ROE（净资产收益率）     → query_profit_data.roeAvg（成长策略）
  2. 净利润同比增长率        → query_growth_data.YOYNI（成长策略）
  3. 分红明细               → query_history_dividend_detail（算股息率，价值策略）
  4. 财报发布日期 pubDate    → PIT 防未来函数关键
测试标的：贵州茅台 sh.600519（高ROE）、工商银行 sh.601398（高股息）
"""

import baostock as bs
import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def main():
    lg = bs.login()
    print(f"登录：{lg.error_code} {lg.error_msg}")

    # ---------- 1. 盈利能力（ROE）----------
    print("\n" + "=" * 70)
    print("【1】query_profit_data 盈利能力 · 贵州茅台 2023Q4")
    print("=" * 70)
    rs = bs.query_profit_data(code="sh.600519", year=2023, quarter=4)
    print(f"字段：{rs.fields}")
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if rows:
        df = pd.DataFrame(rows, columns=rs.fields)
        print(df.to_string(index=False))

    # ROE 历史覆盖（茅台 2015-2025 每年Q4）
    print("\n--- 茅台 ROE(roeAvg) 历年Q4 ---")
    for y in range(2015, 2026):
        rs = bs.query_profit_data(code="sh.600519", year=y, quarter=4)
        while rs.next():
            d = dict(zip(rs.fields, rs.get_row_data()))
            print(f"  {y}Q4  ROE={d.get('roeAvg')}  发布日={d.get('pubDate')}  截止={d.get('statDate')}")

    # ---------- 2. 成长能力（净利润同比）----------
    print("\n" + "=" * 70)
    print("【2】query_growth_data 成长能力 · 茅台 2023Q4")
    print("=" * 70)
    rs = bs.query_growth_data(code="sh.600519", year=2023, quarter=4)
    print(f"字段：{rs.fields}")
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if rows:
        df = pd.DataFrame(rows, columns=rs.fields)
        print(df.to_string(index=False))

    # ---------- 3. 分红明细（股息率原料）----------
    print("\n" + "=" * 70)
    print("【3】query_history_dividend_detail 分红 · 工商银行")
    print("=" * 70)
    # yearType=report 按报告期; operate=分红
    for yt in ["report"]:
        rs = bs.query_history_dividend_detail(code="sh.601398", year="2023", yearType=yt)
        print(f"[{yt}] 字段：{rs.fields}  错误：{rs.error_code} {rs.error_msg}")
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if rows:
            df = pd.DataFrame(rows, columns=rs.fields)
            print(df.to_string(index=False))
        else:
            print("  无数据")

    # 不限年份，看工行能拿到多少年分红
    print("\n--- 工行分红（yearType=operation 按实施年，逐年探测）---")
    for y in range(2015, 2026):
        rs = bs.query_history_dividend_detail(code="sh.601398", year=str(y), yearType="operation")
        cnt = 0
        sample = None
        while rs.next():
            cnt += 1
            if sample is None:
                sample = dict(zip(rs.fields, rs.get_row_data()))
        if cnt:
            print(f"  {y}年实施：{cnt}条，示例：{sample}")

    # ---------- 4. 除权除息（另一个分红数据源）----------
    print("\n" + "=" * 70)
    print("【4】query_dividend_data 除权除息 · 工行 2023")
    print("=" * 70)
    rs = bs.query_dividend_data(code="sh.601398", year="2023", yearType="report")
    print(f"字段：{rs.fields}  错误：{rs.error_code} {rs.error_msg}")
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if rows:
        df = pd.DataFrame(rows, columns=rs.fields)
        print(df.to_string(index=False))

    bs.logout()


if __name__ == "__main__":
    main()
