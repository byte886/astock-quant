#!/usr/bin/env python3
"""
A股全量历史数据下载脚本（Baostock）

特性：
- 全量A股（含已退市），日线数据，1990年至今
- 防限流：单只股票间隔 2-3 秒
- 断点续传：状态文件记录已下载股票，中断后从断点继续
- 增量更新：--incremental 只下载上次更新后的新数据
- 数据质量检查：--check 统计覆盖率、缺失、异常
- 数据目录：data/raw/daily/（按交易所分子目录）

用法：
  python scripts/download_data.py              # 全量下载（断点续传）
  python scripts/download_data.py --incremental # 增量更新（只下载新数据）
  python scripts/download_data.py --check       # 数据质量检查
  python scripts/download_data.py --stock sh.600519  # 只下载指定股票（调试用）
"""

import baostock as bs
import pandas as pd
import os
import sys
import time
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# ============ 配置 ============

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
WORKSPACE_DIR = PROJECT_ROOT / "data" / "_workspace"
STATUS_FILE = WORKSPACE_DIR / "download_status.json"
STOCK_LIST_FILE = WORKSPACE_DIR / "stock_list.csv"
LOG_FILE = WORKSPACE_DIR / "download.log"

# Baostock 日线字段
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"

# 防限流：单只股票间隔（秒）
REQUEST_INTERVAL_MIN = 2.0
REQUEST_INTERVAL_MAX = 3.0

# 起始日期（A股1990年12月开市）
START_DATE = "1990-12-19"

# ============ 工具函数 ============

def log(msg):
    """写日志并打印"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ensure_dirs():
    """确保目录存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    # 按交易所分子目录
    (DATA_DIR / "sh").mkdir(exist_ok=True)
    (DATA_DIR / "sz").mkdir(exist_ok=True)


def load_status():
    """加载下载状态"""
    if STATUS_FILE.exists():
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
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


def save_status(status):
    """保存下载状态"""
    status["updated_at"] = datetime.now().isoformat()
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def sleep_interval():
    """防限流：随机间隔 2-3 秒"""
    import random
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
    df = df.sort_values("code").reset_index(drop=True)

    df.to_csv(STOCK_LIST_FILE, index=False, encoding="utf-8")
    log(f"股票列表已保存：{len(df)} 只股票")
    return df


def download_one_stock(code, start_date, end_date):
    """下载单只股票的日线数据"""
    rs = bs.query_history_k_data_plus(
        code,
        DAILY_FIELDS,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2",  # 2=前复权
    )

    if rs.error_code != '0':
        raise Exception(f"Baostock error: {rs.error_code} - {rs.error_msg}")

    data_list = []
    while rs.next():
        data_list.append(rs.get_row_data())

    if not data_list:
        return None

    df = pd.DataFrame(data_list, columns=rs.fields)
    return df


def save_stock_data(code, df):
    """保存单只股票数据"""
    exchange = code.split(".")[0]  # sh / sz
    filepath = DATA_DIR / exchange / f"{code}.csv"
    df.to_csv(filepath, index=False, encoding="utf-8")
    return filepath


# ============ 主流程 ============

def full_download(args):
    """全量下载（断点续传）"""
    log("=" * 60)
    log("开始全量A股日线数据下载（Baostock，前复权）")
    log(f"数据目录：{DATA_DIR}")
    log(f"状态文件：{STATUS_FILE}")
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

    # 加载状态
    status = load_status()
    if not status["started_at"]:
        status["started_at"] = datetime.now().isoformat()
    status["total_stocks"] = total

    completed_set = set(status["completed"])
    failed_set = set(status["failed"])

    # 确定结束日期
    end_date = datetime.now().strftime("%Y-%m-%d")

    # 遍历下载
    for idx, row in df_stocks.iterrows():
        code = row["code"]
        code_name = row.get("code_name", "")

        # 跳过已完成
        if code in completed_set:
            continue

        # 如果指定了单只股票
        if args.stock and code != args.stock:
            continue

        status["current_stock"] = code
        progress = f"[{idx + 1}/{total}]"

        try:
            log(f"{progress} 下载 {code} {code_name} ...")
            df = download_one_stock(code, START_DATE, end_date)

            if df is None or len(df) == 0:
                log(f"{progress} ⚠️ {code} 无数据（可能已退市或长期停牌），跳过")
                status["skipped"].append(code)
            else:
                filepath = save_stock_data(code, df)
                log(f"{progress} ✅ {code} 完成：{len(df)} 条记录 → {filepath.name}")
                status["completed"].append(code)

        except Exception as e:
            log(f"{progress} ❌ {code} 失败：{e}")
            status["failed"].append(code)

        # 保存状态（每只股票都保存，防中断丢失进度）
        save_status(status)

        # 防限流
        sleep_interval()

    # 完成
    status["current_stock"] = None
    save_status(status)

    log("=" * 60)
    log(f"下载完成！成功：{len(status['completed'])}，失败：{len(status['failed'])}，跳过：{len(status['skipped'])}")
    if status["failed"]:
        log(f"失败列表（前20）：{status['failed'][:20]}")
    log("=" * 60)

    bs.logout()
    log("✅ Baostock 已退出登录")


def incremental_update(args):
    """增量更新（只下载上次更新后的新数据）"""
    log("=" * 60)
    log("开始增量更新（只下载新数据）")
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
            df = download_one_stock(code, start_date, end_date)
            if df is not None and len(df) > 0:
                # 追加到已有文件
                exchange = code.split(".")[0]
                filepath = DATA_DIR / exchange / f"{code}.csv"
                if filepath.exists():
                    df_existing = pd.read_csv(filepath)
                    df_combined = pd.concat([df_existing, df], ignore_index=True)
                    df_combined = df_combined.drop_duplicates(subset=["date"], keep="last")
                    df_combined = df_combined.sort_values("date").reset_index(drop=True)
                    df_combined.to_csv(filepath, index=False, encoding="utf-8")
                else:
                    save_stock_data(code, df)
                updated += 1
                log(f"✅ {code} 增量更新：{len(df)} 条新记录")
            sleep_interval()
        except Exception as e:
            log(f"❌ {code} 增量更新失败：{e}")

    status["updated_at"] = datetime.now().isoformat()
    save_status(status)

    log(f"增量更新完成！更新 {updated} 只股票")
    bs.logout()


def check_data_quality(args):
    """数据质量检查"""
    log("=" * 60)
    log("数据质量检查")
    log("=" * 60)

    status = load_status()
    total = status.get("total_stocks", 0)
    completed = len(status.get("completed", []))
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
        for code in status["completed"][:100]:  # 抽样检查前100只
            exchange = code.split(".")[0]
            filepath = DATA_DIR / exchange / f"{code}.csv"
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
    parser.add_argument("--incremental", action="store_true", help="增量更新（只下载新数据）")
    parser.add_argument("--check", action="store_true", help="数据质量检查")
    parser.add_argument("--stock", type=str, help="只下载指定股票（调试用，如 sh.600519）")
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
