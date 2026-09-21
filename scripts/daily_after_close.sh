#!/bin/bash
# -*- coding: utf-8 -*-
# 每交易日盘后自动编排（launchd com.astock-quant.daily-after-close，周一至周五 18:30）
# ==============================================================================
# 步骤：
#   1. 中证800宇宙日线增量（update_daily_after_close.py，幂等）
#   2. 基准沪深300ETF(sh.510300)当日行情补记（D-15，akshare 全量覆盖，几秒）
#   3. 推进模拟盘：执行到期 pending（次日开盘成交）→ 收盘盯市 → 月末出新信号
# 节假日/非交易日跑了也无害（无新数据，模拟盘无新日期，各步幂等）。
# 单步失败不阻断后续，但退出码非 0 便于排查；全程写日志。
#
# 手动跑：bash scripts/daily_after_close.sh
# ==============================================================================

set -u
REPO="/Users/wenjiechen/Desktop/astock-quant"
PY="$REPO/.venv/bin/python"
LOG="$REPO/data/_workspace/daily_after_close.log"

cd "$REPO" || exit 2
mkdir -p "$(dirname "$LOG")"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
{
echo "=============================================================="
echo "[$(ts)] 盘后编排开始（$(date '+%A')）"

echo "[$(ts)] [1/3] 中证800日线增量 ..."
"$PY" scripts/update_daily_after_close.py
RC1=$?
echo "[$(ts)] [1/3] 结束 rc=$RC1"

echo "[$(ts)] [2/3] 基准沪深300ETF(sh.510300)补记 ..."
"$PY" scripts/download_etf_akshare.py --etf sh.510300
RC2=$?
echo "[$(ts)] [2/3] 结束 rc=$RC2"

echo "[$(ts)] [3/3] 推进模拟盘 ..."
"$PY" scripts/run_paper_trading.py
RC3=$?
echo "[$(ts)] [3/3] 结束 rc=$RC3"

echo "[$(ts)] 盘后编排完成 rc=($RC1,$RC2,$RC3)"
} >> "$LOG" 2>&1

# 任一步非 0 即整体非 0（launchd 可见；失败详情看日志）
exit $(( RC1 != 0 || RC2 != 0 || RC3 != 0 ))
