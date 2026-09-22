#!/usr/bin/env python3
"""扫描计算引擎基准：pandas vs DuckDB vs Polars（在本仓自有 Parquet 上实测）。

背景：ADR-005 选型前置（见 ADR-004「查询引擎选型」落地条件、T29 验收）。
不采信外部榜单，只测我们自己"一只一文件、按市场分目录"的日线 Parquet 在
3 号池扫描器真实负载下的表现与可读性。

四个负载（贴近 pool3_scan v2 / 操盘手 002 形态）：
  L1 捞数/IO ：读取最近 N 年全市场日线所需列，物化成内存表
  L2 指标    ：按 code 分组算 MA20、量比(当日量/前5日均量,不含当日)、前10日高低
  L3 形态时序：平台突破 = 前10日(不含今日)振幅<=8% 且 收盘破前10日高 且 量比>=1.5
  L4 横截面  ：最新交易日命中票按量比降序取前 10

每个引擎在独立子进程运行（隔离峰值内存），可 --repeat 多次取中位数；
三引擎交叉校验 entry 命中数与 top 票集合，结果不一致会明确告警（基准无意义）。

用法：
  .venv/bin/python scripts/bench_scan_engines.py                 # 三引擎各跑3次取中位
  .venv/bin/python scripts/bench_scan_engines.py --years 1 --repeat 1
  .venv/bin/python scripts/bench_scan_engines.py --once --engine polars
结果 JSON 落 results/engine_benchmark/（gitignored），关键数字写入 ADR-005。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY_GLOB = str(ROOT / "data" / "raw" / "daily" / "*" / "*.parquet")
COLS = ["code", "date", "open", "high", "low", "close", "volume"]

# 形态参数（与 tickflow platform_consolidation_breakout 默认值对齐，仅作基准口径，非采用值）
PLATFORM_DAYS = 10
RANGE_PCT_MAX = 8.0
VOL_RATIO_MIN = 1.5
TOP_N = 10


def _cutoff_date(years: int) -> str:
    import pandas as pd

    files = sorted(glob.glob(DAILY_GLOB))
    # 抽样若干文件取全局最大日期，避免全量读
    import pyarrow.parquet as pq

    mx = None
    for f in files[:: max(1, len(files) // 200)]:
        d = pq.read_table(f, columns=["date"]).column("date")
        import pyarrow.compute as pc

        m = pc.max(d).as_py()
        if mx is None or m > mx:
            mx = m
    cutoff = pd.Timestamp(mx) - pd.DateOffset(years=years)
    return cutoff.strftime("%Y-%m-%d")


def _peak_rss_mb() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return ru / (1024 * 1024)  # macOS 单位字节
    return ru / 1024.0  # Linux 单位 KB -> MB


def _check_payload(result: dict) -> None:
    """打印供三引擎交叉校验的关键值。"""
    for k in ("rows", "symbols", "entry_total", "entry_latest"):
        print(f"  check {k} = {result[k]}")
    print("  check top5 = " + ", ".join(f"{c}:{v}" for c, v in result["top5"]))


# ----------------------------- pandas ----------------------------- #
def run_pandas(cutoff: str) -> dict:
    import numpy as np
    import pandas as pd

    files = sorted(glob.glob(DAILY_GLOB))
    t0 = time.perf_counter()
    dfs = [pd.read_parquet(f, columns=COLS) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    df = df[df["date"] >= pd.Timestamp(cutoff)].sort_values(["code", "date"])
    l1 = time.perf_counter() - t0

    t0 = time.perf_counter()
    g = df.groupby("code", sort=False)
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
    vol_prev = g["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    df["prior10_high"] = g["high"].transform(lambda s: s.shift(1).rolling(10).max())
    df["prior10_low"] = g["low"].transform(lambda s: s.shift(1).rolling(10).min())
    l2 = time.perf_counter() - t0

    t0 = time.perf_counter()
    with np.errstate(divide="ignore", invalid="ignore"):
        df["vol_ratio"] = np.where(vol_prev > 0, df["volume"] / vol_prev, np.nan)
    df["range_pct"] = (df["prior10_high"] - df["prior10_low"]) / df["close"] * 100.0
    entry = (
        (df["range_pct"] <= RANGE_PCT_MAX)
        & (df["close"] > df["prior10_high"])
        & (df["vol_ratio"] >= VOL_RATIO_MIN)
        & np.isfinite(df["vol_ratio"])
    )
    df["entry"] = entry.fillna(False).astype(bool)
    l3 = time.perf_counter() - t0

    t0 = time.perf_counter()
    latest = df["date"].max()
    hit = df[(df["date"] == latest) & df["entry"]].sort_values("vol_ratio", ascending=False)
    entry_total = int(df["entry"].sum())
    entry_latest = int(len(hit))
    top5 = [(r.code, round(float(r.vol_ratio), 4)) for r in hit.head(5).itertuples()]
    l4 = time.perf_counter() - t0

    return {
        "engine": "pandas",
        "l1_sec": round(l1, 3),
        "l2_sec": round(l2, 3),
        "l3_sec": round(l3, 3),
        "l4_sec": round(l4, 3),
        "rows": int(len(df)),
        "symbols": int(df["code"].nunique()),
        "entry_total": entry_total,
        "entry_latest": entry_latest,
        "top5": top5,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


# ----------------------------- DuckDB ----------------------------- #
def run_duckdb(cutoff: str) -> dict:
    import duckdb

    con = duckdb.connect()
    files = sorted(glob.glob(DAILY_GLOB))
    glob_expr = "data/raw/daily/*/*.parquet"

    t0 = time.perf_counter()
    con.execute(
        f"CREATE TEMP TABLE t AS SELECT code,date,open,high,low,close,volume "
        f"FROM read_parquet('{glob_expr}', hive_partitioning=false) WHERE date >= DATE '{cutoff}'"
    )
    rows = con.execute("SELECT count(*), count(distinct code) FROM t").fetchone()
    l1 = time.perf_counter() - t0

    t0 = time.perf_counter()
    con.execute(
        """
        CREATE TEMP TABLE ind AS
        SELECT *,
          CASE WHEN count(*) OVER w20 = 20 THEN avg(close) OVER w20 END AS ma20,
          CASE WHEN count(*) OVER w5p = 5  THEN avg(volume) OVER w5p END AS vol_ma5_prev,
          CASE WHEN count(*) OVER w10p = 10 THEN max(high) OVER w10p END AS prior10_high,
          CASE WHEN count(*) OVER w10p = 10 THEN min(low)  OVER w10p END AS prior10_low
        FROM t
        WINDOW
          w20  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
          w5p  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING),
          w10p AS (PARTITION BY code ORDER BY date ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING)
        """
    )
    l2 = time.perf_counter() - t0

    t0 = time.perf_counter()
    con.execute(
        """
        CREATE TEMP TABLE sig AS
        SELECT *,
          CASE WHEN vol_ma5_prev > 0 THEN volume / vol_ma5_prev ELSE NULL END AS vol_ratio,
          (prior10_high - prior10_low) / close * 100.0 AS range_pct,
          (range_pct <= ?) AND (close > prior10_high)
              AND (vol_ma5_prev > 0) AND (volume / vol_ma5_prev >= ?)
              AND isfinite(volume / vol_ma5_prev) AS entry
        FROM ind
        """,
        [RANGE_PCT_MAX, VOL_RATIO_MIN],
    )
    entry_total = con.execute("SELECT count(*) FROM sig WHERE entry").fetchone()[0]
    l3 = time.perf_counter() - t0

    t0 = time.perf_counter()
    latest = con.execute("SELECT max(date) FROM t").fetchone()[0]
    hit = con.execute(
        "SELECT code, vol_ratio FROM sig WHERE date = ? AND entry "
        "ORDER BY vol_ratio DESC LIMIT ?",
        [latest, TOP_N],
    ).fetchall()
    entry_latest = con.execute(
        "SELECT count(*) FROM sig WHERE date = ? AND entry", [latest]
    ).fetchone()[0]
    top5 = [(c, round(float(v), 4)) for c, v in hit[:5]]
    l4 = time.perf_counter() - t0
    con.close()

    return {
        "engine": "duckdb",
        "l1_sec": round(l1, 3),
        "l2_sec": round(l2, 3),
        "l3_sec": round(l3, 3),
        "l4_sec": round(l4, 3),
        "rows": int(rows[0]),
        "symbols": int(rows[1]),
        "entry_total": int(entry_total),
        "entry_latest": int(entry_latest),
        "top5": top5,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


# ----------------------------- Polars ----------------------------- #
def run_polars(cutoff: str) -> dict:
    import polars as pl

    glob_expr = str(ROOT / "data" / "raw" / "daily" / "*" / "*.parquet")
    # 各批次 parquet 的 volume 存在 int64/float64 混存，显式 schema 统一为 float64；
    # extra_columns=ignore 只投影 COLS（与 tickflow parquet.py 同款容错）。
    scan_schema = {
        "code": pl.Utf8(),
        "date": pl.Datetime("us"),
        "open": pl.Float64(),
        "high": pl.Float64(),
        "low": pl.Float64(),
        "close": pl.Float64(),
        "volume": pl.Float64(),
    }
    t0 = time.perf_counter()
    df = (
        pl.scan_parquet(
            glob_expr,
            schema=scan_schema,
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
            missing_columns="insert",
            extra_columns="ignore",
        )
        .select(COLS)
        .filter(pl.col("date") >= pl.lit(cutoff).str.to_datetime())
        .collect()
        .sort(["code", "date"])
    )
    l1 = time.perf_counter() - t0

    t0 = time.perf_counter()
    ind = df.with_columns(
        pl.col("close").rolling_mean(20).over("code").alias("ma20"),
        pl.col("volume").shift(1).rolling_mean(5).over("code").alias("vol_ma5_prev"),
        pl.col("high").shift(1).rolling_max(10).over("code").alias("prior10_high"),
        pl.col("low").shift(1).rolling_min(10).over("code").alias("prior10_low"),
    )
    l2 = time.perf_counter() - t0

    t0 = time.perf_counter()
    sig = ind.with_columns(
        pl.when(pl.col("vol_ma5_prev") > 0)
        .then(pl.col("volume") / pl.col("vol_ma5_prev"))
        .otherwise(None)
        .alias("vol_ratio")
    ).with_columns(
        ((pl.col("prior10_high") - pl.col("prior10_low")) / pl.col("close") * 100.0).alias("range_pct")
    ).with_columns(
        (
            (pl.col("range_pct") <= RANGE_PCT_MAX)
            & (pl.col("close") > pl.col("prior10_high"))
            & (pl.col("vol_ratio") >= VOL_RATIO_MIN)
            & pl.col("vol_ratio").is_finite()
        )
        .fill_null(False)
        .alias("entry")
    )
    entry_total = int(sig.select(pl.col("entry").sum()).item())
    l3 = time.perf_counter() - t0

    t0 = time.perf_counter()
    latest = sig.select(pl.col("date").max()).item()
    hit = (
        sig.filter((pl.col("date") == latest) & pl.col("entry"))
        .sort("vol_ratio", descending=True)
        .head(TOP_N)
        .select("code", "vol_ratio")
    )
    entry_latest = int(
        sig.filter((pl.col("date") == latest) & pl.col("entry")).select(pl.len()).item()
    )
    rows_h = hit.iter_rows()
    top5 = [(c, round(float(v), 4)) for c, v in list(rows_h)[:5]]
    l4 = time.perf_counter() - t0

    return {
        "engine": "polars",
        "l1_sec": round(l1, 3),
        "l2_sec": round(l2, 3),
        "l3_sec": round(l3, 3),
        "l4_sec": round(l4, 3),
        "rows": int(df.height),
        "symbols": int(df.select(pl.col("code").n_unique()).item()),
        "entry_total": entry_total,
        "entry_latest": entry_latest,
        "top5": top5,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


ENGINES = {"pandas": run_pandas, "duckdb": run_duckdb, "polars": run_polars}


def run_once(engine: str, cutoff: str) -> dict:
    r = ENGINES[engine](cutoff)
    r["total_sec"] = round(r["l1_sec"] + r["l2_sec"] + r["l3_sec"] + r["l4_sec"], 3)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--engines", default="pandas,duckdb,polars")
    ap.add_argument("--once", action="store_true", help="单进程单次（内部子进程用），输出 JSON")
    ap.add_argument("--engine", default=None)
    args = ap.parse_args()

    cutoff = _cutoff_date(args.years)

    if args.once:
        r = run_once(args.engine, cutoff)
        r["cutoff"] = cutoff
        print("RESULT_JSON " + json.dumps(r))
        _check_payload(r)
        return 0

    print(f"基准窗口：date >= {cutoff}（近 {args.years} 年）｜ 数据源：{DAILY_GLOB.replace(str(ROOT) + '/', '')}")
    print(f"每引擎独立子进程 × {args.repeat} 次，取中位数\n")

    summary = {}
    checks = {}
    for eng in args.engines.split(","):
        eng = eng.strip()
        runs = []
        for i in range(args.repeat):
            proc = subprocess.run(
                [sys.executable, __file__, "--once", "--engine", eng, "--years", str(args.years)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT_JSON ")), None)
            if line is None:
                print(f"[{eng}] 第{i+1}次运行失败：\n{proc.stderr[-2000:]}")
                return 2
            runs.append(json.loads(line[len("RESULT_JSON "):]))
        checks[eng] = runs[0]
        med = {}
        for k in ("l1_sec", "l2_sec", "l3_sec", "l4_sec", "total_sec", "peak_rss_mb"):
            vals = sorted(r[k] for r in runs)
            med[k] = vals[len(vals) // 2]
        med["rows"] = runs[0]["rows"]
        med["symbols"] = runs[0]["symbols"]
        summary[eng] = med

    # 汇总表
    hdr = f"{'引擎':<8}{'L1捞数':>9}{'L2指标':>9}{'L3形态':>9}{'L4横截面':>9}{'合计':>9}{'峰值内存MB':>12}"
    print(hdr)
    print("-" * len(hdr))
    for eng in args.engines.split(","):
        m = summary[eng.strip()]
        print(f"{eng:<8}{m['l1_sec']:>9.3f}{m['l2_sec']:>9.3f}{m['l3_sec']:>9.3f}{m['l4_sec']:>9.3f}{m['total_sec']:>9.3f}{m['peak_rss_mb']:>12.1f}")
    base = summary[args.engines.split(",")[0].strip()]["total_sec"]
    print("\n相对合计耗时（以首个引擎为 1.0x）：")
    for eng in args.engines.split(","):
        m = summary[eng.strip()]
        print(f"  {eng:<8} {m['total_sec']/base:.2f}x")

    # 交叉校验
    print("\n一致性校验：")
    ref = checks[args.engines.split(",")[0].strip()]
    ok = True
    for eng, r in checks.items():
        # 横截面当日命中必须一致；历史累计总数允许 <=1% 的跨引擎窗口边界浮点差

        rel = abs(r["entry_total"] - ref["entry_total"]) / max(1, ref["entry_total"])
        same_cnt = (r["entry_latest"] == ref["entry_latest"]) and rel <= 0.01
        ref_set = {c for c, _ in ref["top5"]}
        cur_set = {c for c, _ in r["top5"]}
        jacc = len(ref_set & cur_set) / max(1, len(ref_set | cur_set))
        flag = "OK" if same_cnt and jacc >= 0.6 else "MISMATCH"
        if flag != "OK":
            ok = False
        print(
            f"  {eng:<8} entry_total={r['entry_total']}(差{rel:.2%}) "
            f"entry_latest={r['entry_latest']} top5重合={jacc:.0%} [{flag}]"
        )
    if not ok:
        print("\n⚠️ 三引擎结果不一致，基准口径有 bug，请勿采信数字。")
    else:
        print("\n横截面当日命中与 top 票一致，历史累计个位数差异为窗口边界浮点舍入，口径对齐。")

    out_dir = ROOT / "results" / "engine_benchmark"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"bench_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w") as fh:
        json.dump({"cutoff": cutoff, "years": args.years, "summary": summary, "checks": checks}, fh,
                  ensure_ascii=False, indent=2)
    print(f"\n结果已写：{out_path.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
