#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟盘账户（价值策略 MVP）
==========================
与回测同一套选股与成本口径，但按"每日增量"运行：
  盘后（信号日 T 收盘后）用截至 T 的 PIT 数据选股 → 存 pending_target；
  下一交易日以【开盘价】执行 pending（扣除成本）；
  每日按收盘价盯市，记净值。

状态持久化在 data/paper/account.json：
  {cash, holdings:{code:shares}, nav_history:[{date,nav,cash,value}],
   pending_target:[code], pending_signal_date:"YYYY-MM-DD", last_exec:"YYYY-MM-DD"}
"""

import json
from pathlib import Path
import pandas as pd
import numpy as np

INITIAL_CASH = 1_000_000.0
COMMISSION = 0.00025
STAMP_TAX = 0.001
SLIPPAGE = 0.001


class PaperAccount:
    def __init__(self, path):
        self.path = Path(path)
        self.state = {
            "cash": INITIAL_CASH,
            "holdings": {},
            "nav_history": [],
            "pending_target": [],
            "pending_signal_date": None,
            "last_exec": None,
        }
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text()))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))

    # ---------- 估值 ----------
    def market_value(self, close_row):
        mv = 0.0
        for code, sh in self.state["holdings"].items():
            px = close_row.get(code, np.nan)
            if np.isfinite(px):
                mv += sh * px
        return mv

    def nav(self, close_row):
        return self.state["cash"] + self.market_value(close_row)

    # ---------- 执行 pending（次日开盘价成交） ----------
    def execute_pending(self, open_row, preclose_row=None, tradestatus_row=None, isst_row=None):
        target = self.state["pending_target"]
        if not target:
            return []
        # 1) 全卖旧仓，回收现金（扣卖出成本：佣金+印花税+滑点）
        proceeds, cost = 0.0, 0.0
        for code, sh in self.state["holdings"].items():
            px = open_row.get(code, np.nan)
            if not np.isfinite(px) or px <= 0:
                continue
            v = sh * px
            proceeds += v
            cost += v * (COMMISSION + STAMP_TAX + SLIPPAGE)
        available = self.state["cash"] + proceeds - cost
        # 2) 按新目标等权买入（扣买入成本：佣金+滑点）
        if available <= 0 or not target:
            return []
        # 每只预算预留买入佣金+滑点，避免满仓买入后现金小幅为负
        budget_each = available / len(target) / (1 + COMMISSION + SLIPPAGE)
        new_holdings, buy_val = {}, 0.0
        for code in target:
            px = open_row.get(code, np.nan)
            if not np.isfinite(px) or px <= 0:
                continue
            sh = int(budget_each / px)  # 整手按股（向下取整再留余量）
            if sh <= 0:
                continue
            new_holdings[code] = sh
            buy_val += sh * px
        cost += buy_val * (COMMISSION + SLIPPAGE)
        self.state["cash"] = available - buy_val - cost
        self.state["holdings"] = new_holdings
        self.state["pending_target"] = []
        self.state["pending_signal_date"] = None
        return list(new_holdings.keys())

    # ---------- 月末生成新信号 ----------
    def set_pending(self, target, signal_date):
        self.state["pending_target"] = list(target)
        self.state["pending_signal_date"] = pd.Timestamp(signal_date).strftime("%Y-%m-%d")
