#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部免费数据源探针（服务 I9 / T31：1号业绩、2号资金净额、板块催化）
=====================================================================
逐项实测各通道在【当前网络】下是否可得、字段是什么，输出人读报告。
换网络环境（家里/操盘手机器）后应重跑复测——东财行情系接口(push2)曾在
某网络下被服务端断连，而 datacenter 系正常，结论带网络前提。

用法：
  python scripts/probe_external_data.py                 # 全项探针
  python scripts/probe_external_data.py --code 002531   # 指定代表股
报告：results/external_data_probe/probe_YYYYMMDD_HHMM.txt（本地不入库）
"""

import sys
import time
import signal
import argparse
from pathlib import Path
from datetime import datetime

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import akshare as ak  # noqa: E402

OUT_DIR = PROJECT_ROOT / "results/external_data_probe"


class TimeoutErr(Exception):
    pass


def _alarm(signum, frame):
    raise TimeoutErr()


def probe(report, name, fn, timeout=30, retries=2):
    """单项探针：超时/异常重试，结果写报告。"""
    for t in range(retries + 1):
        try:
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(timeout)
            df = fn()
            signal.alarm(0)
            cols = list(df.columns) if hasattr(df, "columns") else []
            line = f"✅ {name}: shape={getattr(df, 'shape', '?')} cols={cols[:12]}"
            report.append(line)
            if hasattr(df, "head"):
                report.append(df.head(2).to_string()[:500])
            return True
        except Exception as e:
            signal.alarm(0)
            err = f"{type(e).__name__}: {str(e)[:100]}"
            if t < retries:
                time.sleep(2)
            else:
                report.append(f"❌ {name}: {err}（重试{retries}次仍失败）")
                return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="002531", help="代表股（默认天顺风能）")
    args = ap.parse_args()
    code = args.code
    mkt = "sh" if code.startswith("6") else "sz"

    report = [f"外部数据源探针 · {datetime.now():%Y-%m-%d %H:%M}",
              f"akshare {ak.__version__}；代表股 {code}", "=" * 70, ""]

    # ---------- 东财 datacenter-web 系（历史库类） ----------
    report.append("【东财 datacenter-web 系】")
    probe(report, "龙虎榜详情 stock_lhb_detail_em(近5日)",
          lambda: ak.stock_lhb_detail_em(
              start_date=(datetime.now() - pd.Timedelta(days=7)).strftime("%Y%m%d"),
              end_date=datetime.now().strftime("%Y%m%d")))
    report.append("")

    # ---------- 东财 push2/push2his 系（行情/资金流，部分网络被断连） ----------
    report.append("【东财 push2/push2his 行情资金流系（若全❌=该网络被服务端断连）】")
    probe(report, "个股历史资金流 stock_individual_fund_flow",
          lambda: ak.stock_individual_fund_flow(stock=code, market=mkt))
    probe(report, "东财实时行情 stock_zh_a_spot_em", ak.stock_zh_a_spot_em)
    probe(report, "行业资金流排名 stock_sector_fund_flow_rank",
          lambda: ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流"))
    probe(report, "东财概念板块 stock_board_concept_name_em",
          ak.stock_board_concept_name_em)
    report.append("")

    # ---------- 新浪系（资金流截面快照） ----------
    report.append("【新浪系：资金流截面快照，每日盘后存档可积累历史】")
    probe(report, "新浪资金流即时排行 stock_fund_flow_individual(即时)",
          lambda: ak.stock_fund_flow_individual(symbol="即时"))
    probe(report, "新浪资金流3日排行 stock_fund_flow_individual(3日排行)",
          lambda: ak.stock_fund_flow_individual(symbol="3日排行"))
    report.append("")

    # ---------- 同花顺系（板块行情/资金，成分股函数本版 akshare 缺失） ----------
    report.append("【同花顺系：板块目录/资金/指数历史】")
    probe(report, "同花顺行业板块一览 stock_board_industry_summary_ths",
          ak.stock_board_industry_summary_ths)
    probe(report, "同花顺概念板块目录 stock_board_concept_name_ths",
          ak.stock_board_concept_name_ths)
    probe(report, "概念指数历史 stock_board_concept_index_ths(风电,近1月)",
          lambda: ak.stock_board_concept_index_ths(
              symbol="风电",
              start_date=(datetime.now() - pd.Timedelta(days=32)).strftime("%Y%m%d"),
              end_date=datetime.now().strftime("%Y%m%d")))
    report.append("")
    report.append("注：个股→概念成分归属，本版 akshare 无直接函数，"
                  "落地时改用东财 datacenter RPT 接口或问财，再探针补测。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"probe_{datetime.now():%Y%m%d_%H%M}.txt"
    out.write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    print(f"\n报告已存：{out}")


if __name__ == "__main__":
    main()
