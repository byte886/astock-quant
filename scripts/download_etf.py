#!/usr/bin/env python3
"""
A股ETF数据下载脚本（Baostock）— 支持日线 + 5分钟线

特性：
- 全量ETF（沪市+深市，1588只），前复权
- 支持 --freq daily/minute/both
- 防限流：日线 1-2 秒，5分钟线 2-3 秒
- 断点续传：状态文件记录已下载ETF，中断后从断点继续
- 增量更新：--incremental 只下载上次更新后的新数据
- 数据目录：data/raw/etf/daily/ 和 data/raw/etf/minute/

用法：
  python scripts/download_etf.py --freq minute       # 下载ETF 5分钟线
  python scripts/download_etf.py --freq both          # 日线+5分钟线一起下
  python scripts/download_etf.py --freq daily          # 只下日线（默认）
  python scripts/download_etf.py --incremental         # 增量更新
  python scripts/download_etf.py --check               # 数据质量检查
  python scripts/download_etf.py --etf sh.510300      # 只下载指定ETF（调试用）
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
ETF_LIST_FILE = WORKSPACE_DIR / "etf_list.csv"
LOG_FILE = WORKSPACE_DIR / "download.log"

# 日线字段
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg"

# 5分钟线字段
MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"

# 防限流间隔（秒）
DAILY_INTERVAL_MIN = 1.0
DAILY_INTERVAL_MAX = 2.0
MINUTE_INTERVAL_MIN = 2.0
MINUTE_INTERVAL_MAX = 3.0

# 单只下载超时（秒）
DOWNLOAD_TIMEOUT = 300


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


def get_dirs(freq):
    """根据频率返回数据目录和状态文件"""
    if freq == "minute":
        data_dir = PROJECT_ROOT / "data" / "raw" / "etf" / "minute"
        status_file = WORKSPACE_DIR / "download_status_etf_minute.json"
    else:
        data_dir = PROJECT_ROOT / "data" / "raw" / "etf" / "daily"
        status_file = WORKSPACE_DIR / "download_status_etf.json"
    return data_dir, status_file


def ensure_dirs(freq):
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    data_dir, _ = get_dirs(freq)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "sh").mkdir(exist_ok=True)
    (data_dir / "sz").mkdir(exist_ok=True)


def load_status(freq):
    _, status_file = get_dirs(freq)
    if status_file.exists():
        with open(status_file, "r", encoding="utf-8") as f:
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


def save_status(status, freq):
    status["updated_at"] = datetime.now().isoformat()
    _, status_file = get_dirs(freq)
    with open(status_file, "w", encoding="utf-8") as f:
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
        df = pd.read_csv(ETF_LIST_FILE)
        return df

    log("从 Baostock 获取ETF列表...")
    rs = bs.query_all_stock(day="2026-09-16")
    etf_list = []
    while (rs.error_code == '0') & rs.next():
        row = rs.get_row_data()
        code = row[0]
        if (code.startswith("sh.51") or code.startswith("sh.56") or
                code.startswith("sh.58") or code.startswith("sz.15") or
                code.startswith("sz.16")):
            etf_list.append(row)

    df = pd.DataFrame(etf_list, columns=rs.fields)
    df = df.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)
    df.to_csv(ETF_LIST_FILE, index=False, encoding="utf-8")
    log(f"ETF列表已保存：{len(df)} 只")
    return df


def download_one_etf(code, start_date, end_date, freq, max_retry=2):
    """下载单只ETF数据，支持重连重试和超时保护"""
    fields = MINUTE_FIELDS if freq == "minute" else DAILY_FIELDS
    frequency = "5" if freq == "minute" else "d"

    for attempt in range(max_retry + 1):
        try:
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(DOWNLOAD_TIMEOUT)

            rs = bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
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


def save_etf_data(code, df, freq):
    data_dir, _ = get_dirs(freq)
    exchange = code.split(".")[0]
    filepath = data_dir / exchange / f"{code}.csv"
    df.to_csv(filepath, index=False, encoding="utf-8")
    return filepath


# ============ 主流程 ============

def full_download(args):
    """全量下载（断点续传）"""
    freq = args.freq
    freq_name = "5分钟线" if freq == "minute" else "日线"

    log("=" * 60)
    log(f"开始全量ETF{freq_name}数据下载（Baostock，前复权）")
    data_dir, _ = get_dirs(freq)
    log(f"数据目录：{data_dir}")
    log("=" * 60)

    lg = bs.login()
    if lg.error_code != '0':
        log(f"❌ Baostock 登录失败：{lg.error_code} - {lg.error_msg}")
        sys.exit(1)
    log("✅ Baostock 登录成功")

    df_etfs = get_etf_list()
    total = len(df_etfs)
    log(f"共 {total} 只ETF待下载")

    status = load_status(freq)
    if not status["started_at"]:
        status["started_at"] = datetime.now().isoformat()
    status["total_etfs"] = total

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = "2006-01-01" if freq == "minute" else "2000-01-01"

    interval_min = MINUTE_INTERVAL_MIN if freq == "minute" else DAILY_INTERVAL_MIN
    interval_max = MINUTE_INTERVAL_MAX if freq == "minute" else DAILY_INTERVAL_MAX

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
            df = download_one_etf(code, start_date, end_date, freq)

            if df is None or len(df) == 0:
                log(f"{progress} ⚠️ {code} 无数据，跳过")
                status["skipped"].append(code)
            else:
                filepath = save_etf_data(code, df, freq)
                log(f"{progress} ✅ {code} 完成：{len(df)} 条记录")
                status["completed"].append(code)
        except Exception as e:
            log(f"{progress} ❌ {code} 失败：{e}")
            status["failed"].append(code)

        save_status(status, freq)
        time.sleep(random.uniform(interval_min, interval_max))

    status["current_etf"] = None
    save_status(status, freq)

    log("=" * 60)
    log(f"ETF{freq_name}下载完成：成功 {len(status['completed'])}，失败 {len(status['failed'])}，跳过 {len(status['skipped'])}")
    log("=" * 60)

    bs.logout()
    log("✅ Baostock 已退出登录")


def incremental_update(args):
    """增量更新"""
    freq = args.freq
    freq_name = "5分钟线" if freq == "minute" else "日线"

    log("=" * 60)
    log(f"开始ETF{freq_name}增量更新")
    log("=" * 60)

    lg = bs.login()
    if lg.error_code != '0':
        log(f"❌ Baostock 登录失败：{lg.error_code} - {lg.error_msg}")
        sys.exit(1)
    log("✅ Baostock 登录成功")

    status = load_status(freq)
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

    interval_min = MINUTE_INTERVAL_MIN if freq == "minute" else DAILY_INTERVAL_MIN
    interval_max = MINUTE_INTERVAL_MAX if freq == "minute" else DAILY_INTERVAL_MAX

    updated = 0
    for code in status["completed"]:
        try:
            df = download_one_etf(code, start_date, end_date, freq)
            if df is not None and len(df) > 0:
                data_dir, _ = get_dirs(freq)
                exchange = code.split(".")[0]
                filepath = data_dir / exchange / f"{code}.csv"
                if filepath.exists():
                    df_existing = pd.read_csv(filepath)
                    df_combined = pd.concat([df_existing, df], ignore_index=True)
                    subset = ["date", "time"] if freq == "minute" else ["date"]
                    df_combined = df_combined.drop_duplicates(subset=subset, keep="last")
                    df_combined = df_combined.sort_values(subset).reset_index(drop=True)
                    df_combined.to_csv(filepath, index=False, encoding="utf-8")
                else:
                    save_etf_data(code, df, freq)
                updated += 1
                log(f"✅ {code} 增量更新：{len(df)} 条新记录")
            time.sleep(random.uniform(interval_min, interval_max))
        except Exception as e:
            log(f"❌ {code} 增量更新失败：{e}")

    status["updated_at"] = datetime.now().isoformat()
    save_status(status, freq)

    log(f"增量更新完成！更新 {updated} 只ETF")
    bs.logout()


def check_data_quality(args):
    """数据质量检查"""
    freq = args.freq
    freq_name = "5分钟线" if freq == "minute" else "日线"

    log("=" * 60)
    log(f"ETF{freq_name}数据质量检查")
    log("=" * 60)

    status = load_status(freq)
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
        data_dir, _ = get_dirs(freq)
        for code in list(set(status["completed"]))[:50]:
            exchange = code.split(".")[0]
            filepath = data_dir / exchange / f"{code}.csv"
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
    parser = argparse.ArgumentParser(description="A股ETF数据下载（Baostock，日线+5分钟线）")
    parser.add_argument("--freq", type=str, default="daily", choices=["daily", "minute", "both"],
                        help="下载频率：daily(日线)/minute(5分钟线)/both(两者都下)，默认daily")
    parser.add_argument("--incremental", action="store_true", help="增量更新（只下载新数据）")
    parser.add_argument("--check", action="store_true", help="数据质量检查")
    parser.add_argument("--etf", type=str, help="只下载指定ETF（调试用，如 sh.510300）")
    args = parser.parse_args()

    if args.freq == "both":
        # both 模式：先下日线，再下5分钟线
        for f in ["daily", "minute"]:
            args.freq = f
            ensure_dirs(f)
            if args.check:
                check_data_quality(args)
            elif args.incremental:
                incremental_update(args)
            else:
                full_download(args)
    else:
        ensure_dirs(args.freq)
        if args.check:
            check_data_quality(args)
        elif args.incremental:
            incremental_update(args)
        else:
            full_download(args)


if __name__ == "__main__":
    main()
