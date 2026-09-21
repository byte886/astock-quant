#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日频月度调仓回测引擎（个股）
============================
- 信号日：每月最后交易日 T（盘后用截至 T 的 PIT 数据选股）
- 成交日：次月首个交易日，以【开盘价】调仓
- 涨停买不进、跌停卖不出、停牌不交易；卖不掉的滞持股冻结
- 成本：买入 佣金万2.5+滑点千1；卖出 佣金万2.5+印花税千1+滑点千1
- 日频盯市；调仓日分两段（收盘→开盘旧仓，开盘→收盘新仓）
"""

import pandas as pd
import numpy as np
from . import data as D

COMMISSION = 0.00025     # 佣金 万2.5（双边）
STAMP_TAX = 0.001        # 印花税 千1（卖出）
SLIPPAGE = 0.001         # 滑点 千1


def _limit_pct(code, is_st=0):
    """涨跌停幅度：创业板20%，ST 5%，主板10%。"""
    if is_st == 1:
        return 0.05
    num = code.split(".")[1]
    if num.startswith(("300", "301")):
        return 0.20
    return 0.10


def _tradable_open(code, td, panels, want_buy):
    """判断成交日 td 开盘能否成交。
    want_buy=True（买入）：开盘涨停或停牌 → 买不进；
    want_buy=False（卖出）：开盘跌停或停牌 → 卖不出。
    返回 (can_trade, open, preclose)。
    """
    try:
        op = panels["open"].loc[td, code]
        pc = panels["preclose"].loc[td, code]
    except Exception:
        return False, np.nan, np.nan
    if not np.isfinite(op) or not np.isfinite(pc) or pc <= 0:
        return False, np.nan, np.nan
    # 停牌
    if "tradestatus" in panels and code in panels["tradestatus"].columns:
        ts = panels["tradestatus"].loc[td, code] if td in panels["tradestatus"].index else np.nan
        if ts == 0:
            return False, op, pc
    if "volume" in panels and code in panels["volume"].columns:
        vol = panels["volume"].loc[td, code] if td in panels["volume"].index else np.nan
        if np.isfinite(vol) and vol == 0:
            return False, op, pc
    is_st = 0
    if "isST" in panels and code in panels["isST"].columns:
        v = panels["isST"].loc[td, code] if td in panels["isST"].index else np.nan
        if v == 1:
            is_st = 1
    chg = op / pc - 1
    lim = _limit_pct(code, is_st)
    if want_buy and chg >= lim - 0.001:      # 开盘封涨停 → 买不进
        return False, op, pc
    if (not want_buy) and chg <= -lim + 0.001:  # 开盘封跌停 → 卖不出
        return False, op, pc
    return True, op, pc


def run_backtest(panels, factor_model, factor="value", top_n=15,
                 start="2015-01-01", end=None, market_filter=None):
    """market_filter：可选 callable(信号日 d, panels) -> bool。
    返回 True 时该期目标为空（能卖的都卖出、转现金），用于市场择时归因。
    默认 None = 行为与原回测完全一致。"""
    close = panels["close"]
    dates = close.index
    dates = dates[(dates >= pd.Timestamp(start))]
    if end:
        dates = dates[dates <= pd.Timestamp(end)]
    mends = D.month_end_dates(dates, start=start)

    # 信号日 -> 成交日（下一交易日）映射
    trade_dates = dates
    sig2exec = {}
    for sig in mends[:-1]:
        nxt = trade_dates[trade_dates > sig]
        if len(nxt):
            sig2exec[sig] = nxt[0]

    nav = 1.0
    holdings = {}          # code -> 权重
    nav_records = []
    trades = []
    pending_target = None
    pending_sig = None

    date_set = list(dates)
    for i, d in enumerate(date_set):
        prev_d = date_set[i - 1] if i > 0 else None

        # ---------- 成交日：开盘调仓 ----------
        if pending_sig is not None and d == sig2exec.get(pending_sig):
            target = pending_target
            old = dict(holdings)

            # 1) 处理卖出：旧仓不在目标的，尝试卖
            frozen = {}
            for c, w in old.items():
                if c in target:
                    continue
                can, _, _ = _tradable_open(c, d, panels, want_buy=False)
                if not can:
                    frozen[c] = w   # 跌停/停牌，卖不掉，冻结

            # 2) 目标股可成交性（已持有的保留；新买的要能买）
            exec_targets = []
            rejected = []
            for c in target:
                if c in old:
                    exec_targets.append(c)      # 继续持有，不受涨停限制
                else:
                    can, _, _ = _tradable_open(c, d, panels, want_buy=True)
                    if can:
                        exec_targets.append(c)
                    else:
                        rejected.append(c)

            # 3) 新权重：滞持冻结，剩余资金在目标间等权
            w_frozen = sum(frozen.values())
            new = dict(frozen)
            if exec_targets:
                w_each = max(0.0, 1.0 - w_frozen) / len(exec_targets)
                for c in exec_targets:
                    new[c] = w_each
            cash_w = max(0.0, 1.0 - sum(new.values()))

            # 4) 换手与成本
            allc = set(old) | set(new)
            sell_w = sum(max(old.get(c, 0) - new.get(c, 0), 0) for c in allc)
            buy_w = sum(max(new.get(c, 0) - old.get(c, 0), 0) for c in allc)
            cost = (buy_w * (COMMISSION + SLIPPAGE)
                    + sell_w * (COMMISSION + STAMP_TAX + SLIPPAGE))
            nav *= (1 - cost)

            # 5) 调仓日分段盯市：旧仓 prev_close→open
            if prev_d is not None:
                r_pre = 0.0
                for c, w in old.items():
                    try:
                        pc = panels["preclose"].loc[d, c]
                        pc_prev = close.loc[prev_d, c]
                        if np.isfinite(pc) and np.isfinite(pc_prev) and pc_prev > 0:
                            r_pre += w * (pc / pc_prev - 1)
                    except Exception:
                        pass
                nav *= (1 + r_pre)

            trades.append({
                "signal_date": pending_sig.strftime("%Y-%m-%d"),
                "trade_date": d.strftime("%Y-%m-%d"),
                "factor": factor,
                "target": ",".join(target),
                "rejected_buy": ",".join(rejected),
                "frozen": ",".join(frozen.keys()),
                "holdings": ",".join(exec_targets),
                "cash_w": round(cash_w, 4),
                "turnover": round(sell_w + buy_w, 4),
                "cost": round(cost, 5),
            })
            holdings = new
            pending_target = None
            pending_sig = None

            # 6) 新仓 open→close
            r_post = 0.0
            for c, w in holdings.items():
                try:
                    op = panels["open"].loc[d, c]
                    cl = close.loc[d, c]
                    if np.isfinite(op) and np.isfinite(cl) and op > 0:
                        r_post += w * (cl / op - 1)
                except Exception:
                    pass
            nav *= (1 + r_post)
            nav_records.append({"date": d, "nav": nav, "cash": cash_w})
            continue

        # ---------- 普通日 / 信号日收盘盯市 ----------
        if prev_d is not None and holdings:
            r = 0.0
            for c, w in holdings.items():
                try:
                    c0 = close.loc[prev_d, c]
                    c1 = close.loc[d, c]
                    if np.isfinite(c0) and np.isfinite(c1) and c0 > 0:
                        r += w * (c1 / c0 - 1)
                except Exception:
                    pass
            nav *= (1 + r)

        cash_w = max(0.0, 1.0 - sum(holdings.values()))
        nav_records.append({"date": d, "nav": nav, "cash": cash_w})

        # ---------- 信号日盘后选股，次日执行 ----------
        if d in sig2exec:
            if market_filter is not None and market_filter(d, panels):
                pending_target = []          # 择时过滤：本期空仓转现金
            else:
                ranked, _ = factor_model.select(d, panels, factor=factor, top_n=top_n)
                pending_target = [c for c, _ in ranked]
            pending_sig = d

    nav_df = pd.DataFrame(nav_records).set_index("date")
    trades_df = pd.DataFrame(trades)
    return nav_df, trades_df
