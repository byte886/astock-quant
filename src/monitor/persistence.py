# -*- coding: utf-8 -*-
"""持久化层：预警台账、每轮快照、双源对账样本、板块状态机、停机状态、单例锁。

所有写盘失败都只记日志、绝不抛给扫描主流程（监控可用性优先）。
"""
import csv
import json
import fcntl
import sys
from datetime import datetime

from .config import (WORKSPACE, ALERT_DIR, SNAP_DIR, DIFF_DIR,
                     STATE_FILE, LOCK_FILE, log)

ALERT_COLS = ["时间", "级别", "板块类型", "板块代码", "板块名", "涨幅%", "涨停数",
              "涨速%", "量比", "上涨占比", "主力净流入(元)", "命中持仓", "命中候选",
              "阈值快照", "消息"]
SNAP_HEADER = ["时间", "数据源", "板块代码", "板块名", "涨幅%", "涨停数",
               "涨速%", "量比", "上涨家数", "下跌家数"]
DIFF_HEADER = ["时间", "板块名", "东财涨幅%", "腾讯涨幅%", "涨幅差(东-腾)",
               "东财涨停", "腾讯涨停", "东财上涨", "腾讯上涨", "东财下跌", "腾讯下跌"]

# 板块状态里需要在落盘/读回时做 datetime <-> isoformat 转换的字段
BOARD_TIME_FIELDS = ("ts", "lost_ts", "last_fire_ts", "wave_start_ts", "prev_wave_end_ts")


def append_alert(row):
    """把一条预警追加到当日台账 CSV（首次写入带表头）。"""
    p = ALERT_DIR / f"{datetime.now():%Y-%m-%d}.csv"
    exists = p.exists()
    with open(p, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALERT_COLS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def save_snapshot(now, boards, source):
    """把本轮全板块指标追加到当日快照 CSV；只用于事后复盘，失败仅记日志。"""
    if not boards:
        return
    try:
        f = SNAP_DIR / f"{now:%Y-%m-%d}.csv"
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        rows = [[ts, source, b.get("code") or "", b.get("name"), b.get("chg"),
                 b.get("zt"), b.get("speed"), b.get("lb"), b.get("up"), b.get("down")]
                for b in boards]
        new_file = not f.exists()
        with open(f, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(SNAP_HEADER)
            w.writerows(rows)
    except Exception as e:
        log(f"[snapshot] 快照写入失败（不影响监测）：{type(e).__name__}: {e}")


def append_diff_rows(now, rows):
    """把一批双源对账行追加到当日对账 CSV（首次写入带表头）。"""
    f = DIFF_DIR / f"{now:%Y-%m-%d}.csv"
    new_file = not f.exists()
    with open(f, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(DIFF_HEADER)
        w.writerows(rows)


def board_state_path(day=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    return WORKSPACE / f"board_states_{day}.json"


def load_board_states(day=None):
    """读回当天板块状态（时间字段反序列化为 datetime）；无文件/损坏返回 {}。"""
    states = {}
    try:
        p = board_state_path(day)
        if not p.exists():
            return states
        raw = json.loads(p.read_text(encoding="utf-8"))
        for k, v in raw.items():
            for fld in BOARD_TIME_FIELDS:
                if v.get(fld):
                    try:
                        v[fld] = datetime.fromisoformat(v[fld])
                    except Exception:
                        v[fld] = None
            states[k] = v
        log(f"[状态机] 已恢复当日板块状态 {len(states)} 个（重启不重复推送、保留次数/波次）")
    except Exception:
        log("[状态机] 恢复当日状态失败，按空状态启动：\n" + _traceback())
    return states


def save_board_states(states, day=None):
    """落盘板块状态；只存真正触发过/在波的板块，避免写几百个空状态。"""
    try:
        out = {}
        for k, v in states.items():
            if not (v.get("fire_count", 0) or v.get("active") or v.get("wave", 0)):
                continue
            out[k] = {kk: (vv.isoformat() if isinstance(vv, datetime) else vv)
                      for kk, vv in v.items()}
        board_state_path(day).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def single_instance():
    """fcntl 文件锁单例；已有实例运行则退出进程，返回持有的锁文件句柄。"""
    fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("已有监测实例在运行，退出。")
        sys.exit(0)
    return fp


def load_last_scan():
    """读取最近一次正常扫描时间（'%Y-%m-%d %H:%M:%S'）；无记录/损坏返回 None。"""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_scan")
    except Exception:
        return None
    return None


def save_last_scan(ts_str=None):
    """写入最近一次正常扫描时间；默认取当前时间。"""
    try:
        ts = ts_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATE_FILE.write_text(json.dumps({"last_scan": ts}, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


def _traceback():
    import traceback
    return traceback.format_exc()
