# -*- coding: utf-8 -*-
"""reconcile 层：双源按板块名对齐、临界带分歧、汇总与落盘（纯观测）。"""
import csv
from datetime import datetime

import pytest

from src.monitor import reconcile as rc
from src.monitor.config import DIFF_DIR, CHG_L1


def _em(name="A", chg=2.6, zt=3, up=10, down=2):
    return {"name": name, "chg": chg, "zt": zt, "up": up, "down": down}


def _tx(name="A", chg=2.4, zt=3, up=9, down=3):
    return {"name": name, "chg": chg, "zt": zt, "up": up, "down": down}


@pytest.mark.parametrize("ec,tc,inside", [
    (2.0, 1.0, True), (3.0, 5.0, True), (1.9, 1.0, False),
    (3.1, 4.0, False), (1.0, 2.5, True),
])
def test_in_critical_band(ec, tc, inside):
    assert rc.in_critical_band(ec, tc) is inside


def test_reconcile_pair_aligned():
    got = rc.reconcile_pair(_em(chg=2.6, zt=3), {"A": _tx(chg=2.4, zt=2)}, "ts")
    assert got is not None
    row, ad, ztmm, band = got
    assert row[1] == "A" and row[2] == 2.6 and row[3] == 2.4
    assert round(ad, 3) == 0.2 and ztmm is True  # 涨停家数不一致
    assert band is True  # 2.6 过线、2.4 不过线 -> 临界带跨阈值分歧


def test_reconcile_pair_same_side_no_cross():
    got = rc.reconcile_pair(_em(chg=2.6), {"A": _tx(chg=2.6)}, "ts")
    assert got[3] is False


def test_reconcile_pair_outside_band_flag_none():
    got = rc.reconcile_pair(_em(chg=1.0), {"A": _tx(chg=1.0)}, "ts")
    assert got[3] is None and got[1] == 0.0


def test_reconcile_pair_missing_or_none():
    assert rc.reconcile_pair(_em(), {}, "ts") is None
    assert rc.reconcile_pair(_em(chg=None), {"A": _tx()}, "ts") is None
    assert rc.reconcile_pair(_em(), {"A": _tx(chg=None)}, "ts") is None


def test_collect_rows_and_summary():
    em = [_em("A", 2.6, 3), _em("B", 1.0, 0), _em("C", 4.0, 6)]
    tx = {"A": _tx("A", 2.4, 3), "C": _tx("C", 4.2, 6)}  # B 无法对齐
    rows, abs_diffs, ztmm, band = rc.collect_reconcile_rows(em, tx, "ts")
    assert len(rows) == 2
    assert ztmm == 0
    summary = rc.reconcile_summary(sorted(abs_diffs), ztmm, band)
    assert "配对 2" in summary and "临界带跨阈值分歧" in summary


def test_reconcile_sources_writes_csv(monkeypatch):
    now = datetime(2026, 10, 12, 10, 0)
    tx_list = [_tx("A", 2.4, 3)]
    monkeypatch.setattr(rc, "tx_spot", lambda: "DF")
    monkeypatch.setattr(rc, "tx_board_agg", lambda smap, df: tx_list)
    rc.reconcile_sources(now, [_em("A", 2.6, 3)], {"x": []})
    f = DIFF_DIR / "2026-10-12.csv"
    assert f.exists()
    rows = list(csv.reader(open(f, encoding="utf-8-sig")))
    assert rows[0][1] == "板块名" and rows[1][1] == "A"


def test_reconcile_sources_empty_agg(monkeypatch):
    monkeypatch.setattr(rc, "tx_spot", lambda: "DF")
    monkeypatch.setattr(rc, "tx_board_agg", lambda smap, df: [])
    rc.reconcile_sources(datetime(2026, 10, 12), [_em()], {})  # 不抛、不落盘


def test_reconcile_sources_swallows_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(rc, "tx_spot", boom)
    rc.reconcile_sources(datetime(2026, 10, 12), [_em()], {})  # 异常被吞，不影响主流程


# ---------------- 变异边界加固 ----------------
def test_critical_band_tc_side_boundaries():
    assert rc.in_critical_band(1.0, 2.0) is True   # ec 出带、tc 恰下界（杀 tc 下界 <= -> <）
    assert rc.in_critical_band(5.0, 3.0) is True   # ec 出带、tc 恰上界（杀 tc 上界 <= -> <）


def test_pair_band_threshold_equality_each_side():
    g1 = rc.reconcile_pair(_em(chg=2.5, zt=5), {"A": _tx(chg=2.6, zt=5)}, "ts")
    assert g1[3] is True   # ec 恰 2.5 不算过、tc 过 -> 分歧（杀 ec 的 > -> >=）
    g2 = rc.reconcile_pair(_em(chg=2.6, zt=5), {"A": _tx(chg=2.5, zt=5)}, "ts")
    assert g2[3] is True   # tc 恰 2.5（杀 tc 的 > -> >=）


def test_pair_missing_zt_defaults_zero():
    e = {"name": "A", "chg": 2.6, "up": 1, "down": 1}
    t = {"A": {"name": "A", "chg": 2.4, "up": 1, "down": 1}}
    row, ad, ztmm, band = rc.reconcile_pair(e, t, "ts")
    assert row[5] == 0 and row[6] == 0 and ztmm is False   # 缺 zt 键按 0（杀 or 0 的 0->1）


def test_summary_exact_counts_n20():
    diffs = [0.1] * 18 + [0.3, 0.5]      # 已排序，n=20
    band = [True, False] * 10
    s = rc.reconcile_summary(diffs, 1, band)
    # med=d[10]=0.10；P90 正常索引 int(18)=18 -> d[18]=0.30（杀 n*0.9 -> n/0.9）
    # |差|<=0.3 共 19 个（含恰好 0.3，杀 <= -> <）= 95%（杀 / -> *）；cross=10/20（杀 1->0）
    assert "P90 0.30" in s and "0.3pct 占 95%" in s
    assert "分歧 10/20" in s and "最大 0.50" in s
