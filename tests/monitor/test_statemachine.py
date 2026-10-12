# -*- coding: utf-8 -*-
"""statemachine 层（核心纯逻辑）：分级、宽限/波次、增量与文案。"""
from datetime import datetime, timedelta

import pytest

from src.monitor import statemachine as sm
from src.monitor.config import (CHG_L1, ZT_L1, ZT_L2, LB_L2, UP_RATIO,
                                SPEED_L1, SPEED_LATE, CONCENTRATION, STEP_ZT,
                                RELAPSE_GRACE_MIN)

NOW = datetime(2026, 10, 12, 10, 0)


def board(chg=3.0, zt=5, lb=2.0, speed=None, up=80, down=10,
          code="BK1", name="A"):
    return {"name": name, "code": code, "chg": chg, "zt": zt, "lb": lb,
            "speed": speed, "up": up, "down": down}


# ---------------- 基础占比/分级 ----------------
def test_up_ratio():
    assert sm.up_ratio(80, 20) == 0.8
    assert sm.up_ratio(None, 20) is None
    assert sm.up_ratio(0, 0) is None


def test_meets_l1_speed():
    hit_speed, hit_chg, lvl = sm.meets_l1(1.0, 0, SPEED_L1, SPEED_L1)
    assert (hit_speed, hit_chg, lvl) == (True, False, "L1")


def test_meets_l1_chg_zt():
    hit_speed, hit_chg, lvl = sm.meets_l1(CHG_L1 + 0.1, ZT_L1, None, SPEED_L1)
    assert (hit_speed, hit_chg, lvl) == (False, True, "L1")


def test_meets_l1_none_and_boundary():
    assert sm.meets_l1(1.0, 0, 0.2, SPEED_L1)[2] == "NONE"
    # 涨幅等于阈值（不满足 >）、涨停差一家
    assert sm.meets_l1(CHG_L1, ZT_L1 - 1, None, SPEED_L1)[2] == "NONE"


@pytest.mark.parametrize("chg,zt,lb,upr,ok", [
    (CHG_L1 + 0.1, ZT_L2, LB_L2, UP_RATIO, True),
    (CHG_L1 + 0.1, ZT_L2, None, None, True),   # 量比/占比缺失放行
    (CHG_L1 + 0.1, ZT_L2, LB_L2 - 0.1, UP_RATIO, False),
    (CHG_L1 + 0.1, ZT_L2, LB_L2, UP_RATIO - 0.01, False),
    (CHG_L1, ZT_L2, LB_L2, UP_RATIO, False),
    (CHG_L1 + 0.1, ZT_L2 - 1, LB_L2, UP_RATIO, False),
])
def test_meets_l2(chg, zt, lb, upr, ok):
    assert sm.meets_l2(chg, zt, lb, upr) is ok


def test_classify_signal_none_chg():
    assert sm.classify_signal(board(chg=None), {}, {}, False) is None


def test_classify_signal_zt_backfill():
    b = board(chg=3.0, zt=None, lb=None, up=None, down=None)
    sig = sm.classify_signal(b, {"A": 4}, {"A": 2}, False)
    assert sig["zt"] == 4  # 行业优先
    sig2 = sm.classify_signal(board(chg=3.0, zt=None, name="B"), {}, {"B": 2}, False)
    assert sig2["zt"] == 2


def test_classify_signal_late_threshold_and_code():
    sig = sm.classify_signal(board(chg=1.0, zt=0, speed=SPEED_LATE), {}, {}, True)
    assert sig["speed_thr"] == SPEED_LATE and sig["cur_lvl"] == "L1"
    sig_nocode = sm.classify_signal(board(chg=1.0, zt=0, code=None, speed=0),
                                    {}, {}, False)
    assert sig_nocode["code"] == "A" and sig_nocode["raw_code"] == ""


# ---------------- 主线占比 ----------------
def test_concentration():
    sig = board_sig(zt=0)
    assert sm.concentration(sig, None, 10)[0] is None
    sig2 = board_sig(zt=2)
    ratio, prev, first = sm.concentration(sig2, {"conc": 0.0}, 10)
    assert round(ratio, 3) == 0.2 and first is True
    _, _, first2 = sm.concentration(sig2, {"conc": 0.2}, 10)
    assert first2 is False


def board_sig(zt=5, chg=3.0):
    return {"chg": chg, "zt": zt, "lb": 2.0, "speed": None}


def test_snapshot_fields_uses_prev_when_ratio_none():
    snap = sm.snapshot_fields(board_sig(zt=0), None, 0.07, NOW)
    assert snap["conc"] == 0.07 and snap["ts"] == NOW
    snap2 = sm.snapshot_fields(board_sig(zt=2), 0.2, 0.07, NOW)
    assert snap2["conc"] == 0.2


# ---------------- 消退宽限 ----------------
def test_settle_from_none():
    sig = {"chg": 1.0}
    st = sm.settle_while_inactive(None, sig, {"ts": NOW}, NOW)
    assert st["active"] is False and st["lvl"] == "NONE"


def _active_state(lost_offset=None):
    st = sm.empty_board_state()
    st.update({"active": True, "lvl": "L1", "wave": 1, "peak_zt": 5})
    if lost_offset is not None:
        st["lost_ts"] = NOW + timedelta(minutes=lost_offset)
    return st


def test_settle_within_grace_keeps_active():
    st = _active_state()
    out = sm.settle_while_inactive(st, {"chg": 1.0}, {"ts": NOW}, NOW)
    assert out["lost_ts"] == NOW and out["active"] is True
    assert out["trough_chg"] == 1.0


def test_settle_after_grace_ends_wave():
    st = _active_state(lost_offset=-RELAPSE_GRACE_MIN - 1)
    out = sm.settle_while_inactive(st, {"chg": 0.5}, {"ts": NOW}, NOW)
    assert out["active"] is False and out["lvl"] == "NONE"
    assert out["prev_wave_end_ts"] == NOW and out["lost_ts"] is None


def test_close_expired_grace():
    sm.close_expired_grace(None, NOW)  # 不抛
    active = _active_state(lost_offset=-1)
    sm.close_expired_grace(active, NOW)
    assert active["active"] is True
    expired = _active_state(lost_offset=-RELAPSE_GRACE_MIN - 1)
    sm.close_expired_grace(expired, NOW)
    assert expired["active"] is False and expired["lvl"] == "NONE"


# ---------------- 波次 ----------------
def test_begin_first_wave():
    sig = {"cur_lvl": "L2", "zt": 5}
    st, tag, gap = sm.begin_new_wave(None, sig, NOW)
    assert st["wave"] == 1 and tag == "首次启动" and gap is None
    assert st["fired_l2"] is True and st["fired_l1"] is False
    assert st["wave_start_ts"] == NOW


def test_begin_relaunch_wave():
    old = sm.empty_board_state()
    old.update({"wave": 1, "active": False, "fire_count": 3,
                "last_fire_ts": NOW - timedelta(minutes=30),
                "last_fire_lvl": "L1", "trough_chg": 0.3})
    sig = {"cur_lvl": "L1", "zt": 3}
    st, tag, gap = sm.begin_new_wave(old, sig, NOW)
    assert st["wave"] == 2 and tag == "再度启动·第2波" and gap == 30.0
    assert st["fire_count"] == 3  # 跨波沿用累计次数
    assert st["last_fire_zt"] is None  # 不沿用上波指标
    assert st["trough_at_relaunch"] == 0.3


def test_advance_active_wave_decisions():
    # 升级 L1->L2
    st = _active_state()
    st.update({"lvl": "L1", "fired_l2": False, "last_fire_zt": 3})
    assert sm.advance_active_wave(st, {"cur_lvl": "L2", "zt": 5}, False) == ("L2", "升级 L1→L2")
    # 首次成主线
    st2 = _active_state()
    st2.update({"lvl": "L2", "fired_l2": True, "last_fire_zt": 5})
    assert sm.advance_active_wave(st2, {"cur_lvl": "L2", "zt": 5}, True) == ("L2", "首次成为市场主线")
    # 走强一步
    st3 = _active_state()
    st3.update({"lvl": "L2", "fired_l2": True, "last_fire_zt": 3})
    assert sm.advance_active_wave(st3, {"cur_lvl": "L2", "zt": 3 + STEP_ZT}, False) == ("L2", "走强一步")
    # 无台阶静默，并清消退计时
    st4 = _active_state(lost_offset=-2)
    st4.update({"lvl": "L2", "fired_l2": True, "last_fire_zt": 5, "trough_chg": 1.0})
    assert sm.advance_active_wave(st4, {"cur_lvl": "L2", "zt": 5}, False) is None
    assert st4["lost_ts"] is None and st4["trough_chg"] is None


# ---------------- 增量行 ----------------
def test_delta_zt():
    assert sm.delta_zt(None, 5) is None
    assert sm.delta_zt(5, 5) is None
    assert "5→7" in sm.delta_zt(5, 7) and "（+2）" in sm.delta_zt(5, 7)
    assert "（-1）" in sm.delta_zt(5, 4)


def test_delta_chg():
    assert sm.delta_chg(None, 3.0) is None
    assert sm.delta_chg(3.0, 3.4) is None      # 差 0.4 不报
    assert sm.delta_chg(3.0, 3.5) is not None  # 边界 0.5
    assert sm.delta_chg(3.0, 2.0) is not None


def test_delta_lb():
    assert sm.delta_lb(None, 1.5) is None
    assert sm.delta_lb(1.5, 1.6) is None
    # 1.7-1.5 浮点为 0.1999... < 0.2，用差 0.3 的值稳定越过阈值（锁定真实判定）
    assert sm.delta_lb(1.5, 1.8) is not None
    assert sm.delta_lb(1.5, None) is None


def test_format_deltas():
    sig = {"zt": 7, "chg": 4.0, "lb": 1.8}
    st = {"last_fire_zt": 5, "last_fire_chg": 3.0, "last_fire_lb": 1.5}
    line = sm.format_deltas(st, sig)
    assert line.startswith("变化：") and "涨停" in line and "涨幅" in line and "量比" in line
    assert sm.format_deltas({"last_fire_zt": 7, "last_fire_chg": 4.0,
                             "last_fire_lb": 1.8}, sig) == ""


def test_format_reasons():
    l2_sig = {"l2": True, "hit_l1_speed": False, "hit_l1_chg": True, "speed_thr": SPEED_L1}
    assert sm.format_reasons(l2_sig)[0].startswith("满足L2")
    sp = {"l2": False, "hit_l1_speed": True, "hit_l1_chg": False, "speed_thr": SPEED_LATE}
    assert sm.format_reasons(sp) == [f"涨速≥{SPEED_LATE}%"]
    both = {"l2": False, "hit_l1_speed": True, "hit_l1_chg": True, "speed_thr": SPEED_L1}
    assert len(sm.format_reasons(both)) == 2


def test_format_tag_extra():
    assert sm.format_tag_extra("首次启动", 1, None, None) == ""
    assert sm.format_tag_extra("升级 L1→L2", 2, None, None) == "（今日第2次）"
    extra = sm.format_tag_extra("再度启动·第2波", 4, 30.0, 0.3)
    assert "今日第4次" in extra and "30分钟" in extra and "最低曾至" in extra
    extra_dec = sm.format_tag_extra("再度启动·第2波", 4, 12.5, None)
    assert "12.5分钟" in extra_dec


def test_concentration_line():
    assert sm.concentration_line(None) == ""
    main = sm.concentration_line(CONCENTRATION)
    assert "⭐" in main and "市场主线" in main
    side = sm.concentration_line(0.1)
    assert "暂非主线" in side


def test_metrics_line():
    sig = {"chg": 3.0, "zt": 5, "speed": 1.5, "lb": 2.0, "up_ratio": 0.8}
    line = sm.metrics_line(sig, 40, ["持仓3"])
    assert "涨幅 +3.00%" in line and "全市场涨停 40" in line and "持仓3" in line
    minimal = sm.metrics_line({"chg": 1.0, "zt": 0, "speed": None,
                               "lb": None, "up_ratio": None}, 0, [])
    assert "涨速" not in minimal and "量比" not in minimal and "全市场涨停" not in minimal


def test_build_alert_message_levels():
    sig = sm.classify_signal(board(chg=3.0, zt=5, lb=2.0, up=80, down=10),
                             {}, {}, False)
    st = sm.empty_board_state()
    st.update({"last_fire_zt": 3, "last_fire_chg": 2.6, "last_fire_lb": 1.5,
               "trough_at_relaunch": None})
    title, msg = sm.build_alert_message(sig, 40, 0.2, "L2", "首次启动", 1, None,
                                        st, NOW, ["持仓3"], "\nHINT", "eastmoney")
    assert title.startswith("🔴") and "首次启动" in title
    assert "满足L2" in msg and "变化：" in msg and "持仓3" in msg and "HINT" in msg
    assert "兜底" not in msg  # 主源 eastmoney 不带口径标注
    sig_l1 = sm.classify_signal(board(chg=1.0, zt=0, speed=SPEED_L1, lb=None,
                                      up=None, down=None), {}, {}, False)
    title1, msg1 = sm.build_alert_message(sig_l1, 40, None, "L1", "首次启动", 1,
                                          None, sm.empty_board_state(), NOW, [],
                                          "", "eastmoney")
    assert title1.startswith("🟠") and f"涨速≥{SPEED_L1}%" in msg1


def test_source_caveat_by_source():
    assert sm.source_caveat("eastmoney") == ""
    cav_tx = sm.source_caveat("tencent_agg")
    assert "腾讯" in cav_tx and "等权" in cav_tx
    cav_sina = sm.source_caveat("sina")
    assert "新浪" in cav_sina and "缺涨停" in cav_sina


def test_build_alert_message_fallback_caveat():
    sig = sm.classify_signal(board(chg=3.0, zt=5, lb=2.0, up=80, down=10),
                             {}, {}, False)
    _, msg_tx = sm.build_alert_message(sig, 40, None, "L2", "首次启动", 1, None,
                                       sm.empty_board_state(), NOW, [], "",
                                       "tencent_agg")
    assert "腾讯个股聚合兜底" in msg_tx
    _, msg_sina = sm.build_alert_message(sig, 40, None, "L2", "首次启动", 1,
                                         None, sm.empty_board_state(), NOW, [],
                                         "", "sina")
    assert "新浪板块兜底" in msg_sina


# ---------------- 变异测试边界加固（杀死 SURVIVED 位点） ----------------
def test_empty_state_initial_wave_zero():
    st = sm.empty_board_state()
    assert st["wave"] == 0 and st["fire_count"] == 0
    assert st["active"] is False and st["lvl"] == "NONE"


def test_up_ratio_edge_cases():
    assert sm.up_ratio(3, 0) == 1.0          # down=0 但非 None，仍计算
    assert sm.up_ratio(0, 5) is None        # up=0 假值
    assert sm.up_ratio(3, 1) == 0.75        # 精确分母（杀 + -> -）


def test_meets_l1_chg_threshold_equality_and_and():
    # chg 恰等于阈值：> 不满足（杀 > -> >=）
    assert sm.meets_l1(CHG_L1, ZT_L1, None, SPEED_L1)[1] is False
    # 涨停足但涨幅不达：and 不可退化为 or（杀 and -> or）
    assert sm.meets_l1(1.0, ZT_L1, None, SPEED_L1)[1] is False
    # 刚过阈值即命中
    assert sm.meets_l1(CHG_L1 + 0.01, ZT_L1, None, SPEED_L1)[1] is True


def test_classify_zt_fallback_zero():
    b = {"name": "X", "code": "BK", "chg": 3.0}  # 无 zt 字段，归集也无
    sig = sm.classify_signal(b, {}, {}, False)
    assert sig["zt"] == 0


def test_concentration_first_time_at_threshold():
    # conc 恰 = 0.15：>= 满足，首次成为主线（杀 >= -> >）
    ratio, prev, first = sm.concentration({"zt": 15}, None, 100)
    assert ratio == 0.15 and prev == 0.0 and first is True
    ratio2, _, first2 = sm.concentration({"zt": 14}, None, 100)
    assert ratio2 == 0.14 and first2 is False


def _active_lost_st(peak=None, minutes=0):
    st = sm.empty_board_state()
    st.update({"active": True, "lvl": "L1", "lost_ts": NOW - timedelta(minutes=minutes)})
    if peak is not None:
        st["peak_zt"] = peak
    return st


def test_settle_grace_boundary_9_vs_10():
    sig = {"chg": 0.0}
    st9 = sm.settle_while_inactive(_active_lost_st(peak=5, minutes=9), sig, {}, NOW)
    assert st9["active"] is True and st9["lost_ts"] is not None
    st10 = sm.settle_while_inactive(_active_lost_st(peak=5, minutes=10), sig, {}, NOW)
    assert st10["active"] is False and st10["lvl"] == "NONE"
    assert st10["prev_wave_end_ts"] == NOW and st10["lost_ts"] is None


def test_settle_peak_default_zero_when_missing():
    st = sm.settle_while_inactive(_active_lost_st(peak=None, minutes=10),
                                  {"chg": 0.0}, {}, NOW)
    assert st["prev_wave_peak_zt"] == 0


def test_close_expired_grace_boundary_and_peak_default():
    st9 = _active_lost_st(peak=None, minutes=9)
    sm.close_expired_grace(st9, NOW)
    assert st9["active"] is True
    st10 = _active_lost_st(peak=None, minutes=10)
    sm.close_expired_grace(st10, NOW)
    assert st10["active"] is False and st10["prev_wave_peak_zt"] == 0


def test_begin_new_wave_first_wave_number():
    st, tag, gap = sm.begin_new_wave(sm.empty_board_state(),
                                     {"cur_lvl": "L1", "zt": 3}, NOW)
    assert st["wave"] == 1 and tag == "首次启动" and gap is None


def test_advance_strength_step_with_none_last_fire_zt():
    st = sm.empty_board_state()
    st.update({"active": True, "lvl": "L1", "fired_l1": True, "fired_l2": False,
               "last_fire_zt": None})
    # zt=2、上次基准 None 视为 0：2-0>=STEP_ZT 触发走强（杀 0 -> 1）
    lvl, tag = sm.advance_active_wave(st, {"cur_lvl": "L1", "zt": STEP_ZT}, False)
    assert tag == "走强一步"


def test_delta_lb_threshold_equality():
    assert sm.delta_lb(0, 0.2) is not None      # 恰 0.2：>= 满足（杀 >= -> >）
    assert sm.delta_lb(0, 0.19) is None


def test_concentration_and_metrics_percent_text():
    assert "30%" in sm.concentration_line(0.3)   # 主线分支（杀 * -> /）
    assert "10%" in sm.concentration_line(0.1)   # 非主线分支（杀 * -> /）
    line = sm.metrics_line({"chg": 1.0, "zt": 0, "speed": None, "lb": None,
                            "up_ratio": 0.75}, 0, [])
    assert "75%" in line                          # 上涨占比百分比（杀 * -> /）


# ---------------- 变异边界加固（第二轮） ----------------
def test_up_ratio_exact_denominator_and_unit_total():
    assert sm.up_ratio(8, 2) == 0.8      # 分母 up+down=10（杀 return 处分母 + -> -）
    assert sm.up_ratio(1, 5) == 1 / 6    # down>up：条件 (up+down)>0 成立（杀条件处 + -> - 变 up-down<0）
    assert sm.up_ratio(1, 0) == 1.0      # 合计恰 1 也成立（杀 >0 的 0 -> 1 变 >1）
    assert sm.up_ratio(0, 0) is None
    assert sm.up_ratio(None, 2) is None


def test_begin_relaunch_gap_keeps_one_decimal():
    old = {"wave": 1, "fire_count": 1, "active": False, "peak_zt": 3,
           "last_fire_ts": datetime(2026, 10, 12, 10, 0)}
    now = datetime(2026, 10, 12, 10, 26, 42)   # 距 26 分 42 秒 = 26.7 分钟
    _, _, gap = sm.begin_new_wave(old, {"cur_lvl": "L1", "zt": 3}, now)
    assert gap == 26.7                    # round(...,1)（杀 round 第二参 1 -> 0）


def test_concentration_main_line_percent():
    line = sm.concentration_line(0.3)
    assert "30%" in line and "主线" in line   # 主线分支 conc*100（杀 * -> /）
    assert "≥15%" in line                     # 阈值常量 CONCENTRATION*100（杀常量处 * -> /）
    assert sm.concentration_line(None) == ""


def test_settle_and_close_missing_peak_defaults_zero():
    now = datetime(2026, 10, 12, 11, 0)
    st = {"active": True, "lost_ts": now - timedelta(minutes=11), "wave": 1}  # 无 peak_zt（脏数据/迁移）
    sm.settle_while_inactive(st, {"cur_lvl": "NONE", "chg": 0.5}, {}, now)
    assert st["active"] is False and st["prev_wave_peak_zt"] == 0   # 杀 settle 的 get 默认 0 -> 1
    st2 = {"active": True, "lost_ts": now - timedelta(minutes=11), "wave": 1}
    sm.close_expired_grace(st2, now)
    assert st2["active"] is False and st2["prev_wave_peak_zt"] == 0  # 杀 close 的 get 默认 0 -> 1
