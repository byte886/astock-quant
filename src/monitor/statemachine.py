# -*- coding: utf-8 -*-
"""状态机层（纯逻辑，无任何网络/文件 IO）：板块信号分级、消退宽限、波次、文案。

状态：每板块 state ∈ {NONE,L1,L2}，当天每级只推一次；L1->L2 升级再推；
条件全消失并超过消退宽限后重新放量算"新一波（再度启动·第W波）"，宽限内抖动静默。
本层只做判定与文本组装，推送/落台账等副作用由 monitor 层执行。
"""
from .config import (CHG_L1, SPEED_L1, SPEED_LATE, ZT_L1, ZT_L2, LB_L2,
                     UP_RATIO, CONCENTRATION, STEP_ZT, RELAPSE_GRACE_MIN)


def empty_board_state():
    """一个从未达标过的板块的初始状态。"""
    return {"wave": 0, "active": False, "lvl": "NONE",
            "fired_l1": False, "fired_l2": False, "fire_count": 0,
            "peak_zt": 0, "lost_ts": None, "trough_chg": None,
            "prev_wave_end_ts": None, "last_fire_ts": None,
            "last_fire_lvl": None, "last_fire_zt": None,
            "last_fire_chg": None, "last_fire_lb": None}


def up_ratio(up, down):
    """板块内上涨家数占比；家数缺失或合计为 0 时返回 None。"""
    if up and down is not None and (up + down) > 0:
        return up / (up + down)
    return None


def meets_l1(chg, zt_n, speed, speed_thr):
    """L1 触发：涨速达标，或涨幅+涨停数达标。返回 (涨速命中, 涨幅命中, 基础级别)。"""
    hit_speed = speed is not None and speed >= speed_thr
    hit_chg = chg > CHG_L1 and zt_n >= ZT_L1
    return hit_speed, hit_chg, ("L1" if (hit_speed or hit_chg) else "NONE")


def meets_l2(chg, zt_n, lb, upr):
    """L2 触发：涨幅+涨停数达标，且量比、上涨占比同时达标。"""
    price_ok = chg > CHG_L1 and zt_n >= ZT_L2
    breadth_ok = ((lb is None or lb >= LB_L2)
                  and (upr is None or upr >= UP_RATIO))
    return bool(price_ok and breadth_ok)


def classify_signal(b, zt_by_industry, zt_by_concept, late):
    """提取板块字段并判定当下级别，返回信号 dict；chg 缺失返回 None。"""
    chg = b["chg"]
    if chg is None:
        return None
    zt_n = b.get("zt")
    if zt_n is None:
        zt_n = zt_by_industry.get(b["name"], 0) or zt_by_concept.get(b["name"], 0)
    speed, lb = b.get("speed"), b.get("lb")
    upr = up_ratio(b.get("up"), b.get("down"))
    speed_thr = SPEED_LATE if late else SPEED_L1
    hit_l1_speed, hit_l1_chg, base_lvl = meets_l1(chg, zt_n, speed, speed_thr)
    l2 = meets_l2(chg, zt_n, lb, upr)
    cur_lvl = "L2" if l2 else base_lvl
    return {"name": b["name"], "raw_code": b.get("code") or "",
            "code": b.get("code") or b["name"],
            "chg": chg, "zt": zt_n, "speed": speed, "lb": lb,
            "up": b.get("up"), "down": b.get("down"), "up_ratio": upr,
            "speed_thr": speed_thr, "hit_l1_speed": hit_l1_speed,
            "hit_l1_chg": hit_l1_chg, "l2": l2, "cur_lvl": cur_lvl}


def concentration(sig, st, n_zt):
    """计算主线占比、上一轮占比、以及'本轮是否首次成为主线'。"""
    zt_n = sig["zt"]
    conc_ratio = (zt_n / n_zt) if (n_zt and zt_n) else None
    prev_conc = (st or {}).get("conc") or 0.0
    first_conc = (conc_ratio is not None and conc_ratio >= CONCENTRATION
                  and prev_conc < CONCENTRATION)
    return conc_ratio, prev_conc, first_conc


def snapshot_fields(sig, conc_ratio, prev_conc, now):
    """每轮落盘的行情快照（供重启恢复与消退期追踪）。"""
    return {"chg": sig["chg"], "zt": sig["zt"], "lb": sig["lb"],
            "speed": sig["speed"],
            "conc": conc_ratio if conc_ratio is not None else prev_conc,
            "ts": now}


def settle_while_inactive(st, sig, snap, now):
    """情形一：当下不达标——进入/维持/结束消退宽限，此情形永不推送。"""
    if st is None:
        st = empty_board_state()
    if st.get("active"):
        if st.get("lost_ts") is None:
            st["lost_ts"] = now
        if sig["chg"] is not None:
            tc = st.get("trough_chg")
            st["trough_chg"] = sig["chg"] if tc is None else min(tc, sig["chg"])
        if (now - st["lost_ts"]).total_seconds() / 60.0 >= RELAPSE_GRACE_MIN:
            st["active"] = False
            st["lvl"] = "NONE"
            st["prev_wave_end_ts"] = now
            st["prev_wave_peak_zt"] = st.get("peak_zt", 0)
            st["lost_ts"] = None
    st.update(snap)
    return st


def close_expired_grace(st, now):
    """健壮性：上一波掉出阈值、消退已满宽限此刻才恢复，先结算上一波。"""
    if (st and st.get("active") and st.get("lost_ts") is not None and
            (now - st["lost_ts"]).total_seconds() / 60.0 >= RELAPSE_GRACE_MIN):
        st["active"] = False
        st["lvl"] = "NONE"
        st["prev_wave_end_ts"] = now
        st["prev_wave_peak_zt"] = st.get("peak_zt", 0)
        st["lost_ts"] = None


def begin_new_wave(st, sig, now):
    """非活跃→达标：当天首波，或上一波消退满宽限后的再度放量。"""
    old = st or {}
    wave = int(old.get("wave", 0)) + 1
    relaunch_gap = None
    if old.get("last_fire_ts") is not None:
        relaunch_gap = round((now - old["last_fire_ts"]).total_seconds() / 60.0, 1)
    cur_lvl, zt_n = sig["cur_lvl"], sig["zt"]
    st = {"wave": wave, "active": True, "lvl": cur_lvl,
          "fired_l1": cur_lvl == "L1", "fired_l2": cur_lvl == "L2",
          "fire_count": int(old.get("fire_count", 0)),
          "last_fire_ts": old.get("last_fire_ts"),
          "last_fire_lvl": old.get("last_fire_lvl"),
          "last_fire_zt": None, "last_fire_chg": None, "last_fire_lb": None,
          "peak_zt": zt_n, "lost_ts": None, "trough_chg": None,
          "trough_at_relaunch": old.get("trough_chg"),
          "wave_start_ts": now,
          "prev_wave_end_ts": old.get("prev_wave_end_ts")}
    stage_tag = "首次启动" if wave == 1 else f"再度启动·第{wave}波"
    return st, stage_tag, relaunch_gap


def advance_active_wave(st, sig, first_conc):
    """活跃波：清消退计时，仅在升级/成主线/走强台阶时再报；否则返回 None（静默）。"""
    st["lost_ts"] = None
    st["trough_chg"] = None
    cur_lvl, zt_n = sig["cur_lvl"], sig["zt"]
    prev_lvl = st.get("lvl")
    if prev_lvl == "L1" and cur_lvl == "L2" and not st.get("fired_l2"):
        return cur_lvl, "升级 L1→L2"
    if first_conc:
        return cur_lvl, "首次成为市场主线"
    if zt_n - int(st.get("last_fire_zt") or 0) >= STEP_ZT:
        return cur_lvl, "走强一步"
    return None


def delta_zt(bf_zt, zt_n):
    if bf_zt is None:
        return None
    dz = zt_n - int(bf_zt)
    return f"涨停 {int(bf_zt)}→{zt_n} 家（{dz:+d}）" if dz else None


def delta_chg(bf_chg, chg):
    if bf_chg is not None and abs(chg - bf_chg) >= 0.5:
        return f"涨幅 {bf_chg:+.2f}% → {chg:+.2f}%"
    return None


def delta_lb(bf_lb, lb):
    if bf_lb is not None and lb is not None and abs(lb - bf_lb) >= 0.2:
        return f"量比 {bf_lb:.2f} → {lb:.2f}"
    return None


def format_deltas(st, sig):
    """与'上一次收到的预警'相比的增量行（首次启动无对比基准则为空）。"""
    parts = [delta_zt(st.get("last_fire_zt"), sig["zt"]),
             delta_chg(st.get("last_fire_chg"), sig["chg"]),
             delta_lb(st.get("last_fire_lb"), sig["lb"])]
    deltas = [d for d in parts if d]
    return ("变化：" + "；".join(deltas)) if deltas else ""


def format_reasons(sig):
    """命中条件说明（写进消息，让用户一眼看到为什么报）。"""
    if sig["l2"]:
        return [f"满足L2：涨幅>{CHG_L1}% 且 涨停≥{ZT_L2} 且 量比≥{LB_L2} "
                f"且 上涨占比>{UP_RATIO*100:.0f}%"]
    reasons = []
    if sig["hit_l1_speed"]:
        reasons.append(f"涨速≥{sig['speed_thr']}%")
    if sig["hit_l1_chg"]:
        reasons.append(f"涨幅>{CHG_L1}% 且 涨停≥{ZT_L1}")
    return reasons


def format_tag_extra(stage_tag, fire_no, relaunch_gap, trough):
    """次数/波次/距上次预警：一眼看清这是今天第几次、为什么又发。"""
    if stage_tag and stage_tag.startswith("再度启动"):
        bits = [f"今日第{fire_no}次"]
        if relaunch_gap is not None:
            gap_txt = int(relaunch_gap) if float(relaunch_gap).is_integer() else relaunch_gap
            bits.append(f"距上次预警{gap_txt}分钟")
        extra = f"（{'，'.join(bits)}）"
        if trough is not None:
            extra += f"\n上一波消退期涨幅最低曾至 {trough:+.2f}%，本次为重新放量"
        return extra
    return f"（今日第{fire_no}次）" if fire_no > 1 else ""


def concentration_line(conc_ratio):
    if conc_ratio is None:
        return ""
    if conc_ratio >= CONCENTRATION:
        return f"\n⭐ 当前市场主线（占今日涨停 {conc_ratio*100:.0f}%，≥{CONCENTRATION*100:.0f}%）"
    return (f"\n占今日涨停 {conc_ratio*100:.0f}%"
            f"（主线阈值≥{CONCENTRATION*100:.0f}%，暂非主线）")


def metrics_line(sig, n_zt, hold_tags):
    """消息正文中的行情指标行。"""
    chg, zt_n = sig["chg"], sig["zt"]
    speed, lb, upr = sig["speed"], sig["lb"], sig["up_ratio"]
    line = f"涨幅 {chg:+.2f}% ｜ 涨停 {zt_n} 家"
    if speed is not None:
        line += f" ｜ 涨速 {speed:+.2f}%"
    if lb is not None:
        line += f" ｜ 量比 {lb:.2f}"
    if upr is not None:
        line += f" ｜ 上涨占比 {upr*100:.0f}%"
    if n_zt:
        line += f" ｜ 全市场涨停 {n_zt}"
    if hold_tags:
        line += f" ｜ {'/'.join(hold_tags)}"
    return line


def build_alert_message(sig, n_zt, conc_ratio, level, stage_tag,
                        fire_no, relaunch_gap, st, now, hold_tags, hint_line):
    """组装推送标题与正文（纯函数；持仓标签与未配置提示由 monitor 传入）。"""
    delta_line = format_deltas(st, sig)
    reasons = format_reasons(sig)
    conc_line = concentration_line(conc_ratio)
    tag_extra = format_tag_extra(stage_tag, fire_no, relaunch_gap,
                                 st.get("trough_at_relaunch"))
    head = now.strftime("%H:%M")
    msg = (f"[{head}] {stage_tag} · {level}{tag_extra}\n"
           + (delta_line + "\n" if delta_line else "")
           + f"触发：{' ＋ '.join(reasons)}\n"
           + metrics_line(sig, n_zt, hold_tags)
           + conc_line + hint_line)
    title = f"{'🔴' if level == 'L2' else '🟠'} {sig['name']} · {stage_tag}"
    return title, msg
