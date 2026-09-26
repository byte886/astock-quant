#!/bin/bash
# -*- coding: utf-8 -*-
# 每交易日盘后自动编排（launchd com.astock-quant.daily-after-close，周一至周五 18:30）
# ==============================================================================
# 本编排现在只做一件事：推进模拟盘。
#   · 执行到期 pending（次日开盘成交）→ 收盘盯市 → 月末出新信号
#
# 数据增量（个股/ETF 日线、5分钟线）已统一交给常驻的 incremental-worker
# （scripts/incremental_update.py --daemon），它在盘后 15:05 起按优先级串行更新。
# 这里不再跑 baostock/akshare 增量，避免同一 baostock 账号两个进程并发登录挂起（W3）。
# worker 在 15:05 后先更新核心池，到 18:30 模拟盘运行时，所需数据已就绪。
# 节假日/非交易日跑了也无害（模拟盘无新日期，幂等）。
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
echo "[$(ts)] 盘后编排开始（$(date '+%A')）— 仅模拟盘（数据增量由 incremental-worker 负责）"

echo "[$(ts)] [1/1] 推进模拟盘 ..."
"$PY" scripts/run_paper_trading.py
RC=$?
echo "[$(ts)] [1/1] 结束 rc=$RC"

echo "[$(ts)] 盘后编排完成 rc=$RC"
} >> "$LOG" 2>&1

exit $RC
