# -*- coding: utf-8 -*-
"""config 层：日志落盘等。"""
from src.monitor import config


def test_log_appends_line_with_newline():
    marker = "MUTPROBE_CONFIG_LOG_LINE"
    config.log(marker)
    last = config.LOG.read_text(encoding="utf-8").splitlines()[-1]
    assert marker in last   # 落盘成功（杀 f.write(line + "\\n") 的 + -> -，变异会抛错被吞而不写）
