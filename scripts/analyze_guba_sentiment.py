#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股吧情绪反向指标 · 历史对照验证
================================

输入：
  - data/raw/guba/zssh000001_sample.csv  （上证指数吧采样点，47个）
  - data/raw/etf/daily/sh/sh.510300.csv  （沪深300ETF日线，大盘代理）

方法：
  采样点按时间倒序（page越大越旧）。相邻两个有效采样点之间：
    帖子数 ≈ (page差) × 80
    天数  = 两个锚点(date_oldest)的日期差
    日均发帖 = 帖子数 / 天数
  由此重建【每月日均发帖量】，与沪深300月末点位/月度涨跌对照。

验证目标（散户情绪 = 反向指标）：
  - 发帖量极高（散户狂热）的月份，之后大盘是否见顶下跌？
  - 发帖量极低（散户冷清）的月份，之后大盘是否见底上涨？

输出：results/sentiment_verification/guba_sentiment_vs_market.csv
"""

import csv
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = PROJECT_ROOT / "data/raw/guba/zssh000001_sample.csv"
INDEX_CSV = PROJECT_ROOT / "data/raw/etf/daily/sh/sh.510300.csv"
OUT_DIR = PROJECT_ROOT / "results/sentiment_verification"


def parse_dt(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def main():
    # 1. 读采样点
    # 注意：页面帖子按"最后回复"乱序，t[0]/t[-1]不代表整页时间范围，
    #       因此对每行存的两个时间取 max 作为该页代表时间（新页代表时间 > 旧页）
    rows = []
    with open(SAMPLE_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                t0 = parse_dt(r["date_newest"])
                t1 = parse_dt(r["date_oldest"])
                rep = max(t0, t1)   # 该页代表时间（较晚者）
                rows.append({
                    "page": int(r["page"]),
                    "n": int(r["n_posts"]),
                    "rep": rep,
                    "lo": min(t0, t1),
                    "clicks": int(r["total_clicks"]),
                    "comments": int(r["total_comments"]),
                })
            except Exception:
                continue
    rows.sort(key=lambda x: x["page"])  # page小=新, page大=旧
    print(f"采样点：{len(rows)} 个，时间 {rows[-1]['rep'].date()} ~ {rows[0]['rep'].date()}")

    # 2. 相邻区间 → 日均发帖量（page升序，rows[i]新、rows[i+1]旧）
    intervals = []
    for i in range(len(rows) - 1):
        cur = rows[i]       # 较新(小page)
        nxt = rows[i + 1]   # 较旧(大page)
        page_gap = nxt["page"] - cur["page"]          # >0
        post_in_gap = page_gap * 80                    # 区间帖子数(近似)
        days = (cur["rep"] - nxt["rep"]).days
        if days <= 0:
            continue
        daily_post = post_in_gap / days                # 日均发帖
        mid = nxt["rep"] + (cur["rep"] - nxt["rep"]) / 2
        intervals.append({"date": mid, "daily_post": daily_post})
    print(f"有效区间：{len(intervals)} 个")

    # 3. 读沪深300ETF日线
    idx = []
    with open(INDEX_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                idx.append({"date": datetime.strptime(r["date"], "%Y-%m-%d"),
                            "close": float(r["close"])})
            except Exception:
                continue
    idx.sort(key=lambda x: x["date"])
    # 月末点位
    month_close = {}
    for d in idx:
        key = (d["date"].year, d["date"].month)
        month_close[key] = d["close"]  # 同key后出现的是月末最后一天
    months = sorted(month_close.keys())
    print(f"沪深300月末数据：{len(months)} 个月，{months[0]} ~ {months[-1]}")

    # 4. 把区间日均发帖按月份归并
    month_post = {}
    month_cnt = {}
    for iv in intervals:
        key = (iv["date"].year, iv["date"].month)
        month_post[key] = month_post.get(key, 0.0) + iv["daily_post"]
        month_cnt[key] = month_cnt.get(key, 0) + 1
    month_avg_post = {k: month_post[k] / month_cnt[k] for k in month_post}

    # 5. 合并：每月 [日均发帖, 沪深300收盘, 月度涨跌]
    table = []
    for i in range(1, len(months)):
        m = months[i]
        pm = months[i - 1]
        if m not in month_avg_post:
            continue
        chg = (month_close[m] / month_close[pm] - 1) * 100
        table.append({
            "month": f"{m[0]}-{m[1]:02d}",
            "daily_post": round(month_avg_post[m]),
            "hs300_close": round(month_close[m], 3),
            "month_chg_pct": round(chg, 2),
        })

    # 6. 输出
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / "guba_sentiment_vs_market.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["month", "daily_post", "hs300_close", "month_chg_pct"])
        w.writeheader()
        w.writerows(table)
    print(f"\n已保存：{out_csv}（{len(table)}个月）")

    # 7. 关键验证：发帖量最高/最低的月份 vs 前后大盘
    print("\n" + "=" * 70)
    print("【验证1】散户发帖量最高的10个月（情绪最热）")
    print("=" * 70)
    top_hot = sorted(table, key=lambda x: x["daily_post"], reverse=True)[:10]
    print(f"{'月份':<10}{'日均发帖':>10}{'沪深300':>10}{'当月涨跌%':>10}")
    for r in top_hot:
        print(f"{r['month']:<10}{r['daily_post']:>10}{r['hs300_close']:>10}{r['month_chg_pct']:>10}")

    print("\n" + "=" * 70)
    print("【验证2】散户发帖量最低的10个月（情绪最冷）")
    print("=" * 70)
    cold = sorted(table, key=lambda x: x["daily_post"])[:10]
    print(f"{'月份':<10}{'日均发帖':>10}{'沪深300':>10}{'当月涨跌%':>10}")
    for r in cold:
        print(f"{r['month']:<10}{r['daily_post']:>10}{r['hs300_close']:>10}{r['month_chg_pct']:>10}")

    # 8. 展望验证：发帖量分组 → 次月/次三月平均涨跌
    print("\n" + "=" * 70)
    print("【验证3】发帖量分组 → 未来1/3个月沪深300平均涨跌%")
    print("=" * 70)
    post_sorted = sorted([t["daily_post"] for t in table])
    q = lambda p: post_sorted[int(len(post_sorted) * p)]
    thr_low, thr_high = q(0.2), q(0.8)
    by_level = {"冷清(低20%)": [], "正常": [], "狂热(高20%)": []}
    for i, r in enumerate(table):
        if r["daily_post"] <= thr_low:
            grp = "冷清(低20%)"
        elif r["daily_post"] >= thr_high:
            grp = "狂热(高20%)"
        else:
            grp = "正常"
        # 次月涨跌
        if i + 1 < len(table):
            by_level[grp].append(table[i + 1]["month_chg_pct"])
    print(f"分组阈值：冷清≤{int(thr_low):,}帖/天，狂热≥{int(thr_high):,}帖/天")
    print(f"{'情绪分组':<14}{'样本数':>8}{'次月平均涨跌%':>14}")
    for g, vals in by_level.items():
        if vals:
            print(f"{g:<14}{len(vals):>8}{sum(vals)/len(vals):>14.2f}")


if __name__ == "__main__":
    main()
