# -*- coding: utf-8 -*-
"""aggregator 层：腾讯个股 spot 等权聚合成板块行情。"""
import numpy as np
import pandas as pd
import pytest

from src.monitor.aggregator import (_is_zt_stock, tx_board_agg, _new_agg_bucket,
                                    _accumulate_price, _accumulate_volume,
                                    _accumulate_tx_row, _boards_from_buckets)


# 无前缀 6 位码：startswith 能正确进入 20cm 分支（刻画函数本身的分支逻辑）
@pytest.mark.parametrize("code,zdf,zt", [
    ("300001", 19.9, True), ("300001", 19.0, False),
    ("688001", 19.9, True), ("688001", 9.9, False),
    ("830001", 19.9, True), ("430001", 19.9, True),
    ("600001", 9.9, True), ("600001", 19.9, True), ("600001", 9.8, False),
    ("000001", 9.9, True), ("000001", 9.8, False),
    ("600001", None, False), ("600001", np.nan, False),
])
def test_is_zt_stock_plain_six_digit(code, zdf, zt):
    assert _is_zt_stock(zdf, code) is zt


# 生产真实输入是带前缀码（腾讯 spot：sz300308/sh688808/bj830xxx）。
# 锁定当前行为（ISSUES I16）：startswith("30"/"68"/"8") 对带前缀码不命中，
# 创业/科创/北交 20cm 票也落到 9.9% 分支 -> 9.9% 即被误计涨停（涨停数虚高）。
# 修复后本组三条 9.9% 应为 False；本次行为守恒，先锁定现状。
@pytest.mark.parametrize("code,zdf,zt", [
    ("sz300001", 9.9, True),
    ("sh688001", 9.9, True),
    ("bj830001", 9.9, True),
    ("sz300001", 19.9, True),
    ("sh600001", 9.9, True),
    ("sz000001", 9.8, False),
])
def test_is_zt_stock_prefixed_current_behavior(code, zdf, zt):
    assert _is_zt_stock(zdf, code) is zt


def _spot():
    return pd.DataFrame([
        {"code": "sh600001", "zdf": 10.0, "lb": 2.0, "speed": 3.0},   # A 涨停10cm
        {"code": "sz000002", "zdf": 2.0, "lb": 1.0, "speed": 0.5},    # A
        {"code": "sz000006", "zdf": -1.0, "lb": 1.5, "speed": -0.5},  # A 下跌
        {"code": "sz000007", "zdf": None, "lb": None, "speed": None}, # A 无效，不计
        {"code": "sz300003", "zdf": 19.9, "lb": 3.0, "speed": 4.0},   # B 创业涨停
        {"code": "sh688004", "zdf": 9.9, "lb": 1.0, "speed": 1.0},    # B 科创9.9不涨停
        {"code": "bj830005", "zdf": 19.9, "lb": 2.0, "speed": 2.0},   # C 北交涨停
    ])


def _mapping():
    return {
        "sh600001": ["concept:A"], "sz000002": ["concept:A"],
        "sz000006": ["concept:A"], "sz000007": ["concept:A"],
        "sz300003": ["concept:B"], "sh688004": ["concept:B"],
        "bj830005": ["industry:C"],
    }


def test_tx_board_agg_none_inputs():
    assert tx_board_agg({}, _spot()) is None
    assert tx_board_agg(_mapping(), None) is None
    assert tx_board_agg(_mapping(), pd.DataFrame()) is None


def test_tx_board_agg_values():
    out = tx_board_agg(_mapping(), _spot())
    by = {b["name"]: b for b in out}
    a = by["A"]
    assert round(a["chg"], 6) == round(11.0 / 3, 6)
    assert a["zt"] == 1
    assert a["up"] == 2 and a["down"] == 1
    assert round(a["lb"], 6) == round(4.5 / 3, 6)
    b = by["B"]
    # I16 锁定现状：两只均为带前缀的 20cm 码，9.9% 分支把它们都计为涨停 -> zt=2；
    # 修复 20cm 判定后此处应为 1（仅 sz300003@19.9 真涨停）。
    assert b["zt"] == 2 and b["up"] == 2
    assert round(b["chg"], 6) == round((19.9 + 9.9) / 2, 6)
    c = by["C"]
    assert c["zt"] == 1 and round(c["chg"], 6) == 19.9


def test_tx_board_agg_skips_unmapped():
    df = pd.DataFrame([{"code": "sz999999", "zdf": 5.0, "lb": 1.0, "speed": 1.0}])
    assert tx_board_agg(_mapping(), df) is None


def test_bucket_helpers():
    a = _new_agg_bucket()
    _accumulate_price(a, 10.0, "sh600001")
    _accumulate_price(a, np.nan, "sz000002")
    _accumulate_volume(a, 2.0, 3.0)
    assert a["n"] == 1 and a["zt"] == 1 and a["up"] == 1
    # 行累加：一只票属于两个板块
    board = {}
    row = {"zdf": 9.9, "lb": 1.0, "speed": 1.0}
    _accumulate_tx_row(board, "sh600001", ["concept:X", "industry:Y"], row)
    assert set(board) == {"X", "Y"}
    out = _boards_from_buckets({"空": _new_agg_bucket()})
    assert out is None


# ---------------- 变异边界加固 ----------------
def test_accumulate_price_zero_neither_up_nor_down():
    a = _new_agg_bucket()
    _accumulate_price(a, 0.0, "sh600001")   # 平盘：计 n，不计 up/down（杀 >->>= / <-><=）
    assert a["n"] == 1 and a["up"] == 0 and a["down"] == 0


def test_accumulate_price_small_positive_is_up_not_down():
    a = _new_agg_bucket()
    _accumulate_price(a, 0.5, "sh600001")   # 0<涨幅<1（杀 zdf>0 / zdf<0 的 0->1）
    assert a["up"] == 1 and a["down"] == 0


def test_accumulate_volume_nan_speed_not_added():
    a = _new_agg_bucket()
    _accumulate_volume(a, 1.0, np.nan)      # sp=NaN：lb 累加、speed 不累加也不污染（杀 and->or）
    assert a["lb_sum"] == 1.0 and a["speed_sum"] == 0.0


def test_accumulate_volume_valid_speed_added():
    a = _new_agg_bucket()
    _accumulate_volume(a, 1.0, 1.0)         # 有效 sp 必须累加（杀 delete not）
    assert a["speed_sum"] == 1.0
