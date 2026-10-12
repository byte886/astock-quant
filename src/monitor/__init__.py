# -*- coding: utf-8 -*-
"""盘中全板块异动实时监测器（分层包）。

分层（依赖只能从上往下，见 pyproject.toml [tool.importlinter]）：
  cli -> monitor -> reconcile / notifier / persistence
                 -> statemachine / aggregator / datasources -> codes -> config
"""
__all__ = ["cli"]
