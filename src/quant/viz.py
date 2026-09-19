#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可视化：净值曲线 + 回撤图 + 年度收益。"""

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

# 中文字体：从系统已安装字体里挑第一个可用的
from matplotlib import font_manager  # noqa: E402
_available = {f.name for f in font_manager.fontManager.ttflist}
for f in ["Heiti SC", "STHeiti", "Songti SC", "Hiragino Sans GB",
          "Arial Unicode MS", "PingFang SC", "SimHei"]:
    if f in _available:
        matplotlib.rcParams["font.sans-serif"] = [f]
        break
matplotlib.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "results/multifactor_backtest"


def plot_nav_drawdown(navs, bench, dds, yearly, save=OUT / "multifactor_report.png"):
    OUT.mkdir(parents=True, exist_ok=True)
    colors = {"价值(高股息)": "#c0392b", "成长(YOYNI+ROE)": "#2471a3", "沪深300": "#7f8c8d"}
    fig, axes = plt.subplots(3, 1, figsize=(13, 13), gridspec_kw={"height_ratios": [3, 1.6, 1.6]})

    # 1. 净值
    ax = axes[0]
    for name, nav in navs.items():
        ax.plot(nav.index, nav.values, label=name, lw=1.8, color=colors.get(name))
    b = bench.reindex(nav.index).ffill()
    ax.plot(b.index, b.values / b.iloc[0], label="沪深300", lw=1.4,
            ls="--", color=colors["沪深300"])
    ax.set_title("多因子选股策略净值（月度调仓 · 15只等权 · 含成本）", fontsize=14)
    ax.set_ylabel("净值（初始=1）")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)

    # 2. 回撤
    ax = axes[1]
    for name, dd in dds.items():
        ax.fill_between(dd.index, dd.values * 100, 0, alpha=0.35,
                        color=colors.get(name), label=name)
    ax.set_title("回撤（水下曲线）")
    ax.set_ylabel("回撤 %")
    ax.legend(loc="lower left")
    ax.grid(alpha=0.3)

    # 3. 年度收益
    ax = axes[2]
    yr = pd.DataFrame(yearly)
    yr.plot(kind="bar", ax=ax, width=0.8,
            color=[colors.get(c, "#999") for c in yr.columns])
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("年度收益对比")
    ax.set_ylabel("收益率 %")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=1))
    ax.legend(loc="lower left", fontsize=9)
    ax.grid(alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(save, dpi=130, bbox_inches="tight")
    plt.close()
    return save
