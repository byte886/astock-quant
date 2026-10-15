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
LOT_SHARES = 100  # A 股买入最小单位：1 手 = 100 股
SELL_RATE = COMMISSION + STAMP_TAX + SLIPPAGE   # 卖出费率：佣金+印花税+滑点
BUY_RATE = COMMISSION + SLIPPAGE                # 买入费率：佣金+滑点（买入无印花税）


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
    def _liquidate(self, open_row):
        """卖出可成交的旧仓。

        开盘价取不到（停牌/无数据）的持仓保留、不卖出；返回
        (卖出净回款, 保留持仓)。卖出净回款 = 卖出额 − 卖出费，只在
        这里扣一次卖出费（历史上曾在买入后又扣一次，导致现金虚减透支）。
        """
        carried = dict(self.state["holdings"])
        proceeds = 0.0
        sell_cost = 0.0
        for code, sh in list(self.state["holdings"].items()):
            px = open_row.get(code, np.nan)
            if not np.isfinite(px) or px <= 0:
                continue  # 停牌/无价：保留旧仓，不参与本轮调仓
            value = sh * px
            proceeds += value
            sell_cost += value * SELL_RATE
            carried.pop(code, None)
        net_proceeds = self.state["cash"] + proceeds - sell_cost
        return net_proceeds, carried

    @staticmethod
    def _lot_shares(price, budget):
        """预算内能买的整手股数（100 股一手，向下取整）。"""
        return int(budget / price / LOT_SHARES) * LOT_SHARES

    def _buy(self, target, open_row, available):
        """按等权预算买入新目标，返回 (新持仓, 期末现金)。

        每只成交额预算 = 可用现金 / 只数 / (1 + 买入费率)，已预留买入佣金+
        滑点；再按 100 股整手向下取整，故"成交额+买入费"必不超过该只预算，
        全部买入后期末现金必然 ≥ 0（不允许透支）。
        """
        budget_each = available / len(target) / (1 + BUY_RATE)
        new_holdings = {}
        buy_value = 0.0
        for code in target:
            px = open_row.get(code, np.nan)
            if not np.isfinite(px) or px <= 0:
                continue  # 停牌/涨停买不到：跳过，不占用预算
            sh = self._lot_shares(px, budget_each)
            if sh <= 0:
                continue
            new_holdings[code] = sh
            buy_value += sh * px
        cash = available - buy_value - buy_value * BUY_RATE
        return new_holdings, cash

    def execute_pending(self, open_row, preclose_row=None, tradestatus_row=None, isst_row=None):
        target = self.state["pending_target"]
        if not target:
            return []
        # 1) 卖出可成交旧仓（停牌保留），得到可用现金（卖出费仅在此处扣一次）
        available, carried = self._liquidate(open_row)
        if available <= 0:
            # 无可用现金：保留已卖出的净回款与停牌旧仓，不再买入
            self.state["cash"] = available
            self.state["holdings"] = carried
            self._clear_pending()
            return []
        # 2) 等权买入新目标（预算预留买入费、整手向下）
        bought, cash = self._buy(target, open_row, available)
        # 现金硬约束：预算+整手向下本应保证 ≥0，若仍为负即逻辑错误，宁报错不透支
        if cash < -0.01:
            raise RuntimeError(f"模拟盘买入后现金为负 {cash:.2f}，违反不透支约束")
        new_holdings = dict(carried)
        new_holdings.update(bought)
        self.state["cash"] = cash
        self.state["holdings"] = new_holdings
        self._clear_pending()
        return list(bought.keys())

    def _clear_pending(self):
        self.state["pending_target"] = []
        self.state["pending_signal_date"] = None

    # ---------- 月末生成新信号 ----------
    def set_pending(self, target, signal_date):
        self.state["pending_target"] = list(target)
        self.state["pending_signal_date"] = pd.Timestamp(signal_date).strftime("%Y-%m-%d")
