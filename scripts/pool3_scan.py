#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool3_scan.py — 操盘手 3 号股票池扫描器（1 号左侧困境反转 / 2 号右侧突破波段）

口径来源（操盘手原话，见 09_调研底稿与素材/操盘手经验/）：
- 3 号池量能规则（经验003）：量比 = 当日成交量 / HHV(VOL,60)（近60交易日最高量），
  落 0.45~0.65 为缩量观察区；>0.65 量仍强；<0.45 严重缩量磨底。
- 1 号策略·左侧困境反转（经验004，操盘手定义）：相对底部区间 + 业绩减亏/拐点预期
  + TRIX 低位金叉 + 资金放量试盘K线 + 缩量磨底；左侧分批、设强止损、无放量试盘只观察、不追高。
- 2 号策略·右侧突破波段（经验004，操盘手定义）：放量突破关键压力位 + 增量资金持续进场
  + 均线多头排列 + 板块/题材催化；无明确突破信号一律不介入。

诚实边界（务必读）：
- 本地【日线】只能算 技术面+量能：位置、TRIX、量比、均线、突破、试盘、缩量、支撑/压力。
- 1 号的“业绩减亏/拐点”需财务数据：仅当中证800财务文件存在时给提示，否则标“待人工确认”。
- 2 号的“增量资金净额 / 板块题材催化”免费日线无法提供，统一标“待确认”，不伪造。
- TRIX/均线/突破等参数为常用默认值，**阈值需操盘手确认**，本脚本不替他定参。
- 所有输出仅为研究参考，不构成投资建议，不自动下单（项目红线）。

用法：
  .venv/bin/python scripts/pool3_scan.py                 # 用各票最新交易日
  .venv/bin/python scripts/pool3_scan.py --date 2026-09-18
  .venv/bin/python scripts/pool3_scan.py --pool config/pool3_constituents.csv
"""
import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "raw" / "daily"
FUND_DIR = ROOT / "data" / "raw" / "fundamentals"
OUT_DIR = ROOT / "results" / "pool3_scan"

# ---------------- 可调参数（默认值，待操盘手确认） ----------------
P = dict(
    vol_window=60,        # HHV(VOL,N)，操盘手口径=60（约3个月）
    vol_lo=0.45, vol_hi=0.65,   # 3号池缩量观察区间
    ma_fast=5, ma_mid1=10, ma_mid2=20, ma_slow=60,
    pos_window=250,       # 位置百分位窗口（约1年）
    trix_n=12, trix_sig=9,      # TRIX 三重EMA周期与信号线
    trix_low_pct=0.30,    # TRIX“低位”判定：处于近250日分位<30%
    cross_lookback=10,    # 近N日内出现金叉算有效
    breakout_vol=1.5,     # 突破放量：当日量 > 1.5 * MA5量
    test_vol=1.5,         # 试盘放量：当日量 > 1.5 * MA20量
    grind_volmed=0.60,    # 缩量磨底：近20日量比中位数 < 0.60
    bottom_pos=0.30,      # 底部区间：250日位置分位 < 0.30
    left_chg20_max=0.20,  # 1号试错保护：近20日涨幅>20%视为已脱离底部，不追高
    left_vr3_max=0.70,    # 1号试错保护：近3日量比均值>0.70视为连续放量主升，不追高
    near_resist=0.97,     # 接近压力位：收盘 >= 0.97 * 60日高点
)


def load_daily(code: str) -> pd.DataFrame | None:
    ex = code.split(".")[0]
    f = DAILY_DIR / ex / f"{code}.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # 只要正常交易日（tradestatus=1），剔除停牌/零量
    if "tradestatus" in df.columns:
        df = df[df["tradestatus"].astype(str) == "1"]
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["volume"] > 0].dropna(subset=["close"]).reset_index(drop=True)
    return df


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def trix_series(close: pd.Series, n: int, sig: int):
    m1 = ema(close, n)
    m2 = ema(m1, n)
    m3 = ema(m2, n)
    trix = m3.pct_change() * 100.0
    trma = trix.rolling(sig).mean()
    return trix, trma


def fund_hint(code: str) -> str:
    """1号策略的业绩拐点要件：有本地财务文件才给提示，否则明确缺口。"""
    for sub, col in [("growth", None), ("profit", None)]:
        d = FUND_DIR / sub
        if not d.exists():
            continue
        fs = list(d.glob(f"{code}.csv")) + list(d.glob(f"*{code.split('.')[1]}*.csv"))
        if fs:
            try:
                fdf = pd.read_csv(fs[0])
                return f"有财务数据({sub})，需人工判读减亏/拐点"
            except Exception:
                return f"有财务文件({sub})读取失败"
    return "无本地财务数据，业绩减亏/拐点需人工确认"


def scan_one(code: str, name: str, asof: str | None, p: dict) -> dict:
    df = load_daily(code)
    if df is None:
        return {"code": code, "name": name, "状态": "缺日线数据"}
    if asof:
        df = df[df["date"] <= pd.Timestamp(asof)]
    if len(df) < 60:
        return {"code": code, "name": name, "状态": f"样本不足({len(df)}日)"}

    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    last = df.iloc[-1]
    asof_date = last["date"].strftime("%Y-%m-%d")

    # ---- 量比（3号池核心）----
    hhv60 = vol.rolling(p["vol_window"]).max()
    vol_ratio = (vol / hhv60).iloc[-1]
    if vol_ratio > p["vol_hi"]:
        vol_tag = ">65%量仍强"
    elif vol_ratio >= p["vol_lo"]:
        vol_tag = "45-65%观察区"
    else:
        vol_tag = "<45%缩量磨底"
    vr3 = (vol / hhv60).iloc[-3:]
    persistent = bool(((vr3 >= p["vol_lo"]) & (vr3 <= p["vol_hi"])).all())

    # ---- 均线与多头排列 ----
    ma5, ma10, ma20, ma60 = [close.rolling(n).mean().iloc[-1] for n in
                             (p["ma_fast"], p["ma_mid1"], p["ma_mid2"], p["ma_slow"])]
    bull_ma = ma5 > ma10 > ma20 > ma60 if not np.isnan(ma60) else False

    # ---- 位置百分位（250日）----
    w = min(p["pos_window"], len(df))
    ll, hh = low.rolling(w).min().iloc[-1], high.rolling(w).max().iloc[-1]
    pos = (last["close"] - ll) / (hh - ll) if hh > ll else np.nan
    is_bottom = (not np.isnan(pos)) and pos < p["bottom_pos"]

    # ---- TRIX 与低位金叉 ----
    trix, trma = trix_series(close, p["trix_n"], p["trix_sig"])
    cross = (trix > trma) & (trix.shift(1) <= trma.shift(1))
    lb = min(p["cross_lookback"], len(df))
    recent_cross = bool(cross.iloc[-lb:].any())
    trix_now, trma_now = trix.iloc[-1], trma.iloc[-1]
    tw = min(p["pos_window"], len(df))
    trix_pct = (trix.iloc[-1] - trix.iloc[-tw:].min()) / (
        trix.iloc[-tw:].max() - trix.iloc[-tw:].min() + 1e-12)
    trix_low = (not np.isnan(trix_pct)) and trix_pct < p["trix_low_pct"]
    low_golden = recent_cross and trix_low

    # ---- 2号：放量突破关键压力位（压力=近60日不含当日最高）----
    resist60 = high.iloc[-61:-1].max() if len(df) > 61 else high.iloc[:-1].max()
    support60 = low.iloc[-61:-1].min() if len(df) > 61 else low.iloc[:-1].min()
    ma5_vol = vol.rolling(5).mean().iloc[-1]
    vol_surge = last["volume"] > p["breakout_vol"] * ma5_vol
    is_breakout = bool(last["close"] > resist60 and vol_surge)
    near_breakout = bool(last["close"] >= p["near_resist"] * resist60 and last["close"] <= resist60)
    # 增量资金代理（非真实净额）：近5日价涨且量能温和放大
    chg5 = close.iloc[-1] / close.iloc[-6] - 1 if len(df) > 6 else np.nan
    vol_up = vol.iloc[-5:].mean() > vol.iloc[-20:-5].mean()
    fund_proxy = bool(chg5 > 0 and vol_up)

    # ---- 1号：放量试盘K线（底部、近5日内出现放量阳线、不追高）----
    opens = df["open"]
    ma20_vol = vol.rolling(20).mean()
    def test_bar(i):
        if i < 20: return False
        yang = close.iloc[i] > opens.iloc[i]          # 当日收阳
        pct = close.iloc[i] / close.iloc[i-1] - 1
        return (yang and vol.iloc[i] > p["test_vol"] * ma20_vol.iloc[i]
                and 0 < pct < 0.07)                   # 放量但未大涨，不追高
    recent_test = any(test_bar(i) for i in range(len(df)-5, len(df)))
    # 缩量磨底：近20日量比中位数低
    vr_series = vol / hhv60
    grind = bool(vr_series.iloc[-20:].median() < p["grind_volmed"])

    # ---- 1号“不追高”保护：排除已脱离底部/连续放量主升的票 ----
    # 操盘手1号的试盘是“底部首根放量、随后缩量”，不是连板主升后再追。
    chg20 = (close.iloc[-1] / close.iloc[-21] - 1) if len(df) > 21 else np.nan
    vr3_mean = float(vr_series.iloc[-3:].mean())
    # 近20日已大涨 或 近3日持续巨量（贴着60日天量）= 已启动，不能再当左侧试错
    left_too_late = bool((not np.isnan(chg20) and chg20 > p["left_chg20_max"])
                         or vr3_mean > p["left_vr3_max"])

    # ---- 1号 / 2号 要件命中 ----
    fund = fund_hint(code)
    s1 = {
        "底部区间": is_bottom,
        "TRIX低位金叉": low_golden,
        "放量试盘K线": recent_test,
        "缩量磨底": grind,
        "业绩减亏/拐点": fund,   # 文本：待确认/有数据
    }
    s1_tech_hits = sum(1 for k in ["底部区间", "TRIX低位金叉", "放量试盘K线", "缩量磨底"] if s1[k])
    s2 = {
        "放量突破压力": is_breakout,
        "均线多头排列": bull_ma,
        "增量资金持续": fund_proxy,        # 仅量价代理
        "板块/题材催化": "待确认（日线不含）",
    }
    s2_hits = sum(1 for k in ["放量突破压力", "均线多头排列", "增量资金持续"] if s2[k] is True)

    # ---- 信号状态（严格按操盘手纪律，宁可不发信号）----
    if is_breakout and bull_ma:
        status = "2号突破候选（资金净额/板块催化待确认）"
    elif is_bottom and recent_test and not left_too_late:
        # 底部 + 首次放量试盘 + 未脱离底部，才允许左侧小仓试错
        status = "1号左侧-可小仓试错候选（业绩拐点/止损待确认）"
    elif is_bottom and recent_test and left_too_late:
        # 已有放量阳线但近期大涨/连续巨量——已启动，不能再当左侧追
        status = "1号-已放量启动/脱离底部，不追高（观察）"
    elif is_bottom and (low_golden or grind):
        # 操盘手：没有放量试盘只观察、不轻易试仓
        status = "1号左侧-仅观察（缺放量试盘，不试仓）"
    elif near_breakout and bull_ma:
        status = "2号-临近压力位观察（未突破不介入）"
    elif vol_tag == "45-65%观察区":
        status = "量能观察池（1/2号信号未齐备）"
    else:
        status = "无信号/等待"

    return {
        "code": code, "name": name, "截至日": asof_date,
        "收盘": round(last["close"], 2),
        "量比": round(float(vol_ratio), 2), "量能档": vol_tag,
        "近20日涨幅": round(float(chg20), 2) if not np.isnan(chg20) else None,
        "近3日量比均值": round(vr3_mean, 2),
        "近3日持续在区间": "是" if persistent else "否",
        "位置分位250d": round(float(pos), 2) if not np.isnan(pos) else None,
        "均线多头": "是" if bull_ma else "否",
        "TRIX": None if np.isnan(trix_now) else round(float(trix_now), 3),
        "TRIX信号": ("低位金叉" if low_golden else "近期金叉" if recent_cross else
                  "金叉" if trix_now > trma_now else "死叉/弱势"),
        "近5日试盘": "是" if recent_test else "否",
        "放量突破": "是" if is_breakout else ("临近" if near_breakout else "否"),
        "缩量磨底": "是" if grind else "否",
        "支撑位60d": round(float(support60), 2),
        "压力位60d": round(float(resist60), 2),
        "1号技术要件命中": f"{s1_tech_hits}/4",
        "1号业绩要件": fund,
        "2号技术要件命中": f"{s2_hits}/3",
        "2号催化要件": "待确认",
        "信号状态": status,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(ROOT / "config" / "pool3_constituents.csv"))
    ap.add_argument("--date", default=None, help="评级截止日 YYYY-MM-DD，默认各票最新交易日")
    ap.add_argument("--outdir", default=str(OUT_DIR))
    args = ap.parse_args()

    pool = pd.read_csv(args.pool)
    stocks = pool[pool["asset_type"] == "stock"]
    rows, missing = [], []
    for _, r in stocks.iterrows():
        res = scan_one(r["baostock_code"], r["name"], args.date, P)
        if res.get("状态", "").startswith(("缺日线", "样本不足")):
            missing.append(res)
        else:
            rows.append(res)

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    tag = args.date or (rows[0]["截至日"] if rows else dt.date.today().isoformat())
    dfout = pd.DataFrame(rows)
    csv_path = outdir / f"pool3_scan_{tag}.csv"
    dfout.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # ---- Markdown 报告 ----
    order = {"2号突破候选": 0, "1号左侧-可小仓试错": 1, "1号-已放量启动": 2,
             "1号左侧-仅观察": 3, "2号-临近压力位": 4, "量能观察池": 5, "无信号": 6}
    def rank(s):
        for k, v in order.items():
            if s.startswith(k): return v
        return 9
    dfout["_r"] = dfout["信号状态"].map(rank)
    dfout = dfout.sort_values(["_r", "量比"], ascending=[True, False])

    cols = ["code", "name", "收盘", "量比", "量能档", "近20日涨幅", "位置分位250d", "均线多头",
            "TRIX信号", "近5日试盘", "放量突破", "1号技术要件命中", "2号技术要件命中", "信号状态"]
    lines = [
        f"# 3号股票池扫描报告（截至 {tag}）", "",
        "> 口径：操盘手 3 号池量能规则 + 1 号左侧困境反转 / 2 号右侧突破波段（经验003/004）。",
        "> **仅为技术面+量能研究参考，不构成投资建议，不自动下单。**",
        "> 业绩减亏/拐点、增量资金净额、板块题材催化为日线不可得维度，标注“待确认”，需人工或另接数据源。",
        f"> 参数（默认值，**待操盘手确认**）：HHV窗口{P['vol_window']}、量比区间{P['vol_lo']}~{P['vol_hi']}、"
        f"TRIX({P['trix_n']},{P['trix_sig']})、底部位置<{P['bottom_pos']}、突破放量>{P['breakout_vol']}×MA5、"
        f"试错保护近20日涨幅≤{P['left_chg20_max']}且近3日量比均值≤{P['left_vr3_max']}。",
        "> **量比口径校准提示（开放问题）**：本量比严格按 `VOL/HHV(VOL,60)` 用 Baostock 日线成交量计算，"
        "与操盘手在通达信终端肉眼估算值存在差异（已核查非公式/复权问题，系数据源与人工估算差异）；"
        "0.45~0.65 阈值在 Baostock 口径上尚需用历史样本重新校准，边界标的以操盘手通达信选股器实测为准。",
        "",
        f"## 一、信号汇总（共 {len(dfout)} 只，缺数据 {len(missing)} 只）", "",
        "| 代码 | 名称 | 收盘 | 量比 | 量能档 | 近20日涨 | 位置 | 均线多头 | TRIX | 试盘 | 突破 | 1号命中 | 2号命中 | 信号状态 |",
        "|---|---|--|--|--|--|--|--|--|--|--|--|--|--|",
    ]
    for _, x in dfout.iterrows():
        lines.append("| " + " | ".join(str(x[c]) for c in cols) + " |")
    if missing:
        lines += ["", "## 二、缺数据/样本不足", ""]
        for m in missing:
            lines.append(f"- {m['code']} {m['name']}：{m['状态']}")
    lines += ["", "## 三、关键价位与待确认项", "",
              "完整字段（支撑/压力位、TRIX数值、业绩要件等）见同目录 CSV：`" + csv_path.name + "`。", "",
              "### 纪律提醒（操盘手原话口径）",
              "- 1 号：左侧分批、设强止损；**没有放量试盘只观察、不轻易试仓；不追高**。",
              "- 2 号：**无明确放量突破信号一律不介入**，不提前埋伏突破。",
              "- 资金面权重 50% > 基本面/消息面各 20% > 技术面 10%；技术信号只是最后 10% 的择时确认。"]
    md_path = outdir / f"pool3_scan_{tag}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"✅ 已扫描 {len(rows)} 只，缺数据 {len(missing)} 只")
    print(f"CSV : {csv_path}")
    print(f"报告: {md_path}")
    if missing:
        print("缺数据:", [m["code"] for m in missing])
    # 终端简表
    show = dfout[dfout["信号状态"] != "无信号/等待"][["code", "name", "量比", "信号状态"]]
    if len(show):
        print("\n有信号/在观察区：")
        print(show.to_string(index=False))


if __name__ == "__main__":
    main()
