# -*- coding: utf-8 -*-
"""双源口径对账（仅东财可用时低频采样，纯观测、不参与触发）。

把"东财官方加权板块涨幅"与"腾讯成分等权聚合涨幅"按板块名对齐并落盘，
累积偏差样本供 scripts/reconcile_source_diff.py 盘后量化。任何异常只记日志。
"""
from .config import CHG_L1, log
from .datasources import tx_spot
from .aggregator import tx_board_agg
from .persistence import append_diff_rows


def in_critical_band(ec, tc):
    """两源涨幅任一落在 2.0~3.0% 触发线附近。"""
    return (2.0 <= ec <= 3.0) or (2.0 <= tc <= 3.0)


def reconcile_pair(e, tx_map, ts):
    """对齐一对东财/腾讯板块；返回 (row, abs_diff, zt_mismatch, band_flag)。
    无法对齐返回 None；band_flag 仅在临界带内给出 True/False，否则 None。"""
    t = tx_map.get(e.get("name"))
    ec, tc = e.get("chg"), (t or {}).get("chg")
    if not t or ec is None or tc is None:
        return None
    d = ec - tc
    ez = e.get("zt") or 0
    tz = t.get("zt") or 0
    band_flag = None
    if in_critical_band(ec, tc):
        band_flag = (ec > CHG_L1) != (tc > CHG_L1)
    row = [ts, e.get("name"), round(ec, 3), round(tc, 3), round(d, 3),
           ez, tz, e.get("up"), t.get("up"), e.get("down"), t.get("down")]
    return row, abs(d), (ez != tz), band_flag


def collect_reconcile_rows(em_list, tx_map, ts):
    """遍历东财板块按名对齐腾讯，返回 (rows, abs_diffs, zt_mismatch, band_flags)。"""
    rows, abs_diffs, band = [], [], []
    zt_mismatch = 0
    for e in em_list:
        got = reconcile_pair(e, tx_map, ts)
        if got is None:
            continue
        row, ad, ztmm, band_flag = got
        rows.append(row)
        abs_diffs.append(ad)
        if ztmm:
            zt_mismatch += 1
        if band_flag is not None:
            band.append(band_flag)
    return rows, abs_diffs, zt_mismatch, band


def reconcile_summary(abs_diffs, zt_mismatch, band):
    """生成双源对账的汇总日志行（abs_diffs 需已排序）。"""
    n = len(abs_diffs)
    med = abs_diffs[n // 2]
    p90 = abs_diffs[min(n - 1, int(n * 0.9))]
    within = sum(1 for x in abs_diffs if x <= 0.3) / n * 100
    cross = sum(1 for x in band if x)
    return (f"[reconcile] 双源对账：配对 {n} 板块，涨幅|差|中位 {med:.2f}/P90 {p90:.2f}/"
            f"最大 {abs_diffs[-1]:.2f} 个百分点，|差|≤0.3pct 占 {within:.0f}%；"
            f"涨停家数不一致 {zt_mismatch} 个；2.5%临界带跨阈值分歧 {cross}/{len(band)}")


def reconcile_sources(now, em_boards_data, smap):
    """拉一次腾讯聚合，按板块名对齐两源指标并落盘；纯观测，异常只记日志。"""
    try:
        tx_list = tx_board_agg(smap, tx_spot())
        if not tx_list:
            log("[reconcile] 腾讯聚合为空，跳过本轮双源对账")
            return
        tx_map = {b["name"]: b for b in tx_list}
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        rows, abs_diffs, zt_mismatch, band = collect_reconcile_rows(
            em_boards_data, tx_map, ts)
        if not rows:
            log("[reconcile] 两源板块名无配对（成分映射口径可能不一致），跳过")
            return
        append_diff_rows(now, rows)
        abs_diffs.sort()
        log(reconcile_summary(abs_diffs, zt_mismatch, band))
    except Exception as e:
        log(f"[reconcile] 对账失败（不影响监测）：{type(e).__name__}: {e}")
