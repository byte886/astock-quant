# -*- coding: utf-8 -*-
"""codes 层：代码归一化与持仓 CSV 读取。"""
import pytest

from src.monitor.codes import normalize_code, load_csv_codes, _row_code_name


@pytest.mark.parametrize("raw,expected", [
    ("600519", "sh600519"),
    ("688981", "sh688981"),
    ("900001", "sh900001"),
    ("000001", "sz000001"),
    ("300750", "sz300750"),
    ("200001", "sz200001"),
    ("830799", "bj830799"),
    ("430047", "bj430047"),
    # ISSUES I16 已修复：920 北交新号段先于 "9" 判断，归一为 bj
    ("920001", "bj920001"),
    ("920123", "bj920123"),
    ("600519.SH", "sh600519"),  # 带交易所后缀：取点号前 6 位
    (" 000001 ", "sz000001"),   # 空白
])
def test_normalize_code(raw, expected):
    assert normalize_code(raw) == expected


def test_row_code_name_en_and_zh():
    code, name = _row_code_name({"code": "600519", "name": "贵州茅台"})
    assert code == "sh600519" and name == "贵州茅台"
    code2, name2 = _row_code_name({"代码": "300750", "名称": "宁德时代"})
    assert code2 == "sz300750" and name2 == "宁德时代"


def test_row_code_name_empty_code_pads_to_sz000000():
    # 既有行为：空 code 经 zfill(6) 变成 "000000" 归一为 sz000000（不会返回 None）
    code, name = _row_code_name({"code": "", "name": "x"})
    assert code == "sz000000" and name == "x"


def test_load_csv_codes_missing(tmp_path):
    assert load_csv_codes(str(tmp_path / "nope.csv")) == {}


def test_load_csv_codes_ok_and_skips_comments(tmp_path):
    p = tmp_path / "holdings.csv"
    p.write_text("# 注释行\ncode,name\n600519,贵州茅台\n\n# 另一条\n300750,宁德时代\n",
                 encoding="utf-8")
    codes = load_csv_codes(str(p))
    assert codes == {"sh600519": "贵州茅台", "sz300750": "宁德时代"}


def test_load_csv_codes_bad_file(tmp_path):
    p = tmp_path / "holdings.csv"
    p.write_text("code,name\n", encoding="utf-8")
    # 只有表头：DictReader 无数据行 -> {}
    assert load_csv_codes(str(p)) == {}
