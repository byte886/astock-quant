# -*- coding: utf-8 -*-
"""监控包测试公共夹具。

最顶部（import src.monitor 之前）设置 MON_ROOT 到隔离临时目录，使 config 导入时的
mkdir 与全部落盘都落在临时目录，绝不污染真实 data/。
"""
import os
import sys
import tempfile

# 必须在首次 import src.monitor.config 之前设置
_TEST_ROOT = tempfile.mkdtemp(prefix="mon_pytest_")
os.environ["MON_ROOT"] = _TEST_ROOT

import pytest  # noqa: E402


@pytest.fixture()
def fake_akshare(monkeypatch):
    """注入一个假的 akshare 模块（测试按需往里面塞属性）。"""
    import types
    fake = types.ModuleType("akshare")
    monkeypatch.setitem(sys.modules, "akshare", fake)
    return fake


@pytest.fixture()
def no_push(monkeypatch):
    """把推送替换为记录调用，避免测试误发企业微信/Server酱。"""
    calls = []

    def _fake_push(title, content):
        calls.append((title, content))
        return True

    import src.monitor.notifier as notifier
    import src.monitor.monitor as monitor
    monkeypatch.setattr(notifier, "push", _fake_push)
    monkeypatch.setattr(monitor, "push", _fake_push)
    import src.monitor.cli as cli
    monkeypatch.setattr(cli, "push", _fake_push)
    calls_mod = calls
    calls_mod.clear()
    return calls_mod
