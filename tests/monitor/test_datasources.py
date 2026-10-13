# -*- coding: utf-8 -*-
"""datasources 层：四路取数、板块映射缓存/兜底、交易日历（全部 mock 网络）。"""
import json
import types
from datetime import datetime
from datetime import time as dtime

import pandas as pd
import pytest

from src.monitor import datasources as DS
from src.monitor import config


# ---------------- 东财 push2 ----------------
def _em_json():
    return json.dumps({"data": {"diff": [
        {"f2": 10, "f3": 2.6, "f6": 1e9, "f10": 1.5, "f12": "BK1",
         "f14": "A", "f22": 1.0, "f104": 8, "f105": 2},
        {"f2": 5, "f3": "-", "f6": "-", "f10": "-", "f12": "BK2",
         "f14": "B", "f22": "-", "f104": "-", "f105": "-"},
    ]}})


def test_em_boards_ok(monkeypatch):
    monkeypatch.setattr(DS.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=_em_json()))
    rows = DS.em_boards("concept")
    assert len(rows) == 2
    assert rows[0]["code"] == "BK1" and rows[0]["name"] == "A"
    assert rows[0]["chg"] == 2.6 and rows[0]["lb"] == 1.5
    assert rows[1]["chg"] is None  # "-" 安全转 None


def test_em_boards_fail(monkeypatch):
    monkeypatch.setattr(DS.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="garbage"))
    assert DS.em_boards("concept") is None


# ---------------- 腾讯 / 涨停池 / 新浪 ----------------
def test_tx_spot_ok_and_fail(fake_akshare):
    fake_akshare.stock_zh_a_spot_tx = lambda: pd.DataFrame({"code": ["sh600001"]})
    assert DS.tx_spot() is not None

    def boom():
        raise RuntimeError("x")
    fake_akshare.stock_zh_a_spot_tx = boom
    assert DS.tx_spot() is None


def test_zt_pool_ok_and_fail(fake_akshare):
    fake_akshare.stock_zt_pool_em = lambda date: pd.DataFrame(
        {"代码": ["600001", "300001"], "名称": ["甲", "乙"],
         "连板数": [1, 2], "所属行业": ["银行", "电子"]})
    out = DS.zt_pool("20261012")
    assert out[0]["code"] == "sh600001" and out[1]["code"] == "sz300001"
    assert out[1]["lianban"] == 2 and out[0]["industry"] == "银行"
    fake_akshare.stock_zt_pool_em = lambda date: (_ for _ in ()).throw(RuntimeError("x"))
    assert DS.zt_pool("20261012") is None


def test_sina_boards_ok_and_fail(fake_akshare):
    fake_akshare.stock_sector_spot = lambda indicator: pd.DataFrame(
        {"板块": ["A"], "涨跌幅": [2.0], "总成交额": [1e8]})
    rows = DS.sina_boards("概念")
    assert rows[0]["name"] == "A" and rows[0]["chg"] == 2.0
    fake_akshare.stock_sector_spot = lambda indicator: (_ for _ in ()).throw(RuntimeError("x"))
    assert DS.sina_boards("概念") is None


# ---------------- 板块成分映射 ----------------
def test_build_sector_map_uses_cache(fake_akshare, monkeypatch):
    cache = config.WORKSPACE / "sector_map_20261012.json"
    cache.write_text(json.dumps({"sh600001": ["industry:银行"]}), encoding="utf-8")
    called = {"n": 0}
    fake_akshare.stock_board_industry_name_em = lambda: called.__setitem__("n", 1)
    out = DS.build_sector_map("20261012")
    assert out == {"sh600001": ["industry:银行"]} and called["n"] == 0


def test_build_sector_map_em_full(fake_akshare):
    codes = [f"60{str(i).zfill(4)}" for i in range(1, 121)]
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame(
        {"板块名称": ["银行"]})
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame(
        {"板块名称": []})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame(
        {"代码": codes})
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame(
        {"代码": []})
    out = DS.build_sector_map("20261013", background=False)
    assert len(out) == 120 and out["sh600001"] == ["industry:银行"]
    assert (config.WORKSPACE / "sector_map_20261013.json").exists()


def test_build_sector_map_falls_back_sina(fake_akshare, monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("em blocked")
    fake_akshare.stock_board_industry_name_em = _raise
    fake_akshare.stock_board_concept_name_em = _raise
    fake_akshare.stock_board_industry_cons_em = _raise
    fake_akshare.stock_board_concept_cons_em = _raise
    fake_akshare.stock_sector_spot = lambda indicator: pd.DataFrame(
        {"label": ["new_abc"], "板块": ["元宇宙"]})
    fake_akshare.stock_sector_detail = lambda sector: pd.DataFrame(
        {"code": ["300001", "688001"]})
    out = DS.build_sector_map("20261014", background=False)
    assert "sz300001" in out and "sh688001" in out


# ---------------- 交易日历 / 交易时段 ----------------
def test_load_trade_dates_cache_fresh():
    config.TRADE_CAL.write_text(json.dumps(["2026-10-12"]), encoding="utf-8")
    assert DS.load_trade_dates() == {"2026-10-12"}


def test_load_trade_dates_fetch_and_fail(fake_akshare):
    config.TRADE_CAL.unlink(missing_ok=True)
    fake_akshare.tool_trade_date_hist_sina = lambda: pd.DataFrame(
        {"trade_date": ["2026-10-12", "2026-10-12", "2026-10-13"]})
    assert DS.load_trade_dates() == {"2026-10-12", "2026-10-13"}
    config.TRADE_CAL.unlink(missing_ok=True)
    fake_akshare.tool_trade_date_hist_sina = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert DS.load_trade_dates() is None


def test_is_trading_time(monkeypatch):
    # 周末
    sat = datetime(2026, 10, 10, 10, 0)
    assert DS.is_trading_time(sat) is False
    # 工作日盘中，日历含当天
    mon = datetime(2026, 10, 12, 10, 0)
    monkeypatch.setattr(DS, "load_trade_dates", lambda: {"2026-10-12"})
    assert DS.is_trading_time(mon) is True
    # 午间休市
    assert DS.is_trading_time(datetime(2026, 10, 12, 11, 45)) is False
    # 节假日（日历不含）
    assert DS.is_trading_time(datetime(2026, 10, 1, 10, 0)) is False
    # 日历拉取失败降级：非周末盘中时间按 True
    monkeypatch.setattr(DS, "load_trade_dates", lambda: None)
    assert DS.is_trading_time(mon) is True


# ---------------- 变异边界加固 ----------------
def test_em_boards_fs_param_by_kind(monkeypatch):
    seen = []
    def _cap(*a, **k):
        seen.append(a[0])
        return types.SimpleNamespace(stdout=_em_json())
    monkeypatch.setattr(DS.subprocess, "run", _cap)
    DS.em_boards("concept")
    DS.em_boards("industry")
    assert "fs=m:90+t:3" in seen[0][-1]   # 概念 t:3（杀 kind == -> !=）
    assert "fs=m:90+t:2" in seen[1][-1]   # 行业 t:2


def test_sina_collect_skips_half_blank_and_uses_first_col(fake_akshare):
    fake_akshare.stock_sector_spot = lambda indicator: pd.DataFrame(
        {"label": ["a", "new_x"], "板块": [None, "元宇宙"]})     # 首行有 label 无板块名 -> 跳过
    fake_akshare.stock_sector_detail = lambda sector: pd.DataFrame(
        {"证券代码": ["300001"]})                                 # 无 code 列 -> 取首列
    c2b = DS._sina_sector_map()
    assert "sz300001" in c2b
    assert all("None" not in tag for tags in c2b.values() for tag in tags)  # 杀 or -> and、columns[0] 0->1


def test_em_names_and_cons_fallback_second_column(fake_akshare):
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame(
        {"x": [1], "行业名": ["电子"]})                           # 无 板块名称 -> 取 columns[1]
    assert DS._em_board_names("industry") == ["电子"]             # 杀 columns[1] 1->0
    c2b = {}
    fake_cons = lambda symbol: pd.DataFrame({"x": [1], "代码2": ["600001"]})  # 无 代码 -> columns[1]
    DS._collect_em_cons(c2b, "industry", fake_cons, ["电子"])
    assert "sh600001" in c2b                                      # 杀 columns[1] 1->0


def test_build_map_full_threshold100_no_sina_chinese(fake_akshare, monkeypatch):
    sina_calls = {"n": 0}
    monkeypatch.setattr(DS, "_sina_sector_map",
                        lambda: sina_calls.__setitem__("n", sina_calls["n"] + 1) or {})
    codes = [f"60{str(i).zfill(4)}" for i in range(1, 101)]      # 恰 100 只
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": ["银行"]})
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame({"代码": codes})
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    out = DS.build_sector_map("20261015", background=False)
    assert len(out) == 100 and sina_calls["n"] == 0               # 完整/恰100 不兜底（杀 ok True->False、< -> <=）
    txt = (config.WORKSPACE / "sector_map_20261015.json").read_text(encoding="utf-8")
    assert "银行" in txt and "\\u" not in txt                     # 中文原文落盘（杀 ensure_ascii）


def test_trade_cal_one_hour_old_still_fresh(fake_akshare):
    import os as _os
    import time as _time
    config.TRADE_CAL.write_text(json.dumps(["2026-10-12"]), encoding="utf-8")
    past = _time.time() - 3600
    _os.utime(config.TRADE_CAL, (past, past))
    calls = {"n": 0}
    fake_akshare.tool_trade_date_hist_sina = lambda: calls.__setitem__("n", 1)
    assert DS.load_trade_dates() == {"2026-10-12"} and calls["n"] == 0  # 杀 7*86400 的 * -> /


def test_trade_cal_exact_seven_days_refetches(fake_akshare, monkeypatch):
    import os as _os
    fixed = 2_000_000_000.0
    config.TRADE_CAL.write_text(json.dumps(["2026-10-12"]), encoding="utf-8")
    _os.utime(config.TRADE_CAL, (fixed - 7 * 86400, fixed - 7 * 86400))
    monkeypatch.setattr(DS.time, "time", lambda: fixed)           # age 恰 7*86400 秒
    calls = {"n": 0}
    def _cal():
        calls["n"] += 1
        return pd.DataFrame({"trade_date": ["2026-10-12"]})
    fake_akshare.tool_trade_date_hist_sina = _cal
    DS.load_trade_dates()
    assert calls["n"] == 1                                        # 恰7天过期重拉（杀 < -> <=）


def test_is_trading_time_weekend_hard_rule_and_boundaries(monkeypatch):
    monkeypatch.setattr(DS, "load_trade_dates", lambda: None)     # 日历降级
    assert DS.is_trading_time(datetime(2026, 10, 10, 10, 0)) is False  # 周六硬休市（杀 >= -> >）
    def t(h, m):
        return datetime(2026, 10, 12, h, m)
    assert DS.is_trading_time(t(9, 30)) is True
    assert DS.is_trading_time(t(11, 30)) is True
    assert DS.is_trading_time(t(13, 0)) is True
    assert DS.is_trading_time(t(15, 0)) is True
    assert DS.is_trading_time(t(9, 29)) is False
    assert DS.is_trading_time(t(11, 31)) is False
    assert DS.is_trading_time(t(15, 1)) is False


def test_sina_collect_skips_row_without_board_name(fake_akshare):
    # 空串板块名（falsy；注意 pandas 的 None 会变 NaN 而 NaN 为 truthy，空串才走跳过分支）
    fake_akshare.stock_sector_spot = lambda indicator: pd.DataFrame(
        {"label": ["x"], "板块": [""]})
    fake_akshare.stock_sector_detail = lambda sector: pd.DataFrame({"code": ["300001"]})
    c2b = {}
    DS._collect_sina_indicator(c2b, "概念", "concept")
    assert c2b == {}                                        # 空板块名整行跳过（杀 or -> and 误纳）


def test_build_map_few_cons_still_falls_back_sina(fake_akshare, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(DS, "_sina_sector_map",
                        lambda: calls.__setitem__("n", calls["n"] + 1) or {})
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": ["银行"]})  # 有 names
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame({"代码": ["600001"]})
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    DS.build_sector_map("20261016", background=False)  # 仅 1 只 <100
    assert calls["n"] == 1                                 # ok=True 但成分不足仍兜底（杀 or -> and）


# ---------------- 盘前预建 / 盘中后台兜底（开盘零盲区，2026-10-13 修复） ----------------
def _clear_maps():
    for p in config.WORKSPACE.glob("sector_map_*.json"):
        p.unlink()


def test_build_map_today_cache_hits_without_spawn(monkeypatch):
    _clear_maps()
    (config.WORKSPACE / "sector_map_20261020.json").write_text(
        json.dumps({"sh600001": ["industry:银行"]}), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(DS, "_spawn_map_build",
                        lambda today: spawned.append(today) or True)
    out = DS.build_sector_map("20261020")
    assert out == {"sh600001": ["industry:银行"]} and spawned == []  # 当日缓存命中不触发后台


def test_build_map_background_uses_stale_and_spawns(monkeypatch):
    _clear_maps()
    (config.WORKSPACE / "sector_map_20261012.json").write_text(
        json.dumps({"sh600001": ["industry:旧"]}), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(DS, "_spawn_map_build",
                        lambda today: spawned.append(today) or True)
    out = DS.build_sector_map("20261013")          # 当日缺失，立即用历史兜底、不阻塞
    assert out == {"sh600001": ["industry:旧"]} and spawned == ["20261013"]


def test_build_map_background_no_cache_returns_empty(monkeypatch):
    _clear_maps()
    monkeypatch.setattr(DS, "_spawn_map_build", lambda today: True)
    assert DS.build_sector_map("20261013") == {}   # 无任何历史缓存也不阻塞，返回空 map


def test_latest_stale_map_picks_newest_and_skips_corrupt():
    _clear_maps()
    (config.WORKSPACE / "sector_map_20261010.json").write_text(
        json.dumps({"a": ["industry:old"]}), encoding="utf-8")
    (config.WORKSPACE / "sector_map_20261012.json").write_text("corrupt{", encoding="utf-8")  # 最新的损坏
    assert DS._latest_stale_map("20261013") == {"a": ["industry:old"]}   # 跳过损坏取次新
    assert DS._latest_stale_map("20261012") == {"a": ["industry:old"]}   # 当日(损坏)也排除


def test_spawn_map_build_single_flight(monkeypatch):
    import threading
    gate, started = threading.Event(), threading.Event()
    calls = []

    def _fake_build(today):
        calls.append(today)
        started.set()
        gate.wait(5)

    monkeypatch.setattr(DS, "_build_and_cache_sector_map", _fake_build)
    DS._spawn_map_build("20261030")
    DS._spawn_map_build("20261030")   # 第二次不重复起线程（single-flight）
    assert started.wait(2)
    assert calls == ["20261030"]      # 构建函数只被调用一次
    gate.set()
    for _ in range(50):
        if "20261030" not in DS._map_building:
            break
        threading.Event().wait(0.05)
    assert "20261030" not in DS._map_building    # 收尾后释放，可再次构建
    second = threading.Event()
    monkeypatch.setattr(DS, "_build_and_cache_sector_map",
                        lambda today: second.set())
    DS._spawn_map_build("20261031")
    assert second.wait(2)                          # 释放后新日期可重新启动


def test_spawn_map_build_uses_daemon_thread(monkeypatch):
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None, **kw):
            captured["daemon"] = daemon
            captured["name"] = name

        def start(self):
            pass

    monkeypatch.setattr(DS.threading, "Thread", _FakeThread)
    _clear_maps()
    DS._map_building.clear()
    DS._spawn_map_build("20261101")
    assert captured["daemon"] is True              # 后台建映射线程必须 daemon，主进程退出不被挂住
    assert captured["name"] == "build-sector-map-20261101"


def test_background_then_next_round_picks_up_today_cache(fake_akshare):
    _clear_maps()
    (config.WORKSPACE / "sector_map_20261012.json").write_text(
        json.dumps({"sh600001": ["industry:旧"]}), encoding="utf-8")
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": ["银行"]})
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame({"代码": ["600001"]})
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    DS._build_and_cache_sector_map("20261013")          # 模拟后台线程已把当日映射建好
    assert DS.build_sector_map("20261013") == {"sh600001": ["industry:银行"]}  # 下轮自动切当日


# ---------- 板块映射行业维度完整性（I18 加固：行业是盘中主维度，不能因概念成功而静默缺失） ----------

def test_build_map_industry_missing_triggers_sina(fake_akshare, monkeypatch):
    _clear_maps()
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": ["某概念"]})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame(
        {"代码": [f"{i:06d}" for i in range(1, 151)]})   # 概念 150 只，总量已够
    sina_calls = []
    monkeypatch.setattr(DS, "_sina_sector_map", lambda: (sina_calls.append(1) or {}))
    DS._build_and_cache_sector_map("20261013")
    assert sina_calls == [1]        # 行业缺失，即使概念充足也必须补新浪


def test_build_map_industry_ok_concept_missing_no_sina(fake_akshare, monkeypatch):
    _clear_maps()
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": ["银行"]})
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame(
        {"代码": [f"{i:06d}" for i in range(1, 151)]})   # 行业 150 只，总量够
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    sina_calls = []
    monkeypatch.setattr(DS, "_sina_sector_map", lambda: (sina_calls.append(1) or {}))
    DS._build_and_cache_sector_map("20261013")
    assert sina_calls == []         # 行业是主维度且充足，概念缺失不强行补新浪（口径不一致）


def test_build_map_all_empty_triggers_sina_by_count(fake_akshare, monkeypatch):
    _clear_maps()
    fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": []})
    fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame({"代码": []})
    sina_calls = []
    monkeypatch.setattr(DS, "_sina_sector_map", lambda: (sina_calls.append(1) or {}))
    DS._build_and_cache_sector_map("20261013")
    assert sina_calls == [1]        # 东财全空、总量不足 100，补新浪


def test_build_map_industry_coverage_warn_boundary(fake_akshare, monkeypatch, capsys):
    """完成后行业覆盖健康告警：恰 100 只（合格线）不告警、99 只告警（杀 206 < -> <=）。"""
    def _build(n, date):
        _clear_maps()
        fake_akshare.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": ["银行"]})
        fake_akshare.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": []})
        fake_akshare.stock_board_industry_cons_em = lambda symbol: pd.DataFrame(
            {"代码": [f"60{str(i).zfill(4)}" for i in range(1, n + 1)]})
        fake_akshare.stock_board_concept_cons_em = lambda symbol: pd.DataFrame({"代码": []})
        monkeypatch.setattr(DS, "_sina_sector_map", lambda: {})   # 兜底也补不到行业
        DS._build_and_cache_sector_map(date)
        return capsys.readouterr().out

    assert "行业映射仍缺失" not in _build(100, "20261201")       # 恰 100 只=合格线，不告警
    assert "行业映射仍缺失" in _build(99, "20261202")            # 99 只不足，必须告警


def test_latest_stale_prefers_complete_industry_over_newer_residual():
    _clear_maps()
    concept_only = {f"sz{i:06d}": ["concept:概念"] for i in range(150)}
    (config.WORKSPACE / "sector_map_20261012.json").write_text(
        json.dumps(concept_only), encoding="utf-8")        # 较新但缺行业（残映射）
    industry_ok = {f"sh60{i:03d}": ["industry:银行"] for i in range(100)}
    (config.WORKSPACE / "sector_map_20261010.json").write_text(
        json.dumps(industry_ok), encoding="utf-8")        # 较旧但行业完整
    assert DS._latest_stale_map("20261013") == industry_ok


def test_latest_stale_degrades_when_all_lack_industry():
    _clear_maps()
    concept_only = {f"sz{i:06d}": ["concept:概念"] for i in range(150)}
    (config.WORKSPACE / "sector_map_20261012.json").write_text(
        json.dumps(concept_only), encoding="utf-8")
    assert DS._latest_stale_map("20261013") == concept_only   # 都缺行业也降级返回最近非空
