# -*- coding: utf-8 -*-
"""通知层：企业微信群机器人 + Server酱 双通道推送。

两通道相互独立，都会尝试，绝不因一个通道成功就短路另一个（保持历史行为）。
"""
import os

from .config import log


def _push_wecom(body, title):
    """企业微信群机器人推送；成功返回 True，未配置/失败返回 False。"""
    webhook = os.environ.get("WECOM_BOT_WEBHOOK")
    if not webhook:
        return False
    try:
        import requests
        resp = requests.post(webhook, json={"msgtype": "markdown",
                      "markdown": {"content": body}}, timeout=8)
        try:
            rj = resp.json()
            ec, emsg = rj.get("errcode"), rj.get("errmsg")
        except Exception:
            ec, emsg = None, (resp.text or "")[:80]
        if ec == 0:
            log(f"[push:wecom] 发送成功：{title}（errcode=0, HTTP{resp.status_code}）")
            return True
        log(f"[push:wecom] 发送失败：HTTP{resp.status_code} errcode={ec} errmsg={emsg}")
    except Exception as e:
        log(f"[push:wecom] 发送异常：{type(e).__name__}: {e}")
    return False


def _push_serverchan(title, content):
    """Server酱推送；成功返回 True，未配置/失败返回 False。"""
    key = os.environ.get("SERVERCHAN_KEY")
    if not key:
        return False
    try:
        import requests
        resp = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                      data={"title": title, "desp": content}, timeout=8)
        try:
            code = resp.json().get("code")
        except Exception:
            code = None
        if code == 0:
            log(f"[push:serverchan] 发送成功：{title}（code=0, HTTP{resp.status_code}）")
            return True
        log(f"[push:serverchan] 发送失败：HTTP{resp.status_code} resp={(resp.text or '')[:100]}")
    except Exception as e:
        log(f"[push:serverchan] 发送异常：{type(e).__name__}: {e}")
    return False


def push(title, content):
    """双通道推送；返回是否至少一个通道成功。未配置通道仅留痕。"""
    body = f"**{title}**\n{content}"
    wecom_ok = _push_wecom(body, title)
    chan_ok = _push_serverchan(title, content)  # 两通道都尝试，不因一方成功而短路
    pushed = wecom_ok or chan_ok
    if not pushed:
        if os.environ.get("WECOM_BOT_WEBHOOK") or os.environ.get("SERVERCHAN_KEY"):
            log(f"[push] 已配置通道但均未成功送达：{title}")
        else:
            log(f"[NO-PUSH] 未配置 webhook，仅留痕：{title}")
    return pushed
