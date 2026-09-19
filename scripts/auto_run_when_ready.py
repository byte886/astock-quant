#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轮询等待数据下齐，自动跑多因子正式回测。
- 每 10 分钟检查一次状态文件；
- 财务 completed >= FUND_MIN 且 中证800日线处理完（completed+failed >= DAILY_MIN）即触发；
- 触发后运行 run_multifactor_backtest.py，结果写 results/multifactor_backtest/；
- 最多等待 MAX_WAIT_HOURS，超时退出并记录。
"""

import json
import subprocess
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "data/_workspace"
LOG = WS / "logs/auto_backtest.log"
FUND_MIN = 690       # 财务至少完成 690/706（容忍少量失败）
DAILY_MIN = 700      # 中证800日线处理完（completed+failed）
INTERVAL = 600       # 10 分钟
MAX_WAIT_HOURS = 12


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def counts():
    fund = json.loads((WS / "fundamentals_status.json").read_text())
    daily = json.loads((WS / "csi800_daily_status.json").read_text())
    fc = len(fund.get("completed", []))
    dc = len(daily.get("completed", [])) + len(daily.get("failed", {}))
    return fc, dc


def main():
    log("自动回测守护启动，等待数据下齐 ...")
    waited = 0
    while waited < MAX_WAIT_HOURS * 3600:
        try:
            fc, dc = counts()
            log(f"进度检查：财务 {fc}/706，中证800日线处理 {dc}/706")
            if fc >= FUND_MIN and dc >= DAILY_MIN:
                log("数据已下齐，开始正式回测 ...")
                r = subprocess.run(
                    [str(ROOT / ".venv/bin/python"),
                     str(ROOT / "scripts/run_multifactor_backtest.py")],
                    cwd=ROOT, capture_output=True, text=True)
                with open(LOG, "a") as f:
                    f.write(r.stdout + "\n" + r.stderr + "\n")
                log(f"回测结束，returncode={r.returncode}，结果见 results/multifactor_backtest/")
                return
        except Exception as e:
            log(f"检查异常（将重试）：{e}")
        time.sleep(INTERVAL)
        waited += INTERVAL
    log("等待超时，未触发回测，请手动检查数据。")


if __name__ == "__main__":
    main()
