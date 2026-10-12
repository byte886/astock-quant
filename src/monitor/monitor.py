# -*- coding: utf-8 -*-
"""编排层：一轮扫描的取数降级链、涨停归集、逐板块状态机评估、推送与落盘副作用。

纯判定/文案全部在 statemachine；网络在 datasources/aggregator；对账在 reconcile；
推送在 notifier；读写盘在 persistence。本层只负责把它们串起来。
"""
import os
from datetime import datetime, time as dtime

from . import statemachine as sm
from .config import RECONCILE_INTERVAL_SEC, THRESHOLD_SNAP, log
from .codes import load_csv_codes, default_holdings_path, default_pool_path
from .datasources import (em_boards, tx_spot, zt_pool, sina_boards,
                          build_sector_map)
from .aggregator import tx_board_agg
from .reconcile import reconcile_sources
from .notifier import push
from .persistence import (append_alert, save_snapshot,
                          load_board_states, save_board_states)


class Monitor:
    def __init__(self):
        self.holdings = load_csv_codes(
            os.environ.get("HOLDINGS_CSV", default_holdings_path()))
        self.pool = load_csv_codes(default_pool_path())
        self.hint_shown = False      # 当天是否已提示过"未配置持仓"
        self.last_reconcile = None   # 上次双源对账采样时间（东财可用时限频）
        # 恢复当日板块状态（重启不把持续板块当新启动、保留次数/波次）
        self.states = load_board_states()

    # ---------- 持仓提示 ----------
    def _hold_hint(self):
        """持仓标签，以及'未配置持仓'的一次性提示。"""
        hold_tags = [f"持仓{len(self.holdings)}"] if self.holdings else []
        hint_line = ""
        if not self.holdings and not self.hint_shown:
            self.hint_shown = True
            hint_line = ("\n💡 未配置持仓：想让板块爆发时提醒你手里的票？"
                         "可在 config/holdings.csv 填入持仓代码即可开启")
        return hold_tags, hint_line

    # ---------- 取数降级链 ----------
    @staticmethod
    def _sina_fallback_boards():
        """腾讯/映射不可用时的新浪板块保底（名称匹配，缺涨停/量比）。"""
        sina = sina_boards("概念") or []
        sina += sina_boards("新浪行业") or []
        boards = [{"code": None, "name": b["name"], "chg": b["chg"],
                   "amount": b["amount"], "lb": None, "speed": None} for b in sina]
        return boards, ("sina" if boards else None)

    def _fetch_boards(self, today):
        """取板块行情，按 东财→腾讯聚合→新浪兜底 降级；返回 (boards, source)。"""
        boards = (em_boards("concept") or []) + (em_boards("industry") or [])
        if boards:
            return boards, "eastmoney"
        log("[scan] 东财板块无数据，尝试腾讯 spot 聚合兜底")
        try:
            smap_now = build_sector_map(today)
        except Exception:
            smap_now = {}
        tx_boards = tx_board_agg(smap_now, tx_spot())
        if tx_boards:
            log(f"[scan] 腾讯聚合兜底成功：{len(tx_boards)} 板块（维度完整）")
            return tx_boards, "tencent_agg"
        log("[scan] 腾讯聚合兜底失败，启用新浪板块兜底")
        return self._sina_fallback_boards()

    # ---------- 涨停归集 ----------
    @staticmethod
    def _build_zt_index(zt):
        """由涨停池建 行业涨停计数、涨停代码集合、涨停总数。"""
        zt_by_industry, zt_codes = {}, set()
        for z in zt:
            zt_codes.add(z["code"])
            if z.get("industry"):
                zt_by_industry[z["industry"]] = zt_by_industry.get(z["industry"], 0) + 1
        return zt_by_industry, zt_codes, len(zt)

    @staticmethod
    def _build_concept_zt(zt_codes, sector_map):
        """由涨停代码反查所属概念，统计每概念涨停数。"""
        zt_by_concept = {}
        for c in zt_codes:
            for tag in sector_map.get(c, []):
                kind, _, nm = tag.partition(":")
                if kind == "concept":
                    zt_by_concept[nm] = zt_by_concept.get(nm, 0) + 1
        return zt_by_concept

    @staticmethod
    def _fill_zt(boards, zt_by_industry, zt_by_concept):
        """东财板块无现成涨停数，用涨停池按行业/概念归集补齐。"""
        for b in boards:
            if b.get("zt") is None:
                b["zt"] = zt_by_industry.get(b["name"], 0) or zt_by_concept.get(b["name"], 0)

    def _maybe_reconcile(self, now, source, boards, sector_map):
        """东财可用时低频做一次双源口径对账（纯观测，不影响信号）。"""
        if source != "eastmoney":
            return
        if (self.last_reconcile is not None
                and (now - self.last_reconcile).total_seconds() < RECONCILE_INTERVAL_SEC):
            return
        reconcile_sources(now, boards, sector_map)
        self.last_reconcile = now

    # ---------- 一轮扫描 ----------
    def scan_once(self):
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        late = now.time() >= dtime(14, 30)

        boards, source = self._fetch_boards(today)

        zt = zt_pool(today) or []
        zt_by_industry, zt_codes, n_zt = self._build_zt_index(zt)

        try:
            sector_map = build_sector_map(today)
        except Exception:
            sector_map = {}
        zt_by_concept = self._build_concept_zt(zt_codes, sector_map)

        if not boards:
            log("[scan] 全部数据源均不可用，本轮无板块数据")
            return None
        self._fill_zt(boards, zt_by_industry, zt_by_concept)
        save_snapshot(now, boards, source)

        for b in boards:
            self._eval_board(b, zt_by_industry, zt_by_concept, n_zt, late, now)

        self._maybe_reconcile(now, source, boards, sector_map)
        save_board_states(self.states)
        return source

    # ---------- 单板块评估（编排状态机纯函数） ----------
    def _eval_board(self, b, zt_by_industry, zt_by_concept, n_zt, late, now):
        """对单个板块做一轮评估：分级 → 宽限/波次 → 决定推送则交 _emit_alert。"""
        sig = sm.classify_signal(b, zt_by_industry, zt_by_concept, late)
        if sig is None:
            return
        key = sig["code"]
        st = self.states.get(key)
        conc_ratio, prev_conc, first_conc = sm.concentration(sig, st, n_zt)
        snap = sm.snapshot_fields(sig, conc_ratio, prev_conc, now)

        # 情形一：当下不达标 → 进入/维持/结束"消退宽限"，永不推送
        if sig["cur_lvl"] == "NONE":
            self.states[key] = sm.settle_while_inactive(st, sig, snap, now)
            return

        # 情形二：当下达标（先健壮结算可能已满宽限的上一波）
        sm.close_expired_grace(st, now)
        relaunch_gap = None
        if not st or not st.get("active"):
            st, stage_tag, relaunch_gap = sm.begin_new_wave(st, sig, now)
            level = sig["cur_lvl"]
        else:
            decision = sm.advance_active_wave(st, sig, first_conc)
            if decision is None:
                # 同一波、无实质台阶（含宽限期内抖动回来）：静默，只刷新快照
                st["lvl"] = sig["cur_lvl"]
                st["peak_zt"] = max(int(st.get("peak_zt") or 0), sig["zt"])
                st.update(snap)
                self.states[key] = st
                return
            level, stage_tag = decision

        self._emit_alert(key, sig, n_zt, conc_ratio, level, stage_tag,
                         relaunch_gap, st, snap, now)

    def _emit_alert(self, key, sig, n_zt, conc_ratio, level, stage_tag,
                    relaunch_gap, st, snap, now):
        """本轮确定推送：计数 → 组装消息 → 推送/落台账 → 回写状态（副作用集中处）。"""
        st["fire_count"] = int(st.get("fire_count", 0)) + 1
        fire_no = st["fire_count"]
        if level == "L2":
            st["fired_l2"] = True
        if level == "L1":
            st["fired_l1"] = True
        chg, zt_n, speed, lb, upr = (sig["chg"], sig["zt"], sig["speed"],
                                     sig["lb"], sig["up_ratio"])
        hold_tags, hint_line = self._hold_hint()
        title, msg = sm.build_alert_message(
            sig, n_zt, conc_ratio, level, stage_tag, fire_no,
            relaunch_gap, st, now, hold_tags, hint_line)
        push(title, msg)
        append_alert({
            "时间": datetime.now().strftime("%H:%M:%S"), "级别": level,
            "板块类型": "概念/行业", "板块代码": sig["raw_code"], "板块名": sig["name"],
            "涨幅%": round(chg, 2), "涨停数": zt_n,
            "涨速%": round(speed, 2) if speed is not None else "",
            "量比": round(lb, 2) if lb is not None else "",
            "上涨占比": round(upr * 100) if upr is not None else "",
            "主力净流入(元)": "",
            "命中持仓": ",".join(self.holdings.values()) if level else "",
            "命中候选": "", "阈值快照": THRESHOLD_SNAP, "消息": msg,
        })
        st.update({"lvl": level, "last_fire_ts": now, "last_fire_lvl": level,
                   "last_fire_zt": zt_n, "last_fire_chg": chg,
                   "last_fire_lb": lb,
                   "peak_zt": max(int(st.get("peak_zt") or 0), zt_n)})
        st.update(snap)
        self.states[key] = st
        log(f"[{level}|{stage_tag}|第{fire_no}次|W{st.get('wave')}] {sig['name']} {msg}")
