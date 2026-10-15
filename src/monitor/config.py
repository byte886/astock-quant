# -*- coding: utf-8 -*-
"""配置层：路径、阈值（环境变量可覆盖）、日志、数值工具。

本层是整个包最底层，不依赖包内任何其它模块；并在导入第一时间清空代理环境变量，
保证后续在函数内 import akshare / 访问国内行情接口时强制直连。
"""
import os
from pathlib import Path
from datetime import datetime

# 国内行情必须直连，禁止任何代理（沙箱会注入 *_FORWARD_PROXY，launchd 继承后会被
# 代理劫持报 ProxyError；必须在 import akshare 前清干净）。本模块在包内最先被导入。
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)
# 上面只清了环境变量，但 requests/urllib 在 macOS 上仍会读取"系统代理"（网络设置里的
# HTTP/SOCKS 代理，多由代理客户端开启），akshare 走 requests 时仍会被劫持。本链路全部
# 是国内行情/企微接口，必须直连：no_proxy=* 使 requests 的 should_bypass_proxies 对任意
# 主机返回 True，彻底绕过系统代理。修复 2026-10-13 开盘 akshare 取东财板块名被系统代理
# 劫持报 ProxyError、板块映射拖到 09:46 才建成、开盘头 16 分钟漏盯的问题。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

# 项目根：线上为 src/monitor 的上两级；测试可用 MON_ROOT 指向隔离临时目录。
ROOT = Path(os.environ.get("MON_ROOT", Path(__file__).resolve().parents[2]))
WORKSPACE = ROOT / "data" / "_workspace"
ALERT_DIR = ROOT / "data" / "intraday_alerts"
SNAP_DIR = WORKSPACE / "snapshots"      # 每轮全板块快照，供事后逐分钟复盘
DIFF_DIR = WORKSPACE / "source_diff"    # 东财 vs 腾讯 两源口径偏差对账样本
for _d in (WORKSPACE, ALERT_DIR, SNAP_DIR, DIFF_DIR):
    _d.mkdir(parents=True, exist_ok=True)
LOG = WORKSPACE / "intraday_monitor.log"
TRADE_CAL = WORKSPACE / "trade_cal.json"
STATE_FILE = WORKSPACE / "monitor_state.json"   # 最近一次正常扫描时间，用于停机补检
LOCK_FILE = WORKSPACE / ".intraday_monitor.lock"


def _f(name, default):
    """读环境变量并转 float；未设置返回默认值。"""
    v = os.environ.get(name)
    return float(v) if v is not None else default


CHG_L1      = _f("MON_CHG_L1", 2.5)
SPEED_L1    = _f("MON_SPEED_L1", 1.5)    # L1 涨速阈值 %（10min）
SPEED_LATE  = _f("MON_SPEED_LATE", 1.0)  # 尾盘(14:30后)涨速阈值 %（5min）
ZT_L1       = int(_f("MON_ZT_L1", 3))
ZT_L2       = int(_f("MON_ZT_L2", 5))
LB_L2       = _f("MON_LB_L2", 1.5)
UP_RATIO    = _f("MON_UP_RATIO", 0.6)
CONCENTRATION = _f("MON_CONC", 0.15)
STEP_ZT       = int(_f("MON_STEP_ZT", 2))
RELAPSE_GRACE_MIN = float(_f("MON_RELAPSE_GRACE_MIN", 10))
LEADER_LIMIT = int(_f("MON_LEADER_LIMIT", 10))   # 板块推送附带个股明细的最多只数
LEADER_UP_MIN = _f("MON_LEADER_UP_MIN", 3.0)     # 领涨股（未涨停）最低涨幅%，低于不算领涨
THRESHOLD_SNAP = (f"chg>{CHG_L1}%|ztL1>={ZT_L1}|ztL2>={ZT_L2}|"
                  f"lb>={LB_L2}|speed>{SPEED_L1}/尾盘{SPEED_LATE}")
POLL_NORMAL = int(_f("MON_POLL_NORMAL", 60))
POLL_LATE   = int(_f("MON_POLL_LATE", 30))
POLL_CLOSED = int(_f("MON_POLL_CLOSED", 60))
HB_STALL_SEC = int(_f("MON_HB_STALL_SEC", 1800))
RECONCILE_INTERVAL_SEC = int(_f("MON_RECONCILE_INTERVAL_SEC", 1800))

EM_HEADERS = ["-H", "Referer: https://quote.eastmoney.com/",
              "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"]


def log(msg):
    """打印并追加到监测日志；日志写入失败绝不影响主流程。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def num(x):
    """东财字段 '-' / '' / None 安全转 float；无法转换返回 None。"""
    try:
        if x in ("-", "", None):
            return None
        return float(x)
    except Exception:
        return None
