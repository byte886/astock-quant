# -*- coding: utf-8 -*-
"""config 层：日志落盘等。"""
import pytest

from src.monitor import config
from src.monitor.config import is_generic_concept


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


# ---------------- 泛化概念板块过滤（is_generic_concept，单一事实源） ----------------
@pytest.mark.parametrize("name", [
    "融资融券", "沪股通", "AH股",            # 交易通道/股本
    "中证500", "茅指数", "创业板综",          # 指数成分/指数板块
    "基金重仓", "机构重仓", "证金持股", "超级品牌",  # 外资/机构/国家队/榜单
    "大盘股", "红利股", "周期股",             # 市值/风格
    "百元股", "破净股", "昨日涨停", "昨日连板_含一字", "高股息", "次新股",  # 价格/估值/形态
    "股权分散", "参股银行",                   # 股本/股权/参股
    "专精特新",                            # 资质
    "业绩预增", "预亏预减",                  # 非年份业绩标签
    "2026中报预增", "2026一季报扭亏",          # 带年份财报（正则命中）
    "  融资融券 ",                         # 带空格也命中（strip）
])
def test_is_generic_concept_true(name):
    assert is_generic_concept(name) is True


@pytest.mark.parametrize("name", [
    "央国企改革", "中特估", "中字头", "一带一路", "化债(AMC)概念", "创投",
    "知识产权", "北交所概念", "REITs概念",       # 政策主线白名单，绝不能滤
    "机器人概念", "创新药", "半导体", "低空经济", "PCB",  # 真题材
    "元件", "证券Ⅱ", "银行",                 # 真行业（industry 名，不在概念名单）
])
def test_is_generic_concept_false(name):
    assert is_generic_concept(name) is False


def test_is_generic_concept_non_string_and_blank():
    assert is_generic_concept(None) is False
    assert is_generic_concept(123) is False
    assert is_generic_concept("") is False
    assert is_generic_concept("   ") is False
