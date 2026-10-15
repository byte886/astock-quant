# -*- coding: utf-8 -*-
"""run_paper_trading 编排逻辑（open_account / advance）端到端流程测试。

用小规模假面板 + 假因子模型，自包含验证，不依赖生产账户处于某个阶段：
  - 信号日盘后出信号，当天不成交（防未来函数），下一交易日开盘才成交；
  - 信号日重跑（无新交易日）不记账，幂等；
  - 月末出新月度信号，次月首个交易日开盘换仓；
  - 全程现金非负、持仓为 100 股整数倍。
"""
import pandas as pd
import pytest

from src.execution.paper_trading import INITIAL_CASH, LOT_SHARES, PaperAccount
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "run_paper_trading",
    Path(__file__).resolve().parents[2] / "scripts/run_paper_trading.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


DATES = [pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-30"),
         pd.Timestamp("2026-10-08")]
CODES = ["A", "B", "C", "D"]


def _frame():
    # 全部 10 元，结构简单便于断言
    return pd.DataFrame(10.0, index=pd.DatetimeIndex(DATES), columns=CODES)


class FakeFM:
    """按信号日返回预定目标；9-29 建仓 [A,B]，9-30 月末换 [C,D]。"""

    def __init__(self):
        self.signals = {
            DATES[0]: [("A", 1.0), ("B", 0.9)],
            DATES[1]: [("C", 1.0), ("D", 0.9)],
        }

    def select(self, d, panels, factor, top_n):
        return self.signals.get(pd.Timestamp(d).normalize(), []), None


@pytest.fixture()
def acct(tmp_path):
    return PaperAccount(tmp_path / "account.json")


def test_signal_not_filled_until_next_open(acct):
    fm, panels = FakeFM(), {"close": _frame(), "open": _frame()}
    R.open_account(acct, fm, panels, DATES[0])
    # 信号日盘后：只有 pending，当天无持仓、无成交
    assert acct.state["holdings"] == {}
    assert acct.state["pending_target"] == ["A", "B"]
    assert len(acct.state["nav_history"]) == 1


def test_full_monthly_rotation_flow(acct):
    fm, panels = FakeFM(), {"close": _frame(), "open": _frame()}
    R.open_account(acct, fm, panels, DATES[0])
    R.advance(acct, fm, panels, [DATES[1], DATES[2]], DATES)

    # 9-30 开盘成交 A、B；月末出新信号 C、D；10-08 开盘换仓为 C、D
    assert set(acct.state["holdings"]) == {"C", "D"}
    assert acct.state["last_exec"] == "2026-10-08"
    # 净值：9-29 初始 + 9-30 + 10-08 共 3 条
    assert [r["date"] for r in acct.state["nav_history"]] == [
        "2026-09-29", "2026-09-30", "2026-10-08"]
    assert acct.state["cash"] >= 0
    assert all(sh % LOT_SHARES == 0 for sh in acct.state["holdings"].values())


def test_rerun_signal_day_is_idempotent(acct):
    fm, panels = FakeFM(), {"close": _frame(), "open": _frame()}
    R.open_account(acct, fm, panels, DATES[0])
    n_before = len(acct.state["nav_history"])
    # 没有新交易日时推进（空 todo），不应记账、不应改变状态
    R.advance(acct, fm, panels, [], DATES)
    assert len(acct.state["nav_history"]) == n_before
    assert acct.state["cash"] == INITIAL_CASH


def test_no_fill_on_signal_day_open(acct):
    """信号日即便当天有 open 行，也不得当天成交（杜绝未来函数）。"""
    fm, panels = FakeFM(), {"close": _frame(), "open": _frame()}
    R.open_account(acct, fm, panels, DATES[0])
    # open_account 只记账，不执行任何买入
    assert acct.state["last_exec"] is None


def test_no_false_signal_on_data_edge_day(acct):
    """回归（I19 延伸）：面板最后一天 10-15（非月末，10-30 才是月末）不得被
    误判为月末而发出换仓信号——否则下一交易日会做一次非月度的错误调仓。
    用含未来交易日的真实日历判定；FakeFM 对 10-15 埋了陷阱信号，只有被误判
    为月末才会取出。"""
    edge_dates = [pd.Timestamp("2026-10-14"), pd.Timestamp("2026-10-15")]
    cal = pd.DatetimeIndex(
        ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-30"])

    class EdgeFM:
        def select(self, d, panels, factor, top_n):
            d = pd.Timestamp(d).normalize()
            if d == pd.Timestamp("2026-10-14"):
                return [("A", 1.0), ("B", 0.9)], None
            if d == pd.Timestamp("2026-10-15"):
                # 陷阱：仅当 10-15 被误判为月末时才会被调用
                return [("C", 1.0), ("D", 0.9)], None
            return [], None

    frame = pd.DataFrame(10.0, index=pd.DatetimeIndex(edge_dates), columns=CODES)
    panels = {"close": frame, "open": frame}
    R.open_account(acct, EdgeFM(), panels, edge_dates[0])
    R.advance(acct, EdgeFM(), panels, [edge_dates[1]], edge_dates, cal)

    # 10-15 开盘只成交 10-14 的建仓信号 A、B；10-15 非月末，不取出 C、D 陷阱
    assert set(acct.state["holdings"]) == {"A", "B"}
    assert acct.state["pending_target"] == []
    assert acct.state["pending_signal_date"] is None
