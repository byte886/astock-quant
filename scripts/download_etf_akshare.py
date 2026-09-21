#!/usr/bin/env python3
"""
A股ETF日线数据下载脚本（AkShare，东方财富数据源）

为什么用 AkShare 而不是 Baostock：
- Baostock 对 ETF 历史数据支持不完整，只能拿到最近几个月
- AkShare（东方财富）ETF 历史数据完整，从ETF成立日开始

特性：
- 全量ETF（沪市+深市，1588只），前复权
- 防限流：单只间隔 1-2 秒
- 断点续传：状态文件记录已下载ETF，中断后从断点继续
- 数据目录：data/raw/etf/daily/（覆盖Baostock的不完整数据）

用法：
  python scripts/download_etf_akshare.py              # 全量下载ETF日线（断点续传）
  python scripts/download_etf_akshare.py --check      # 数据质量检查
  python scripts/download_etf_akshare.py --etf 510300 # 只下载指定ETF（调试用）
"""

import akshare as ak
import pandas as pd
import os
import sys
import time
import json
import argparse
import random
from datetime import datetime
from pathlib import Path

# ============ 配置 ============

PROJECT_ROOT = Path(__file__).parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "data" / "_workspace"
DATA_DIR = PROJECT_ROOT / "data" / "raw" / "etf" / "daily"
STATUS_FILE = WORKSPACE_DIR / "download_status_etf_akshare.json"
ETF_LIST_FILE = WORKSPACE_DIR / "etf_list.csv"
LOG_FILE = WORKSPACE_DIR / "download.log"

# 防限流间隔（秒）— 东方财富接口有反爬，间隔要长
INTERVAL_MIN = 3.0
INTERVAL_MAX = 5.0

# 失败后等待时间（秒）
RETRY_WAIT = 10

# 连续失败阈值（超过后等待更长时间）
CONSECUTIVE_FAIL_THRESHOLD = 3
CONSECUTIVE_FAIL_WAIT = 30

# 单只下载超时（秒）
DOWNLOAD_TIMEOUT = 120


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [ETF-AK] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ensure_dirs():
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sh").mkdir(exist_ok=True)
    (DATA_DIR / "sz").mkdir(exist_ok=True)


def load_status():
    if STATUS_FILE.exists():
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "started_at": None,
        "updated_at": None,
        "total_etfs": 0,
        "completed": [],
        "failed": [],
        "skipped": [],
        "current_etf": None,
    }


def save_status(status):
    status["updated_at"] = datetime.now().isoformat()
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def get_etf_list():
    """从缓存的ETF列表获取"""
    if ETF_LIST_FILE.exists():
        df = pd.read_csv(ETF_LIST_FILE)
        return df
    log("❌ 未找到ETF列表缓存，请先运行 download_etf.py 生成列表")
    sys.exit(1)


def _download_one_etf_sina(code):
    """新浪 ETF 日线回退源（东财 push2his 断连时使用）。

    新浪返回英文列 date/open/high/low/close/volume，无 amount/turn/pctChg；
    基准 load_benchmark 只用 date/close，缺列不影响。
    """
    symbol = code.replace(".", "")  # sh.510300 -> sh510300
    df = ak.fund_etf_hist_sina(symbol=symbol)
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = df["date"].astype(str)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["code"] = code
    return df.sort_values("date").reset_index(drop=True)


def download_one_etf(code, max_retry=3):
    """下载单只ETF日线数据（AkShare），支持重试；东财失败回退新浪。"""
    # code 格式：sh.510300 → 510300
    symbol = code.split(".")[1]

    for attempt in range(max_retry + 1):
        try:
            df = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date="20000101",
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq",  # 前复权
            )

            if df is None or len(df) == 0:
                return None

            # 转换列名为英文，和Baostock格式对齐
            df = df.rename(columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
                "振幅": "amplitude",
                "涨跌幅": "pctChg",
                "涨跌额": "change",
                "换手率": "turn",
            })

            # 添加code列
            df["code"] = code

            # 按日期排序
            df = df.sort_values("date").reset_index(drop=True)

            return df

        except Exception as e:
            if attempt < max_retry:
                wait_time = RETRY_WAIT * (attempt + 1)  # 递增等待
                log(f"⚠️ {code} 下载失败，第 {attempt+1} 次重试（等待{wait_time}秒）：{e}")
                time.sleep(wait_time)
                continue
            # 东财重试耗尽，回退新浪源
            log(f"⚠️ {code} 东方财富源重试耗尽，回退新浪源 ...")
            try:
                df_sina = _download_one_etf_sina(code)
                if df_sina is not None and len(df_sina) > 0:
                    log(f"✅ {code} 新浪源成功：{len(df_sina)} 条（{df_sina['date'].min()} ~ {df_sina['date'].max()}，缺成交额/换手率列）")
                    return df_sina
                return None
            except Exception as e2:
                log(f"❌ {code} 新浪回退源也失败：{e2}")
                raise


def save_etf_data(code, df):
    exchange = code.split(".")[0]
    filepath = DATA_DIR / exchange / f"{code}.csv"
    df.to_csv(filepath, index=False, encoding="utf-8")
    return filepath


# ============ 主流程 ============

def full_download(args):
    """全量下载（断点续传）"""
    log("=" * 60)
    log("开始全量ETF日线数据下载（AkShare/东方财富，前复权）")
    log(f"数据目录：{DATA_DIR}")
    log("=" * 60)

    df_etfs = get_etf_list()
    total = len(df_etfs)
    log(f"共 {total} 只ETF待下载")

    status = load_status()
    if not status["started_at"]:
        status["started_at"] = datetime.now().isoformat()
    status["total_etfs"] = total

    consecutive_fails = 0  # 连续失败计数
    run_failed = 0  # 本次运行失败数（异常计失败；--etf 单只模式下无数据也计失败）

    for idx, row in df_etfs.iterrows():
        code = row["code"]
        code_name = row.get("code_name", "")

        if args.etf and code != args.etf:
            continue

        if code in status["completed"]:
            continue

        # 连续失败太多次，等待更长时间
        if consecutive_fails >= CONSECUTIVE_FAIL_THRESHOLD:
            log(f"⚠️ 连续失败 {consecutive_fails} 次，等待 {CONSECUTIVE_FAIL_WAIT} 秒后继续...")
            time.sleep(CONSECUTIVE_FAIL_WAIT)
            consecutive_fails = 0

        status["current_etf"] = code
        progress = f"[{idx + 1}/{total}]"

        try:
            log(f"{progress} 下载 {code} {code_name} ...")
            df = download_one_etf(code)

            if df is None or len(df) == 0:
                log(f"{progress} ⚠️ {code} 无数据，跳过")
                status["skipped"].append(code)
                consecutive_fails = 0
                if args.etf:
                    run_failed += 1
            else:
                filepath = save_etf_data(code, df)
                log(f"{progress} ✅ {code} 完成：{len(df)} 条记录（{df['date'].min()} ~ {df['date'].max()}）")
                status["completed"].append(code)
                consecutive_fails = 0
        except Exception as e:
            log(f"{progress} ❌ {code} 失败：{e}")
            status["failed"].append(code)
            consecutive_fails += 1
            run_failed += 1

        save_status(status)
        time.sleep(random.uniform(INTERVAL_MIN, INTERVAL_MAX))

    status["current_etf"] = None
    save_status(status)

    log("=" * 60)
    log(f"ETF下载完成：成功 {len(status['completed'])}，失败 {len(status['failed'])}，跳过 {len(status['skipped'])}")
    log("=" * 60)
    return run_failed


def check_data_quality(args):
    """数据质量检查"""
    log("=" * 60)
    log("ETF数据质量检查（AkShare）")
    log("=" * 60)

    status = load_status()
    total = status.get("total_etfs", 0)
    completed = len(set(status.get("completed", [])))
    failed = len(status.get("failed", []))
    skipped = len(status.get("skipped", []))

    log(f"ETF总数：{total}")
    log(f"已下载：{completed}")
    log(f"失败：{failed}")
    log(f"跳过（无数据）：{skipped}")
    log(f"覆盖率：{completed / total * 100:.1f}%" if total > 0 else "覆盖率：N/A")

    if completed > 0:
        record_counts = []
        date_ranges = []
        for code in list(set(status["completed"]))[:50]:
            exchange = code.split(".")[0]
            filepath = DATA_DIR / exchange / f"{code}.csv"
            if filepath.exists():
                df = pd.read_csv(filepath)
                record_counts.append(len(df))
                if "date" in df.columns and len(df) > 0:
                    date_ranges.append((df["date"].min(), df["date"].max()))

        if record_counts:
            log(f"抽样检查（前{len(record_counts)}只）：")
            log(f"  平均记录数：{sum(record_counts) / len(record_counts):.0f}")
            log(f"  最多记录数：{max(record_counts)}")
            log(f"  最少记录数：{min(record_counts)}")

        if date_ranges:
            earliest = min(d[0] for d in date_ranges)
            latest = max(d[1] for d in date_ranges)
            log(f"  最早日期：{earliest}")
            log(f"  最晚日期：{latest}")

    log("=" * 60)


# ============ 入口 ============

def main():
    parser = argparse.ArgumentParser(description="A股ETF日线数据下载（AkShare/东方财富）")
    parser.add_argument("--check", action="store_true", help="数据质量检查")
    parser.add_argument("--etf", type=str, help="只下载指定ETF（调试用，如 sh.510300）")
    args = parser.parse_args()

    ensure_dirs()

    if args.check:
        check_data_quality(args)
        return 0

    run_failed = full_download(args)
    # 有失败时返回非零退出码，供盘后编排脚本捕获（单步失败不阻断但 rc 汇总需真实）
    return 1 if run_failed else 0


if __name__ == "__main__":
    sys.exit(main())
