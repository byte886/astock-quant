#!/usr/bin/env python3
"""
ETF轮动策略回测引擎（MVP）

策略逻辑：
- 候选池：22只ETF（宽基6+行业9+跨境4+防御3）
- 月度调仓：每月最后一个交易日计算动量，选动量最高的N只
- 动量回看期：3/6/12个月（对比三种参数）
- 持仓数量：3-5只（等权配置）
- 防御机制：当所有候选ETF动量都为负时，持有国债/货币ETF
- 交易成本：佣金万2.5，滑点千1

用法：
  python scripts/backtest_etf_rotation.py
  python scripts/backtest_etf_rotation.py --momentum 6 --holdings 4
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
DATA_DIR = PROJECT_ROOT / "data" / "raw" / "etf" / "daily"
OUTPUT_DIR = PROJECT_ROOT / "results" / "etf_rotation_backtest"

# 候选池32只ETF
CANDIDATE_POOL = {
    # 宽基6只
    "sh.510300": "沪深300ETF",
    "sh.510500": "中证500ETF",
    "sz.159915": "创业板ETF",
    "sh.510050": "上证50ETF",
    "sh.588000": "科创50ETF",
    "sh.512100": "中证1000ETF",
    # 行业19只
    "sh.512010": "医药ETF",
    "sz.159928": "消费ETF",
    "sh.515000": "科技ETF",
    "sh.516160": "新能源ETF",
    "sh.512000": "券商ETF",
    "sh.512800": "银行ETF",
    "sh.512660": "军工ETF",
    "sh.512480": "半导体ETF",
    "sh.515880": "通信ETF",
    "sh.512200": "房地产ETF",
    "sh.512980": "传媒ETF",
    "sh.512400": "有色金属ETF",
    "sh.515220": "煤炭ETF",
    "sh.515210": "钢铁ETF",
    "sz.159870": "化工ETF",
    "sh.516950": "基建ETF",
    "sz.159825": "农业ETF",
    "sh.512580": "环保ETF",
    # 跨境4只
    "sh.513100": "纳指ETF",
    "sh.513500": "标普500ETF",
    "sh.513130": "恒生科技ETF",
    "sz.159920": "恒生ETF",
    # 防御3只
    "sh.511010": "国债ETF",
    "sh.511990": "货币ETF",
    "sh.518880": "黄金ETF",
}

# 防御资产（动量全负时持有）— 只持有货币ETF，几乎不跌
DEFENSIVE_ASSETS = ["sh.511990"]  # 货币ETF

# 大盘趋势过滤均线周期
TREND_MA_PERIOD = 200  # 200日均线，放松风控

# 回测起始日期（避开2010年前前复权价格异常的问题）
BACKTEST_START_DATE = "2010-01-01"

# 交易成本
COMMISSION_RATE = 0.00025  # 佣金万2.5
SLIPPAGE_RATE = 0.001      # 滑点千1

# 动量回看期（月）
MOMENTUM_MONTHS = [3, 6, 12]

# 持仓数量
HOLDINGS_OPTIONS = [3, 4, 5]


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def load_etf_data():
    """加载候选池ETF的日线数据"""
    all_data = {}
    missing = []

    for code, name in CANDIDATE_POOL.items():
        exchange = code.split(".")[0]
        filepath = DATA_DIR / exchange / f"{code}.csv"

        if not filepath.exists():
            missing.append(f"{code}({name})")
            continue

        try:
            df = pd.read_csv(filepath)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            # 数据清洗：过滤价格<=0的异常值（前复权早期可能为负），然后前向填充
            df.loc[df["close"] <= 0, "close"] = np.nan
            df["close"] = df["close"].ffill().bfill()
            df = df.dropna(subset=["close"])
            # 只保留有足够历史数据的ETF（至少500个交易日，约2年）
            if len(df) < 500:
                missing.append(f"{code}({name})-数据不足({len(df)}条)")
                continue
            all_data[code] = df
        except Exception as e:
            log(f"⚠️ 加载 {code}({name}) 失败：{e}")
            missing.append(f"{code}({name})")

    log(f"成功加载 {len(all_data)}/{len(CANDIDATE_POOL)} 只ETF")
    if missing:
        log(f"缺失：{', '.join(missing)}")

    return all_data


def calculate_momentum(prices, end_date, months):
    """计算动量：end_date前months个月的收益率"""
    start_date = end_date - pd.DateOffset(months=months)
    # 找到最接近的日期
    valid_prices = prices[prices.index <= end_date]
    if len(valid_prices) < 2:
        return np.nan

    end_price = valid_prices.iloc[-1]
    start_prices = valid_prices[valid_prices.index >= start_date]
    if len(start_prices) < 2:
        return np.nan
    start_price = start_prices.iloc[0]

    if start_price == 0:
        return np.nan

    return end_price / start_price - 1


def get_month_end_dates(dates):
    """获取所有月末日期（用于调仓）"""
    df = pd.DataFrame({"date": dates})
    df["year_month"] = df["date"].dt.to_period("M")
    month_ends = df.groupby("year_month")["date"].max().values
    return pd.to_datetime(month_ends)


def run_backtest(all_data, momentum_months=6, num_holdings=4, use_risk_control=True, use_dual_momentum=True):
    """运行单次回测
    use_risk_control: 是否启用风控（大盘趋势过滤+改进防御触发）
    use_dual_momentum: 是否使用双动量策略（绝对动量>0 且 相对动量>0，按相对动量排序）
    """
    # 合并所有ETF的收盘价
    close_dict = {}
    for code, df in all_data.items():
        close_dict[code] = df.set_index("date")["close"]

    close_df = pd.DataFrame(close_dict)
    close_df = close_df.ffill()  # 前向填充缺失值

    # 大盘趋势过滤：用沪深300ETF的100日均线
    BENCHMARK_CODE = "sh.510300"  # 沪深300ETF
    if BENCHMARK_CODE in close_df.columns:
        close_df["benchmark_ma"] = close_df[BENCHMARK_CODE].rolling(TREND_MA_PERIOD).mean()
    else:
        close_df["benchmark_ma"] = np.nan

    # 获取所有月末调仓日
    all_dates = close_df.index
    month_ends = get_month_end_dates(all_dates)

    # 只保留有足够历史数据的调仓日
    min_date = close_df.index[0] + pd.DateOffset(months=momentum_months + 1)
    month_ends = month_ends[month_ends >= min_date]

    # 只保留回测起始日期之后的调仓日（避开早期前复权价格异常）
    start_dt = pd.Timestamp(BACKTEST_START_DATE)
    month_ends = month_ends[month_ends >= start_dt]

    if len(month_ends) < 2:
        log(f"⚠️ 数据不足，无法回测（动量{momentum_months}个月）")
        return None

    log(f"回测区间：{month_ends[0].strftime('%Y-%m-%d')} ~ {month_ends[-1].strftime('%Y-%m-%d')}，共 {len(month_ends)} 个调仓期，风控={'开' if use_risk_control else '关'}")

    # 回测主循环
    portfolio_value = 1.0  # 初始净值
    holdings = {}  # 当前持仓 {code: 权重}
    nav_history = []  # 净值历史
    trade_history = []  # 交易记录

    for i, rebalance_date in enumerate(month_ends):
        # 计算当日净值（用收盘价）
        if holdings:
            daily_return = 0
            for code, weight in holdings.items():
                if code in close_df.columns and rebalance_date in close_df.index:
                    # 计算从上一调仓日到今天的收益
                    prev_date = month_ends[i-1] if i > 0 else close_df.index[0]
                    if code in close_df.columns:
                        prices = close_df[code].loc[prev_date:rebalance_date]
                        if len(prices) >= 2:
                            period_return = prices.iloc[-1] / prices.iloc[0] - 1
                            daily_return += weight * period_return

            portfolio_value *= (1 + daily_return)

        nav_history.append({
            "date": rebalance_date,
            "nav": portfolio_value,
            "holdings": dict(holdings),
        })

        # 计算动量，选择下一期持仓
        if i < len(month_ends) - 1:
            momentum_scores = {}
            for code in CANDIDATE_POOL.keys():
                if code in close_df.columns:
                    prices = close_df[code]
                    momentum = calculate_momentum(prices, rebalance_date, momentum_months)
                    if not np.isnan(momentum):
                        momentum_scores[code] = momentum

            if not momentum_scores:
                continue

            # 双动量策略：计算相对动量（ETF动量 - 大盘动量）
            if use_dual_momentum and BENCHMARK_CODE in momentum_scores:
                benchmark_momentum = momentum_scores[BENCHMARK_CODE]
                relative_scores = {}
                for code, abs_mom in momentum_scores.items():
                    if code == BENCHMARK_CODE:
                        continue
                    rel_mom = abs_mom - benchmark_momentum
                    # 双动量筛选：绝对动量>0 且 相对动量>0
                    if abs_mom > 0 and rel_mom > 0:
                        relative_scores[code] = rel_mom

                if relative_scores:
                    # 按相对动量排序
                    sorted_etfs = sorted(relative_scores.items(), key=lambda x: x[1], reverse=True)
                else:
                    # 没有符合双动量条件的ETF，转防御
                    sorted_etfs = []
            else:
                # 单动量：按绝对动量排序
                sorted_etfs = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)

            # ========== 风控判断 ==========
            switch_to_defensive = False
            risk_reason = ""

            # 双动量策略下，如果没有符合条件的ETF，自动转防御
            if use_dual_momentum and not sorted_etfs:
                switch_to_defensive = True
                risk_reason = "双动量筛选无符合条件ETF（绝对动量>0且相对动量>0）"

            if use_risk_control and not switch_to_defensive:
                # 风控1：大盘趋势过滤 — 沪深300在100日均线以下时，全部转防御
                if rebalance_date in close_df.index:
                    benchmark_price = close_df.loc[rebalance_date, BENCHMARK_CODE] if BENCHMARK_CODE in close_df.columns else np.nan
                    benchmark_ma = close_df.loc[rebalance_date, "benchmark_ma"] if "benchmark_ma" in close_df.columns else np.nan
                    if not np.isnan(benchmark_price) and not np.isnan(benchmark_ma):
                        if benchmark_price < benchmark_ma:
                            switch_to_defensive = True
                            risk_reason = f"大盘趋势向下（沪深300 {benchmark_price:.2f} < {TREND_MA_PERIOD}日均线 {benchmark_ma:.2f}）"

                # 风控2：改进防御触发 — 超过一半非防御ETF动量为负就转防御
                if not switch_to_defensive:
                    non_defensive_scores = {k: v for k, v in momentum_scores.items() if k not in DEFENSIVE_ASSETS}
                    if non_defensive_scores:
                        negative_count = sum(1 for v in non_defensive_scores.values() if v < 0)
                        negative_ratio = negative_count / len(non_defensive_scores)
                        if negative_ratio > 0.5:
                            switch_to_defensive = True
                            risk_reason = f"超过一半非防御ETF动量为负（{negative_count}/{len(non_defensive_scores)}）"
            else:
                # 无风控：只有所有非防御ETF动量都为负才转防御
                non_defensive_scores = {k: v for k, v in momentum_scores.items() if k not in DEFENSIVE_ASSETS}
                all_negative = all(v < 0 for v in non_defensive_scores.values()) if non_defensive_scores else False
                if all_negative:
                    switch_to_defensive = True
                    risk_reason = "所有非防御ETF动量为负"

            if switch_to_defensive:
                # 持有防御资产（等权配置国债、货币、黄金）
                new_holdings = {}
                available_defensive = [c for c in DEFENSIVE_ASSETS if c in momentum_scores]
                if available_defensive:
                    weight = 1.0 / len(available_defensive)
                    for code in available_defensive:
                        new_holdings[code] = weight
                trade_history.append({
                    "date": rebalance_date,
                    "action": "防御切换",
                    "holdings": new_holdings,
                    "reason": risk_reason,
                })
            else:
                # 选动量最高的N只（排除防御资产，除非防御资产动量也很高）
                top_etfs = sorted_etfs[:num_holdings]
                new_holdings = {}
                weight = 1.0 / len(top_etfs)
                for code, score in top_etfs:
                    new_holdings[code] = weight

                trade_history.append({
                    "date": rebalance_date,
                    "action": "调仓",
                    "holdings": new_holdings,
                    "top_momentum": top_etfs[0][1] if top_etfs else 0,
                })

            # 计算交易成本（换仓部分）
            if holdings:
                turnover = 0
                for code in set(list(holdings.keys()) + list(new_holdings.keys())):
                    old_w = holdings.get(code, 0)
                    new_w = new_holdings.get(code, 0)
                    turnover += abs(old_w - new_w)
                turnover /= 2  # 单边换手率
                transaction_cost = turnover * (COMMISSION_RATE + SLIPPAGE_RATE)
                portfolio_value *= (1 - transaction_cost)

            holdings = new_holdings

    # 计算绩效指标
    nav_df = pd.DataFrame(nav_history)
    nav_df = nav_df.set_index("date")

    total_return = nav_df["nav"].iloc[-1] - 1
    years = (nav_df.index[-1] - nav_df.index[0]).days / 365.25
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # 最大回撤
    nav_series = nav_df["nav"]
    running_max = nav_series.cummax()
    drawdown = (nav_series - running_max) / running_max
    max_drawdown = drawdown.min()

    # 夏普比率（假设无风险利率3%）
    monthly_returns = nav_df["nav"].pct_change().dropna()
    risk_free_rate = 0.03 / 12  # 月化无风险利率
    sharpe_ratio = (monthly_returns.mean() - risk_free_rate) / monthly_returns.std() * np.sqrt(12) if monthly_returns.std() > 0 else 0

    # 胜率（月度收益为正的比例）
    win_rate = (monthly_returns > 0).mean()

    results = {
        "momentum_months": momentum_months,
        "num_holdings": num_holdings,
        "risk_control": "开" if use_risk_control else "关",
        "dual_momentum": "开" if use_dual_momentum else "关",
        "start_date": nav_df.index[0].strftime("%Y-%m-%d"),
        "end_date": nav_df.index[-1].strftime("%Y-%m-%d"),
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "win_rate": win_rate,
        "num_rebalances": len(month_ends),
    }

    return results, nav_df, pd.DataFrame(trade_history)


def main():
    parser = argparse.ArgumentParser(description="ETF轮动策略回测")
    parser.add_argument("--momentum", type=int, default=None, help="动量回看期（月），默认对比3/6/12")
    parser.add_argument("--holdings", type=int, default=None, help="持仓数量，默认对比3/4/5")
    args = parser.parse_args()

    log("=" * 70)
    log("ETF轮动策略回测（MVP）")
    log(f"候选池：{len(CANDIDATE_POOL)}只ETF，月度调仓，等权配置")
    log("=" * 70)

    # 加载数据
    all_data = load_etf_data()

    if len(all_data) < 10:
        log("❌ ETF数据不足，请先下载ETF日线数据")
        sys.exit(1)

    # 确定要测试的参数组合
    momentum_list = [args.momentum] if args.momentum else [6]  # 默认只测6个月（最优）
    holdings_list = [args.holdings] if args.holdings else [3, 4, 5]
    # 对比：双动量+风控 vs 单动量+风控
    test_configs = [
        (True, True, "双动量+风控"),   # use_risk_control, use_dual_momentum
        (True, False, "单动量+风控"),
    ]

    all_results = []

    for momentum in momentum_list:
        for holdings in holdings_list:
            for use_rc, use_dm, config_name in test_configs:
                log("-" * 70)
                log(f"测试参数：动量{momentum}个月，持仓{holdings}只，{config_name}")
                log("-" * 70)

                result = run_backtest(all_data, momentum_months=momentum, num_holdings=holdings,
                                       use_risk_control=use_rc, use_dual_momentum=use_dm)

                if result is None:
                    continue

                results, nav_df, trade_df = result
                all_results.append(results)

                # 保存净值曲线
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                rc_suffix = "rc" if use_rc else "norc"
                dm_suffix = "dm" if use_dm else "sm"
                nav_file = OUTPUT_DIR / f"nav_momentum{momentum}_holdings{holdings}_{rc_suffix}_{dm_suffix}.csv"
                nav_df.to_csv(nav_file, encoding="utf-8")

                trade_file = OUTPUT_DIR / f"trades_momentum{momentum}_holdings{holdings}_{rc_suffix}_{dm_suffix}.csv"
                trade_df.to_csv(trade_file, index=False, encoding="utf-8")

    # 汇总对比
    if all_results:
        log("=" * 70)
        log("回测结果汇总对比")
        log("=" * 70)

        summary_df = pd.DataFrame(all_results)
        summary_df = summary_df[[
            "momentum_months", "num_holdings", "risk_control", "dual_momentum", "start_date", "end_date",
            "total_return", "annual_return", "max_drawdown", "sharpe_ratio", "win_rate"
        ]]
        summary_df.columns = [
            "动量(月)", "持仓数", "风控", "双动量", "开始日期", "结束日期",
            "总收益", "年化收益", "最大回撤", "夏普比率", "月度胜率"
        ]

        # 格式化输出
        display_df = summary_df.copy()
        display_df["总收益"] = display_df["总收益"].apply(lambda x: f"{x*100:.2f}%")
        display_df["年化收益"] = display_df["年化收益"].apply(lambda x: f"{x*100:.2f}%")
        display_df["最大回撤"] = display_df["最大回撤"].apply(lambda x: f"{x*100:.2f}%")
        display_df["夏普比率"] = display_df["夏普比率"].apply(lambda x: f"{x:.2f}")
        display_df["月度胜率"] = display_df["月度胜率"].apply(lambda x: f"{x*100:.1f}%")

        print()
        print(display_df.to_string(index=False))
        print()

        # 保存汇总
        summary_file = OUTPUT_DIR / "backtest_summary.csv"
        summary_df.to_csv(summary_file, index=False, encoding="utf-8")
        log(f"汇总结果已保存：{summary_file}")

        # 找出最优参数
        best = summary_df.loc[summary_df["夏普比率"].idxmax()]
        log(f"最优参数（按夏普比率）：动量{best['动量(月)']}个月，持仓{best['持仓数']}只")
        log(f"  年化收益：{best['年化收益']*100:.2f}%，最大回撤：{best['最大回撤']*100:.2f}%，夏普：{best['夏普比率']:.2f}")

    log("=" * 70)
    log("回测完成！")
    log("=" * 70)


if __name__ == "__main__":
    main()
