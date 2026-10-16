"""data.py 加载层的行为刻画测试（#33 重构安全网）。

这些测试在重构前先固化 _read_table / 财务三函数 / 日线 / 基准 / 成分 /
面板 / month_end_dates 的现有行为，重构后必须全部保持通过（行为守恒）。
全部用 tmp_path 重定向路径常量，不依赖真实数据、不联网。
"""
import json

import numpy as np
import pandas as pd
import pytest

from src.quant import data as D


# ── 造数辅助 ──────────────────────────────────────────────────────────────
def _write_csv(path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=cols).to_csv(path, index=False)


def _daily_rows(n=260, code="sh.600000", start="2025-01-01"):
    """n 个交易日的日线（跳开周末，保证 >=250 行）。"""
    dates = pd.bdate_range(start, periods=n)
    return [
        {
            "date": d.strftime("%Y-%m-%d"),
            "code": code,
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "preclose": 10.4,
            "volume": 1000,
            "amount": 10500.0,
            "turn": 1.2,
            "tradestatus": 1,
            "pctChg": 0.5,
            "peTTM": 8.0,
            "pbMRQ": 1.1,
            "isST": 0,
        }
        for d in dates
    ]


DAILY_COLS = ["date", "code", "open", "high", "low", "close", "preclose",
              "volume", "amount", "turn", "tradestatus", "pctChg",
              "peTTM", "pbMRQ", "isST"]


# ── _read_table：Parquet 优先 / CSV 回退 / 缺失 None ──────────────────────
def test_read_table_prefers_parquet(monkeypatch, tmp_path):
    csv_p = tmp_path / "x.csv"
    pq_p = tmp_path / "x.parquet"
    pd.DataFrame({"v": [1]}).to_csv(csv_p, index=False)
    pd.DataFrame({"v": [99]}).to_parquet(pq_p)
    monkeypatch.setattr(D, "PREFER_PARQUET", True)
    assert D._read_table(csv_p)["v"].tolist() == [99]


def test_read_table_falls_back_to_csv(monkeypatch, tmp_path):
    csv_p = tmp_path / "x.csv"
    pd.DataFrame({"v": [1]}).to_csv(csv_p, index=False)
    monkeypatch.setattr(D, "PREFER_PARQUET", True)
    assert D._read_table(csv_p)["v"].tolist() == [1]


def test_read_table_csv_only_when_flag_off(monkeypatch, tmp_path):
    csv_p = tmp_path / "x.csv"
    pq_p = tmp_path / "x.parquet"
    pd.DataFrame({"v": [1]}).to_csv(csv_p, index=False)
    pd.DataFrame({"v": [99]}).to_parquet(pq_p)
    monkeypatch.setattr(D, "PREFER_PARQUET", False)
    assert D._read_table(csv_p)["v"].tolist() == [1]


def test_read_table_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "PREFER_PARQUET", True)
    assert D._read_table(tmp_path / "nope.csv") is None


# ── 财务三函数 ────────────────────────────────────────────────────────────
PROFIT_COLS = ["code", "pubDate", "statDate", "roeAvg", "netProfit"]
GROWTH_COLS = ["code", "pubDate", "statDate", "YOYNI"]
DIV_COLS = ["code", "dividOperateDate", "dividCashPsBeforeTax"]


def test_load_profit_parses_and_sorts(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "FUND_DIR", tmp_path)
    _write_csv(tmp_path / "profit" / "sh.600000.csv",
               [{"code": "sh.600000", "pubDate": "2020-04-01",
                 "statDate": "2020-03-31", "roeAvg": "0.10", "netProfit": 1},
                {"code": "sh.600000", "pubDate": "2019-04-01",
                 "statDate": "2019-03-31", "roeAvg": "0.09", "netProfit": 1}],
               PROFIT_COLS)
    df = D.load_profit("sh.600000")
    assert pd.api.types.is_datetime64_any_dtype(df["pubDate"])
    assert pd.api.types.is_datetime64_any_dtype(df["statDate"])
    assert pd.api.types.is_numeric_dtype(df["roeAvg"])
    # 按 pubDate 升序
    assert df["pubDate"].is_monotonic_increasing
    assert df["roeAvg"].tolist() == [0.09, 0.10]


def test_load_growth_parses_yoyni(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "FUND_DIR", tmp_path)
    _write_csv(tmp_path / "growth" / "sh.600000.csv",
               [{"code": "sh.600000", "pubDate": "2020-04-01",
                 "statDate": "2020-03-31", "YOYNI": "0.31"}],
               GROWTH_COLS)
    df = D.load_growth("sh.600000")
    assert pd.api.types.is_numeric_dtype(df["YOYNI"])
    assert df["YOYNI"].iloc[0] == pytest.approx(0.31)


def test_load_dividend_maps_cash_from_pretax(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "FUND_DIR", tmp_path)
    _write_csv(tmp_path / "dividend" / "sh.600000.csv",
               [{"code": "sh.600000", "dividOperateDate": "2020-06-26",
                 "dividCashPsBeforeTax": "0.30"}],
               DIV_COLS)
    df = D.load_dividend("sh.600000")
    assert pd.api.types.is_datetime64_any_dtype(df["dividOperateDate"])
    assert pd.api.types.is_numeric_dtype(df["cash"])
    assert df["cash"].iloc[0] == pytest.approx(0.30)
    # 原列保留
    assert "dividCashPsBeforeTax" in df.columns


@pytest.mark.parametrize("subdir,loader", [
    ("profit", D.load_profit),
    ("growth", D.load_growth),
    ("dividend", D.load_dividend),
])
def test_financial_loaders_missing_return_empty(monkeypatch, tmp_path,
                                                subdir, loader):
    monkeypatch.setattr(D, "FUND_DIR", tmp_path)
    assert loader("sh.999999").empty


# ── 基准 ──────────────────────────────────────────────────────────────────
def test_load_benchmark_returns_close_series(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "ROOT", tmp_path)
    p = tmp_path / "data/raw/etf/daily/sh/sh.510300.csv"
    _write_csv(p, [{"date": "2020-01-02", "close": "4.1"},
                   {"date": "2020-01-03", "close": "4.2"}],
               ["date", "close"])
    s = D.load_benchmark()
    assert isinstance(s, pd.Series)
    assert s.tolist() == [4.1, 4.2]
    assert s.index.name == "date"


def test_load_benchmark_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        D.load_benchmark()


# ── _read_daily：清洗 ─────────────────────────────────────────────────────
def test_read_daily_cleans_and_sorts(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "DAILY_DIR", tmp_path)
    _write_csv(tmp_path / "sh" / "sh.600000.csv",
               [{"date": "2020-01-03", "code": "sh.600000", "open": 10,
                 "high": 11, "low": 9, "close": 10.5, "preclose": 10.4,
                 "volume": 100, "turn": 1, "tradestatus": 1, "pctChg": 0.5,
                 "peTTM": 8, "pbMRQ": 1, "isST": 0},
                {"date": "2020-01-02", "code": "sh.600000", "open": 9.8,
                 "high": 10, "low": 9.5, "close": 10, "preclose": 9.9,
                 "volume": 90, "turn": 0.9, "tradestatus": 1, "pctChg": 0.2,
                 "peTTM": 8, "pbMRQ": 1, "isST": 0}],
               DAILY_COLS)
    df = D._read_daily("sh.600000")
    assert df["date"].is_monotonic_increasing
    assert pd.api.types.is_numeric_dtype(df["close"])
    assert pd.api.types.is_numeric_dtype(df["tradestatus"])
    assert pd.api.types.is_numeric_dtype(df["isST"])


def test_read_daily_nonpositive_price_to_nan(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "DAILY_DIR", tmp_path)
    _write_csv(tmp_path / "sh" / "sh.600000.csv",
               [{"date": "2020-01-02", "code": "sh.600000", "open": 0,
                 "high": 10, "low": 9, "close": -1, "preclose": 10,
                 "volume": 1, "turn": 1, "tradestatus": 1, "pctChg": 0,
                 "peTTM": 8, "pbMRQ": 1, "isST": 0}],
               DAILY_COLS)
    df = D._read_daily("sh.600000")
    assert np.isnan(df["open"].iloc[0])
    assert np.isnan(df["close"].iloc[0])


def test_read_daily_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "DAILY_DIR", tmp_path)
    assert D._read_daily("sh.999999") is None


# ── load_constituents ─────────────────────────────────────────────────────
def test_load_constituents_from_status_json(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "WORKSPACE", tmp_path)
    monkeypatch.setattr(D, "DAILY_DIR", tmp_path / "empty_daily")
    (tmp_path / "csi800_daily_status.json").write_text(json.dumps({
        "completed": ["sh.600000", "sz.000001"],
        "failed": {"sh.688001": "err"},
    }))
    codes = D.load_constituents()
    # status 分支：completed ∪ failed，原样（含 688，这是现有行为）
    assert set(codes) == {"sh.600000", "sz.000001", "sh.688001"}


def test_load_constituents_scan_dir_excludes_688(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "WORKSPACE", tmp_path / "no_status")
    daily = tmp_path / "daily"
    for mkt, stem in [("sh", "sh.600000"), ("sz", "sz.000001"),
                      ("sh", "sh.688001")]:
        _write_csv(daily / mkt / f"{stem}.csv",
                   [{"date": "2020-01-02"}], ["date"])
    monkeypatch.setattr(D, "DAILY_DIR", daily)
    codes = D.load_constituents()
    assert "sh.600000" in codes and "sz.000001" in codes
    assert all("688" not in c for c in codes)


# ── load_price_panel ──────────────────────────────────────────────────────
def test_load_price_panel_builds_and_skips_short(monkeypatch, tmp_path):
    daily = tmp_path / "daily"
    # 两只足量
    for mkt, code in [("sh", "sh.600000"), ("sz", "sz.000001")]:
        _write_csv(daily / mkt / f"{code}.csv", _daily_rows(260, code), DAILY_COLS)
    # 一只不足 250 行，应被跳过
    _write_csv(daily / "sh" / "sh.600001.csv",
               _daily_rows(10, "sh.600001"), DAILY_COLS)
    monkeypatch.setattr(D, "DAILY_DIR", daily)

    panels = D.load_price_panel(
        ["sh.600000", "sz.000001", "sh.600001"])
    assert set(panels["codes"]) == {"sh.600000", "sz.000001"}
    assert set(panels["close"].columns) == {"sh.600000", "sz.000001"}
    assert panels["close"].shape[0] == 260


# ── month_end_dates（回测批量口径，历史完整序列）──────────────────────────
def test_month_end_dates_picks_last_trade_day_per_month():
    dates = pd.DatetimeIndex([
        "2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02"])
    me = D.month_end_dates(dates, start="2020-01-01")
    assert pd.Timestamp("2020-01-31") in me
    assert pd.Timestamp("2020-02-28") in me
    assert pd.Timestamp("2020-03-02") in me  # 序列最后一天也作为该月月末
    assert pd.Timestamp("2020-01-30") not in me


def test_month_end_dates_respects_start():
    dates = pd.DatetimeIndex(["2019-12-31", "2020-01-31"])
    me = D.month_end_dates(dates, start="2020-01-01")
    assert pd.Timestamp("2019-12-31") not in me
    assert pd.Timestamp("2020-01-31") in me


# ── 边界：默认成分 / 缺关键字列 / 空 status 退化 / 极简日线 ─────────────────
def test_load_price_panel_default_codes(monkeypatch, tmp_path):
    daily = tmp_path / "daily"
    for mkt, code in [("sh", "sh.600000"), ("sz", "sz.000001")]:
        _write_csv(daily / mkt / f"{code}.csv", _daily_rows(260, code), DAILY_COLS)
    monkeypatch.setattr(D, "DAILY_DIR", daily)
    monkeypatch.setattr(D, "load_constituents",
                        lambda: ["sh.600000", "sz.000001"])
    panels = D.load_price_panel()
    assert set(panels["codes"]) == {"sh.600000", "sz.000001"}


def test_load_price_panel_excludes_missing_key_col(monkeypatch, tmp_path):
    daily = tmp_path / "daily"
    rows = _daily_rows(260, "sh.600000")
    cols = [c for c in DAILY_COLS if c != "close"]  # 缺关键字列 close
    _write_csv(daily / "sh" / "sh.600000.csv", rows, cols)
    monkeypatch.setattr(D, "DAILY_DIR", daily)
    panels = D.load_price_panel(["sh.600000"])
    assert "sh.600000" not in panels["codes"]


def test_load_constituents_empty_status_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "WORKSPACE", tmp_path)
    (tmp_path / "csi800_daily_status.json").write_text(
        json.dumps({"completed": [], "failed": {}}))
    daily = tmp_path / "daily"  # 只建 sh，不建 sz（覆盖目录不存在分支）
    _write_csv(daily / "sh" / "sh.600000.csv",
               [{"date": "2020-01-02"}], ["date"])
    monkeypatch.setattr(D, "DAILY_DIR", daily)
    assert "sh.600000" in D.load_constituents()


def test_read_daily_minimal_columns(monkeypatch, tmp_path):
    # 只有 date/close，缺所有可选列（覆盖防御性分支）
    monkeypatch.setattr(D, "DAILY_DIR", tmp_path)
    _write_csv(tmp_path / "sh" / "sh.600000.csv",
               [{"date": "2020-01-02", "close": 10.0},
                {"date": "2020-01-03", "close": 10.5}],
               ["date", "close"])
    df = D._read_daily("sh.600000")
    assert len(df) == 2
    assert df["close"].tolist() == [10.0, 10.5]


def test_load_price_panel_missing_optional_field_still_included(monkeypatch, tmp_path):
    # 足量但缺 tradestatus/isST（非关键字列）：仍纳入，且不建这两个空面板
    daily = tmp_path / "daily"
    cols = [c for c in DAILY_COLS if c not in ("tradestatus", "isST")]
    _write_csv(daily / "sh" / "sh.600000.csv", _daily_rows(260), cols)
    monkeypatch.setattr(D, "DAILY_DIR", daily)
    panels = D.load_price_panel(["sh.600000"])
    assert panels["codes"] == ["sh.600000"]
    assert "tradestatus" not in panels and "isST" not in panels
