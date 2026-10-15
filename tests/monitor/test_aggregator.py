# -*- coding: utf-8 -*-
"""aggregator 层：腾讯个股 spot 等权聚合成板块行情。"""
import numpy as np
import pandas as pd
import pytest

from src.monitor.aggregator import (_is_zt_stock, tx_board_agg, _new_agg_bucket,
                                    _accumulate_price, _accumulate_volume,
                                    _accumulate_tx_row, _boards_from_buckets,
                                    _redundant_level_tags, board_stock_detail)


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


# 生产真实输入是带前缀码（腾讯 spot：sz300308/sh688808/bj830xxx/bj920xxx）。
# ISSUES I16 已修复：判定前剥离 sh/sz/bj 前缀，创业/科创/北交 20cm 票按 19.9 判，
# 因此三条 9.9%（未达 20cm 涨停）均为 False；10cm 的 sh600/sz00 仍按 9.9 判涨停。
@pytest.mark.parametrize("code,zdf,zt", [
    ("sz300001", 9.9, False),    # 创业 20cm，9.9% 未涨停
    ("sh688001", 9.9, False),    # 科创 20cm，9.9% 未涨停
    ("bj830001", 9.9, False),    # 北交（按 20cm 桶），9.9% 未涨停
    ("bj920001", 9.9, False),    # 北交 920 新号段（按 20cm 桶），9.9% 未涨停
    ("sz300001", 19.8, False),   # 创业 20cm，差 0.1 未涨停
    ("sz300001", 19.9, True),
    ("sh688001", 19.9, True),
    ("bj830001", 19.9, True),
    ("bj920001", 19.9, True),
    ("sh600001", 9.9, True),     # 沪市 10cm，9.9% 涨停
    ("sz000001", 9.9, True),     # 深市 10cm，9.9% 涨停
    ("sz000001", 9.8, False),
    # —— 6 位裸码（无市场前缀）同样须按号段分桶，锁死 len(c) > 6 的边界 ——
    ("300001", 9.9, False),      # 裸码创业 20cm：9.9% 未涨停（若 len>6 误写成 >=6，裸码被砍前缀会落到 10cm 误判 True）
    ("300001", 19.9, True),
    ("688001", 9.9, False),      # 裸码科创 20cm
    ("600001", 9.9, True),       # 裸码沪市 10cm
    ("000001", 9.8, False),      # 裸码深市 10cm
])
def test_is_zt_stock_prefixed_after_fix(code, zdf, zt):
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
    # ISSUES I16 已修复：sh688004@9.9 是科创 20cm、未涨停，不计 zt；
    # 仅 sz300003@19.9 真涨停 -> zt=1（两只均上涨，up=2，涨幅均值不变）。
    assert b["zt"] == 1 and b["up"] == 2
    assert round(b["chg"], 6) == round((19.9 + 9.9) / 2, 6)
    c = by["C"]
    assert c["zt"] == 1 and round(c["chg"], 6) == 19.9


def test_tx_board_agg_skips_unmapped():
    df = pd.DataFrame([{"code": "sz999999", "zdf": 5.0, "lb": 1.0, "speed": 1.0}])
    assert tx_board_agg(_mapping(), df) is None


# ---------------- 东财二级/三级行业（XⅡ/XⅢ）层级去重 ----------------
def test_redundant_level_tags_drops_identical_iii():
    # 林业Ⅱ 与 林业Ⅲ 成分完全相同 -> 丢弃三级"林业Ⅲ"，保留二级"林业Ⅱ"
    mapping = {
        "sh600001": ["industry:林业Ⅱ", "industry:林业Ⅲ"],
        "sh600002": ["industry:林业Ⅱ", "industry:林业Ⅲ"],
    }
    assert _redundant_level_tags(mapping) == {"林业Ⅲ"}


def test_redundant_level_tags_keeps_distinct_ii_iii():
    # 一个二级下有多个三级、成分不同（Ⅱ为并集，Ⅲ为子集）-> 都保留，不误删
    mapping = {
        "sh600001": ["industry:传媒", "industry:游戏Ⅱ", "industry:游戏Ⅲ"],
        "sh600002": ["industry:传媒", "industry:游戏Ⅱ", "industry:影视Ⅲ"],
    }
    assert _redundant_level_tags(mapping) == set()


def test_tx_board_agg_dedupes_identical_level():
    # 同一批票同时挂 Ⅱ/Ⅲ：聚合只产出一个二级板块，不重复
    mapping = {
        "sh600001": ["industry:林业Ⅱ", "industry:林业Ⅲ"],
        "sh600002": ["industry:林业Ⅱ", "industry:林业Ⅲ"],
    }
    df = pd.DataFrame([
        {"code": "sh600001", "zdf": 3.0, "lb": 2.0, "speed": 1.6},
        {"code": "sh600002", "zdf": 1.0, "lb": 1.0, "speed": 1.4},
    ])
    names = {b["name"] for b in tx_board_agg(mapping, df)}
    assert "林业Ⅱ" in names and "林业Ⅲ" not in names


def test_tx_board_agg_keeps_distinct_level_and_concept():
    # 成分不同的 Ⅱ/Ⅲ 与概念标签都保留
    mapping = {
        "sh600001": ["industry:游戏Ⅱ", "industry:游戏Ⅲ", "concept:AI"],
        "sh600002": ["industry:游戏Ⅱ", "industry:影视Ⅲ"],
    }
    df = pd.DataFrame([
        {"code": "sh600001", "zdf": 3.0, "lb": 2.0, "speed": 1.6},
        {"code": "sh600002", "zdf": 1.0, "lb": 1.0, "speed": 1.4},
    ])
    names = {b["name"] for b in tx_board_agg(mapping, df)}
    assert {"游戏Ⅱ", "游戏Ⅲ", "影视Ⅲ", "AI"} <= names


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


# ---------------- 板块带动个股明细（涨停封板 + 未涨停领涨，需求一 #37） ----------------
def _detail_zt():
    """元件板块涨停池：中伟3板、生益2板（行业字段缺失，靠 mapping 反查）、崇达首板（重复一次）；另含一只银行票。"""
    return [
        {"code": "sz300919", "name": "中伟股份", "lianban": 3, "industry": "元件"},
        {"code": "sh600183", "name": "生益科技", "lianban": 2, "industry": None},
        {"code": "sz002815", "name": "崇达技术", "lianban": 1, "industry": "元件"},
        {"code": "sz002815", "name": "崇达技术", "lianban": 1, "industry": "元件"},  # 重复，去重
        {"code": "sh600000", "name": "浦发银行", "lianban": 5, "industry": "银行"},  # 非本板块
    ]


def _detail_mapping():
    return {
        "sh600183": ["industry:元件", "concept:PCB"],   # 行业字段缺失，靠它反查命中
        "sh600584": ["industry:元件"],                   # 长电 +7.2
        "sz002463": ["industry:元件"],                   # 沪电 +6.5（3号池，标⭐）
        "sz002916": ["concept:PCB", "industry:元件"],   # 深南 +5.0（元件行业，也属 PCB 概念）
        "sz002800": ["industry:元件"],                   # +2.0，不达 3% 门槛
        "sh600000": ["industry:银行"],                   # +9.0 但非本板块
    }


def _detail_spot():
    return pd.DataFrame([
        {"code": "sh600584", "name": "长电科技", "zdf": 7.2},
        {"code": "sz002463", "name": "沪电股份", "zdf": 6.5},
        {"code": "sz002916", "name": "深南电路", "zdf": 5.0},
        {"code": "sz002800", "name": "低涨股", "zdf": 2.0},
        {"code": "sh600000", "name": "浦发银行", "zdf": 9.0},
    ])


_WATCH = {"sh600183", "sz002463"}


def test_board_stock_detail_zt_order_leaders_star_dedup():
    out = board_stock_detail("元件", _detail_zt(), _detail_mapping(),
                             _detail_spot(), _WATCH)
    codes = [s["code"] for s in out]
    # 涨停按连板降序在前，领涨按涨幅降序补齐；银行票/低涨股不入；崇达去重
    assert codes == ["sz300919", "sh600183", "sz002815",
                     "sh600584", "sz002463", "sz002916"]
    assert [s["is_zt"] for s in out] == [True, True, True, False, False, False]
    assert [s["lianban"] for s in out if s["is_zt"]] == [3, 2, 1]
    assert [s["zdf"] for s in out if not s["is_zt"]] == [7.2, 6.5, 5.0]
    # 持仓/3号池标⭐：生益（涨停）、沪电（领涨）
    stars = {s["code"] for s in out if s["star"]}
    assert stars == {"sh600183", "sz002463"}
    assert "sz002916" in codes and "sh600000" not in codes


def test_board_stock_detail_concept_board_matches_via_mapping():
    # 触发的是概念板块"PCB"：涨停池只有行业字段（=元件），需靠 mapping 的 concept:PCB 反查
    zt = [
        {"code": "sz002916", "name": "深南电路", "lianban": 2, "industry": "元件"},
        {"code": "sz002815", "name": "崇达技术", "lianban": 1, "industry": "元件"},
    ]
    mapping = {
        "sz002916": ["concept:PCB", "industry:元件"],   # 属 PCB 概念 -> 命中
        "sz002815": ["industry:元件"],                   # 仅元件行业，不在 PCB 概念 -> 不命中
    }
    out = board_stock_detail("PCB", zt, mapping, pd.DataFrame(), set())
    assert [s["code"] for s in out] == ["sz002916"]


def test_board_stock_detail_total_capped_at_limit():
    # 涨停 3 只，limit=4 -> 只补 1 只领涨（涨幅最高的长电），总数不超过 4
    out = board_stock_detail("元件", _detail_zt(), _detail_mapping(),
                             _detail_spot(), _WATCH, limit=4)
    assert len(out) == 4
    assert [s["code"] for s in out] == ["sz300919", "sh600183", "sz002815", "sh600584"]


def test_board_stock_detail_up_min_threshold():
    # 门槛抬到 6%：只有长电 7.2、沪电 6.5（>=6%），深南 5.0 被滤掉
    out = board_stock_detail("元件", _detail_zt(), _detail_mapping(),
                             _detail_spot(), _WATCH, up_min=6.0)
    leaders = [s["code"] for s in out if not s["is_zt"]]
    assert leaders == ["sh600584", "sz002463"]


def test_board_stock_detail_spot_missing_or_empty_mapping_only_zt():
    # 无快照：只给涨停，不抛错
    out = board_stock_detail("元件", _detail_zt(), _detail_mapping(), None, set())
    assert [s["code"] for s in out] == ["sz300919", "sh600183", "sz002815"]
    # 映射为空：生益（行业字段缺失）无法反查被剔除，只剩行业字段直接命中的两只
    out2 = board_stock_detail("元件", _detail_zt(), {}, _detail_spot(), set())
    assert [s["code"] for s in out2] == ["sz300919", "sz002815"]
    # 空 DataFrame 同样安全降级
    out3 = board_stock_detail("元件", _detail_zt(), _detail_mapping(),
                              pd.DataFrame(), set())
    assert len(out3) == 3 and all(s["is_zt"] for s in out3)


def test_board_stock_detail_empty_inputs():
    assert board_stock_detail("元件", [], {}, None, set()) == []
    assert board_stock_detail("元件", None, None, None, None) == []
