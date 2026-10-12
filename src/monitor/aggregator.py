# -*- coding: utf-8 -*-
"""聚合层：用腾讯个股 spot + 板块成分映射，等权算术平均聚合出板块行情。

纯计算为主（仅在整体失败时记日志），补齐东财 push2 缺失时新浪兜底没有的
涨停数/量比/涨速/上涨占比，使 L1/L2 在腾讯兜底数据源下也能完整触发。
"""
from .config import log


def _is_zt_stock(zdf, code):
    """个股是否触及涨停（按代码前缀区分 20cm / 10cm 涨跌停幅度）。"""
    import pandas as pd
    if zdf is None or pd.isna(zdf):
        return False
    c = str(code)
    if c.startswith(("30", "68", "8", "4", "92")):  # 创业/科创/北交 20cm
        return zdf >= 19.9
    return zdf >= 9.9


def _new_agg_bucket():
    return {"chg_sum": 0.0, "n": 0, "zt": 0, "lb_sum": 0.0,
            "speed_sum": 0.0, "up": 0, "down": 0}


def _accumulate_price(a, zdf, code):
    """把一只个股的涨幅累加到板块桶（家数、涨跌家数、涨停数）。"""
    import pandas as pd
    if zdf is None or pd.isna(zdf):
        return
    a["chg_sum"] += zdf
    a["n"] += 1
    if zdf > 0:
        a["up"] += 1
    elif zdf < 0:
        a["down"] += 1
    if _is_zt_stock(zdf, code):
        a["zt"] += 1


def _accumulate_volume(a, lb, sp):
    """把一只个股的量比/涨速累加到板块桶。"""
    import pandas as pd
    if lb is not None and not pd.isna(lb):
        a["lb_sum"] += lb
    if sp is not None and not pd.isna(sp):
        a["speed_sum"] += sp


def _accumulate_tx_row(board_agg, code, tags, row):
    """把一行腾讯个股数据，按其所属成分标签累加进对应板块桶。"""
    zdf = row.get("zdf")
    for tag in tags:
        _, _, nm = tag.partition(":")
        a = board_agg.setdefault(nm, _new_agg_bucket())
        _accumulate_price(a, zdf, code)
        _accumulate_volume(a, row.get("lb"), row.get("speed"))


def _boards_from_buckets(board_agg):
    """把板块桶聚合成输出行（等权算术平均）；空桶跳过，无输出返回 None。"""
    out = []
    for nm, a in board_agg.items():
        if a["n"] == 0:
            continue
        out.append({
            "code": None, "name": nm,
            "chg": a["chg_sum"] / a["n"],
            "zt": a["zt"],
            "lb": (a["lb_sum"] / a["n"]) if a["lb_sum"] else None,
            "speed": (a["speed_sum"] / a["n"]) if a["speed_sum"] else None,
            "up": a["up"], "down": a["down"],
        })
    return out or None


def tx_board_agg(mapping, tx_df):
    """聚合板块行情，返回 [{code,name,chg,zt,lb,speed,up,down}]；映射/spot 不可用返回 None。"""
    import pandas as pd
    if not mapping or tx_df is None or tx_df.empty:
        return None
    try:
        tx_df = tx_df.copy()
        for col in ("zdf", "lb", "speed"):
            tx_df[col] = pd.to_numeric(tx_df[col], errors="coerce")
        board_agg = {}
        for _, row in tx_df.iterrows():
            code = row["code"]
            tags = mapping.get(code)
            if not tags:
                continue
            _accumulate_tx_row(board_agg, code, tags, row)
        return _boards_from_buckets(board_agg)
    except Exception as e:
        log(f"[tx_agg] 聚合失败：{type(e).__name__}")
        return None
