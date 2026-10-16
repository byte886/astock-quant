# -*- coding: utf-8 -*-
"""持仓盯盘（需求二 #38）纯计算测试：全部喂构造数据，不碰网络/推送。"""
from datetime import datetime

import pandas as pd

from src.monitor import holdings as H
from src.monitor.aggregator import is_dt_stock, limit_pct


def _spot(rows):
    return pd.DataFrame(rows, columns=["code", "name", "zxj", "zdf", "lb", "speed"])


# ---------------- 状态标记 ----------------
def test_flag_zt_dt_drop_normal():
    assert H._flag(10.0, "sh600000") == "涨停"     # 10cm 触板
    assert H._flag(9.8, "sh600000") is None
    assert H._flag(19.9, "sz300001") == "涨停"    # 创业板 20cm
    assert H._flag(-9.9, "sh600000") == "跌停"
    assert H._flag(-19.9, "sh688001") == "跌停"   # 科创 20cm 跌停
    assert H._flag(-7.0, "sh600000") == "大跌"
    assert H._flag(-6.9, "sh600000") is None
    assert H._flag(2.0, "sh600000") is None
    assert H._flag(None, "sh600000") is None


def test_limit_pct_and_dt_by_board():
    assert limit_pct("sh600000") == 9.9
    assert limit_pct("sz300001") == 19.9
    assert limit_pct("sh688001") == 19.9
    assert limit_pct("bj830799") == 19.9
    assert is_dt_stock(-9.91, "sh600000") is True
    assert is_dt_stock(-9.0, "sh600000") is False


# ---------------- 组装 ----------------
def test_build_holdings_rows_missing_sort_pool():
    spot = _spot([
        ["sh600111", "北方稀土", 20.0, 9.9, 2.0, 0.5],    # 涨停
        ["sh600000", "浦发银行", 10.0, -8.0, 1.2, 0.1],   # 大跌，应排最前
        ["sz000001", "平安银行", 12.0, 1.0, 0.9, -0.2],
    ])
    h = {"sh600000": "浦发", "sz000001": "平安", "sh600111": "北方稀土",
         "sh600999": "缺失票"}
    pool = {"sz000001": "平安"}
    mapping = {"sh600000": ["industry:银行"], "sz000001": ["industry:银行"],
               "sh600111": ["industry:稀土", "concept:永磁"]}
    agg = [{"name": "银行", "chg": -1.0, "zt": 0},
           {"name": "稀土", "chg": 3.2, "zt": 3}]
    fired = {"CRO概念": {"count": 2, "level": "L2"}}

    rows, missing = H.build_holdings(h, pool, spot, mapping, agg, fired)

    assert missing == ["缺失票"]
    # 大跌(浦发) -> 涨停(北方) -> 普通(平安)
    assert [r["name"] for r in rows] == ["浦发银行", "北方稀土", "平安银行"]
    pf = rows[0]
    assert pf["flag"] == "大跌" and pf["price"] == 10.0 and pf["lb"] == 1.2
    assert pf["in_pool"] is False
    assert rows[2]["in_pool"] is True
    # 行业带当前行情；概念仅在今日已触发时带出（永磁未报、CRO 与本票无关）
    north_tags = [b["name"] for b in rows[1]["boards"]]
    assert "稀土" in north_tags and "永磁" not in north_tags
    # 银行板块今日平淡，fired 为 None
    bank = next(b for b in rows[0]["boards"] if b["name"] == "银行")
    assert bank["fired"] is None and bank["chg"] == -1.0


def test_build_holdings_empty_spot():
    rows, missing = H.build_holdings({"sh600000": "浦发"}, {}, None, {}, [], {})
    assert rows == [] and missing == ["浦发"]


# ---------------- 板块异动汇总 ----------------
def test_collect_board_moves_dedup_and_filter():
    rows = [
        {"name": "甲", "boards": [
            {"name": "半导体设备", "chg": 3.4, "zt": 1, "fired": {"count": 2, "level": "L1"}},
            {"name": "平淡板块", "chg": 0.1, "zt": 0, "fired": None},
            {"name": "下跌脉冲", "chg": -1.5, "zt": 3, "fired": None}]},
        {"name": "乙", "boards": [
            {"name": "半导体设备", "chg": 3.4, "zt": 1, "fired": {"count": 2, "level": "L1"}},
            {"name": "走强未报", "chg": 2.8, "zt": 0, "fired": None}]},
    ]
    txt = "\n".join(H.collect_board_moves(rows))
    assert "半导体设备" in txt and "甲、乙" in txt and "今日L1·2次" in txt
    assert "走强未报" in txt and "当前走强" in txt
    assert "平淡板块" not in txt
    assert "下跌脉冲" not in txt      # 板块整体跌、仅 3 家涨停，不算走强


def test_collect_board_moves_none_when_quiet():
    rows = [{"name": "甲", "boards": [
        {"name": "银行", "chg": -0.5, "zt": 0, "fired": None}]}]
    assert H.collect_board_moves(rows) == []


# ---------------- 渲染 ----------------
def test_format_holdings_basic_sections():
    rows = [{"code": "sh600000", "name": "浦发银行", "price": 10.0, "zdf": -7.5,
             "lb": 1.2, "speed": 0.1, "flag": "大跌", "in_pool": False,
             "boards": [{"name": "银行", "chg": -0.5, "zt": 0, "fired": None}]}]
    msg = H.format_holdings(rows, [])
    assert "腾讯快照 · 1只持仓" in msg
    assert "浦发银行" in msg and "大跌" in msg and "量比1.2" in msg
    assert "持仓盯盘 ·" not in msg          # 标题由 notifier 统一加粗，正文不重复
    assert "🔥 所属板块异动" not in msg      # 银行平淡，不出异动区


def test_format_holdings_fired_board_missing_star():
    rows = [{"code": "sz000001", "name": "平安银行", "price": 12.0, "zdf": 1.0,
             "lb": 0.9, "speed": -0.2, "flag": None, "in_pool": True,
             "boards": [{"name": "CRO概念", "chg": 3.6, "zt": 4,
                         "fired": {"count": 3, "level": "L2"}}]}]
    msg = H.format_holdings(rows, ["缺失票"])
    assert "平安银行 ⭐" in msg and "⭐=同时在3号池" in msg
    assert "未取到快照：缺失票" in msg
    assert "CRO概念" in msg and "今日L2·3次" in msg and "+3.60%" in msg


def test_format_holdings_empty():
    assert H.format_holdings([], []) is None


def test_session_label():
    assert H.session_label(datetime(2026, 10, 15, 9, 45)) == "上午盘"
    assert H.session_label(datetime(2026, 10, 15, 13, 5)) == "下午盘"


def test_session_label_noon_boundary():
    assert H.session_label(datetime(2026, 10, 15, 11, 59, 59)) == "上午盘"
    assert H.session_label(datetime(2026, 10, 15, 12, 0, 0)) == "下午盘"
    assert H.session_label(datetime(2026, 10, 15, 12, 0, 30)) == "下午盘"


def test_spot_index_none_empty_and_rows():
    assert H._spot_index(None) == {}
    assert H._spot_index(pd.DataFrame(columns=["code", "name"])) == {}
    idx = H._spot_index(_spot([["sh600000", "浦发", 1, 1, 1, 1]]))
    assert list(idx.keys()) == ["sh600000"]


def test_num_nan_bad_and_normal():
    assert H._num("3.5") == 3.5
    assert H._num(None) is None
    assert H._num(float("nan")) is None
    assert H._num("-") is None
    assert H._num("abc") is None


def test_tag_names_prefixes_and_none():
    assert H._tag_names(["industry:银行", "concept:永磁"], "industry:") == ["银行"]
    assert H._tag_names(["industry:银行"], "concept:") == []
    assert H._tag_names(None, "industry:") == []


def test_board_notable_boundary_and_fired():
    assert H._board_is_notable({"chg": 2.5, "zt": 0, "fired": None}) is True    # 恰好 2.5
    assert H._board_is_notable({"chg": 2.49, "zt": 0, "fired": None}) is False
    assert H._board_is_notable({"chg": -1.0, "zt": 5, "fired": None}) is False  # 下跌脉冲
    assert H._board_is_notable({"chg": -1.0, "zt": 5,
                                "fired": {"count": 1, "level": "L1"}}) is True


def test_collect_moves_fired_without_zt():
    # 今日已触发但当前涨停为 0、涨幅为负：仍是事实记录，走 fired 分支但不带涨停
    rows = [{"name": "甲", "boards": [
        {"name": "焦炭Ⅱ", "chg": -6.0, "zt": 0, "fired": {"count": 1, "level": "L1"}}]}]
    lines = H.collect_board_moves(rows)
    assert len(lines) == 1 and "今日L1·1次" in lines[0] and "涨停" not in lines[0]


def test_build_holdings_filters_generic_concept_even_if_fired():
    # 泛化概念（融资融券）即便今日真实触发、行情很强，也不在持仓盯盘里复活；
    # 真概念（机器人）正常保留。
    spot = _spot([["sh600000", "浦发银行", 10.0, 1.0, 1.0, 0.1]])
    mapping = {"sh600000": ["industry:银行", "concept:融资融券", "concept:机器人"]}
    agg = [{"name": "银行", "chg": 0.2, "zt": 0},
           {"name": "融资融券", "chg": 4.5, "zt": 4},
           {"name": "机器人", "chg": 4.8, "zt": 3}]
    fired = {"融资融券": {"count": 1, "level": "L1"},
             "机器人": {"count": 1, "level": "L1"}}
    rows, _ = H.build_holdings({"sh600000": "浦发"}, {}, spot, mapping, agg, fired)
    moves = "\n".join(H.collect_board_moves(rows))
    assert "融资融券" not in moves
    assert "机器人" in moves


def test_collect_moves_fired_first_then_chg_desc_and_cap():
    boards = [
        {"name": "F1", "chg": 3.0, "zt": 0, "fired": {"count": 1, "level": "L1"}},
        {"name": "F2", "chg": 1.0, "zt": 0, "fired": {"count": 2, "level": "L2"}},
    ]
    boards += [{"name": f"S{i}", "chg": 2.5 + i, "zt": 0, "fired": None}
               for i in range(10)]
    moves = H.collect_board_moves([{"name": "甲", "boards": boards}])
    assert len(moves) == H.HOLD_BOARD_MAX_MOVES == 10
    assert moves[0].startswith("· F1") and moves[1].startswith("· F2")  # 今日已触发优先
    assert "S9" in moves[2] and "S2" in moves[9]                        # 其余按涨幅降序取前8
    assert "S0" not in "\n".join(moves) and "S1" not in "\n".join(moves)


def test_move_sort_key_none_chg_is_negative_infinity():
    # 同一"已触发"组内：涨幅缺失（None）视为负无穷，必须排在有涨幅板块之后
    fired = {"count": 1, "level": "L1"}
    k_none = H._move_sort_key(("NULL", {"fired": fired, "chg": None}))
    k_low = H._move_sort_key(("LOW", {"fired": fired, "chg": 0.1}))
    k_high = H._move_sort_key(("HIGH", {"fired": fired, "chg": 5.0}))
    assert k_high < k_low < k_none                    # 降序：高涨幅在前，None 垫后
    # 但"已触发"仍是第一排序键：涨幅再高的未触发板块，也排在已触发的 None 之后
    k_quiet = H._move_sort_key(("Q", {"fired": None, "chg": 9.0}))
    assert k_none < k_quiet


# ---------------- 取数编排（mock 掉全部 IO） ----------------
def _patch_io(monkeypatch, push_calls, spot=None, holdings=None):
    monkeypatch.setattr(H, "load_csv_codes", lambda path:
                        {"sh600000": "浦发"} if holdings is None else holdings)
    monkeypatch.setattr(H, "tx_spot", lambda: _spot(spot) if spot is not None else None)
    monkeypatch.setattr(H, "build_sector_map", lambda today: {"sh600000": ["industry:银行"]})
    monkeypatch.setattr(H, "tx_board_agg", lambda m, s: [{"name": "银行", "chg": 0.5, "zt": 0}])
    monkeypatch.setattr(H, "load_fired_boards", lambda: {})
    monkeypatch.setattr(H.notifier, "push", lambda t, c: push_calls.append((t, c)))


def test_run_once_default_push(monkeypatch):
    calls = []
    _patch_io(monkeypatch, calls, spot=[["sh600000", "浦发", 10.0, 1.0, 1.0, 0.1]])
    title, content = H.run_once()                # 默认 push=True
    assert len(calls) == 1 and "浦发" in content
    assert title.startswith("📊")


def test_run_once_dry_does_not_push(monkeypatch):
    calls = []
    _patch_io(monkeypatch, calls, spot=[["sh600000", "浦发", 10.0, 1.0, 1.0, 0.1]])
    title, content = H.run_once(push=False)
    assert calls == [] and content is not None and title.startswith("📊")


def test_run_once_spot_failure_pushes_alert(monkeypatch):
    calls = []
    _patch_io(monkeypatch, calls, spot=None)
    title, content = H.run_once()
    assert len(calls) == 1 and "取数失败" in title and "板块监控不受影响" in content


def test_run_once_no_holdings_skips(monkeypatch):
    calls = []
    _patch_io(monkeypatch, calls, spot=[["sh600000", "浦发", 10.0, 1.0, 1.0, 0.1]],
              holdings={})
    assert H.run_once() == (None, None)
    assert calls == []


def test_run_once_logs_full_body_even_when_not_push(monkeypatch, capsys):
    calls = []
    _patch_io(monkeypatch, calls, spot=[["sh600000", "浦发", 10.0, 1.0, 1.0, 0.1]])
    H.run_once(push=False)                    # dry-run 不推送，但完整正文也要落日志
    out = capsys.readouterr().out
    assert calls == []
    assert "[holdings] 本次盯盘正文（push=False）" in out
    assert "浦发" in out and "持仓盯盘" in out


def test_main_dry_prints_and_passes_push_false(monkeypatch, capsys):
    got = {}
    monkeypatch.setattr(H, "run_once",
                        lambda push=True: (got.update(push=push) or ("t", "c")))
    assert H.main(["--dry-run"]) == 0
    assert got["push"] is False                    # dry-run 不推送
    assert "t" in capsys.readouterr().out          # 打印消息


def test_main_default_pushes_and_no_print(monkeypatch, capsys):
    got = {}
    monkeypatch.setattr(H, "run_once",
                        lambda push=True: (got.update(push=push) or ("t", "c")))
    assert H.main([]) == 0
    assert got["push"] is True                     # 默认推送
    assert capsys.readouterr().out == ""           # 非 dry 不打印


def test_main_none_argv_returns_1(monkeypatch):
    monkeypatch.setattr(H, "run_once", lambda push=True: (None, None))
    assert H.main() == 1                           # argv 默认 None 也能跑


def test_main_dry_no_content_no_print(monkeypatch, capsys):
    monkeypatch.setattr(H, "run_once", lambda push=True: (None, None))
    assert H.main(["--dry-run"]) == 1
    assert capsys.readouterr().out == ""


def test_format_stock_line_flags_and_star():
    vals = {"price": 10.0, "zdf": -1.0, "lb": 1.2, "speed": 0.3}
    # 有标记、无⭐
    only_flag = H._format_stock_line({"name": "甲", "flag": "大跌", "in_pool": False, **vals})
    assert "大跌" in only_flag and "⭐" not in only_flag and "10.00" in only_flag
    # 无标记、有⭐
    only_star = H._format_stock_line({"name": "乙", "flag": None, "in_pool": True, **vals})
    assert "⭐" in only_star and "跌停" not in only_star and "大涨" not in only_star
    # 涨停且有⭐
    zt = H._format_stock_line({"name": "丙", "flag": "涨停", "in_pool": True, **vals})
    assert "涨停" in zt and "⭐" in zt


def test_format_stock_line_positive_shows_plus_sign():
    # 正涨幅/正涨速必须带 "+"，量比固定 1 位小数（杀 sign 与 nd 变异）
    up = H._format_stock_line({"name": "丁", "flag": None, "in_pool": False,
                               "price": 10.0, "zdf": 2.5, "lb": 1.0, "speed": 1.2})
    assert "+2.50%" in up and "量比1.0" in up and "涨速+1.20" in up
    # 涨幅为负但涨速为正：只有涨速带 +，精准定位 speed 的 sign
    mixed = H._format_stock_line({"name": "戊", "flag": None, "in_pool": False,
                                  "price": 8.0, "zdf": -0.5, "lb": 2.0, "speed": 0.8})
    assert "-0.50%" in mixed and "涨速+0.80" in mixed and "量比2.0" in mixed


def test_format_stock_line_none_values_show_dash():
    line = H._format_stock_line({"name": "甲", "flag": None, "in_pool": False,
                                 "price": None, "zdf": None, "lb": None, "speed": None})
    assert line.count("—") >= 3


def test_stock_row_name_fallback():
    # spot 缺 name 时回落到 CSV 名称；各字段都正确取到
    r1 = {"zdf": 1.0, "zxj": 5.0, "lb": 1.0, "speed": 0.3}
    row = H._stock_row("sh600000", "浦发", r1, True)
    assert row["name"] == "浦发" and row["zdf"] == 1.0 and row["price"] == 5.0
    assert row["lb"] == 1.0 and row["speed"] == 0.3
    assert row["flag"] is None and row["in_pool"] is True and row["boards"] == []
    # CSV 名也为空时回落到代码
    r2 = {"name": "", "zdf": 10.0}
    assert H._stock_row("sh600000", "", r2, False)["name"] == "sh600000"
    # spot 自带名称时优先用现货名（短路，不看 CSV/代码）
    r3 = {"name": "现货名", "zdf": 1.0}
    assert H._stock_row("sh600000", "CSV名", r3, False)["name"] == "现货名"
