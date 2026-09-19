#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
因子层（严格 PIT）
==================
价值因子 dividend_yield：过去12个月已除息现金分红 / 信号日收盘价，要求连续3年分红。
成长因子 growth：最新已披露财报 YOYNI（净利润同比增速），门槛最新年报 ROE>=10%。

所有财务数据只允许使用 pubDate / dividOperateDate <= 信号日的记录，杜绝未来函数。
"""

import pandas as pd
import numpy as np
from . import data as D

ROE_MIN = 0.10          # 成长策略 ROE 门槛
DIV_YEARS = 3           # 价值策略要求连续分红年数


class FactorModel:
    def __init__(self, codes):
        self.codes = codes
        self._profit = {}
        self._growth = {}
        self._div = {}

    def _p(self, code):
        if code not in self._profit:
            self._profit[code] = D.load_profit(code)
        return self._profit[code]

    def _g(self, code):
        if code not in self._growth:
            self._growth[code] = D.load_growth(code)
        return self._growth[code]

    def _d(self, code):
        if code not in self._div:
            self._div[code] = D.load_dividend(code)
        return self._div[code]

    # ---------- 通用可投资过滤 ----------
    def _tradable(self, code, T, panels):
        """信号日 T：非ST、未停牌、有收盘价。"""
        try:
            if "isST" in panels and code in panels["isST"].columns:
                st = panels["isST"].loc[:T, code].dropna()
                if len(st) and st.iloc[-1] == 1:
                    return False
            if "tradestatus" in panels and code in panels["tradestatus"].columns:
                ts = panels["tradestatus"].loc[:T, code].dropna()
                if len(ts) and ts.iloc[-1] == 0:
                    return False
            close = panels["close"].loc[:T, code].dropna()
            if len(close) < 250:        # 上市/历史不足1年
                return False
            return True
        except Exception:
            return False

    def _close_at(self, code, T, panels):
        s = panels["close"].loc[:T, code].dropna()
        return s.iloc[-1] if len(s) else np.nan

    # ---------- 价值因子：股息率 ----------
    def dividend_yield(self, code, T, panels):
        div = self._d(code)
        if div.empty or "dividOperateDate" not in div.columns:
            return np.nan
        d = div.dropna(subset=["dividOperateDate", "cash"])
        d = d[d["dividOperateDate"] <= T]
        if d.empty:
            return np.nan

        # 连续 N 个完整自然年分红（完整年 = 信号日所在年之前的年份）
        years_required = [T.year - k for k in range(1, DIV_YEARS + 1)]
        for y in years_required:
            yd = d[(d["dividOperateDate"] >= f"{y}-01-01") &
                   (d["dividOperateDate"] <= f"{y}-12-31")]
            if yd["cash"].sum() <= 0:
                return np.nan

        # 过去12个月已除息现金分红
        lookback = T - pd.DateOffset(years=1)
        recent = d[d["dividOperateDate"] > lookback]
        ttm_div = recent["cash"].sum()
        if ttm_div <= 0:
            return np.nan

        px = self._close_at(code, T, panels)
        if not np.isfinite(px) or px <= 0:
            return np.nan
        return ttm_div / px

    # ---------- 成长因子：YOYNI + ROE门槛 ----------
    def growth_score(self, code, T, panels):
        g = self._g(code)
        p = self._p(code)
        if g.empty:
            return np.nan
        gv = g[g["pubDate"] <= T].dropna(subset=["YOYNI"])
        if gv.empty:
            return np.nan
        latest = gv.iloc[-1]
        yoyni = latest["YOYNI"]
        if not np.isfinite(yoyni):
            return np.nan

        # ROE 质量门槛：最新已披露年报（statDate 为 12-31）ROE >= 10%
        if p.empty:
            return np.nan
        ann = p[(p["pubDate"] <= T) & (p["statDate"].dt.month == 12)].dropna(subset=["roeAvg"])
        if ann.empty or ann.iloc[-1]["roeAvg"] < ROE_MIN:
            return np.nan
        # 净利润为正（最新年报）
        if "netProfit" in ann.columns:
            try:
                if pd.to_numeric(ann.iloc[-1]["netProfit"], errors="coerce") <= 0:
                    return np.nan
            except Exception:
                pass
        return yoyni

    # ---------- 批量选股 ----------
    def select(self, T, panels, factor="value", top_n=15):
        scores = {}
        fn = self.dividend_yield if factor == "value" else self.growth_score
        for code in self.codes:
            if not self._tradable(code, T, panels):
                continue
            v = fn(code, T, panels)
            if np.isfinite(v):
                scores[code] = v
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_n], scores
