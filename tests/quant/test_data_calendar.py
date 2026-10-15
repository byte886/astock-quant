# -*- coding: utf-8 -*-
"""data.py 交易日历与月末判定测试（确定性，不依赖网络）。

回归重点：数据面板最后一天（数据截断，如 10-15）不得被误判为月末——
旧逻辑用"下一交易日越界即月末"，会在 10-15 发出假信号、导致次日误换仓。
"""
import pandas as pd

from src.quant import data as D

# 假日历：2026 年 9 月（9-29/30，月末 30）、10 月（8/15/16/19/30，月末 30）
FAKE_CAL = pd.DatetimeIndex([
    "2026-09-29", "2026-09-30",
    "2026-10-08", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-30",
    "2026-11-02",
])


def test_real_calendar_month_end():
    assert D.is_month_end_trade_date("2026-09-30", FAKE_CAL) is True
    assert D.is_month_end_trade_date("2026-10-30", FAKE_CAL) is True


def test_real_calendar_non_month_end():
    # 10-15 之后同 10 月还有 10-30，不是月末（本次 bug 核心）
    assert D.is_month_end_trade_date("2026-10-15", FAKE_CAL) is False
    assert D.is_month_end_trade_date("2026-10-16", FAKE_CAL) is False
    assert D.is_month_end_trade_date("2026-09-29", FAKE_CAL) is False


def test_fallback_cross_month_next_session():
    # 无日历：all_dates 中下一交易日跨月 → 月末
    dates = pd.DatetimeIndex(["2026-09-29", "2026-09-30", "2026-10-08"])
    assert D.is_month_end_trade_date("2026-09-30", None, dates) is True
    assert D.is_month_end_trade_date("2026-09-29", None, dates) is False


def test_fallback_data_edge_is_conservative_false():
    # 无日历 + d 是面板最后一天（数据截断、无下一交易日）：保守 False，不发假信号
    dates = pd.DatetimeIndex(["2026-10-08", "2026-10-15"])
    assert D.is_month_end_trade_date("2026-10-15", None, dates) is False


def test_no_calendar_no_dates_returns_false():
    assert D.is_month_end_trade_date("2026-10-15", None, None) is False


def test_calendar_date_not_in_calendar_returns_false():
    # 日历里没有该月任何交易日 → False（不臆断）
    assert D.is_month_end_trade_date("2020-01-31", FAKE_CAL) is False


def test_load_trade_calendar_uses_cache_when_fresh(monkeypatch, tmp_path):
    cache = tmp_path / "trade_calendar.csv"
    future = pd.Timestamp.now() + pd.Timedelta(days=200)
    pd.DataFrame({"trade_date": pd.to_datetime(["2026-10-15", future])}).to_csv(
        cache, index=False)
    monkeypatch.setattr(D, "CAL_CACHE", cache)

    def _boom(*a, **k):
        raise AssertionError("缓存足够新时不应联网刷新")

    monkeypatch.setattr("akshare.tool_trade_date_hist_sina", _boom, raising=False)
    cal = D.load_trade_calendar()
    assert cal is not None and cal.max() >= future


def test_load_trade_calendar_fallback_to_cache_on_network_error(monkeypatch, tmp_path):
    cache = tmp_path / "trade_calendar.csv"
    pd.DataFrame({"trade_date": pd.to_datetime(["2026-10-15"])}).to_csv(
        cache, index=False)
    monkeypatch.setattr(D, "CAL_CACHE", cache)

    import builtins
    _real_import = builtins.__import__

    def _import_fail(name, *a, **k):
        if name == "akshare":
            raise ImportError("simulated offline")
        return _real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _import_fail)
    # 缓存偏旧（覆盖不足）且联网失败：回退到偏旧缓存而不是抛异常
    cal = D.load_trade_calendar()
    assert cal is not None
    assert pd.Timestamp("2026-10-15") in cal


def _fake_akshare(monkeypatch, frame):
    import sys
    import types
    fake = types.SimpleNamespace(
        tool_trade_date_hist_sina=lambda: frame)
    monkeypatch.setitem(sys.modules, "akshare", fake)


def test_load_trade_calendar_refreshes_and_persists(monkeypatch, tmp_path):
    # 无缓存 + akshare 成功：联网刷新并落盘（覆盖成功分支）
    cache = tmp_path / "trade_calendar.csv"
    monkeypatch.setattr(D, "CAL_CACHE", cache)
    frame = pd.DataFrame({"trade_date": pd.to_datetime(
        ["2026-10-15", "2026-10-30", "2026-12-31"])})
    _fake_akshare(monkeypatch, frame)

    cal = D.load_trade_calendar()
    assert pd.Timestamp("2026-12-31") in cal
    assert cache.exists()  # 已落盘


def test_load_trade_calendar_corrupt_cache_refreshes(monkeypatch, tmp_path):
    # 缓存损坏（读 CSV 成功但缺 trade_date 列 / 内容非法）→ 回退 None 后刷新
    cache = tmp_path / "trade_calendar.csv"
    cache.write_text("foo,bar\n1,2\n", encoding="utf-8")
    monkeypatch.setattr(D, "CAL_CACHE", cache)
    frame = pd.DataFrame({"trade_date": pd.to_datetime(["2026-10-30"])})
    _fake_akshare(monkeypatch, frame)

    cal = D.load_trade_calendar()
    assert cal is not None and pd.Timestamp("2026-10-30") in cal


def test_fallback_d_not_in_dates_returns_false():
    # 无日历、all_dates 非空但 d 不在其中 → 不臆断，False
    dates = pd.DatetimeIndex(["2026-10-14"])
    assert D.is_month_end_trade_date("2026-10-15", None, dates) is False


def test_load_trade_calendar_empty_cache_refreshes(monkeypatch, tmp_path):
    # 缓存存在但为空日历（只有表头）→ 视为无缓存，联网刷新
    cache = tmp_path / "trade_calendar.csv"
    pd.DataFrame({"trade_date": pd.to_datetime([])}).to_csv(cache, index=False)
    monkeypatch.setattr(D, "CAL_CACHE", cache)
    frame = pd.DataFrame({"trade_date": pd.to_datetime(["2026-10-30"])})
    _fake_akshare(monkeypatch, frame)

    cal = D.load_trade_calendar()
    assert cal is not None and pd.Timestamp("2026-10-30") in cal
