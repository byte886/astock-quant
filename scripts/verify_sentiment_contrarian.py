#!/usr/bin/env python3
"""
散户情绪反向指标 · 历史数据验证脚本

验证逻辑：
- 用成交量/换手率作为散户情绪的代理指标
- 放量（情绪高）后，未来收益是否偏低？
- 缩量（情绪低）后，未来收益是否偏高？
- 如果成立，则"散户情绪是反向指标"逻辑正确

用法：
  python scripts/verify_sentiment_contrarian.py
  python scripts/verify_sentiment_contrarian.py --sample 100   # 只抽样100只
"""

import pandas as pd
import numpy as np
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime

# ============ 配置 ============

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
OUTPUT_DIR = PROJECT_ROOT / "results" / "sentiment_verification"

# 情绪代理指标参数
SHORT_WINDOW = 5       # 短期均量窗口
LONG_WINDOW = 60       # 长期均量窗口

# 分组
GROUPS = 5  # 极高/高/中/低/极低

# 未来收益窗口
FORWARD_WINDOWS = [5, 10, 20]

# 最少数据量要求
MIN_RECORDS = 200


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def load_all_stocks(sample_size=None):
    """加载所有已下载的个股日线数据"""
    all_data = []
    stock_count = 0

    for exchange in ["sh", "sz"]:
        exchange_dir = DATA_DIR / exchange
        if not exchange_dir.exists():
            continue

        files = sorted(exchange_dir.glob("*.csv"))
        if sample_size:
            files = files[:sample_size]

        for filepath in files:
            code = filepath.stem
            try:
                df = pd.read_csv(filepath)
                if len(df) < MIN_RECORDS:
                    continue

                # 确保列名正确
                df["code"] = code
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)

                # 转换数值列
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")

                all_data.append(df)
                stock_count += 1
            except Exception as e:
                log(f"⚠️ 加载 {code} 失败：{e}")

    log(f"共加载 {stock_count} 只股票，{sum(len(d) for d in all_data)} 条记录")
    return all_data


def calculate_sentiment_proxy(df):
    """计算情绪代理指标：短期均量/长期均量"""
    df = df.copy()

    # 成交量均线
    df["vol_short_ma"] = df["volume"].rolling(SHORT_WINDOW).mean()
    df["vol_long_ma"] = df["volume"].rolling(LONG_WINDOW).mean()

    # 情绪代理指标 = 短期均量 / 长期均量
    df["sentiment_proxy"] = df["vol_short_ma"] / df["vol_long_ma"]

    # 换手率（如果有）
    if "turn" in df.columns:
        df["turn"] = pd.to_numeric(df["turn"], errors="coerce")
        df["turn_short_ma"] = df["turn"].rolling(SHORT_WINDOW).mean()
        df["turn_long_ma"] = df["turn"].rolling(LONG_WINDOW).mean()
        df["sentiment_turn"] = df["turn_short_ma"] / df["turn_long_ma"]

    # 未来收益
    for n in FORWARD_WINDOWS:
        df[f"forward_return_{n}d"] = df["close"].shift(-n) / df["close"] - 1

    return df


def run_verification(all_data, proxy_col="sentiment_proxy"):
    """运行验证：按情绪代理指标分组，统计未来收益"""
    log(f"开始验证（代理指标：{proxy_col}）...")

    # 合并所有股票数据
    combined = []
    for df in all_data:
        df = calculate_sentiment_proxy(df)
        # 只保留有有效数据的行
        valid = df.dropna(subset=[proxy_col] + [f"forward_return_{n}d" for n in FORWARD_WINDOWS])
        combined.append(valid[[proxy_col] + [f"forward_return_{n}d" for n in FORWARD_WINDOWS]])

    combined_df = pd.concat(combined, ignore_index=True)
    log(f"有效样本数：{len(combined_df)}")

    # 按情绪代理指标分组
    combined_df["group"] = pd.qcut(combined_df[proxy_col], GROUPS, labels=["极低", "低", "中", "高", "极高"])

    # 统计每组的未来收益
    results = []
    for group_name in ["极低", "低", "中", "高", "极高"]:
        group_data = combined_df[combined_df["group"] == group_name]
        row = {"分组": group_name, "样本数": len(group_data)}
        for n in FORWARD_WINDOWS:
            col = f"forward_return_{n}d"
            row[f"未来{n}日平均收益"] = group_data[col].mean()
            row[f"未来{n}日中位数收益"] = group_data[col].median()
            row[f"未来{n}日胜率"] = (group_data[col] > 0).mean()
        results.append(row)

    results_df = pd.DataFrame(results)

    # 计算多空收益差（极低组 - 极高组）
    log("=" * 70)
    log(f"验证结果（代理指标：{proxy_col}）")
    log("=" * 70)
    print()
    print(results_df.to_string(index=False, float_format=lambda x: f"{x:.4f}" if abs(x) < 1 else f"{x:.0f}"))
    print()

    # 多空收益差
    log("-" * 70)
    log("多空收益差（极低组 - 极高组，正值=反向指标成立）：")
    for n in FORWARD_WINDOWS:
        low_return = results_df[results_df["分组"] == "极低"][f"未来{n}日平均收益"].values[0]
        high_return = results_df[results_df["分组"] == "极高"][f"未来{n}日平均收益"].values[0]
        diff = low_return - high_return
        log(f"  未来{n}日：{diff:.4f} ({diff*100:.2f}%) {'✅ 反向指标成立' if diff > 0 else '❌ 不成立'}")

    log("-" * 70)

    # 保存结果
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"verification_{proxy_col}.csv"
    results_df.to_csv(output_file, index=False, encoding="utf-8")
    log(f"结果已保存：{output_file}")

    return results_df


def main():
    parser = argparse.ArgumentParser(description="散户情绪反向指标历史数据验证")
    parser.add_argument("--sample", type=int, default=None, help="抽样股票数量（默认全部）")
    args = parser.parse_args()

    log("=" * 70)
    log("散户情绪反向指标 · 历史数据验证")
    log(f"验证逻辑：放量（情绪高）后未来收益是否偏低？缩量（情绪低）后是否偏高？")
    log("=" * 70)

    # 加载数据
    all_data = load_all_stocks(sample_size=args.sample)

    if not all_data:
        log("❌ 没有可用数据，请先下载个股日线数据")
        sys.exit(1)

    # 验证1：成交量代理
    results_vol = run_verification(all_data, proxy_col="sentiment_proxy")

    # 验证2：换手率代理（如果数据中有换手率）
    has_turn = any("turn" in df.columns for df in all_data)
    if has_turn:
        results_turn = run_verification(all_data, proxy_col="sentiment_turn")

    log("=" * 70)
    log("验证完成！")
    log("=" * 70)


if __name__ == "__main__":
    main()
