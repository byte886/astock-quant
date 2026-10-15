# -*- coding: utf-8 -*-
"""monitor 编排层：取数降级链、涨停归集、scan_once、单板块状态流端到端。"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.monitor import monitor as M
from src.monitor.monitor import Monitor


@pytest.fixture()
def mon(monkeypatch, no_push):
    m = Monitor()
    monkeypatch.setattr(M, "reconcile_sources", lambda *a, **k: None)
    return m


def bk(name="A", code="BK1", chg=3.0, zt=5, lb=2.0, speed=None, up=80, down=10):
    return {"name": name, "code": code, "chg": chg, "zt": zt, "lb": lb,
            "speed": speed, "up": up, "down": down}


# ---------------- 涨停归集 ----------------
def test_build_zt_index():
    zt = [{"code": "sh600001", "industry": "银行"},
          {"code": "sz300001", "industry": "电子"},
          {"code": "sz300002", "industry": "电子"}]
    by_ind, codes, n = Monitor._build_zt_index(zt)
    assert by_ind == {"银行": 1, "电子": 2} and codes == {"sh600001", "sz300001", "sz300002"}
    assert n == 3


def test_build_concept_zt():
    smap = {"sz300001": ["concept:AI", "industry:电子"],
            "sz300002": ["concept:AI"]}
    out = Monitor._build_concept_zt({"sz300001", "sz300002"}, smap)
    assert out == {"AI": 2}


def test_fill_zt():
    boards = [bk(name="银行", zt=None), bk(name="AI", zt=7)]
    Monitor._fill_zt(boards, {"银行": 3}, {"AI": 2})
    assert boards[0]["zt"] == 3   # 回填行业
    assert boards[1]["zt"] == 7   # 已有值不改


# ---------------- 取数降级链 ----------------
def test_fetch_boards_eastmoney(mon, monkeypatch):
    monkeypatch.setattr(M, "em_boards", lambda kind: [bk()] if kind == "concept" else [])
    boards, source, _spot = mon._fetch_boards("20261012")
    assert source == "eastmoney" and boards


def test_fetch_boards_tencent_fallback(mon, monkeypatch):
    monkeypatch.setattr(M, "em_boards", lambda kind: None)
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    monkeypatch.setattr(M, "tx_board_agg", lambda smap, df: [bk()])
    boards, source, _spot = mon._fetch_boards("20261012")
    assert source == "tencent_agg" and boards


def test_fetch_boards_tencent_map_raises_still_agg(mon, monkeypatch):
    monkeypatch.setattr(M, "em_boards", lambda kind: None)
    def _raise(d):
        raise RuntimeError("map")
    monkeypatch.setattr(M, "build_sector_map", _raise)
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    monkeypatch.setattr(M, "tx_board_agg", lambda smap, df: [bk()])
    _, source, _spot = mon._fetch_boards("20261012")
    assert source == "tencent_agg"


def test_fetch_boards_sina_fallback(mon, monkeypatch):
    monkeypatch.setattr(M, "em_boards", lambda kind: None)
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    monkeypatch.setattr(M, "tx_board_agg", lambda smap, df: None)
    monkeypatch.setattr(M, "sina_boards",
                        lambda ind: [{"name": "S", "chg": 1.0, "amount": 1}] if ind == "概念" else [])
    boards, source, _spot = mon._fetch_boards("20261012")
    assert source == "sina" and boards[0]["name"] == "S"


def test_maybe_reconcile_throttled(mon, monkeypatch):
    calls = []
    monkeypatch.setattr(M, "reconcile_sources",
                        lambda now, b, sm: calls.append(now))
    now = datetime(2026, 10, 12, 10, 0)
    mon._maybe_reconcile(now, "tencent_agg", [], {})  # 非东财不调
    assert calls == []
    mon._maybe_reconcile(now, "eastmoney", [], {})
    mon._maybe_reconcile(now + timedelta(minutes=10), "eastmoney", [], {})  # 30min 内
    assert len(calls) == 1


# ---------------- scan_once ----------------
def test_scan_once_emits_and_persists(mon, monkeypatch, no_push):
    monkeypatch.setattr(M, "em_boards",
                        lambda kind: [bk("A", "BK1", 3.0, 5, 2.0, up=80, down=10),
                                      bk("C", "BK3", 0.5, 0, None, up=1, down=5)]
                        if kind == "concept" else [])
    monkeypatch.setattr(M, "zt_pool", lambda d: [])
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    source = mon.scan_once()
    assert source == "eastmoney"
    assert len(no_push) == 1  # 仅 A 达 L2 推送，C 不推
    assert "兜底" not in no_push[0][1]  # 主源 eastmoney 不带兜底口径标注
    assert mon.states["BK1"]["fire_count"] == 1


def test_scan_once_alert_includes_stock_detail(mon, monkeypatch, no_push):
    # 需求一 #37：板块达 L2 推送时，消息须点出带动个股（涨停封板在前、未涨停领涨补齐）
    monkeypatch.setattr(M, "em_boards",
                        lambda kind: [bk("元件", "BK1", 3.0, 5, 2.0, up=80, down=10)]
                        if kind == "concept" else [])
    monkeypatch.setattr(M, "zt_pool", lambda d: [
        {"code": "sz300919", "name": "中伟股份", "lianban": 3, "industry": "元件"},
        {"code": "sh600183", "name": "生益科技", "lianban": 2, "industry": "元件"},
        {"code": "sz002815", "name": "崇达技术", "lianban": 1, "industry": "元件"},
        {"code": "sh600000", "name": "浦发银行", "lianban": 5, "industry": "银行"},
    ])
    monkeypatch.setattr(M, "build_sector_map", lambda d: {
        "sh600183": ["industry:元件"],
        "sh600584": ["industry:元件"],
        "sz002463": ["industry:元件"],
        "sh600000": ["industry:银行"],
    })
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame([
        {"code": "sh600584", "name": "长电科技", "zdf": 7.2},
        {"code": "sz002463", "name": "沪电股份", "zdf": 6.5},
        {"code": "sh600000", "name": "浦发银行", "zdf": 9.0},   # 非本板块，不得混入
    ]))
    assert mon.scan_once() == "eastmoney"
    assert len(no_push) == 1
    msg = no_push[0][1]
    assert "封板：中伟股份(3连板)、生益科技(2连板)、崇达技术" in msg
    assert "领涨：长电科技 +7.2%、沪电股份 +6.5%" in msg
    assert "浦发银行" not in msg and "该板块共5只涨停" in msg


def test_scan_once_all_sources_empty(mon, monkeypatch, no_push):
    monkeypatch.setattr(M, "em_boards", lambda kind: None)
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    monkeypatch.setattr(M, "tx_board_agg", lambda smap, df: None)
    monkeypatch.setattr(M, "sina_boards", lambda ind: [])
    monkeypatch.setattr(M, "zt_pool", lambda d: [])
    assert mon.scan_once() is None
    assert no_push == []


def test_scan_once_tencent_fallback_message_caveat(mon, monkeypatch, no_push):
    # 东财不可用、腾讯聚合兜底且板块达 L2：source=tencent_agg，推送须带口径标注
    monkeypatch.setattr(M, "em_boards", lambda kind: None)
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    monkeypatch.setattr(M, "tx_board_agg",
                        lambda smap, df: [bk("TX", "BK9", 3.0, 5, 2.0,
                                             up=80, down=10)])
    monkeypatch.setattr(M, "sina_boards", lambda ind: [])
    monkeypatch.setattr(M, "zt_pool", lambda d: [])
    assert mon.scan_once() == "tencent_agg"
    assert len(no_push) == 1
    assert "腾讯个股聚合兜底" in no_push[0][1]


# ---------------- 单板块完整状态流（行为守恒核心） ----------------
def test_board_wave_lifecycle(mon, no_push):
    t0 = datetime(2026, 10, 12, 10, 0)

    def ev(chg, zt, now, lb=None, up=None, down=None, speed=None):
        # 用独立板块 BKW，避免与其它用例落盘的当日状态串扰
        mon._eval_board(bk(name="W", code="BKW", chg=chg, zt=zt, lb=lb,
                           speed=speed, up=up, down=down),
                        [], {}, {}, 10, False, now, "eastmoney", {}, None, set())

    ev(2.6, 3, t0)                                   # R1 首次 L1
    ev(2.7, 3, t0 + timedelta(minutes=2), lb=1.6)    # R2 同波无台阶 -> 静默
    ev(3.0, 5, t0 + timedelta(minutes=4), lb=2.0, up=80, down=10)  # R3 升级 L2
    ev(1.0, 0, t0 + timedelta(minutes=6))            # R4 掉标，进入消退宽限
    ev(3.0, 5, t0 + timedelta(minutes=8), lb=2.0, up=80, down=10)  # R5 宽限内回来 -> 静默
    # 仍在第1波：L1/L2 已推标记都应保留（每波重置，故须在第2波 begin 前断言）
    assert mon.states["BKW"]["fired_l1"] is True   # R1 为 L1（杀 level == -> !=、fired_l1 True->False）
    assert mon.states["BKW"]["fired_l2"] is True   # R3 升级 L2（杀 fired_l2 True->False）
    ev(1.0, 0, t0 + timedelta(minutes=20))           # R6 再次掉标，记 lost
    ev(2.6, 3, t0 + timedelta(minutes=31))           # R7 超宽限后再度放量 -> 第2波

    assert len(no_push) == 3
    assert "首次启动" in no_push[0][0]
    assert "升级 L1→L2" in no_push[1][0] and "升级 L1→L2" in no_push[1][1]
    assert "再度启动·第2波" in no_push[2][0]
    assert mon.states["BKW"]["wave"] == 2
    assert mon.states["BKW"]["fire_count"] == 3


def test_hold_hint_once(mon):
    mon.holdings = {}
    tags, hint = mon._hold_hint()
    assert tags == [] and "未配置持仓" in hint and mon.hint_shown is True
    _, hint2 = mon._hold_hint()
    assert hint2 == ""  # 只提示一次
    mon.holdings = {"sh600519": "贵州茅台"}
    tags3, _ = mon._hold_hint()
    assert tags3 == ["持仓1"]


# ---------------- 变异边界加固 ----------------
def test_sina_fallback_merges_both_indicators(mon, monkeypatch):
    monkeypatch.setattr(M, "sina_boards",
                        lambda ind: [{"name": "C", "chg": 1.0, "amount": 1}] if ind == "概念"
                        else [{"name": "I", "chg": 0.5, "amount": 1}])
    boards, src = Monitor._sina_fallback_boards()
    assert src == "sina" and {b["name"] for b in boards} == {"C", "I"}  # 两类并入（杀 +=）


def test_fill_zt_industry_zero_uses_concept():
    boards = [bk(name="AI", zt=None)]
    Monitor._fill_zt(boards, {}, {"AI": 2})     # 行业计数 0、概念 2
    assert boards[0]["zt"] == 2                 # 取概念侧（杀 or -> and 变 0）


def test_scan_once_zt_pool_none_treated_empty(mon, monkeypatch, no_push):
    monkeypatch.setattr(M, "em_boards",
                        lambda kind: [bk("A", "BK1", 3.0, 5, 2.0, up=80, down=10)]
                        if kind == "concept" else [])
    monkeypatch.setattr(M, "zt_pool", lambda d: None)     # None 必须按空处理（杀 or -> and）
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    assert mon.scan_once() == "eastmoney"


def test_scan_once_late_flag_boundary(mon, monkeypatch):
    monkeypatch.setattr(M, "em_boards",
                        lambda kind: [bk("A", "BK1", 0.1, 0, up=1, down=1)]
                        if kind == "concept" else [])
    monkeypatch.setattr(M, "zt_pool", lambda d: [])
    monkeypatch.setattr(M, "build_sector_map", lambda d: {})
    monkeypatch.setattr(M, "tx_spot", lambda: pd.DataFrame())
    seen = {}
    monkeypatch.setattr(M.sm, "classify_signal",
                        lambda b, zi, zc, late: (seen.__setitem__("late", late), None)[1])

    class _DT30(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 12, 14, 30)    # 恰 14:30
    monkeypatch.setattr(M, "datetime", _DT30)
    mon.scan_once()
    assert seen["late"] is True                 # 杀 late 的 >= -> >

    class _DT29(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 12, 14, 29)
    monkeypatch.setattr(M, "datetime", _DT29)
    mon.scan_once()
    assert seen["late"] is False


def test_fill_zt_missing_both_sides_is_zero():
    boards = [bk(name="X", zt=None)]
    Monitor._fill_zt(boards, {}, {})          # 行业/概念都无 X
    assert boards[0]["zt"] == 0               # 两个 get 默认 0（杀概念侧默认 0 -> 1）
