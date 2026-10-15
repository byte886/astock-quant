# -*- coding: utf-8 -*-
"""PaperAccount 模拟盘账户单元测试（纯函数级，不依赖真实行情数据）。

重点覆盖 I19 现金透支修复：
  - 卖出费用只扣一次（历史上买入后又扣了一次，导致每次调仓现金虚减）；
  - 买入按 100 股整手、预算预留买入费，期末现金恒非负；
  - 停牌旧仓保留、停牌新目标跳过；
  - 多次满仓调仓后现金始终 ≥ 0（不透支）。
"""
import numpy as np
import pytest

from src.execution.paper_trading import (
    BUY_RATE,
    INITIAL_CASH,
    LOT_SHARES,
    SELL_RATE,
    PaperAccount,
)


def _row(prices):
    """{code: price} → 可被 .get(code, nan) 取值的 Series（缺票即停牌）。"""
    return __import__("pandas").Series(prices, dtype=float)


@pytest.fixture()
def acct(tmp_path):
    return PaperAccount(tmp_path / "account.json")


def test_initial_account(acct):
    assert acct.state["cash"] == INITIAL_CASH
    assert acct.state["holdings"] == {}
    assert acct.state["pending_target"] == []


def test_lot_shares_rounds_down_to_lot(acct):
    # 预算 10000 元、价 33 元：10000/33/100=3.03 手 → 300 股
    assert acct._lot_shares(33.0, 10_000.0) == 300
    # 预算不足一手 → 0 股
    assert acct._lot_shares(200.0, 1_000.0) == 0


def test_first_buy_no_overdraft(acct):
    targets = [f"sh.60000{i}" for i in range(15)]
    prices = {c: 10.0 for c in targets}
    acct.set_pending(targets, "2026-09-21")
    acct.execute_pending(_row(prices))
    assert acct.state["cash"] >= 0
    assert len(acct.state["holdings"]) == 15
    assert all(sh % LOT_SHARES == 0 for sh in acct.state["holdings"].values())


def test_sell_fee_charged_once(acct):
    """单只调仓：卖出额 10000，卖出费 13.5 只扣一次；期末现金可手算。"""
    old, new = "sh.600000", "sh.600001"
    acct.state["cash"] = 0.0
    acct.state["holdings"] = {old: 1000}
    acct.set_pending([new], "2026-09-30")
    prices = _row({old: 10.0, new: 10.0})
    acct.execute_pending(prices)

    available = 1000 * 10.0 * (1 - SELL_RATE)          # 9986.5
    budget = available / (1 + BUY_RATE)                # 成交额预算
    expect_sh = int(budget / 10.0 / LOT_SHARES) * LOT_SHARES  # 900 股
    assert acct.state["holdings"][new] == expect_sh
    expect_cash = available - expect_sh * 10.0 * (1 + BUY_RATE)
    assert acct.state["cash"] == pytest.approx(expect_cash, abs=1e-6)
    assert acct.state["cash"] > 0


def test_rebalance_full_position_no_overdraft(acct):
    """I19 核心：满仓 15 只 → 月末全换 15 只，现金不允许为负。"""
    old = [f"sh.60000{i}" for i in range(15)]
    new = [f"sz.00000{i}" for i in range(15)]
    # 首次满仓
    acct.set_pending(old, "2026-09-21")
    acct.execute_pending(_row({c: 10.0 for c in old}))
    cash_after_first = acct.state["cash"]
    assert cash_after_first >= 0
    # 月末换仓（价格各异，制造满仓后调仓）
    acct.set_pending(new, "2026-09-30")
    prices = {c: 10.0 + 0.05 * i for i, c in enumerate(old)}
    prices.update({c: 11.0 - 0.03 * i for i, c in enumerate(new)})
    acct.execute_pending(_row(prices))
    assert acct.state["cash"] >= 0, "满仓调仓后现金不得为负"
    assert set(acct.state["holdings"]).issubset(set(new))


def test_repeated_rebalances_never_overdraft(acct):
    """连续 20 轮满仓调仓、随机价格，现金始终非负。"""
    rng = np.random.default_rng(42)
    universe = [f"sh.60{ i:03d}" for i in range(60)]
    cur = universe[:15]
    acct.set_pending(cur, "2026-09-21")
    acct.execute_pending(_row({c: float(rng.uniform(3, 30)) for c in cur}))
    for r in range(20):
        nxt = universe[15 + (r % 3) * 15: 15 + (r % 3) * 15 + 15]
        acct.set_pending(nxt, f"2026-10-{r+1:02d}")
        prices = {c: float(rng.uniform(3, 30)) for c in set(cur) | set(nxt)}
        acct.execute_pending(_row(prices))
        assert acct.state["cash"] >= 0, f"第 {r} 轮调仓现金为负 {acct.state['cash']}"
        cur = list(acct.state["holdings"])


def test_halted_old_holding_kept(acct):
    """旧仓停牌（开盘无价）卖不掉，必须保留、不能被新仓覆盖丢失。"""
    halted, liquid, new = "sh.600000", "sh.600001", "sh.600002"
    acct.state["cash"] = 0.0
    acct.state["holdings"] = {halted: 500, liquid: 500}
    acct.set_pending([new], "2026-09-30")
    # 只给 liquid、new 价格，halted 停牌
    acct.execute_pending(_row({liquid: 10.0, new: 10.0}))
    assert acct.state["holdings"].get(halted) == 500, "停牌旧仓必须保留"
    assert new in acct.state["holdings"]


def test_halted_target_skipped(acct):
    """新目标停牌买不到：跳过，不占预算，现金仍非负。"""
    targets = ["sh.600000", "sh.600001"]
    acct.set_pending(targets, "2026-09-21")
    acct.execute_pending(_row({"sh.600000": 10.0}))  # 600001 无价
    assert "sh.600000" in acct.state["holdings"]
    assert "sh.600001" not in acct.state["holdings"]
    assert acct.state["cash"] >= 0


def test_no_pending_is_noop(acct):
    assert acct.execute_pending(_row({"sh.600000": 10.0})) == []
    assert acct.state["cash"] == INITIAL_CASH


def test_pending_cleared_after_execution(acct):
    acct.set_pending(["sh.600000"], "2026-09-21")
    acct.execute_pending(_row({"sh.600000": 10.0}))
    assert acct.state["pending_target"] == []
    assert acct.state["pending_signal_date"] is None


def test_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "acct.json"
    a = PaperAccount(path)
    a.set_pending(["sh.600000"], "2026-09-21")
    a.save()
    b = PaperAccount(path)
    assert b.state["pending_target"] == ["sh.600000"]
    assert b.state["cash"] == INITIAL_CASH


def test_market_value_skips_halted(acct):
    acct.state["holdings"] = {"A": 100, "B": 100}
    # B 无行情（停牌），只计 A
    assert acct.market_value(_row({"A": 10.0})) == 1000.0


def test_buy_skips_unaffordable_lot(acct):
    # 可用现金 1000、2 只：万元高价股买不起一手，低价股正常成交
    bought, cash = acct._buy(["PRICEY", "CHEAP"],
                             _row({"PRICEY": 10000.0, "CHEAP": 1.0}), 1000.0)
    assert "PRICEY" not in bought
    assert "CHEAP" in bought and bought["CHEAP"] % LOT_SHARES == 0
    assert cash >= 0


def test_available_nonpositive_keeps_cash_and_halted(acct):
    """现金为负且旧仓全停牌（卖不掉）：不得买入，保留现金与停牌旧仓。"""
    acct.state["cash"] = -100.0
    acct.state["holdings"] = {"HALT": 100}
    acct.set_pending(["NEW"], "2026-09-30")
    acct.execute_pending(_row({"NEW": 10.0}))  # HALT 无开盘价
    assert acct.state["cash"] == -100.0
    assert acct.state["holdings"] == {"HALT": 100}
    assert "NEW" not in acct.state["holdings"]


def test_negative_cash_after_buy_raises(acct, monkeypatch):
    """现金硬约束：即便 _buy 异常返回负现金，也必须报错而不是静默透支。"""
    acct.set_pending(["A"], "2026-09-21")
    monkeypatch.setattr(acct, "_buy", lambda target, row, avail: ({"A": 100}, -50.0))
    with pytest.raises(RuntimeError, match="现金为负"):
        acct.execute_pending(_row({"A": 10.0}))
