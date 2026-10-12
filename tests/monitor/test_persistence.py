# -*- coding: utf-8 -*-
"""persistence 层：台账、快照、对账样本、板块状态、停机状态、单例锁。"""
import os
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import src.monitor
from src.monitor import persistence as P
from src.monitor.config import STATE_FILE
from src.monitor import config

PROJECT_ROOT = Path(src.monitor.__file__).resolve().parents[2]


def test_append_alert_header_and_row():
    row = {c: "" for c in P.ALERT_COLS}
    row.update({"时间": "10:00:00", "级别": "L2", "板块名": "A", "消息": "m"})
    P.append_alert(row)
    f = config.ALERT_DIR / f"{datetime.now():%Y-%m-%d}.csv"
    text = f.read_text(encoding="utf-8-sig")
    assert "级别" in text and "A" in text and "m" in text


def test_save_snapshot_new_file_and_empty():
    now = datetime(2026, 10, 12, 10, 0)
    P.save_snapshot(now, [{"code": "BK1", "name": "A", "chg": 3.0, "zt": 5,
                           "speed": 1.0, "lb": 2.0, "up": 8, "down": 2}], "eastmoney")
    f = config.SNAP_DIR / "2026-10-12.csv"
    assert "板块名" in f.read_text(encoding="utf-8-sig")
    # boards 为空直接返回，不新增内容
    before = f.read_text(encoding="utf-8-sig")
    P.save_snapshot(now, [], "eastmoney")
    assert f.read_text(encoding="utf-8-sig") == before


def test_append_diff_rows():
    P.append_diff_rows(datetime(2026, 10, 13), [["ts", "A", 2.6, 2.4, 0.2, 3, 3,
                                                 1, 1, 1, 1]])
    f = config.DIFF_DIR / "2026-10-13.csv"
    assert "板块名" in f.read_text(encoding="utf-8-sig")


def test_board_state_roundtrip_filters_empty():
    now = datetime(2026, 10, 12, 10, 0)
    states = {
        "BK1": {"fire_count": 2, "active": True, "wave": 1, "ts": now,
                "last_fire_ts": now, "lost_ts": None},
        "BK_EMPTY": {"fire_count": 0, "active": False, "wave": 0},
    }
    P.save_board_states(states, day="2026-10-12")
    loaded = P.load_board_states(day="2026-10-12")
    assert set(loaded) == {"BK1"}
    assert loaded["BK1"]["ts"] == now and loaded["BK1"]["last_fire_ts"] == now


def test_load_board_states_missing_and_corrupt():
    assert P.load_board_states(day="1999-01-01") == {}
    p = P.board_state_path(day="2050-01-01")
    p.write_text("{not json", encoding="utf-8")
    assert P.load_board_states(day="2050-01-01") == {}


def test_last_scan_roundtrip():
    STATE_FILE.unlink(missing_ok=True)  # 排除其它用例共享 MON_ROOT 的残留
    assert P.load_last_scan() is None
    P.save_last_scan("2026-10-12 10:00:00")
    assert P.load_last_scan() == "2026-10-12 10:00:00"
    P.save_last_scan()  # 默认当前时间
    assert P.load_last_scan() is not None


def test_load_last_scan_corrupt():
    STATE_FILE.write_text("bad", encoding="utf-8")
    assert P.load_last_scan() is None


def test_single_instance_blocks_second_process():
    fp = P.single_instance()
    assert hasattr(fp, "fileno")
    # 另起进程持同一锁文件，应触发 SystemExit（exit code 0）
    code = (
        "import os,sys; sys.path.insert(0, os.getcwd());"
        "from src.monitor import persistence as P; P.single_instance()"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env=dict(os.environ), cwd=str(PROJECT_ROOT))
    # 子进程继承 MON_ROOT，定位同一锁文件；持锁被占 -> 内部 sys.exit(0)
    assert r.returncode == 0


# ---------------- 变异边界加固 ----------------
def test_append_alert_header_written_once():
    import csv as _csv
    row = {c: "" for c in P.ALERT_COLS}
    P.append_alert(row)
    P.append_alert(row)
    f = config.ALERT_DIR / f"{datetime.now():%Y-%m-%d}.csv"
    rows = list(_csv.reader(open(f, encoding="utf-8-sig")))
    assert sum(1 for r in rows if r and r[0] == "时间") == 1   # 追加不重复表头（杀 delete not）


def test_snapshot_blank_code_and_header_once():
    import csv as _csv
    now = datetime(2026, 10, 14, 10, 0)
    b = {"code": None, "name": "A", "chg": 1.0, "zt": 0,
         "speed": None, "lb": None, "up": 0, "down": 0}
    P.save_snapshot(now, [b], "tx")
    P.save_snapshot(now, [b], "tx")
    rows = list(_csv.reader(open(config.SNAP_DIR / "2026-10-14.csv", encoding="utf-8-sig")))
    assert sum(1 for r in rows if r and r[0] == "时间") == 1   # 表头唯一（杀 new_file 的 delete not）
    data = [r for r in rows if r and r[0] != "时间"]
    assert len(data) == 2 and all(r[2] == "" for r in data)    # 无 code 落空串而非 None（杀 or -> and）


def test_save_board_states_empty_active_only_chinese():
    states = {
        "EMPTY": {},                              # 无任何键：按默认 0 过滤掉（杀 get 默认 0 -> 1）
        "ACT": {"active": True},                  # 仅 active 为真也必须保留（杀 or -> and）
        "CN": {"active": True, "wave": 1, "name": "出版"},  # 中文必须原文落盘（杀 ensure_ascii）
    }
    P.save_board_states(states, day="2026-10-15")
    text = P.board_state_path("2026-10-15").read_text(encoding="utf-8")
    loaded = json.loads(text)
    assert "EMPTY" not in loaded and "ACT" in loaded and "CN" in loaded
    assert "出版" in text and "\\u" not in text


def test_snapshot_first_write_starts_with_header():
    import csv as _csv
    now = datetime(2026, 10, 16, 10, 0)
    P.save_snapshot(now, [{"code": "BK9", "name": "Z", "chg": 1.0, "zt": 0,
                           "speed": None, "lb": None, "up": 0, "down": 0}], "tx")
    rows = list(_csv.reader(open(config.SNAP_DIR / "2026-10-16.csv", encoding="utf-8-sig")))
    assert rows[0][0] == "时间" and rows[1][2] == "BK9"   # 全新文件首行必为表头（杀 delete not）
