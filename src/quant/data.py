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


def load_profit(code):
    p = FUND_DIR / "profit" / f"{code}.csv"
    df = _read_table(p)
    if df is None:
        return pd.DataFrame()
    df["pubDate"] = pd.to_datetime(df["pubDate"], errors="coerce")
    df["statDate"] = pd.to_datetime(df["statDate"], errors="coerce")
    df["roeAvg"] = pd.to_numeric(df["roeAvg"], errors="coerce")
    return df.sort_values("pubDate")


def load_growth(code):
    p = FUND_DIR / "growth" / f"{code}.csv"
    df = _read_table(p)
    if df is None:
        return pd.DataFrame()
    df["pubDate"] = pd.to_datetime(df["pubDate"], errors="coerce")
    df["statDate"] = pd.to_datetime(df["statDate"], errors="coerce")
    df["YOYNI"] = pd.to_numeric(df["YOYNI"], errors="coerce")
    return df.sort_values("pubDate")


def load_dividend(code):
    p = FUND_DIR / "dividend" / f"{code}.csv"
    df = _read_table(p)
    if df is None:
        return pd.DataFrame()
    df["dividOperateDate"] = pd.to_datetime(df["dividOperateDate"], errors="coerce")
    df["cash"] = pd.to_numeric(df["dividCashPsBeforeTax"], errors="coerce")
    return df


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
    """给定交易日索引，返回每月最后一个交易日（>= start）。"""
    s = pd.Series(dates, index=dates)
    grp = s.groupby([dates.year, dates.month]).max()
    me = pd.DatetimeIndex(grp.values)
    return me[me >= pd.Timestamp(start)]
