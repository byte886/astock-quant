#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绩效指标（日频净值）。"""

import pandas as pd
import numpy as np

TRADING_DAYS = 244
RF_ANNUAL = 0.02


def annual_return(nav):
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    total = nav.iloc[-1] / nav.iloc[0] - 1
    return (1 + total) ** (1 / years) - 1 if years > 0 else np.nan


def max_drawdown(nav):
    dd = nav / nav.cummax() - 1
    return dd.min(), dd


def performance(nav, bench=None, turnover_series=None):
    nav = nav.dropna()
    rets = nav.pct_change().dropna()
    ann = annual_return(nav)
    vol = rets.std() * np.sqrt(TRADING_DAYS)
    mdd, dd = max_drawdown(nav)
    sharpe = (rets.mean() * TRADING_DAYS - RF_ANNUAL) / vol if vol > 0 else np.nan
    calmar = ann / abs(mdd) if mdd < 0 else np.nan

    # 月度胜率
    mnav = nav.resample("ME").last()
    mret = mnav.pct_change().dropna()
    win_m = (mret > 0).mean()

    out = {
        "起始": nav.index[0].strftime("%Y-%m-%d"),
        "结束": nav.index[-1].strftime("%Y-%m-%d"),
        "总收益": nav.iloc[-1] / nav.iloc[0] - 1,
        "年化收益": ann,
        "年化波动": vol,
        "最大回撤": mdd,
        "夏普": sharpe,
        "卡玛": calmar,
        "月度胜率": win_m,
    }

    if bench is not None:
        b = bench.reindex(nav.index).ffill().dropna()
        b = b / b.iloc[0]
        strat = nav.reindex(b.index)
        excess = strat / b
        out["基准年化"] = annual_return(b)
        out["超额年化(几何)"] = ann - out["基准年化"]
        out["超额最大回撤"] = (excess / excess.cummax() - 1).min()

    if turnover_series is not None and len(turnover_series):
        years = (nav.index[-1] - nav.index[0]).days / 365.25
        out["年化换手"] = turnover_series.sum() / max(years, 1e-9)

    return out, dd, mret


def yearly_returns(nav):
    mnav = nav.resample("YE").last()
    yr = mnav.pct_change().dropna()
    yr.index = yr.index.year
    return yr
