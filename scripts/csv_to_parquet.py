#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSV → Parquet(zstd) 幂等转换器（astock-quant 本地数据，不依赖网络）
====================================================================
背景与决策见 `02_决策记录/ADR-004-本地行情存储采用Parquet并引入DuckDB只读查询.md`。

做什么
------
把 data/raw 下白名单目录里已经下好的 CSV，**在同目录、同名**生成一份 Parquet（zstd 压缩）：
    raw/daily/{sh,sz}/code.csv            -> code.parquet
    raw/minute/{sh,sz}/code.csv           -> code.parquet
    raw/etf/daily/{sh,sz}/code.csv        -> code.parquet
    raw/etf/minute/{sh,sz}/code.csv       -> code.parquet
    raw/fundamentals/{profit,growth,dividend}/code.csv -> code.parquet
（guba 等非结构化目录不在白名单内，不转。）

安全约束（重要）
----------------
1. **只读 CSV、绝不修改/删除 CSV**；Parquet 是旁路产物，任何时候删掉 .parquet
   都能用本脚本重建，src/quant/data.py 也保留 "parquet 优先、CSV 回退"。
2. **不打扰正在跑的全市场下载**：CSV 在最近 --stable-seconds（默认 120s）内被写过，
   说明下载进程可能正在写它，本轮跳过（skipped_busy），下次再转。
3. **幂等可重跑**：已存在且新于 CSV 的 Parquet 跳过（up_to_date）；中断只留
   `.parquet.tmp-<pid>` 临时文件，写成功后才原子 rename，绝不留半截 Parquet。
4. 每只都做**往返对账**：行数、列集合、每列非空计数、数值列 sum/min/max、
   日期列 min/max、首末行逐值、字符串列一致；任一不符判 failed，不产出正式 Parquet。

用法
----
    .venv/bin/python scripts/csv_to_parquet.py                 # 全量幂等转换
    .venv/bin/python scripts/csv_to_parquet.py --limit 3       # 每个白名单根各转3只（试跑）
    .venv/bin/python scripts/csv_to_parquet.py --only data/raw/minute/sh/sh.600000.csv
    .venv/bin/python scripts/csv_to_parquet.py --force         # 忽略"已最新"，强制重转
    .venv/bin/python scripts/csv_to_parquet.py --verify-duckdb # 转完用 DuckDB 跨文件总量对账

报告写到 data/_workspace/csv_to_parquet_report.json（data/ 不入库）。
退出码：存在 failed 为 1，否则 0。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api import types as ptypes

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
WORKSPACE = ROOT / "data" / "_workspace"

# 白名单根：只转这些目录（相对 data/raw）。新增结构化数据目录时在此登记。
ROOTS = ["daily", "minute", "etf", "fundamentals"]

# ---- 列类型规范（baostock 固定 schema；未识别列一律按字符串无损落盘，绝不猜成数值）----
DATE_COLS = {
    "date", "pubDate", "statDate",
    "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate", "dividPlanDate",
    "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate",
}
STR_COLS = {"code", "time", "dividCashStock"}   # time=baostock 17位毫秒戳原文，保留字符串
INT_COLS = {"adjustflag", "tradestatus", "isST"}
# 读 CSV 时强制按字符串读的列（避免 17 位 time 被推断成整数、code 被误转）
READ_STR = {"code": str, "time": str}

TMP_SUFFIX = f".parquet.tmp-{os.getpid()}"


def log(*a):
    print(*a, flush=True)


def csv_files(root: Path):
    """白名单根下的全部 csv，按相对路径排序，保证可复现的处理顺序。"""
    if not root.exists():
        return []
    return sorted(root.rglob("*.csv"))


def typeify(raw: pd.DataFrame, rel: str) -> pd.DataFrame:
    """按列名规范把原始 CSV DataFrame 转成应落盘的类型；未知列无损转字符串。"""
    df = pd.DataFrame(index=raw.index)
    for col in raw.columns:
        s = raw[col]
        if col in STR_COLS:
            df[col] = s.astype("string")        # code/time/中文方案列：统一 pandas 字符串类型
        elif col in DATE_COLS:
            df[col] = pd.to_datetime(s, errors="coerce")
        elif col in INT_COLS:
            df[col] = pd.to_numeric(s, errors="coerce").astype("Int64")
        elif ptypes.is_string_dtype(s):
            # pandas3 下文本列 dtype 可能是 str/string 而非 object；
            # 先尝试数值化，若 >1% 非空值无法转数值（如税后分红 '0.495或0.5225'），按文本无损保留
            num = pd.to_numeric(s, errors="coerce")
            nonnull = int(s.notna().sum())
            lost = int((s.notna() & num.isna()).sum())
            if nonnull > 0 and lost / max(nonnull, 1) > 0.01:
                df[col] = s.astype("string")
            else:
                df[col] = num
        else:
            df[col] = pd.to_numeric(s, errors="coerce")
    return df


def semantic_equal(raw: pd.DataFrame, typed: pd.DataFrame, pq: pd.DataFrame):
    """三向语义对账：raw(CSV 原文) ↔ typed(拟写) ↔ pq(Parquet 读回)。

    返回 (ok: bool, problems: list[str])。只做语义等值，不苛求 dtype 字面一致
    （Parquet 往返后 int64/Int64、datetime[ns]/[us] 可能不同，值对即可）。
    """
    problems = []
    if len(raw) != len(typed) or len(raw) != len(pq):
        problems.append(f"行数不一致 csv={len(raw)} typed={len(typed)} parquet={len(pq)}")
        return False, problems
    if list(raw.columns) != list(typed.columns) or list(raw.columns) != list(pq.columns):
        problems.append(f"列集合/顺序不一致 csv={list(raw.columns)} pq={list(pq.columns)}")
        return False, problems

    for col in raw.columns:
        r, t, q = raw[col], typed[col], pq[col]
        # 非空计数
        n_r = int(r.notna().sum())
        n_q = int(pd.Series(q).notna().sum())
        if n_r != n_q:
            problems.append(f"[{col}] 非空计数 csv={n_r} parquet={n_q}")
            continue
        if col in DATE_COLS:
            dr = pd.to_datetime(r, errors="coerce")
            dq = pd.to_datetime(q, errors="coerce")
            # NaT 视为相等；一侧空另一侧有值、或非空值不同，都算不一致
            eq = ((dr == dq) | (dr.isna() & dq.isna()))
            mn_eq = (pd.isna(dr.min()) and pd.isna(dq.min())) or dr.min() == dq.min()
            mx_eq = (pd.isna(dr.max()) and pd.isna(dq.max())) or dr.max() == dq.max()
            if not bool(eq.all()) or not mn_eq or not mx_eq:
                problems.append(f"[{col}] 日期值不一致 min {dr.min()} vs {dq.min()} / max {dr.max()} vs {dq.max()}")
        elif col in INT_COLS:
            a = pd.to_numeric(r, errors="coerce").astype("Float64")
            b = pd.to_numeric(q, errors="coerce").astype("Float64")
            if not _series_eq(a, b):
                problems.append(f"[{col}] 整数列值不一致")
        elif col in STR_COLS or ptypes.is_string_dtype(t):
            # 文本列：先把缺失值统一成哨兵再转字符串（astype(str) 会把 NA 变成 "<NA>"，故先 fillna）
            a = pd.Series(r).fillna("__NA__").astype(str).reset_index(drop=True)
            b = pd.Series(q).fillna("__NA__").astype(str).reset_index(drop=True)
            if not (a.values == b.values).all():
                diff = int((a.values != b.values).sum())
                problems.append(f"[{col}] 字符串列有 {diff} 处不一致")
        else:
            a = pd.to_numeric(r, errors="coerce")
            b = pd.to_numeric(q, errors="coerce")
            if not _num_close(a, b, col):
                problems.append(f"[{col}] 数值列对账失败 sum {a.sum():.6g} vs {b.sum():.6g}")
    return len(problems) == 0, problems


def _series_eq(a: pd.Series, b: pd.Series) -> bool:
    """可空整数/数值列逐值相等（NaN==NaN 视为相等）。"""
    aa = a.astype("Float64").to_numpy(dtype="float64", na_value=np.nan)
    bb = b.astype("Float64").to_numpy(dtype="float64", na_value=np.nan)
    both_nan = np.isnan(aa) & np.isnan(bb)
    return bool(np.all(both_nan | (aa == bb)))


def _num_close(a: pd.Series, b: pd.Series, col: str) -> bool:
    """数值列：非空计数相等前提下，sum/min/max + 逐值 allclose。"""
    aa = a.to_numpy(dtype="float64", na_value=np.nan)
    bb = b.to_numpy(dtype="float64", na_value=np.nan)
    if aa.shape != bb.shape:
        return False
    both_nan = np.isnan(aa) & np.isnan(bb)
    one_nan = np.isnan(aa) ^ np.isnan(bb)
    if one_nan.any():
        return False
    av, bv = aa[~np.isnan(aa)], bb[~np.isnan(bb)]
    if av.size == 0:
        return True
    # 成交量等整数列要求精确；价格/财务浮点列允许极小相对误差
    rtol = 0.0 if col in ("volume",) else 1e-9
    if not np.allclose(av, bv, rtol=rtol, atol=1e-6):
        return False
    if not np.isclose(av.sum(), bv.sum(), rtol=rtol, atol=1e-6):
        return False
    if not (np.isclose(av.min(), bv.min(), rtol=rtol, atol=1e-6)
            and np.isclose(av.max(), bv.max(), rtol=rtol, atol=1e-6)):
        return False
    return True


def convert_one(csv_path: Path, stable_seconds: int, force: bool):
    """转换单只 CSV。返回 (status, detail)。"""
    pq_path = csv_path.with_suffix(".parquet")
    tmp_path = csv_path.with_suffix(TMP_SUFFIX)
    now = time.time()
    csv_mtime = csv_path.stat().st_mtime

    if now - csv_mtime < stable_seconds:
        return "skipped_busy", {"csv": str(csv_path.relative_to(ROOT)),
                                "age_s": round(now - csv_mtime, 1)}
    if pq_path.exists() and pq_path.stat().st_mtime >= csv_mtime and not force:
        return "up_to_date", {"csv": str(csv_path.relative_to(ROOT))}

    t0 = time.time()
    try:
        raw = pd.read_csv(csv_path, dtype=READ_STR, low_memory=False)
    except Exception as e:  # 读 CSV 本身失败（可能正被截断写）：本轮跳过不算坏数据
        if now - csv_mtime < stable_seconds * 3:
            return "skipped_busy", {"csv": str(csv_path.relative_to(ROOT)), "read": repr(e)[:120]}
        return "failed", {"csv": str(csv_path.relative_to(ROOT)), "error": f"读CSV失败 {repr(e)[:120]}"}
    if raw.empty:
        return "skipped_empty", {"csv": str(csv_path.relative_to(ROOT))}

    typed = typeify(raw, str(csv_path.relative_to(ROOT)))
    try:
        typed.to_parquet(tmp_path, engine="pyarrow", compression="zstd", index=False)
        pq = pd.read_parquet(tmp_path)
        ok, problems = semantic_equal(raw, typed, pq)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            return "failed", {"csv": str(csv_path.relative_to(ROOT), ), "error": "; ".join(problems)[:400]}
        os.replace(tmp_path, pq_path)  # 原子落地
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return "failed", {"csv": str(csv_path.relative_to(ROOT)), "error": f"写/对账异常 {repr(e)[:150]}"}

    csv_size = csv_path.stat().st_size
    pq_size = pq_path.stat().st_size
    return "converted", {
        "csv": str(csv_path.relative_to(ROOT)),
        "rows": int(len(raw)), "cols": int(raw.shape[1]),
        "csv_bytes": csv_size, "parquet_bytes": pq_size,
        "ratio_pct": round(100 * pq_size / max(csv_size, 1), 1),
        "elapsed_s": round(time.time() - t0, 2),
    }


def verify_with_duckdb():
    """转完后用 DuckDB 直接对一堆 Parquet 跑 SQL，按目录取行数/股票数/日期范围，
    证明跨文件查询可用；返回可读结果表（不与 CSV 逐行比，逐行比已在 convert_one 做过）。"""
    try:
        import duckdb
    except ImportError:
        return None, "未安装 duckdb（pip install duckdb），跳过跨文件查询校验"
    con = duckdb.connect()
    targets = {
        "daily": "data/raw/daily/*/*.parquet",
        "minute": "data/raw/minute/*/*.parquet",
        "etf_daily": "data/raw/etf/daily/*/*.parquet",
        "etf_minute": "data/raw/etf/minute/*/*.parquet",
        "fund_profit": "data/raw/fundamentals/profit/*.parquet",
        "fund_growth": "data/raw/fundamentals/growth/*.parquet",
        "fund_dividend": "data/raw/fundamentals/dividend/*.parquet",
    }
    rows = []
    for name, glob_pat in targets.items():
        try:
            date_col = "date" if name in ("daily", "minute", "etf_daily", "etf_minute") else "pubDate"
            if name == "fund_dividend":
                date_col = "dividOperateDate"
            q = f"""
                SELECT count(*) AS bars, count(DISTINCT code) AS codes,
                       min({date_col}) AS first_dt, max({date_col}) AS last_dt
                FROM read_parquet('{glob_pat}')
            """
            r = con.execute(q).fetchone()
            rows.append({"dataset": name, "bars": r[0], "distinct_codes": r[1],
                         "min_dt": str(r[2]), "max_dt": str(r[3])})
        except Exception as e:
            rows.append({"dataset": name, "error": repr(e)[:150]})
    return rows, None


def main():
    ap = argparse.ArgumentParser(description="CSV → Parquet(zstd) 幂等转换器")
    ap.add_argument("--stable-seconds", type=int, default=120,
                    help="CSV 最近 N 秒内被写过则跳过（避开在跑的下载），默认120")
    ap.add_argument("--force", action="store_true", help="忽略'已最新'，强制重转")
    ap.add_argument("--limit", type=int, default=0, help="每个白名单根最多转 N 只（试跑），0=不限")
    ap.add_argument("--only", type=str, default="", help="只转指定的单个 CSV 路径")
    ap.add_argument("--verify-duckdb", action="store_true", help="转完用 DuckDB 跨文件总量校验")
    args = ap.parse_args()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    files = []
    if args.only:
        files = [Path(args.only).expanduser().resolve()]
    else:
        for r in ROOTS:
            fs = csv_files(RAW / r)
            if args.limit:
                fs = fs[:args.limit]
            files.extend(fs)

    log(f"待检查 CSV：{len(files)} 个（白名单根：{', '.join(ROOTS)}；stable>{args.stable_seconds}s）")
    buckets = {"converted": [], "up_to_date": [], "skipped_busy": [],
               "skipped_empty": [], "failed": []}
    t_start = time.time()
    for i, f in enumerate(files, 1):
        status, detail = convert_one(f, args.stable_seconds, args.force)
        buckets[status].append(detail)
        if status == "converted":
            log(f"[{i}/{len(files)}] ✅ {detail['csv']}  {detail['rows']}行 "
                f"{detail['csv_bytes']/1024:.0f}KB→{detail['parquet_bytes']/1024:.0f}KB "
                f"({detail['ratio_pct']}%) {detail['elapsed_s']}s")
        elif status == "failed":
            log(f"[{i}/{len(files)}] ❌ {detail.get('csv')}: {detail.get('error')}")
        elif i % 50 == 0 or status == "skipped_busy":
            log(f"[{i}/{len(files)}] {status}: {detail.get('csv', detail)}")

    # 汇总
    def total_bytes(lst, key):
        return sum(int(x.get(key, 0)) for x in lst)
    csv_b = total_bytes(buckets["converted"], "csv_bytes")
    pq_b = total_bytes(buckets["converted"], "parquet_bytes")
    summary = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t_start, 1),
        "counts": {k: len(v) for k, v in buckets.items()},
        "converted_csv_bytes": csv_b,
        "converted_parquet_bytes": pq_b,
        "converted_ratio_pct": round(100 * pq_b / max(csv_b, 1), 1),
    }
    duck_rows, duck_err = (None, None)
    if args.verify_duckdb:
        duck_rows, duck_err = verify_with_duckdb()
        summary["duckdb_verify"] = duck_rows if duck_rows is not None else {"note": duck_err}

    report = {"summary": summary, "items": buckets}
    (WORKSPACE / "csv_to_parquet_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str))

    log("\n" + "=" * 60)
    log(f"转换 {summary['counts']['converted']}｜已最新 {summary['counts']['up_to_date']}｜"
        f"忙跳过 {summary['counts']['skipped_busy']}｜空 {summary['counts']['skipped_empty']}｜"
        f"失败 {summary['counts']['failed']}｜用时 {summary['elapsed_s']}s")
    if csv_b:
        log(f"本次转换体积：{csv_b/1024/1024:.1f}MB CSV → {pq_b/1024/1024:.1f}MB Parquet "
            f"({summary['converted_ratio_pct']}%)")
    if duck_err:
        log(f"DuckDB 校验：{duck_err}")
    elif duck_rows:
        log("DuckDB 跨文件校验：")
        for r in duck_rows:
            if "error" in r:
                log(f"  {r['dataset']}: 错误 {r['error']}")
            else:
                log(f"  {r['dataset']:14s} bars={r['bars']:<10} codes={r['distinct_codes']:<5} "
                    f"{r['min_dt']} ~ {r['max_dt']}")
    log(f"报告：data/_workspace/csv_to_parquet_report.json")
    return 1 if buckets["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
