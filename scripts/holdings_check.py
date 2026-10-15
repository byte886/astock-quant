#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓盯盘定时推送 · 启动入口（薄封装，需求二 #38）。

由 launchd 每个交易日 9:45 / 13:05 各跑一次，跑完即退（非常驻）。业务逻辑单一来源
为 src/monitor/holdings.py，本文件只把项目根加入 sys.path 再调用其 main()。

手动验证：
  python scripts/holdings_check.py --dry-run   # 只打印不推送
  python scripts/holdings_check.py             # 实际推送企业微信
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.monitor.holdings import main  # noqa: E402  (sys.path 需先就位)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
