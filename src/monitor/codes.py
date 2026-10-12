# -*- coding: utf-8 -*-
"""证券代码归一化与持仓/候选 CSV 读取。"""
import csv
from pathlib import Path

from .config import ROOT, log


def normalize_code(c):
    """把各种写法的代码归一为带市场前缀的 6 位代码（sh600519 / sz000001 / bj830799）。"""
    c = str(c).strip().split(".")[0].zfill(6)
    if c.startswith(("60", "68", "9")):
        return "sh" + c
    if c.startswith(("00", "30", "20")):
        return "sz" + c
    if c.startswith(("8", "4", "92")):
        return "bj" + c
    return "sz" + c


def _row_code_name(row):
    """从持仓/候选 CSV 一行取规范化代码与名称；空代码返回 (None, None)。"""
    code = normalize_code(row.get("code") or row.get("代码") or "")
    if not code:
        return None, None
    return code, (row.get("name") or row.get("名称") or "")


def load_csv_codes(path):
    """读 code,name 两列 CSV（# 开头注释、空行忽略），返回 {规范代码: 名称}；缺文件返回 {}。"""
    p = Path(path)
    codes = {}
    if not p.exists():
        return codes
    try:
        with open(p, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        if lines:
            for row in csv.DictReader(lines):
                code, name = _row_code_name(row)
                if code:
                    codes[code] = name
    except Exception as e:
        log(f"[load_csv {p.name}] {e}")
    return codes


def default_holdings_path():
    return str(ROOT / "config" / "holdings.csv")


def default_pool_path():
    return str(ROOT / "config" / "pool3_constituents.csv")
