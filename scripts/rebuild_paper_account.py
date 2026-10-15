#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价值策略模拟盘 · 从指定起始日干净重建账户
==========================================
当账户状态被污染（如现金透支、成交口径修正）或缺失净值时，从信号起始日
（默认 2026-09-21）用修正后的引擎重放：建仓 → 次日开盘成交 → 每日盯市 →
月末换仓，重新生成 data/paper/account.json 与净值曲线。

用法：
  python scripts/rebuild_paper_account.py            # 预检：重建到临时目录并打印，不写生产
  python scripts/rebuild_paper_account.py --apply     # 先备份再覆盖生产账户
  python scripts/rebuild_paper_account.py --start 2026-09-21 --apply
"""
import argparse
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.quant import data as D, factors  # noqa: E402
from src.execution.paper_trading import PaperAccount  # noqa: E402

# 复用日常推进逻辑，保证重建与每日运行口径完全一致
_spec = importlib.util.spec_from_file_location(
    "run_paper_trading", ROOT / "scripts/run_paper_trading.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

ACCT = ROOT / "data/paper/account.json"
OUT = ROOT / "results/paper_trading"


def rebuild(acct_path, start):
    start = pd.Timestamp(start)
    codes = D.load_constituents()
    panels = D.load_price_panel(codes)
    fm = factors.FactorModel(panels["codes"])
    all_dates = list(panels["close"].index)
    cal = D.load_trade_calendar()  # 真实交易日历，避免数据末日被误判为月末
    seg = [d for d in all_dates if d >= start]
    if not seg:
        raise SystemExit(f"起始日 {start.date()} 之后无交易日数据")

    acct_path = Path(acct_path)
    if acct_path.exists():
        # 干净重建语义：从空账户开始（生产账户的备份由调用方在 --apply 前完成）
        acct_path.unlink()
    acct = PaperAccount(acct_path)
    R.open_account(acct, fm, panels, seg[0])
    R.advance(acct, fm, panels, seg[1:], all_dates, cal)
    return acct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-21", help="信号起始日（默认 2026-09-21）")
    ap.add_argument("--apply", action="store_true", help="备份后覆盖生产账户；否则只预检")
    args = ap.parse_args()
    start = pd.Timestamp(args.start)

    if args.apply:
        target = ACCT
        if ACCT.exists():
            backup = ACCT.with_suffix(f".json.bak-{pd.Timestamp.now():%Y%m%d%H%M%S}")
            shutil.copy(ACCT, backup)
            print(f"已备份旧账户 → {backup.name}")
    else:
        target = Path(tempfile.mkdtemp(prefix="paper_rebuild_")) / "account.json"
        print("【预检模式】不写生产账户；加 --apply 才覆盖。\n")

    acct = rebuild(target, start)
    navs = acct.state["nav_history"]
    cashs = [r["cash"] for r in navs]
    lots_ok = all(sh % 100 == 0 for sh in acct.state["holdings"].values())

    print("日期         净值        现金       持仓市值")
    for r in navs:
        print(f"{r['date']}  {r['nav']:>11.2f} {r['cash']:>10.2f} {r['value']:>12.2f}")
    print("\n" + "=" * 56)
    print(f"净值条数：{len(navs)}（{navs[0]['date']} → {navs[-1]['date']}）")
    print(f"现金全程非负：{all(c >= 0 for c in cashs)}；最小现金：{min(cashs):.2f}")
    print(f"持仓全部 100 股整手：{lots_ok}；持仓只数：{len(acct.state['holdings'])}")
    print(f"期末净值：{navs[-1]['nav']:,.2f}；期末现金：{acct.state['cash']:,.2f}")

    if not all(c >= 0 for c in cashs):
        raise SystemExit("❌ 重建后仍有现金为负，已拒绝写盘，请检查成交逻辑")

    if args.apply:
        acct.save()
        OUT.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(navs).to_csv(OUT / "paper_nav.csv", index=False, encoding="utf-8")
        print(f"\n已写入 {ACCT} 并刷新 {OUT/'paper_nav.csv'}")
    else:
        print("\n预检通过（未写生产）。确认无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
