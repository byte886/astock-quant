# -*- coding: utf-8 -*-
"""聚合层：用腾讯个股 spot + 板块成分映射，等权算术平均聚合出板块行情。

纯计算为主（仅在整体失败时记日志），补齐东财 push2 缺失时新浪兜底没有的
涨停数/量比/涨速/上涨占比，使 L1/L2 在腾讯兜底数据源下也能完整触发。
"""
from .config import log


def _is_zt_stock(zdf, code):
    """个股是否触及涨停（按代码前缀区分 20cm / 10cm 涨跌停幅度）。

    腾讯 spot 返回的 code 带市场前缀（sz300308 / sh688808 / bj830xxx / bj920xxx），
    判定 20cm 前需先剥离 sh/sz/bj 前缀，否则创业/科创/北交票会落到 10cm 的 9.9%
    分支而被误计涨停（ISSUES I16）。
    注：北交所实际涨跌幅为 30%，当前与创业/科创同按 20cm(19.9) 桶处理（已知简化，另记）。
    """
    import pandas as pd
    if zdf is None or pd.isna(zdf):
        return False
    c = str(code)
    if len(c) > 6 and c[:2] in ("sh", "sz", "bj"):  # 剥离市场前缀，回到 6 位裸码
        c = c[2:]
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


def _belongs_to_board(tags, board_name):
    """该票的成分标签中是否包含指定板块名（行业/概念同名都命中）。"""
    for tag in tags or []:
        if tag.partition(":")[2] == board_name:
            return True
    return False


def board_stock_detail(board_name, zt, mapping, spot_df, watch_set,
                       limit=10, up_min=3.0):
    """触发板块的"带动个股"明细：涨停封板股优先、未涨停领涨股补齐，总数≤limit。

    数据来源：涨停来自东财涨停池 zt（含连板数、所属行业，行业按官方 industry、
    概念按 mapping 反查）；领涨来自腾讯全市场快照 spot_df 配合成分映射 mapping，
    且只取涨幅≥up_min 的（避免把没涨的票凑成"伪领涨"）。任一路缺失都不抛错——
    spot 缺失时只给涨停名单，绝不影响主推送。
    返回 list[dict(code,name,zdf,lianban,is_zt,star)]，已按展示顺序排序并截断。
    """
    import pandas as pd

    zt_items = {}  # code -> dict（行业/概念双口径可能重复，按 code 去重）
    for z in (zt or []):
        code = z.get("code")
        tags = (mapping or {}).get(code)
        if z.get("industry") == board_name or _belongs_to_board(tags, board_name):
            zt_items[code] = {
                "code": code, "name": z.get("name") or code,
                "zdf": None, "lianban": int(z.get("lianban") or 1),
                "is_zt": True,
            }

    up_items = {}  # 非涨停领涨股，按涨幅取剩余名额
    if spot_df is not None and not getattr(spot_df, "empty", True) and mapping:
        try:
            candidates = []
            for _, r in spot_df.iterrows():
                code = r.get("code")
                if code in zt_items or not _belongs_to_board(mapping.get(code), board_name):
                    continue
                zdf = r.get("zdf")
                if zdf is None or pd.isna(zdf):
                    continue
                zdf = float(zdf)
                if up_min is not None and zdf < up_min:
                    continue
                candidates.append((zdf, code, r.get("name") or code))
            candidates.sort(key=lambda x: x[0], reverse=True)
            for zdf, code, nm in candidates[:max(0, limit - len(zt_items))]:
                up_items[code] = {"code": code, "name": nm, "zdf": zdf,
                                  "lianban": 1, "is_zt": False}
        except Exception:
            up_items = {}

    zt_list = sorted(zt_items.values(), key=lambda s: -s["lianban"])
    up_list = sorted(up_items.values(), key=lambda s: -(s["zdf"] or 0.0))
    out = (zt_list + up_list)[:limit]
    watch = watch_set or set()
    for s in out:
        s["star"] = s["code"] in watch
    return out
