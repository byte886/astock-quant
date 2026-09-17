#!/usr/bin/env python3
"""
A股ETF日线数据下载脚本（Baostock）

特性：
- 全量ETF（沪市+深市，约1500+只），前复权
- 防限流：单只间隔 1-2 秒（ETF数据量小，请求快）
- 断点续传：状态文件记录已下载ETF，中断后从断点继续
- 增量更新：--incremental 只下载上次更新后的新数据
- 数据目录：data/raw/etf/daily/（按交易所分子目录）

用法：
  python scripts/download_etf.py                     # 全量下载ETF日线（断点续传）
  python scripts/download_etf.py --incremental        # ETF日线增量更新
  python scripts/download_etf.py --check              # 数据质量检查
  python scripts/download_etf.py --etf sh.510300     # 只下载指定ETF（调试用）
"""

import baostock as bs
import pandas as pd
import os
import sys
import time
import json
import signal
import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

# ============ 配置 ============

PROJECT_ROOT = Path(__file__).parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "data" / "_workspace"
DATA_DIR = PROJECT_ROOT / "data" / "raw" / "etf" / "daily"
STATUS_FILE = WORKSPACE_DIR / "download_status_etf.json"
ETF_LIST_FILE = WORKSPACE_DIR / "etf_list.csv"
LOG_FILE = WORKSPACE_DIR / "download.log"

# ETF 日线字段
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg"

# 防限流间隔（秒）
INTERVAL_MIN = 1.0
INTERVAL_MAX = 2.0

# 单只下载超时（秒）
DOWNLOAD_TIMEOUT = 120


class DownloadTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise DownloadTimeout("下载超时")


# ============ 工具函数 ============

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [ETF] {msg}"
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


def baostock_reconnect():
    try:
        bs.logout()
    except Exception:
        pass
    time.sleep(3)
    lg = bs.login()
    if lg.error_code == '0':
        log("🔄 Baostock 重连成功")
        return True
    else:
        log(f"❌ Baostock 重连失败：{lg.error_code} - {lg.error_msg}")
        return False


def get_etf_list():
    """获取全部ETF列表（沪市+深市）"""
    if ETF_LIST_FILE.exists():
        log(f"读取缓存的ETF列表：{ETF_LIST_FILE}")
        df = pd.read_csv(ETF_LIST_FILE)
        return df

    log("从 Baostock 获取ETF列表...")
    rs = bs.query_all_stock(day="2026-09-16")
    etf_list = []
    while (rs.error_code == '0') & rs.next():
        row = rs.get_row_data()
        code = row[0]
        # ETF 代码：sh.51/56/58, sz.15/16
        if (code.startswith("sh.51") or code.startswith("sh.56") or
                code.startswith("sh.58") or code.startswith("sz.15") or
                code.startswith("sz.16")):
            etf_list.append(row)

    df = pd.DataFrame(etf_list, columns=rs.fields)
    df = df.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)
    df.to_csv(ETF_LIST_FILE, index=False, encoding="utf-8")
    log(f"ETF列表已保存：{len(df)} 只")
    return df


def download_one_etf(code, start_date, end_date, max_retry=2):
    """下载单只ETF日线数据，支持重连重试和超时保护"""
    for attempt in range(max_retry + 1):
        try:
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(DOWNLOAD_TIMEOUT)

            rs = bs.query_history_k_data_plus(
                code,
                FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",  # 前复权
            )

            if rs.error_code != '0':
                signal.alarm(0)
                if attempt < max_retry:
                    log(f"⚠️ {code} 遇到错误 {rs.error_code}，第 {attempt+1} 次重连重试...")
                    if baostock_reconnect():
                        continue
                raise Exception(f"Baostock error: {rs.error_code} - {rs.error_msg}")

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())

            signal.alarm(0)

            if not data_list:
                return None

            df = pd.DataFrame(data_list, columns=rs.fields)
            return df

        except DownloadTimeout:
            signal.alarm(0)
            if attempt < max_retry:
                log(f"⏰ {code} 下载超时（{DOWNLOAD_TIMEOUT}秒），第 {attempt+1} 次重连重试...")
                baostock_reconnect()
                continue
            raise Exception(f"下载超时（{DOWNLOAD_TIMEOUT}秒）")
        except Exception as e:
            signal.alarm(0)
            if attempt < max_retry and ("网络" in str(e) or "10002007" in str(e) or "Connection" in str(e)):
                log(f"⚠️ {code} 网络异常，第 {attempt+1} 次重连重试...")
                baostock_reconnect()
                continue
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
    log("开始全量ETF日线数据下载（Baostock，前复权）")
    log(f"数据目录：{DATA_DIR}")
    log("=" * 60)

    lg = bs.login()
    if lg.error_code != '0':
        log(f"❌ Baostock 登录失败：{lg.error_code} - {lg.error_msg}")
        sys.exit(1)
    log("✅ Baostock 登录成功")

    df_etfs = get_etf_list()
    total = len(df_etfs)
    log(f"共 {total} 只ETF待下载")

    status = load_status()
    if not status["started_at"]:
        status["started_at"] = datetime.now().isoformat()
    status["total_etfs"] = total

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = "2000-01-01"  # ETF最早约2000年

    for idx, row in df_etfs.iterrows():
        code = row["code"]
        code_name = row.get("code_name", "")

        if args.etf and code != args.etf:
            continue

        if code in status["completed"]:
            continue

        status["current_etf"] = code
        progress = f"[{idx + 1}/{total}]"

        try:
            log(f"{progress} 下载 {code} {code_name} ...")
            df = download_one_etf(code, start_date, end_date)

            if df is None or len(df) == 0:
                log(f"{progress} ⚠️ {code} 无数据，跳过")
                status["skipped"].append(code)
            else:
                filepath = save_etf_data(code, df)
                log(f"{progress} ✅ {code} 完成：{len(df)} 条记录")
                status["completed"].append(code)
        except Exception as e:
            log(f"{progress} ❌ {code} 失败：{e}")
            status["failed"].append(code)

        save_status(status)
        time.sleep(random.uniform(INTERVAL_MIN, INTERVAL_MAX))

    status["current_etf"] = None
    save_status(status)

    log("=" * 60)
    log(f"ETF下载完成：成功 {len(status['completed'])}，失败 {len(status['failed'])}，跳过 {len(status['skipped'])}")
    log("=" * 60)

    bs.logout()
    log("✅ Baostock 已退出登录")


def incremental_update(args):
    """增量更新"""
    log("=" * 60)
    log("开始ETF日线增量更新")
    log("=" * 60)

    lg = bs.login()
    if lg.error_code != '0':
        log(f"❌ Baostock 登录失败：{lg.error_code} - {lg.error_msg}")
        sys.exit(1)
    log("✅ Baostock 登录成功")

    status = load_status()
    if not status["completed"]:
        log("⚠️ 未找到全量下载记录，请先运行全量下载")
        bs.logout()
        sys.exit(1)

    last_update = status.get("updated_at", "")
    if last_update:
        last_date = last_update[:10]
        start_date = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    end_date = datetime.now().strftime("%Y-%m-%d")
    log(f"增量范围：{start_date} ~ {end_date}")

    updated = 0
    for code in status["completed"]:
        try:
            df = download_one_etf(code, start_date, end_date)
            if df is not None and len(df) > 0:
                exchange = code.split(".")[0]
                filepath = DATA_DIR / exchange / f"{code}.csv"
                if filepath.exists():
                    df_existing = pd.read_csv(filepath)
                    df_combined = pd.concat([df_existing, df], ignore_index=True)
                    df_combined = df_combined.drop_duplicates(subset=["date"], keep="last")
                    df_combined = df_combined.sort_values("date").reset_index(drop=True)
                    df_combined.to_csv(filepath, index=False, encoding="utf-8")
                else:
                    save_etf_data(code, df)
                updated += 1
                log(f"✅ {code} 增量更新：{len(df)} 条新记录")
            time.sleep(random.uniform(INTERVAL_MIN, INTERVAL_MAX))
        except Exception as e:
            log(f"❌ {code} 增量更新失败：{e}")

    status["updated_at"] = datetime.now().isoformat()
    save_status(status)

    log(f"增量更新完成！更新 {updated} 只ETF")
    bs.logout()


def check_data_quality(args):
    """数据质量检查"""
    log("=" * 60)
    log("ETF数据质量检查")
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
        for code in list(set(status["completed"]))[:50]:
            exchange = code.split(".")[0]
            filepath = DATA_DIR / exchange / f"{code}.csv"
            if filepath.exists():
                df = pd.read_csv(filepath)
                record_counts.append(len(df))

        if record_counts:
            log(f"抽样检查（前{len(record_counts)}只）：")
            log(f"  平均记录数：{sum(record_counts) / len(record_counts):.0f}")
            log(f"  最多记录数：{max(record_counts)}")
            log(f"  最少记录数：{min(record_counts)}")

    log("=" * 60)


# ============ 入口 ============

def main():
    parser = argparse.ArgumentParser(description="A股ETF日线数据下载（Baostock）")
    parser.add_argument("--incremental", action="store_true", help="增量更新（只下载新数据）")
    parser.add_argument("--check", action="store_true", help="数据质量检查")
    parser.add_argument("--etf", type=str, help="只下载指定ETF（调试用，如 sh.510300）")
    args = parser.parse_args()

    ensure_dirs()

    if args.check:
        check_data_quality(args)
    elif args.incremental:
        incremental_update(args)
    else:
        full_download(args)


if __name__ == "__main__":
    main()
