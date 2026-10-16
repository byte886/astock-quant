#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据加载层：个股日线 + ETF 基准 + 财务数据（ROE/成长/分红）
==================================================
存储双轨：原始 CSV 与同名 Parquet(zstd) 并存，默认 **Parquet 优先、CSV 回退**
（生成脚本 scripts/csv_to_parquet.py，决策见 02_决策记录/ADR-004）；
置模块常量 PREFER_PARQUET=False 可强制只读 CSV。两种格式列语义一致，
下方的日期/数值清洗对 Parquet 是幂等的、对 CSV 是必需的。
所有路径相对项目根目录；v0.1 股票池用当前中证800成分（幸存者偏差见策略文档§8），
历史动态成分股就绪后在 load_constituents(date) 中按 date 切换。
"""

import json
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
DAILY_DIR = ROOT / "data/raw/daily"
FUND_DIR = ROOT / "data/raw/fundamentals"
CONS_DIR = ROOT / "data/raw/index_constituents"
WORKSPACE = ROOT / "data/_workspace"

# 读取开关：True=同名 .parquet 存在则优先读（由 scripts/csv_to_parquet.py 生成），
# 缺失时自动回退 CSV；置 False 可整体退回只读 CSV。
PREFER_PARQUET = True


def _read_table(csv_path):
    """读一张"每只一个文件"的表：优先同名 Parquet，缺失或关开关时回退 CSV。

    传入 .csv 路径即可，Parquet 路径由其同名替换后缀得到；两种格式列语义一致。
    文件都不存在时返回 None，由调用方决定返回 None 还是空 DataFrame。
    """
    csv_path = Path(csv_path)
    pq_path = csv_path.with_suffix(".parquet")
    if PREFER_PARQUET and pq_path.exists():
        return pd.read_parquet(pq_path)
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return None


def load_constituents():
    """返回当前中证800成分股列表（剔除科创板）。

    优先读历史成分目录；v0.1 退化用中证800日线下载状态里的 completed 列表
    （即成功下载日线的成分股，离线可用）。
    """
    status_file = WORKSPACE / "csi800_daily_status.json"
    if status_file.exists():
        st = json.loads(status_file.read_text())
        codes = sorted(set(st.get("completed", [])) | set(st.get("failed", {}).keys()))
        if codes:
            return codes
    # 再退化：扫描日线目录（CSV 与 Parquet 同名，按 stem 去重）
    codes = []
    for mkt in ["sh", "sz"]:
        d = DAILY_DIR / mkt
        if d.exists():
            stems = {p.stem for p in d.glob("*.csv")} | {p.stem for p in d.glob("*.parquet")}
            codes += sorted(stems)
    return sorted(c for c in codes if "688" not in c)


def _read_daily(code):
    mkt = "sh" if code.startswith("sh.") else "sz"
    p = DAILY_DIR / mkt / f"{code}.csv"
    df = _read_table(p)
    if df is None or df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    num_cols = ["open", "high", "low", "close", "preclose", "volume",
                "turn", "pctChg", "peTTM", "pbMRQ"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "tradestatus" in df.columns:
        df["tradestatus"] = pd.to_numeric(df["tradestatus"], errors="coerce")
    if "isST" in df.columns:
        df["isST"] = pd.to_numeric(df["isST"], errors="coerce")
    # 前复权早期可能出现非正价格，置空后前向填充
    for c in ["open", "close", "preclose"]:
        if c in df.columns:
            df.loc[df[c] <= 0, c] = np.nan
    return df.sort_values("date").reset_index(drop=True)


def load_price_panel(codes=None, field="close"):
    """加载多个字段的日线面板：返回 {field: DataFrame(index=date, columns=code)}"""
    if codes is None:
        codes = load_constituents()
    panels = {}
    fields = ["open", "close", "preclose", "tradestatus", "isST", "volume"]
    data = {f: {} for f in fields}
    valid = []
    for code in codes:
        df = _read_daily(code)
        if df is None or len(df) < 250:
            continue
        s = df.set_index("date")
        ok = True
        for f in fields:
            if f in s.columns:
                data[f][code] = s[f]
            elif f in ("open", "close", "preclose", "volume"):
                ok = False
        if ok:
            valid.append(code)
    for f in fields:
        if data[f]:
            panels[f] = pd.DataFrame(data[f]).sort_index().ffill()
    panels["codes"] = valid
    return panels


def _load_fundamental(code, subdir, date_cols, num_cols,
                      sort_col=None, cash_from=None):
    """读一张"每只一个文件"的财务表（profit/growth/dividend）的公共逻辑。

    缺失文件返回空 DataFrame；date_cols 转日期、num_cols 转数值；
    cash_from 给定时新增 cash 列（=该列数值，原列保留，分红表用）；
    sort_col 给定时按其排序（profit/growth 按 pubDate，分红表不排序）。
    列缺失时与原实现一致地抛出 KeyError（这些表由 akshare 固定列下载）。
    """
    df = _read_table(FUND_DIR / subdir / f"{code}.csv")
    if df is None:
        return pd.DataFrame()
    for c in date_cols:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if cash_from is not None:
        df["cash"] = pd.to_numeric(df[cash_from], errors="coerce")
    if sort_col is not None:
        df = df.sort_values(sort_col)
    return df


def load_profit(code):
    return _load_fundamental(
        code, "profit", date_cols=["pubDate", "statDate"],
        num_cols=["roeAvg"], sort_col="pubDate")


def load_growth(code):
    return _load_fundamental(
        code, "growth", date_cols=["pubDate", "statDate"],
        num_cols=["YOYNI"], sort_col="pubDate")


def load_dividend(code):
    return _load_fundamental(
        code, "dividend", date_cols=["dividOperateDate"],
        num_cols=[], cash_from="dividCashPsBeforeTax")


def load_benchmark():
    """沪深300ETF 作为基准，返回日频 close Series。"""
    p = ROOT / "data/raw/etf/daily/sh/sh.510300.csv"
    df = _read_table(p)
    if df is None:
        raise FileNotFoundError(f"基准数据缺失：{p}（CSV 与 Parquet 均不存在）")
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.sort_values("date").set_index("date")["close"]


def month_end_dates(dates, start="2015-01-01"):
    """给定**完整历史**交易日索引，批量返回每月最后一个交易日（>= start）。

    仅用于回测：回测面板是完整历史，按 (年,月) 分组取最大即当月最后交易日。
    注意它会把序列最后一天也当成其所在月的月末（即使该月数据不完整）；
    回测引擎只用 ``mends[:-1]`` 作信号日（最后一个月末没有下一交易日可成交），
    故该边界不影响结果。每日实盘推进（数据面板在最新交易日截断、未来未知）
    不要用它，改用 :func:`is_month_end_trade_date`（真实交易日历，治 I23）。
    """
    s = pd.Series(dates, index=dates)
    grp = s.groupby([dates.year, dates.month]).max()
    me = pd.DatetimeIndex(grp.values)
    return me[me >= pd.Timestamp(start)]


# ── 交易日历（用于准确判定月末，避免数据末日被误判为月末）──────────────────
CAL_CACHE = WORKSPACE / "trade_calendar.csv"


def load_trade_calendar(min_days_ahead=30):
    """A 股交易日历（含交易所预公布的未来交易日），返回 DatetimeIndex。

    优先读本地缓存 data/_workspace/trade_calendar.csv；缓存覆盖不足
    （最大日期距今不足 min_days_ahead 天）时从 akshare 刷新并落盘。
    akshare 拉取失败则回退到现有缓存（即使偏旧），再失败返回 None。
    交易日历由交易所年初公布全年休市安排，可确定性地判定"某月最后交易日"，
    不依赖"面板下一交易日是否跨月"——后者在数据面板最后一天（数据截断）会
    把非月末日误判为月末（例如 10-15 后还有 10-30，却被当成 10 月末）。
    """
    def _read_cache():
        if CAL_CACHE.exists():
            try:
                cal = pd.to_datetime(pd.read_csv(CAL_CACHE)["trade_date"])
                if len(cal):
                    return pd.DatetimeIndex(cal).sort_values().unique()
            except Exception:
                return None
        return None

    cal = _read_cache()
    cutoff = pd.Timestamp.now() + pd.Timedelta(days=min_days_ahead)
    if cal is not None and cal.max() >= cutoff:
        return cal
    try:
        import akshare as ak
        raw = ak.tool_trade_date_hist_sina()
        dates = pd.to_datetime(raw["trade_date"]).sort_values()
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        dates.to_frame(name="trade_date").to_csv(CAL_CACHE, index=False)
        return pd.DatetimeIndex(dates).unique()
    except Exception:
        return cal  # 偏旧缓存（可能为 None）；由调用方决定是否回退


def is_month_end_trade_date(d, cal=None, all_dates=None):
    """判断 d 是否当月最后一个 A 股交易日（确定性，含国庆/春节边界）。

    - 优先用含未来交易日的真实日历 cal：d 等于该月日历中最后一个交易日即月末。
    - 无日历（None）时回退：all_dates 中 d 的下一交易日跨月，则 d 为月末。
    - d 是面板最后一天（数据截断、无下一交易日）且无日历时，保守返回 False：
      宁可月末信号晚一天，也不在非月末（如 10-15）发出假信号、导致次日误换仓。
    """
    d = pd.Timestamp(d)
    if cal is not None and len(cal):
        same_month = [t for t in cal if t.year == d.year and t.month == d.month]
        if same_month:
            return d == max(same_month)
        return False
    if all_dates is not None:
        dates = list(all_dates)
        if d in dates:
            idx = dates.index(d)
            if idx + 1 < len(dates):
                return d.month != dates[idx + 1].month
    return False
