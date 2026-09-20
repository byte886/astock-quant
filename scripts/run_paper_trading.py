#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价值策略模拟盘 · 每日运行入口
================================
日常每天盘后跑一次：
  python scripts/run_paper_trading.py            # 推进一天/出当前信号
首次运行会用最新交易日生成价值策略信号（次日开盘成交），初始化 100 万虚拟账户。

状态在 data/paper/account.json；结果在 results/paper_trading/。
"""

import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.quant import data as D, factors          # noqa: E402
from src.execution.paper_trading import PaperAccount  # noqa: E402

ACCT = ROOT / "data/paper/account.json"
OUT = ROOT / "results/paper_trading"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("加载中证800日线面板 ...")
    codes = D.load_constituents()
    panels = D.load_price_panel(codes)
    fm = factors.FactorModel(panels["codes"])
    close = panels["close"]
    open_ = panels["open"]

    acct = PaperAccount(ACCT)
    dates = list(close.index)
    first_run = (len(acct.state["nav_history"]) == 0)

    if first_run:
        # 首次：从最新交易日开一个干净模拟盘（100万），直接生成当前信号
        d = dates[-1]
        ranked, _ = fm.select(d, panels, factor="value", top_n=15)
        acct.set_pending([c for c, _ in ranked], d)
        acct.state["nav_history"].append({
            "date": d.strftime("%Y-%m-%d"),
            "nav": round(acct.state["cash"], 2),
            "cash": round(acct.state["cash"], 2),
            "value": 0.0,
        })
        acct.save()
    else:
        # 日常：逐日推进（执行 pending→盯市→月末新信号）
        for d in dates:
            if acct.state["pending_target"] and d > pd.Timestamp(acct.state.get("last_exec") or "2000-01-01"):
                if d in open_.index:
                    o = open_.loc[d]
                    acct.execute_pending(o, o)
                    acct.state["last_exec"] = d.strftime("%Y-%m-%d")
            if d in close.index:
                nav = acct.nav(close.loc[d])
                acct.state["nav_history"].append({
                    "date": d.strftime("%Y-%m-%d"),
                    "nav": round(nav, 2),
                    "cash": round(acct.state["cash"], 2),
                    "value": round(acct.market_value(close.loc[d]), 2),
                })
            is_month_end = (d.month != dates[dates.index(d) + 1].month) if dates.index(d) + 1 < len(dates) else True
            if is_month_end and (acct.state["pending_signal_date"] is None):
                ranked, _ = fm.select(d, panels, factor="value", top_n=15)
                if ranked:
                    acct.set_pending([c for c, _ in ranked], d)
        acct.save()

    # ---------- 输出当前状态 ----------
    last = close.index[-1]
    nav_now = acct.nav(close.loc[last])
    print("\n" + "=" * 60)
    print(f"数据截止：{last.date()}")
    print(f"账户净值：{nav_now:,.0f} 元（初始 1,000,000）")
    print(f"现金：{acct.state['cash']:,.0f} 元；持仓 {len(acct.state['holdings'])} 只")
    print(f"待执行信号（{acct.state['pending_signal_date']}）：{len(acct.state['pending_target'])} 只")
    for c in acct.state["pending_target"]:
        print(f"  - {c}")
    # 净值曲线落盘
    if acct.state["nav_history"]:
        pd.DataFrame(acct.state["nav_history"]).to_csv(
            OUT / "paper_nav.csv", index=False, encoding="utf-8")
    print(f"\n账户状态：{ACCT}")
    print(f"净值曲线：{OUT/'paper_nav.csv'}")


if __name__ == "__main__":
    main()
