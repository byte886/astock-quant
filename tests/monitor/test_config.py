# -*- coding: utf-8 -*-
"""config 层：日志落盘等。"""
from src.monitor import config


def test_log_appends_line_with_newline():
    marker = "MUTPROBE_CONFIG_LOG_LINE"
    config.log(marker)
    last = config.LOG.read_text(encoding="utf-8").splitlines()[-1]
    assert marker in last   # 落盘成功（杀 f.write(line + "\\n") 的 + -> -，变异会抛错被吞而不写）


def test_config_forces_direct_connection_bypassing_proxy(monkeypatch):
    import requests
    import src.monitor.config  # noqa: F401  导入即清代理环境变量并设 no_proxy=*
    # 模拟代理环境存在（macOS 系统代理同理），国内行情/企微仍必须直连
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9999")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9999")
    import os
    assert os.environ["no_proxy"] == "*" and os.environ["NO_PROXY"] == "*"
    sess = requests.Session()
    for url in ["https://push2.eastmoney.com/api/x", "https://qyapi.weixin.qq.com/x"]:
        assert requests.utils.should_bypass_proxies(url, None) is True  # no_proxy=* 绕过
        assert sess.merge_environment_settings(url, {}, None, None, None)["proxies"] == {}  # 实际无代理
