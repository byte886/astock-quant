#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子选股回测入口：价值(高股息) + 成长(YOYNI+ROE门槛)
=====================================================
用法：
  python scripts/run_multifactor_backtest.py              # 两个策略都跑
  python scripts/run_multifactor_backtest.py --factor value
  python scripts/run_multifactor_backtest.py --top 15 --start 2015-01-01
"""

import sys
import argparse
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.quant import data as D, factors, engine, metrics, viz  # noqa: E402

OUT = ROOT / "results/multifactor_backtest"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor", choices=["value", "growth", "both"], default="both")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--start", default="2015-01-01")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    names = {"value": "价值(高股息)", "growth": "成长(YOYNI+ROE)"}
    todo = ["value", "growth"] if args.factor == "both" else [args.factor]

    print("加载中证800日线面板 ...")
    codes = D.load_constituents()
    print(f"  成分股 {len(codes)} 只")
    panels = D.load_price_panel(codes)
    print(f"  有效日线 {len(panels['codes'])} 只，"
          f"{panels['close'].index[0].date()} ~ {panels['close'].index[-1].date()}")

    bench = D.load_benchmark()
    fm = factors.FactorModel(panels["codes"])

    navs, dds, yearly, perf_rows, turn = {}, {}, {}, {}, {}
    for fac in todo:
        print(f"\n回测 {names[fac]} ...")
        nav_df, trades = engine.run_backtest(
            panels, fm, factor=fac, top_n=args.top, start=args.start)
        if nav_df.empty:
            print("  无净值结果（财务数据可能未下完）")
            continue
        nav = nav_df["nav"]
        to = trades["turnover"] if not trades.empty else pd.Series(dtype=float)
        perf, dd, _ = metrics.performance(nav, bench=bench, turnover_series=to)
        perf["策略"] = names[fac]
        perf_rows[fac] = perf
        navs[names[fac]] = nav
        dds[names[fac]] = dd
        yearly[names[fac]] = metrics.yearly_returns(nav)
        turn[fac] = to

        nav_df.to_csv(OUT / f"nav_{fac}.csv", encoding="utf-8")
        trades.to_csv(OUT / f"trades_{fac}.csv", index=False, encoding="utf-8")
        print(f"  调仓 {len(trades)} 次，期末净值 {nav.iloc[-1]:.3f}，"
              f"年化 {perf['年化收益']*100:.2f}%，最大回撤 {perf['最大回撤']*100:.2f}%，"
              f"夏普 {perf['夏普']:.2f}")

    if not navs:
        print("\n财务数据尚未下完，请等待 download_fundamentals.py 完成后重跑。")
        return

    # 基准绩效
    b = bench.reindex(navs[list(navs)[0]].index).ffill()
    bperf, _, _ = metrics.performance(b / b.iloc[0])
    bperf["策略"] = "沪深300"
    perf_rows["bench"] = bperf
    yearly["沪深300"] = metrics.yearly_returns(b / b.iloc[0])

    # 汇总表
    cols = ["策略", "起始", "结束", "总收益", "年化收益", "基准年化", "年化波动",
            "最大回撤", "夏普", "卡玛", "月度胜率", "超额年化(几何)", "年化换手"]
    summ = pd.DataFrame(list(perf_rows.values()))
    summ = summ.reindex(columns=[c for c in cols if c in summ.columns])
    show = summ.copy()
    for c in ["总收益", "年化收益", "基准年化", "年化波动", "最大回撤", "月度胜率",
              "超额年化(几何)", "年化换手"]:
        if c in show.columns:
            show[c] = (show[c].astype(float) * 100).round(2).astype(str) + "%"
    for c in ["夏普", "卡玛"]:
        if c in show.columns:
            show[c] = show[c].astype(float).round(2)
    print("\n" + "=" * 80)
    print(show.to_string(index=False))
    summ.to_csv(OUT / "performance_summary.csv", index=False, encoding="utf-8")
    pd.DataFrame(yearly).to_csv(OUT / "yearly_returns.csv", encoding="utf-8")

    # 图
    try:
        img = viz.plot_nav_drawdown(navs, bench, dds, yearly)
        print(f"\n图表已保存：{img}")
    except Exception as e:
        print(f"绘图失败：{e}")
    print(f"结果目录：{OUT}")


if __name__ == "__main__":
    main()
