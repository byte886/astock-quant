#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中板块异动监控 · 启动入口（薄封装）

业务实现已模块化到 src/monitor/（2026-10-12 clean-code-gauntlet 质量改造）。
本文件只做两件事：把项目根加入 sys.path，再调用包的命令行入口 main()。
逻辑单一来源为 src/monitor/，本文件不含任何业务判定，避免双份代码漂移。

兼容两种启动方式：
  - launchd 常驻（plist WorkingDirectory=项目根）；
  - 手动 `python scripts/intraday_sector_monitor.py [--once|--build-map]`。
参数透传由 src.monitor.cli.main 解析：--once 单次扫描 / --build-map 只建板块映射。
"""
import sys
from pathlib import Path

# scripts/ 的上一级即项目根；显式入栈，保证脱离 launchd 工作目录也能 import src.*
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.monitor.cli import main  # noqa: E402  (sys.path 需先就位)

if __name__ == "__main__":
    main()
