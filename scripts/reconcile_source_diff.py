#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双源（东方财富板块"官方加权" vs 腾讯个股"成分等权聚合"）口径偏差 —— 盘后汇总。

数据来源：盘中监控 scripts/intraday_sector_monitor.py 仅在东财可用时低频落的
data/_workspace/source_diff/YYYY-MM-DD.csv（列：时间/板块名/东财涨幅/腾讯涨幅/
涨幅差/东财涨停/腾讯涨停/东财上涨/腾讯上涨/东财下跌/腾讯下跌）。

用途：量化两个源的板块涨幅、涨停家数口径差，判断能否长期只靠腾讯兜底——
  · 若约 ≥95% 的板块 |东财涨幅-腾讯涨幅| ≤ 0.3 个百分点，且 2.5% 触发线临界带
    几乎不出现"一个达标、一个不达标"的跨阈值分歧，则腾讯等权聚合可长期独立承担，
    东财退为可选的交叉校验源；
  · 否则（临界带频繁分歧）应把腾讯聚合从"成分等权平均"改为"流通市值加权"
    （需另补流通市值数据），以贴近东财官方板块指数口径。

用法：
  .venv/bin/python scripts/reconcile_source_diff.py
  .venv/bin/python scripts/reconcile_source_diff.py --dir <自定义对账目录>

纯本地读取 CSV，不联网、不影响盘中监控。
"""
import argparse
import csv
import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data" / "_workspace" / "source_diff"

# 判定阈值（与监控方案口径一致，可调）
WITHIN_PCT = 0.30     # |涨幅差|≤0.3 个百分点视为可接受
WITHIN_RATIO = 0.95   # 至少 95% 板块落在可接受带内
CROSS_RATIO = 0.02    # 临界带跨阈值分歧率上限 2%
BAND_LO, BAND_HI, TRIG = 2.0, 3.0, 2.5   # 临界带与 L1/L2 涨幅触发线


def pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[i]


def main():
    ap = argparse.ArgumentParser(description="汇总东财 vs 腾讯板块口径偏差")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="对账 CSV 所在目录")
    args = ap.parse_args()

    files = sorted(glob.glob(str(Path(args.dir) / "*.csv")))
    if not files:
        print(f"暂无对账样本：{args.dir} 下没有 CSV。")
        print("东财 push2 恢复后，盘中监控会自动每约 30 分钟采样一次（东财被限时不采、零开销）。")
        return

    abs_diffs, signed_diffs = [], []
    zt_mismatch = n = 0
    ts_set = set()
    band_n = band_cross = 0

    for fp in files:
        with open(fp, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    ec = float(r["东财涨幅%"])
                    tc = float(r["腾讯涨幅%"])
                    d = float(r["涨幅差(东-腾)"])
                except (TypeError, ValueError, KeyError):
                    continue
                n += 1
                ts_set.add(r.get("时间", ""))
                abs_diffs.append(abs(d))
                signed_diffs.append(d)
                if str(r.get("东财涨停")) != str(r.get("腾讯涨停")):
                    zt_mismatch += 1
                if BAND_LO <= ec <= BAND_HI or BAND_LO <= tc <= BAND_HI:
                    band_n += 1
                    if (ec > TRIG) != (tc > TRIG):
                        band_cross += 1

    if n == 0:
        print("对账文件存在但没有有效样本行。")
        return

    abs_diffs.sort()
    mean_signed = sum(signed_diffs) / n
    within03 = sum(1 for x in abs_diffs if x <= 0.30) / n * 100
    within05 = sum(1 for x in abs_diffs if x <= 0.50) / n * 100
    cross_rate = (band_cross / band_n) if band_n else 0.0

    print("=" * 66)
    print(f"对账目录：{args.dir}")
    print(f"采样文件 {len(files)} 个（天）｜采样轮次 {len(ts_set)}｜配对板块样本 {n} 条")
    print("-" * 66)
    print("板块涨幅差（东财 − 腾讯，单位：个百分点）")
    print(f"  带符号平均 {mean_signed:+.3f}（正=腾讯整体偏低，负=腾讯整体偏高）")
    print(f"  |差| 中位 {pctile(abs_diffs, 0.50):.3f} ｜ P90 {pctile(abs_diffs, 0.90):.3f} "
          f"｜ P95 {pctile(abs_diffs, 0.95):.3f} ｜ 最大 {abs_diffs[-1]:.3f}")
    print(f"  |差|≤0.3 占 {within03:.1f}% ｜ |差|≤0.5 占 {within05:.1f}%")
    print(f"涨停家数不一致：{zt_mismatch}/{n}（{zt_mismatch / n * 100:.1f}%）")
    if band_n:
        print(f"2.5% 临界带样本 {band_n} 条，其中跨阈值（一达标、一不达标）{band_cross} 条"
              f"（{cross_rate * 100:.1f}%）")
    else:
        print("2.5% 临界带：暂无样本（两源在阈值附近都没有板块）")
    print("-" * 66)

    if within03 / 100 >= WITHIN_RATIO and cross_rate <= CROSS_RATIO:
        print("结论：偏差很小且临界带几乎不分歧 → 腾讯等权聚合可长期独立承担，")
        print("      东财保留为可选的交叉校验源即可。")
    else:
        print("结论：偏差或临界带分歧偏大 → 暂保留「东财优先、失败切腾讯」；")
        print("      若东财长期不可用，宜把腾讯聚合改为「流通市值加权」（需另补市值数据）后再评估。")
    print("=" * 66)


if __name__ == "__main__":
    main()
