#!/usr/bin/env python3
"""
A股全量历史数据下载脚本（Baostock）

特性：
- 支持日线（36年完整）和5分钟线（最近几年）两种频率
- 全量A股（含已退市），防限流：单只股票间隔 2-3 秒
- 断点续传：状态文件记录已下载股票，中断后从断点继续
- 增量更新：--incremental 只下载上次更新后的新数据
- 数据质量检查：--check 统计覆盖率、缺失、异常
- 数据目录：data/raw/daily/ 和 data/raw/minute/（按交易所分子目录）

用法：
  python scripts/download_data.py                     # 全量下载日线（断点续传）
  python scripts/download_data.py --freq minute       # 全量下载5分钟线
  python scripts/download_data.py --incremental        # 日线增量更新
  python scripts/download_data.py --freq minute --incremental  # 分钟线增量更新
  python scripts/download_data.py --check              # 数据质量检查
  python scripts/download_data.py --stock sh.600519   # 只下载指定股票（调试用）
"""

import baostock as bs
import pandas as pd
import os
import sys
import time
import json
import signal
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# ============ 配置 ============

PROJECT_ROOT = Path(__file__).parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "data" / "_workspace"

# 频率配置：数据目录、状态文件、字段、起始日期
FREQ_CONFIG = {
    "daily": {
        "data_dir": PROJECT_ROOT / "data" / "raw" / "daily",
        "status_file": WORKSPACE_DIR / "download_status_daily.json",
        "fields": "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
        "frequency": "d",
        "start_date": "1990-12-19",  # A股开市
        "label": "日线",
    },
    "minute": {
        "data_dir": PROJECT_ROOT / "data" / "raw" / "minute",
        "status_file": WORKSPACE_DIR / "download_status_minute.json",
        "fields": "date,time,code,open,high,low,close,volume,amount,adjustflag",
        "frequency": "5",  # 5分钟线（Baostock最细粒度）
        "start_date": "2006-01-01",  # Baostock分钟线最早约2006年
        "label": "5分钟线",
    },
}

# 股票列表缓存（日线和分钟线共用同一份股票列表）
STOCK_LIST_FILE = WORKSPACE_DIR / "stock_list.csv"
LOG_FILE = WORKSPACE_DIR / "download.log"

# 防限流：单只股票间隔（秒）
REQUEST_INTERVAL_MIN = 2.0
REQUEST_INTERVAL_MAX = 3.0

# 单只股票下载超时（秒），防止 Baostock 挂起导致进程卡死
DOWNLOAD_TIMEOUT = 300  # 5分钟


class DownloadTimeout(Exception):
    """下载超时异常"""
    pass


def _timeout_handler(signum, frame):
    raise DownloadTimeout("Baostock 下载超时")

# ============ 工具函数 ============

def log(msg):
    """写日志并打印"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ensure_dirs(freq):
    """确保目录存在"""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    freqs = ["daily", "minute"] if freq == "both" else [freq]
    for f in freqs:
        data_dir = FREQ_CONFIG[f]["data_dir"]
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "sh").mkdir(exist_ok=True)
        (data_dir / "sz").mkdir(exist_ok=True)


def load_status(freq):
    """加载下载状态"""
    status_file = FREQ_CONFIG[freq]["status_file"]
    if status_file.exists():
        with open(status_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "started_at": None,
        "updated_at": None,
        "total_stocks": 0,
        "completed": [],
        "failed": [],
        "skipped": [],
        "current_stock": None,
    }


def save_status(freq, status):
    """保存下载状态"""
    status_file = FREQ_CONFIG[freq]["status_file"]
    status["updated_at"] = datetime.now().isoformat()
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def sleep_interval(freq):
    """防限流：随机间隔。分钟线用更大间隔避免与日线进程叠加限流。"""
    import random
    if freq == "minute":
        time.sleep(random.uniform(5.0, 6.0))
    else:
        time.sleep(random.uniform(REQUEST_INTERVAL_MIN, REQUEST_INTERVAL_MAX))


def get_stock_list():
    """获取全部A股股票列表（含已退市）"""
    if STOCK_LIST_FILE.exists():
        log(f"读取缓存的股票列表：{STOCK_LIST_FILE}")
        df = pd.read_csv(STOCK_LIST_FILE)
        return df

    log("从 Baostock 获取股票列表（全部A股，含已退市）...")
    # 获取上证股票
    rs_sh = bs.query_all_stock(day="2026-09-15")
    sh_list = []
    while (rs_sh.error_code == '0') & rs_sh.next():
        sh_list.append(rs_sh.get_row_data())
    df_sh = pd.DataFrame(sh_list, columns=rs_sh.fields)

    sleep_interval()

    # 获取深证股票
    rs_sz = bs.query_all_stock(day="2026-09-15")
    sz_list = []
    while (rs_sz.error_code == '0') & rs_sz.next():
        sz_list.append(rs_sz.get_row_data())
    df_sz = pd.DataFrame(sz_list, columns=rs_sz.fields)

    df = pd.concat([df_sh, df_sz], ignore_index=True)
    # 只保留股票（code 以 sh.6 / sz.0 / sz.3 开头），排除指数和基金
    df = df[df["code"].str.match(r"^(sh\.6|sz\.0|sz\.3)")]
    # 排除科创板（sh.688）——用户明确不需要
    df = df[~df["code"].str.startswith("sh.688")]
    # 去重（同一股票可能有多条状态记录）
    df = df.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)

    df.to_csv(STOCK_LIST_FILE, index=False, encoding="utf-8")
    log(f"股票列表已保存：{len(df)} 只股票")
    return df


def baostock_reconnect():
    """Baostock 断线重连：先退出再重新登录"""
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


def download_one_stock(code, start_date, end_date, freq, max_retry=2):
    """下载单只股票数据，支持断线重连重试和超时保护"""
    cfg = FREQ_CONFIG[freq]
    for attempt in range(max_retry + 1):
        try:
            # 设置超时闹钟（防止 Baostock 挂起死循环）
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(DOWNLOAD_TIMEOUT)

            rs = bs.query_history_k_data_plus(
                code,
                cfg["fields"],
                start_date=start_date,
                end_date=end_date,
                frequency=cfg["frequency"],
                adjustflag="2",  # 2=前复权
            )

            if rs.error_code != '0':
                signal.alarm(0)  # 取消闹钟
                # 网络错误（10002007）或登录态失效，尝试重连后重试
                if attempt < max_retry:
                    log(f"⚠️ {code} 遇到错误 {rs.error_code}，第 {attempt+1} 次重连重试...")
                    if baostock_reconnect():
                        continue
                raise Exception(f"Baostock error: {rs.error_code} - {rs.error_msg}")

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())

            signal.alarm(0)  # 取消闹钟

            if not data_list:
                return None

            df = pd.DataFrame(data_list, columns=rs.fields)
            return df

        except DownloadTimeout:
            signal.alarm(0)  # 确保取消闹钟
            if attempt < max_retry:
                log(f"⏰ {code} 下载超时（{DOWNLOAD_TIMEOUT}秒），第 {attempt+1} 次重连重试...")
                baostock_reconnect()
                continue
            raise Exception(f"下载超时（{DOWNLOAD_TIMEOUT}秒），Baostock 可能挂起")
        except Exception as e:
            signal.alarm(0)  # 确保取消闹钟
            # 网络异常也尝试重连
            if attempt < max_retry and ("网络" in str(e) or "10002007" in str(e) or "Connection" in str(e)):
                log(f"⚠️ {code} 网络异常，第 {attempt+1} 次重连重试...")
                baostock_reconnect()
                continue
            raise


def save_stock_data(code, df, freq):
    """保存单只股票数据"""
    cfg = FREQ_CONFIG[freq]
    exchange = code.split(".")[0]  # sh / sz
    filepath = cfg["data_dir"] / exchange / f"{code}.csv"
    df.to_csv(filepath, index=False, encoding="utf-8")
    return filepath


# ============ 主流程 ============

def full_download(args):
    """全量下载（断点续传）"""
    freq = args.freq
    # both 模式：同时下载日线和分钟线，用日线配置做外层循环
    outer_freq = "daily" if freq == "both" else freq
    cfg = FREQ_CONFIG[outer_freq]
    label = "日线+分钟线" if freq == "both" else cfg["label"]

    log("=" * 60)
    log(f"开始全量A股{label}数据下载（Baostock，前复权）")
    log(f"数据目录：{cfg['data_dir']}" + (f" 和 {FREQ_CONFIG['minute']['data_dir']}" if freq == "both" else ""))
    log("=" * 60)

    # 登录
    lg = bs.login()
    if lg.error_code != '0':
        log(f"❌ Baostock 登录失败：{lg.error_code} - {lg.error_msg}")
        sys.exit(1)
    log("✅ Baostock 登录成功")

    # 获取股票列表
    df_stocks = get_stock_list()
    total = len(df_stocks)
    log(f"共 {total} 只股票待下载")

    # 加载状态（both 模式加载两个状态文件）
    status_daily = load_status("daily")
    status_minute = load_status("minute") if freq == "both" else None

    if not status_daily["started_at"]:
        status_daily["started_at"] = datetime.now().isoformat()
    status_daily["total_stocks"] = total
    if freq == "both":
        if not status_minute["started_at"]:
            status_minute["started_at"] = datetime.now().isoformat()
        status_minute["total_stocks"] = total

    # 确定结束日期
    end_date = datetime.now().strftime("%Y-%m-%d")

    # 遍历下载
    for idx, row in df_stocks.iterrows():
        code = row["code"]
        code_name = row.get("code_name", "")

        # 如果指定了单只股票
        if args.stock and code != args.stock:
            continue

        # both 模式：如果日线和分钟线都已完成，跳过
        if freq == "both":
            if code in status_daily["completed"] and code in status_minute["completed"]:
                continue
        else:
            if code in status_daily["completed"]:
                continue

        status_daily["current_stock"] = code
        progress = f"[{idx + 1}/{total}]"

        # ---- 下载日线 ----
        if code not in status_daily["completed"]:
            try:
                log(f"{progress} [日线] 下载 {code} {code_name} ...")
                df = download_one_stock(code, FREQ_CONFIG["daily"]["start_date"], end_date, "daily")

                if df is None or len(df) == 0:
                    log(f"{progress} [日线] ⚠️ {code} 无数据，跳过")
                    status_daily["skipped"].append(code)
                else:
                    filepath = save_stock_data(code, df, "daily")
                    log(f"{progress} [日线] ✅ {code} 完成：{len(df)} 条记录")
                    status_daily["completed"].append(code)
            except Exception as e:
                log(f"{progress} [日线] ❌ {code} 失败：{e}")
                status_daily["failed"].append(code)
            save_status("daily", status_daily)
            sleep_interval("daily")

        # ---- 下载分钟线（both 模式）----
        if freq == "both" and code not in status_minute["completed"]:
            try:
                log(f"{progress} [分钟线] 下载 {code} {code_name} ...")
                df = download_one_stock(code, FREQ_CONFIG["minute"]["start_date"], end_date, "minute")

                if df is None or len(df) == 0:
                    log(f"{progress} [分钟线] ⚠️ {code} 无数据，跳过")
                    status_minute["skipped"].append(code)
                else:
                    filepath = save_stock_data(code, df, "minute")
                    log(f"{progress} [分钟线] ✅ {code} 完成：{len(df)} 条记录")
                    status_minute["completed"].append(code)
            except Exception as e:
                log(f"{progress} [分钟线] ❌ {code} 失败：{e}")
                status_minute["failed"].append(code)
            save_status("minute", status_minute)
            sleep_interval("minute")

    # 完成
    status_daily["current_stock"] = None
    save_status("daily", status_daily)
    if freq == "both":
        status_minute["current_stock"] = None
        save_status("minute", status_minute)

    log("=" * 60)
    log(f"日线：成功 {len(status_daily['completed'])}，失败 {len(status_daily['failed'])}，跳过 {len(status_daily['skipped'])}")
    if freq == "both":
        log(f"分钟线：成功 {len(status_minute['completed'])}，失败 {len(status_minute['failed'])}，跳过 {len(status_minute['skipped'])}")
    log("=" * 60)

    bs.logout()
    log("✅ Baostock 已退出登录")


def incremental_update(args):
    """增量更新（只下载上次更新后的新数据）"""
    freq = args.freq
    cfg = FREQ_CONFIG[freq]

    log("=" * 60)
    log(f"开始{cfg['label']}增量更新（只下载新数据）")
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

    # 确定增量起始日期（上次更新日期 + 1天）
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
            df = download_one_stock(code, start_date, end_date, freq)
            if df is not None and len(df) > 0:
                # 追加到已有文件
                exchange = code.split(".")[0]
                filepath = cfg["data_dir"] / exchange / f"{code}.csv"
                if filepath.exists():
                    df_existing = pd.read_csv(filepath)
                    # 分钟线按 date+time 去重，日线按 date 去重
                    subset = ["date", "time"] if freq == "minute" else ["date"]
                    df_combined = pd.concat([df_existing, df], ignore_index=True)
                    df_combined = df_combined.drop_duplicates(subset=subset, keep="last")
                    df_combined = df_combined.sort_values(subset).reset_index(drop=True)
                    df_combined.to_csv(filepath, index=False, encoding="utf-8")
                else:
                    save_stock_data(code, df, freq)
                updated += 1
                log(f"✅ {code} 增量更新：{len(df)} 条新记录")
            sleep_interval(freq)
        except Exception as e:
            log(f"❌ {code} 增量更新失败：{e}")

    status["updated_at"] = datetime.now().isoformat()
    save_status(freq, status)

    log(f"增量更新完成！更新 {updated} 只股票")
    bs.logout()


def check_data_quality(args):
    """数据质量检查"""
    freq = args.freq
    cfg = FREQ_CONFIG[freq]

    log("=" * 60)
    log(f"{cfg['label']}数据质量检查")
    log("=" * 60)

    status = load_status(freq)
    total = status.get("total_stocks", 0)
    completed = len(set(status.get("completed", [])))
    failed = len(status.get("failed", []))
    skipped = len(status.get("skipped", []))

    log(f"股票总数：{total}")
    log(f"已下载：{completed}")
    log(f"失败：{failed}")
    log(f"跳过（无数据）：{skipped}")
    log(f"覆盖率：{completed / total * 100:.1f}%" if total > 0 else "覆盖率：N/A")

    # 检查已下载文件的记录数
    if completed > 0:
        record_counts = []
        empty_files = []
        for code in list(set(status["completed"]))[:100]:  # 抽样检查前100只
            exchange = code.split(".")[0]
            filepath = cfg["data_dir"] / exchange / f"{code}.csv"
            if filepath.exists():
                df = pd.read_csv(filepath)
                record_counts.append(len(df))
                if len(df) == 0:
                    empty_files.append(code)
            else:
                empty_files.append(code)

        if record_counts:
            log(f"抽样检查（前{len(record_counts)}只）：")
            log(f"  平均记录数：{sum(record_counts) / len(record_counts):.0f}")
            log(f"  最多记录数：{max(record_counts)}")
            log(f"  最少记录数：{min(record_counts)}")
            log(f"  空文件数：{len(empty_files)}")

    log("=" * 60)


# ============ 入口 ============

def main():
    parser = argparse.ArgumentParser(description="A股全量历史数据下载（Baostock）")
    parser.add_argument("--freq", choices=["daily", "minute", "both"], default="daily",
                        help="数据频率：daily=日线（默认），minute=5分钟线，both=日线+分钟线（单进程，防限流）")
    parser.add_argument("--incremental", action="store_true", help="增量更新（只下载新数据）")
    parser.add_argument("--check", action="store_true", help="数据质量检查")
    parser.add_argument("--stock", type=str, help="只下载指定股票（调试用，如 sh.600519）")
    args = parser.parse_args()

    ensure_dirs(args.freq)

    if args.check:
        check_data_quality(args)
    elif args.incremental:
        incremental_update(args)
    else:
        full_download(args)


if __name__ == "__main__":
    main()
