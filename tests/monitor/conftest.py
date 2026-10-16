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


@pytest.fixture(autouse=True)
def _isolate_workspace():
    """每个测试前清空隔离 MON_ROOT 下的落盘目录，杜绝测试间文件污染。

    所有 monitor 测试共享同一个进程级 _TEST_ROOT；若不逐测试清理，使用
    ``datetime.now()``（"今天"）落盘的测试会与写死"今天"日期的测试
    （如快照首行表头）撞同一文件，导致断言失败。
    """
    import shutil
    from pathlib import Path
    from src.monitor import config
    data_dir = Path(_TEST_ROOT) / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    for d in (config.WORKSPACE, config.ALERT_DIR, config.SNAP_DIR,
              config.DIFF_DIR):
        d.mkdir(parents=True, exist_ok=True)
    yield


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
