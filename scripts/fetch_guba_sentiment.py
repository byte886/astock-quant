#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东方财富股吧历史活跃度采样爬虫
================================

目的：采集股吧历史帖子的【活跃度】（发帖量/阅读量/评论量），
     构建散户情绪指数（反向指标），用于历史回测验证。

方法：股吧帖子列表严格按时间倒序分页（每页80条）。
     本脚本【稀疏采样】——每隔 N 页采一页，记录该页的日期范围和
     阅读/评论汇总。后处理时用相邻采样点的「页数差×80 ÷ 天数差」
     重建历史日均活跃度曲线，无需爬取全部上百万帖。

     例：上证指数吧约11.5万页，每200页采1页 = 575次请求 ≈ 20分钟，
     即可得到2015年至今、约575个采样点的周/月度活跃度曲线。

数据来源：https://guba.eastmoney.com/list,<bar_code>_<page>.html
     - 个股吧：bar_code = 股票代码，如 600519
     - 指数吧：bar_code = zssh + 指数代码，如 zssh000001（上证指数）

用法：
     python scripts/fetch_guba_sentiment.py --bar zssh000001 --step 200
     python scripts/fetch_guba_sentiment.py --bar 600519 --step 200
     python scripts/fetch_guba_sentiment.py --bar zssh000001 --step 200 --max-page 120000

输出：data/raw/guba/<bar>_sample.csv，每个采样页一行，断点续传。

注意：保守限速（默认1.5~3.0秒/请求），失败指数退避重试，请勿调高并发。
"""

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "guba"
WORKSPACE = PROJECT_ROOT / "data" / "_workspace"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 字段正则（已验证：每页80条，时间/阅读/评论字段齐全）
RE_TIME = re.compile(r'"post_publish_time":"([^"]+)"')
RE_CLICK = re.compile(r'"post_click_count":(\d+)')
RE_COMMENT = re.compile(r'"post_comment_count":(\d+)')
RE_COUNT = re.compile(r'"count":(\d+)')


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def build_url(bar, page):
    """股吧列表页URL。第1页无页码后缀。"""
    if page <= 1:
        return f"https://guba.eastmoney.com/list,{bar}.html"
    return f"https://guba.eastmoney.com/list,{bar}_{page}.html"


def fetch_page(bar, page, timeout=15):
    """抓取一页，返回 (times[], clicks[], comments[])；失败抛异常。"""
    url = build_url(bar, page)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": f"https://guba.eastmoney.com/list,{bar}.html",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # 东方财富页面为 utf-8
    html = raw.decode("utf-8", errors="ignore")

    times = RE_TIME.findall(html)
    clicks = [int(x) for x in RE_CLICK.findall(html)]
    comments = [int(x) for x in RE_COMMENT.findall(html)]
    total_match = RE_COUNT.search(html)
    total_count = int(total_match.group(1)) if total_match else None
    return times, clicks, comments, total_count, len(html)


def fetch_page_retry(bar, page, max_retries=5):
    """带指数退避的重试。全部失败返回None。"""
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_page(bar, page)
        except Exception as e:
            wait = min(2 ** attempt + random.uniform(0, 2), 60)
            log(f"  ⚠️ 第{page}页第{attempt}次失败: {e}；{wait:.0f}秒后重试")
            time.sleep(wait)
    log(f"  ❌ 第{page}页重试{max_retries}次仍失败，跳过")
    return None


def load_done_pages(csv_path):
    """读取已完成采样的页码，用于断点续传。"""
    done = set()
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    done.add(int(row["page"]))
                except (KeyError, ValueError):
                    continue
    return done


def main():
    ap = argparse.ArgumentParser(description="东方财富股吧历史活跃度采样爬虫")
    ap.add_argument("--bar", default="zssh000001",
                    help="股吧代码：指数 zssh000001，个股 600519（默认上证指数吧）")
    ap.add_argument("--step", type=int, default=200,
                    help="采样间隔（页），默认200，越小越精细但请求越多")
    ap.add_argument("--max-page", type=int, default=120000,
                    help="最大探测页码，默认120000")
    ap.add_argument("--sleep-min", type=float, default=1.5, help="每页最小等待秒数")
    ap.add_argument("--sleep-max", type=float, default=3.0, help="每页最大等待秒数")
    ap.add_argument("--probe-only", action="store_true",
                    help="只探测总页数和时间边界，不做全量采样")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"{args.bar}_sample.csv"

    log("=" * 60)
    log(f"股吧活跃度采样：{args.bar}，间隔{args.step}页")
    log("=" * 60)

    # 第1页：拿总帖子数
    first = fetch_page_retry(args.bar, 1)
    if first is None:
        log("❌ 无法访问第1页，退出")
        sys.exit(1)
    times, clicks, comments, total_count, _ = first
    log(f"首页抓取成功：{len(times)}条，最新帖 {times[0] if times else '?'}")
    if total_count:
        est_pages = total_count // 80 + 1
        log(f"股吧总帖子数（官方count）：{total_count:,}，理论约 {est_pages:,} 页")
    else:
        est_pages = args.max_page
        log("未取到官方count，将用二分探测实际边界")

    if args.probe_only:
        # 二分探测最后一页
        log("二分探测最早数据边界...")
        lo, hi = 1, args.max_page
        last_valid = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            r = fetch_page_retry(args.bar, mid)
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))
            if r and len(r[0]) > 0:
                last_valid = mid
                lo = mid + 1
            else:
                hi = mid - 1
        boundary = fetch_page_retry(args.bar, last_valid)
        earliest = boundary[0][-1] if boundary and boundary[0] else "?"
        log(f"✅ 最早有效页：第{last_valid}页，最早帖子时间：{earliest}")
        return

    # 全量采样
    done = load_done_pages(csv_path)
    if done:
        log(f"断点续传：已完成 {len(done)} 个采样页")

    # 采样页码：1, 1+step, 1+2step ... 直到无数据
    pages = list(range(1, args.max_page + 1, args.step))
    todo = [p for p in pages if p not in done]
    log(f"计划采样 {len(pages)} 个点，本次待采 {len(todo)} 个")

    write_header = not csv_path.exists()
    f_out = open(csv_path, "a", newline="", encoding="utf-8")
    writer = csv.writer(f_out)
    if write_header:
        writer.writerow(["page", "n_posts", "date_newest", "date_oldest",
                         "total_clicks", "total_comments", "fetched_at"])

    empty_streak = 0  # 连续空页计数，用于提前停止
    fetched = 0
    try:
        for i, page in enumerate(todo):
            r = fetch_page_retry(args.bar, page)
            if r is None or len(r[0]) == 0:
                empty_streak += 1
                log(f"[{i+1}/{len(todo)}] 第{page}页无数据（连续{empty_streak}次）")
                if empty_streak >= 3:
                    log("连续3个采样页无数据，判定已到最早边界，停止")
                    break
                time.sleep(random.uniform(args.sleep_min, args.sleep_max))
                continue
            empty_streak = 0
            t, c, cm, _, _ = r
            # 帖子按时间倒序：第0条最新，最后一条最早
            writer.writerow([page, len(t), t[0], t[-1],
                             sum(c[:len(t)]), sum(cm[:len(t)]),
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            f_out.flush()
            fetched += 1
            if (i + 1) % 10 == 0 or i == 0:
                log(f"[{i+1}/{len(todo)}] 第{page}页：{len(t)}条，"
                    f"{t[-1][:10]} ~ {t[0][:10]}")
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))
    finally:
        f_out.close()

    log("=" * 60)
    log(f"✅ 采样完成，本次新增 {fetched} 个点，结果：{csv_path}")
    log("=" * 60)


if __name__ == "__main__":
    main()
