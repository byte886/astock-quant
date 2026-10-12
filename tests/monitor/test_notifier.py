# -*- coding: utf-8 -*-
"""notifier 层：企业微信/Server酱双通道，重点锁定"两通道都尝试、不短路"。"""
import sys
import types

import pytest

from src.monitor import notifier as N


class _Resp:
    def __init__(self, payload=None, status=200, text="", raise_json=False):
        self._payload = payload or {}
        self.status_code = status
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._payload


class _FakeRequests:
    """记录 post 调用，并按 URL/次数返回预设响应或抛异常。"""
    def __init__(self):
        self.calls = []
        self.wecom_resp = _Resp({"errcode": 0})
        self.sc_resp = _Resp({"code": 0})
        self.raise_exc = None

    def post(self, url, json=None, data=None, timeout=None):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        if "sctapi.ftqq.com" in url:
            return self.sc_resp
        return self.wecom_resp


@pytest.fixture()
def fake_requests(monkeypatch):
    fr = _FakeRequests()
    mod = types.ModuleType("requests")
    mod.post = fr.post
    monkeypatch.setitem(sys.modules, "requests", mod)
    monkeypatch.delenv("WECOM_BOT_WEBHOOK", raising=False)
    monkeypatch.delenv("SERVERCHAN_KEY", raising=False)
    return fr


def test_wecom_success(fake_requests, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "http://wecom/x")
    assert N._push_wecom("body", "t") is True
    assert any("wecom/x" in u for u in fake_requests.calls)


def test_wecom_business_fail(fake_requests, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "http://wecom/x")
    fake_requests.wecom_resp = _Resp({"errcode": 93000, "errmsg": "bad"}, 400)
    assert N._push_wecom("body", "t") is False


def test_wecom_unparseable_json_treated_fail(fake_requests, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "http://wecom/x")
    fake_requests.wecom_resp = _Resp(None, 500, text="oops", raise_json=True)
    assert N._push_wecom("body", "t") is False


def test_wecom_exception(fake_requests, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "http://wecom/x")
    fake_requests.raise_exc = RuntimeError("net")
    assert N._push_wecom("body", "t") is False


def test_wecom_not_configured(fake_requests):
    assert N._push_wecom("body", "t") is False
    assert fake_requests.calls == []


def test_serverchan_success(fake_requests, monkeypatch):
    monkeypatch.setenv("SERVERCHAN_KEY", "k1")
    assert N._push_serverchan("t", "c") is True


def test_serverchan_fail_and_exception(fake_requests, monkeypatch):
    monkeypatch.setenv("SERVERCHAN_KEY", "k1")
    fake_requests.sc_resp = _Resp({"code": 40001}, 200)
    assert N._push_serverchan("t", "c") is False
    fake_requests.raise_exc = RuntimeError("x")
    assert N._push_serverchan("t", "c") is False


def test_serverchan_not_configured(fake_requests):
    assert N._push_serverchan("t", "c") is False


def test_push_both_channels_attempted_no_short_circuit(fake_requests, monkeypatch):
    # 关键：企业微信成功后，Server酱仍必须被尝试（历史上两通道独立，不许 or 短路）
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "http://wecom/x")
    monkeypatch.setenv("SERVERCHAN_KEY", "k1")
    assert N.push("title", "content") is True
    assert len(fake_requests.calls) == 2
    assert any("wecom" in u for u in fake_requests.calls)
    assert any("sctapi.ftqq.com" in u for u in fake_requests.calls)


def test_push_all_fail(fake_requests, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "http://wecom/x")
    monkeypatch.setenv("SERVERCHAN_KEY", "k1")
    fake_requests.wecom_resp = _Resp({"errcode": 1}, 500)
    fake_requests.sc_resp = _Resp({"code": 1}, 500)
    assert N.push("t", "c") is False


def test_push_no_channel(fake_requests):
    assert N.push("t", "c") is False
    assert fake_requests.calls == []


# ---------------- 变异边界加固：pushed 聚合与配置分支 ----------------
def test_push_partial_success_either_channel(fake_requests, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "w")
    monkeypatch.setenv("SERVERCHAN_KEY", "s")
    fake_requests.wecom_resp = _Resp({"errcode": 0})
    fake_requests.sc_resp = _Resp({"code": 1}, 500)
    assert N.push("t", "c") is True        # 仅企微成功（杀 pushed 的 or -> and）
    fake_requests.wecom_resp = _Resp({"errcode": 1}, 500)
    fake_requests.sc_resp = _Resp({"code": 0})
    assert N.push("t", "c") is True        # 仅 Server酱成功


def test_push_all_fail_logs_configured(fake_requests, monkeypatch, capsys):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "w")
    monkeypatch.setenv("SERVERCHAN_KEY", "s")
    fake_requests.wecom_resp = _Resp({"errcode": 1}, 500)
    fake_requests.sc_resp = _Resp({"code": 1}, 500)
    assert N.push("t", "c") is False
    assert "已配置通道但均未成功" in capsys.readouterr().out   # 杀 if not pushed 的 delete not


def test_push_only_one_channel_configured_fail(fake_requests, monkeypatch, capsys):
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "w")                # 只配企微、不配 Server酱
    fake_requests.wecom_resp = _Resp({"errcode": 1}, 500)
    assert N.push("t", "c") is False
    assert "已配置通道但均未成功" in capsys.readouterr().out   # 杀配置判断的 or -> and
