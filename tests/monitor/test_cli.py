# -*- coding: utf-8 -*-
"""cli 层：上次成功时间解析、看门狗、单轮轮询、停机补检、单次入口。"""
from datetime import datetime, timedelta

import pytest

from src.monitor import cli
from src.monitor.config import POLL_NORMAL, POLL_LATE, POLL_CLOSED, HB_STALL_SEC


class _FakeMon:
    def __init__(self, src="eastmoney", raise_exc=None):
        self.src = src
        self.raise_exc = raise_exc
        self.scanned = 0

    def scan_once(self):
        self.scanned += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.src


def test_parse_last_success():
    assert cli._parse_last_success(None).replace(second=0, microsecond=0) == \
        datetime.now().replace(second=0, microsecond=0)
    assert cli._parse_last_success("2026-10-12 09:30:00") == datetime(2026, 10, 12, 9, 30)
    assert cli._parse_last_success("garbage").replace(second=0, microsecond=0) == \
        datetime.now().replace(second=0, microsecond=0)


# ---------------- 看门狗 ----------------
def test_watchdog_silent_when_closed(monkeypatch, no_push):
    monkeypatch.setattr(cli, "current_session_start", lambda now: None)
    st = cli._RunState(datetime.now() - timedelta(hours=2))
    cli._watchdog(st, datetime.now())
    assert no_push == []


def test_watchdog_alerts_then_cooldown(monkeypatch, no_push):
    monkeypatch.setattr(cli, "current_session_start",
                        lambda now: datetime(2026, 10, 12, 9, 30))
    now = datetime(2026, 10, 12, 10, 31)
    st = cli._RunState(now - timedelta(seconds=HB_STALL_SEC + 60))
    cli._watchdog(st, now)
    assert len(no_push) == 1 and st.down_alerted is True and st.last_hb == now
    # 冷却期内再次检查，不重复告警
    cli._watchdog(st, now + timedelta(minutes=10))
    assert len(no_push) == 1


def test_watchdog_no_alert_within_stall(monkeypatch, no_push):
    monkeypatch.setattr(cli, "current_session_start",
                        lambda now: now.replace(hour=9, minute=30, second=0, microsecond=0))
    st = cli._RunState(datetime.now())
    cli._watchdog(st, datetime.now())
    assert no_push == []


def test_watchdog_lunch_break_not_alert(monkeypatch, no_push):
    """I22：午休后下午开盘，末次成功扫描在 11:29，开盘 1 分钟内不应误报（休市不计静默）。"""
    monkeypatch.setattr(cli, "current_session_start",
                        lambda now: now.replace(hour=13, minute=0, second=0, microsecond=0))
    now = datetime(2026, 10, 12, 13, 1)
    st = cli._RunState(datetime(2026, 10, 12, 11, 29, 55))
    cli._watchdog(st, now)
    assert no_push == [] and st.down_alerted is False


def test_watchdog_afternoon_real_still_alerts(monkeypatch, no_push):
    """下午开盘后真的连续 30 分钟扫不出数据（last_success 仍停在上午），仍须告警。"""
    monkeypatch.setattr(cli, "current_session_start",
                        lambda now: now.replace(hour=13, minute=0, second=0, microsecond=0))
    now = datetime(2026, 10, 12, 13, 35)
    st = cli._RunState(datetime(2026, 10, 12, 11, 29, 55))
    cli._watchdog(st, now)
    assert len(no_push) == 1 and st.down_alerted is True


def test_watchdog_overnight_open_not_alert(monkeypatch, no_push):
    """隔夜后上午开盘，末次成功扫描在前一交易日 15:00，开盘 1 分钟内不应误报。"""
    monkeypatch.setattr(cli, "current_session_start",
                        lambda now: now.replace(hour=9, minute=30, second=0, microsecond=0))
    now = datetime(2026, 10, 12, 9, 31)
    st = cli._RunState(datetime(2026, 10, 9, 15, 0))
    cli._watchdog(st, now)
    assert no_push == [] and st.down_alerted is False


# ---------------- 单轮轮询 ----------------
def test_poll_closed(monkeypatch):
    monkeypatch.setattr(cli, "is_trading_time", lambda now: False)
    assert cli._poll_once(_FakeMon(), cli._RunState(datetime.now())) == POLL_CLOSED


def test_poll_success_normal(monkeypatch, no_push):
    class _MorningDT(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 12, 10, 0)   # 固定非尾盘：任何时刻运行都应走常态档 60s
    monkeypatch.setattr(cli, "datetime", _MorningDT)
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    st = cli._RunState(_MorningDT(2026, 10, 12, 10, 0))
    secs = cli._poll_once(_FakeMon(), st)
    assert secs == POLL_NORMAL and st.last_success is not None


def test_poll_late_session(monkeypatch, no_push):
    class _LateDT(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 12, 14, 31)
    monkeypatch.setattr(cli, "datetime", _LateDT)
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    assert cli._poll_once(_FakeMon(), cli._RunState(datetime(2026, 10, 12, 14, 31))) == POLL_LATE


def test_poll_all_sources_down_selfcheck(monkeypatch, no_push):
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    st = cli._RunState(datetime.now())
    cli._poll_once(_FakeMon(src=None), st)
    assert len(no_push) == 1 and st.selfcheck_done is True
    # 已自检后持续失败，不再重复高声量告警
    cli._poll_once(_FakeMon(src=None), st)
    assert len(no_push) == 1


def test_poll_recovery_pushes(monkeypatch, no_push):
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    st = cli._RunState(datetime.now())
    st.down_alerted = True
    cli._poll_once(_FakeMon(src="tencent_agg"), st)
    assert any("已恢复" in t for t, _ in no_push) and st.down_alerted is False


def test_poll_exception_selfcheck(monkeypatch, no_push):
    class _MorningDT(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 12, 10, 0)   # 固定非尾盘：异常自检后返回常态档 60s
    monkeypatch.setattr(cli, "datetime", _MorningDT)
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    st = cli._RunState(_MorningDT(2026, 10, 12, 10, 0))
    secs = cli._poll_once(_FakeMon(raise_exc=RuntimeError("x")), st)
    assert secs == POLL_NORMAL and len(no_push) == 1


# ---------------- 停机补检 ----------------
def test_gap_check_skips_when_short_or_closed(monkeypatch, no_push):
    monkeypatch.setattr(cli, "is_trading_time", lambda: True)
    m = _FakeMon()
    recent = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    cli._gap_check(m, cli._RunState(datetime.now()), recent)
    assert m.scanned == 0
    monkeypatch.setattr(cli, "is_trading_time", lambda: False)
    old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    cli._gap_check(m, cli._RunState(datetime.now()), old)
    assert m.scanned == 0


def test_gap_check_success_and_failures(monkeypatch, no_push):
    monkeypatch.setattr(cli, "is_trading_time", lambda: True)
    old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    st = cli._RunState(datetime.now())
    cli._gap_check(_FakeMon(src="eastmoney"), st, old)
    assert st.selfcheck_done is True and no_push == []
    # 全源不可用 -> 自检失败告警
    st2 = cli._RunState(datetime.now())
    cli._gap_check(_FakeMon(src=None), st2, old)
    assert len(no_push) == 1
    # 抛异常 -> 自检异常告警
    st3 = cli._RunState(datetime.now())
    cli._gap_check(_FakeMon(raise_exc=RuntimeError("x")), st3, old)
    assert len(no_push) == 2


# ---------------- 单次入口 ----------------
def test_main_build_map(monkeypatch):
    def _forbid():
        raise AssertionError("build-map 不应取单例锁")
    monkeypatch.setattr(cli, "single_instance", _forbid)
    seen = {}

    def _fake_build(d, **kw):
        seen["d"] = d
        seen["kw"] = kw
        return {}
    monkeypatch.setattr(cli, "build_sector_map", _fake_build)
    cli.main(["prog", "--build-map"])
    assert seen["d"] == datetime.now().strftime("%Y%m%d")
    assert seen["kw"].get("background") is False   # 盘前必须同步建完（杀 False->True）


def test_main_once(monkeypatch, no_push):
    monkeypatch.setattr(cli, "single_instance", lambda: object())
    fake = _FakeMon()
    monkeypatch.setattr(cli, "Monitor", lambda: fake)
    saved = {}
    monkeypatch.setattr(cli, "save_last_scan", lambda *a, **k: saved.setdefault("x", 1))
    cli.main(["prog", "--once"])
    assert fake.scanned == 1 and saved.get("x") == 1


# ---------------- 变异边界加固 ----------------
def test_runstate_initial_flags():
    st = cli._RunState(datetime(2026, 10, 12, 10, 0))
    assert st.down_alerted is False and st.selfcheck_done is False and st.last_hb is None


def test_watchdog_stall_exact_boundary(monkeypatch, no_push):
    monkeypatch.setattr(cli, "current_session_start",
                        lambda now: datetime(2026, 10, 12, 9, 30))
    now = datetime(2026, 10, 12, 10, 30)
    st_eq = cli._RunState(now - timedelta(seconds=HB_STALL_SEC))   # 恰 1800s
    cli._watchdog(st_eq, now)
    assert len(no_push) == 1                                        # 恰阈值告警（杀 >= -> >）
    st_before = cli._RunState(now - timedelta(seconds=HB_STALL_SEC - 1))  # 1799s
    cli._watchdog(st_before, now)
    assert len(no_push) == 1                                        # 差 1 秒不告警


def test_gap_check_exact_five_minutes_triggers(monkeypatch, no_push):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 12, 10, 0)
    monkeypatch.setattr(cli, "datetime", _DT)
    monkeypatch.setattr(cli, "is_trading_time", lambda: True)
    monkeypatch.setattr(cli, "save_last_scan", lambda *a, **k: None)
    m = _FakeMon(src="eastmoney")
    prev = datetime(2026, 10, 12, 9, 55).strftime("%Y-%m-%d %H:%M:%S")  # gap 恰 5 分钟
    cli._gap_check(m, cli._RunState(_DT(2026, 10, 12, 10, 0)), prev)
    assert m.scanned == 1   # 恰 5 分钟：gap<5 为 False 必须补检（杀 < -> <=）


def test_gap_failure_and_exception_mark_selfcheck(monkeypatch, no_push):
    monkeypatch.setattr(cli, "is_trading_time", lambda: True)
    old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    st1 = cli._RunState(datetime.now())
    cli._gap_check(_FakeMon(src=None), st1, old)
    assert st1.selfcheck_done is True                  # 全源失败（杀 L82 True -> False）
    st2 = cli._RunState(datetime.now())
    cli._gap_check(_FakeMon(raise_exc=RuntimeError("x")), st2, old)
    assert st2.selfcheck_done is True                  # 异常（杀 L88 True -> False）


def test_poll_exact_1430_is_late(monkeypatch, no_push):
    class _DT(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 12, 14, 30)           # 恰 14:30（杀 >= -> >）
    monkeypatch.setattr(cli, "datetime", _DT)
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    assert cli._poll_once(_FakeMon(), cli._RunState(_DT(2026, 10, 12, 14, 30))) == POLL_LATE


def test_poll_exception_marks_selfcheck_done(monkeypatch, no_push):
    monkeypatch.setattr(cli, "is_trading_time", lambda now: True)
    st = cli._RunState(datetime.now())
    cli._poll_once(_FakeMon(raise_exc=RuntimeError("x")), st)
    assert st.selfcheck_done is True                   # 杀 L117 True -> False
