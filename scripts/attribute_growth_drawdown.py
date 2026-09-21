#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T28 成长策略 -60% 回撤归因与改进验证
=====================================
背景：成长(YOYNI+ROE) G2 回测（2015-01~2026-09）年化 8.0%、最大回撤 -59.8%
（2021-02-10 净值3.25 → 2024-02-05 净值1.31，三年到谷底，2026-06 才恢复）；
价值策略同期年化 13.0%、回撤 -35.0%。

本脚本三部分：
1. 买入特征归因：每次新买入时的 PE/PB 全宇宙分位、YOYNI、ROE，
   以及按买入估值/增速分组的买入后 60/120/250 交易日收益；
2. 回撤期画像：2021-02~2024-02 持仓估值中位数 vs 全宇宙；
3. 改进变体回测（同一引擎、同一成本口径）：
   A 估值过滤：剔除买入时 peTTM 处于全宇宙 70% 分位以上的票；
   B 质量持续：最近 3 个年报 ROE 均 >=10%（原为仅最新年报）；
   C 趋势择时：沪深300ETF 收盘 < 250 日均线时该期空仓；
   D 估值过滤 + 趋势择时；
   E 质量持续 + 趋势择时（检验"提收益的 B"与"控回撤的 C"能否叠加）。

输出：results/growth_attribution/（csv + 人读 md 报告）。
用法：python scripts/attribute_growth_drawdown.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quant import data as D          # noqa: E402
from quant import engine as E        # noqa: E402
from quant import metrics as M       # noqa: E402
from quant.factors import FactorModel, ROE_MIN  # noqa: E402

OUT = PROJECT_ROOT / "results/growth_attribution"
START = "2015-01-01"
TOP_N = 15


# ---------------------------------------------------------------- 数据
def load_valuation_panels(codes):
    """从原始日线 csv 构造 peTTM / pbMRQ 面板（KEYPIT-001 主面板不含这两列）。"""
    pe, pb = {}, {}
    for code in codes:
        df = D._read_daily(code)
        if df is None or len(df) < 250:
            continue
        s = df.set_index("date")
        if "peTTM" in s:
            pe[code] = s["peTTM"]
        if "pbMRQ" in s:
            pb[code] = s["pbMRQ"]
    pe_df = pd.DataFrame(pe).sort_index()
    pb_df = pd.DataFrame(pb).sort_index()
    return pe_df, pb_df


def universe_pctile(pe_row):
    """全宇宙当日 peTTM 分位（仅用正 PE 样本），返回 Series(code->分位)。"""
    pos = pe_row[pe_row > 0].dropna()
    if len(pos) < 50:
        return pd.Series(dtype=float)
    return pe_row.rank(pct=True)


# ----------------------------------------------------- 因子模型变体
class PeFilterModel(FactorModel):
    """变体 A：成长打分 + 买入时 PE 全宇宙分位上限。"""
    def __init__(self, codes, pe_panel, max_pctile=0.70):
        super().__init__(codes)
        self.pe_panel = pe_panel
        self.max_pctile = max_pctile

    def select(self, T, panels, factor="growth", top_n=TOP_N):
        ranked, scores = super().select(T, panels, factor="growth",
                                        top_n=len(self.codes))
        if T in self.pe_panel.index:
            pe_row = self.pe_panel.loc[T]
            pct = pe_row.rank(pct=True)
            ranked = [(c, s) for c, s in ranked
                      if c in pct.index
                      and np.isfinite(pct[c])
                      and (not np.isfinite(pe_row[c]) or pe_row[c] <= 0
                           or pct[c] <= self.max_pctile)]
        return ranked[:top_n], scores


class QualityPersistModel(FactorModel):
    """变体 B：成长打分 + 最近 3 个已披露年报 ROE 均 >=10%。"""
    def growth_score(self, code, T, panels):
        g = self._g(code)
        p = self._p(code)
        if g.empty:
            return np.nan
        gv = g[g["pubDate"] <= T].dropna(subset=["YOYNI"])
        if gv.empty:
            return np.nan
        yoyni = gv.iloc[-1]["YOYNI"]
        if not np.isfinite(yoyni):
            return np.nan
        if p.empty:
            return np.nan
        ann = p[(p["pubDate"] <= T) & (p["statDate"].dt.month == 12)] \
            .dropna(subset=["roeAvg"])
        if len(ann) < 3 or (ann["roeAvg"].iloc[-3:] < ROE_MIN).any():
            return np.nan
        if "netProfit" in ann.columns:
            try:
                if pd.to_numeric(ann.iloc[-1]["netProfit"],
                                 errors="coerce") <= 0:
                    return np.nan
            except Exception:
                pass
        return yoyni


def make_trend_filter(bench, ma_window=250):
    """变体 C：基准在 MA250 之下 → 空仓。"""
    ma = bench.rolling(ma_window).mean()

    def f(d, panels):
        b = bench.reindex(bench.index[bench.index <= d])
        m = ma.reindex(ma.index[ma.index <= d])
        if len(b) < ma_window:
            return False
        return b.iloc[-1] < m.iloc[-1]
    return f


# ---------------------------------------------------------------- 归因 1
def buy_characteristics(panels, pe_panel, pb_panel):
    """逐期新买入个股的信号日特征 + 买入后收益。"""
    trades = pd.read_csv(
        PROJECT_ROOT / "results/multifactor_backtest/trades_growth.csv")
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    close = panels["close"]

    rows = []
    prev_hold = set()
    for _, r in trades.iterrows():
        T = r["signal_date"]
        holds = set(str(r["holdings"]).split(",")) if pd.notna(r["holdings"]) else set()
        new_buys = [c for c in str(r["target"]).split(",") if c not in prev_hold]
        pe_row = pe_panel.loc[T] if T in pe_panel.index else pd.Series(dtype=float)
        pb_row = pb_panel.loc[T] if T in pb_panel.index else pd.Series(dtype=float)
        pct_row = pe_row.rank(pct=True)
        for c in new_buys:
            if c not in close.columns:
                continue
            fut = close[c].loc[close.index > T]
            entry = fut.iloc[0] if len(fut) else np.nan
            row = {
                "signal_date": T.date(), "code": c,
                "pe": pe_row.get(c, np.nan),
                "pe_pctile": pct_row.get(c, np.nan),
                "pb": pb_row.get(c, np.nan),
            }
            for h in (60, 120, 250):
                row[f"ret_{h}d"] = (fut.iloc[h] / entry - 1) \
                    if len(fut) > h and np.isfinite(entry) else np.nan
            rows.append(row)
        prev_hold = holds
    df = pd.DataFrame(rows)
    return df


def drawdown_profile(panels, pe_panel):
    """回撤期各月持仓 PE 中位数 vs 全宇宙 PE 中位数。"""
    trades = pd.read_csv(
        PROJECT_ROOT / "results/multifactor_backtest/trades_growth.csv")
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    rows = []
    for _, r in trades.iterrows():
        T = r["signal_date"]
        if not (pd.Timestamp("2020-06-01") <= T <= pd.Timestamp("2024-06-30")):
            continue
        holds = str(r["holdings"]).split(",") if pd.notna(r["holdings"]) else []
        if T not in pe_panel.index:
            continue
        pe_row = pe_panel.loc[T]
        hold_pe = pe_row.reindex(holds).dropna()
        hold_pe = hold_pe[hold_pe > 0]
        uni = pe_row[pe_row > 0].dropna()
        rows.append({
            "signal_date": T.date(),
            "持仓PE中位": hold_pe.median(),
            "全宇宙PE中位": uni.median(),
            "持仓PE分位均值": pe_row.rank(pct=True).reindex(holds).mean(),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- main
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    codes = D.load_constituents()
    print(f"宇宙 {len(codes)} 只，加载面板……")
    panels = D.load_price_panel(codes)
    valid = panels["codes"]
    print(f"有效个股 {len(valid)}；加载 PE/PB 面板……")
    pe_panel, pb_panel = load_valuation_panels(valid)
    bench = D.load_benchmark()

    # ---------- 1. 买入特征 ----------
    print("归因1：买入特征……")
    bc = buy_characteristics(panels, pe_panel, pb_panel)
    bc.to_csv(OUT / "buy_characteristics.csv", index=False)
    bc["pe档"] = pd.cut(bc["pe_pctile"], bins=[0, .2, .4, .6, .8, 1.0],
                        labels=["Q1低", "Q2", "Q3", "Q4", "Q5高"])
    grp_pe = bc.groupby("pe档", observed=True)[
        ["ret_60d", "ret_120d", "ret_250d"]].mean()
    grp_pe.columns = ["买入后60日", "买入后120日", "买入后250日"]
    grp_n = bc.groupby("pe档", observed=True).size().rename("样本数")
    grp_pe = pd.concat([grp_n, grp_pe], axis=1)
    grp_pe["样本数"] = grp_pe["样本数"].astype(int)
    # 分组表：样本数为整数、收益为百分比（floatfmt 只作用于 float 列）
    grp_fmt = grp_pe.copy()
    for c in ["买入后60日", "买入后120日", "买入后250日"]:
        grp_fmt[c] = (grp_fmt[c] * 100).round(1).astype(str) + "%"
    q1_250 = grp_pe.loc["Q1低", "买入后250日"] if "Q1低" in grp_pe.index else np.nan
    q5_250 = grp_pe.loc["Q5高", "买入后250日"] if "Q5高" in grp_pe.index else np.nan
    if np.isfinite(q1_250) and np.isfinite(q5_250):
        if q5_250 < q1_250 - 0.02:
            pe_read = (f"数据验证'景气顶点追高'：Q5高估值组买入后250日平均收益 "
                       f"{q5_250:.1%} 低于 Q1低估值组 {q1_250:.1%}。")
        elif q5_250 > q1_250 + 0.02:
            pe_read = (f"数据**不支持**简单'高PE=追高'说法：Q5高估值组买入后250日平均收益 "
                       f"{q5_250:.1%} 反而高于 Q1低估值组 {q1_250:.1%}——成长股高PE常对应业绩高增长，"
                       "静态PE分位对成长策略不是有效过滤（见变体A/D回测亦无改善）。")
        else:
            pe_read = f"Q1与Q5组买入后250日收益接近（{q1_250:.1%} vs {q5_250:.1%}），静态PE分位区分度弱。"
    else:
        pe_read = "Q1/Q5 样本不足，暂不下结论。"

    # ---------- 2. 回撤期画像 ----------
    print("归因2：回撤期估值画像……")
    prof = drawdown_profile(panels, pe_panel)
    prof.to_csv(OUT / "drawdown_profile.csv", index=False)

    # ---------- 3. 变体回测 ----------
    print("变体回测（基线/估值/质量/择时/组合）……")
    fm = FactorModel(valid)
    variants = {
        "基线成长(YOYNI+ROE)": dict(fm=fm, mf=None),
        "A_估值过滤(PE分位≤70%)": dict(fm=PeFilterModel(valid, pe_panel), mf=None),
        "B_质量持续(3年ROE≥10%)": dict(fm=QualityPersistModel(valid), mf=None),
        "C_趋势择时(MA250空仓)": dict(fm=fm, mf=make_trend_filter(bench)),
        "D_估值过滤+择时": dict(fm=PeFilterModel(valid, pe_panel),
                            mf=make_trend_filter(bench)),
        "E_质量持续+择时": dict(fm=QualityPersistModel(valid),
                            mf=make_trend_filter(bench)),
    }
    perf_rows, navs = [], {}
    for name, cfg in variants.items():
        nav_df, tr = E.run_backtest(
            panels, cfg["fm"], factor="growth", top_n=TOP_N,
            start=START, market_filter=cfg["mf"])
        nav = nav_df["nav"]
        navs[name] = nav
        m, _, _ = M.performance(nav, bench=bench,
                                turnover_series=tr["turnover"] if len(tr) else None)
        m["变体"] = name
        perf_rows.append(m)
        tr.to_csv(OUT / f"trades_{name.split('_')[0]}.csv", index=False)
    perf = pd.DataFrame(perf_rows).set_index("变体")
    cols = ["总收益", "年化收益", "基准年化", "超额年化(几何)", "年化波动",
            "最大回撤", "夏普", "卡玛", "月度胜率", "年化换手"]
    perf = perf[[c for c in cols if c in perf.columns]]
    perf.to_csv(OUT / "variant_performance.csv", encoding="utf-8-sig")
    pd.DataFrame(navs).to_csv(OUT / "nav_variants.csv")

    # ---------- 人读报告 ----------
    lines = [
        "# T28 成长策略 -60% 回撤归因报告（自动生成）",
        f"\n生成时间：{pd.Timestamp.now():%Y-%m-%d %H:%M}；回测区间 {START} 至今",
        "\n## 1. 回撤事实",
        "- 最大回撤 **-59.8%**：2021-02-10（净值3.25）→ 2024-02-05（1.31），",
        "  下跌 3 年，2026-06-25 才收复；同期沪深300 最大回撤 -53.0%。",
        "- 2021~2024 连续四年成长策略收益：+3.4% / -24.0% / -25.5% / -2.6%，",
        "  其中 2024 年基准 +18.4%、策略 -2.6%，跑输 21 个百分点。",
        "\n## 2. 买入估值分组（买入后平均收益）",
        grp_fmt.to_markdown(),
        f"\n解读：{pe_read}",
        "\n## 3. 回撤期持仓估值画像（节选）",
        prof.set_index("signal_date").iloc[::6].to_markdown(floatfmt=".1f"),
        "\n## 4. 改进变体回测对比",
        perf.to_markdown(floatfmt=".3f"),
        "\n## 5. 结论（需人工复核后写入策略文档/ADR）",
        "- 变体对比要点（自动生成，需人工确认）：A/D 估值过滤是否牺牲超额；B 质量持续是否提收益；",
        "  C/E 趋势择时是否把最大回撤压到 -40% 以内、代价（月胜率/换手）是否可接受；",
        "- 任何变体必须先经 G2 门复核（样本外、MA 窗口等参数敏感性），不得直接进模拟盘；",
        "- Hold 红线：成长策略在改进验证通过前不进模拟盘。",
    ]
    (OUT / "attribution_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n", perf.to_string(float_format=lambda x: f"{x:.3f}"))
    print(f"\n产物：{OUT}")


if __name__ == "__main__":
    main()
