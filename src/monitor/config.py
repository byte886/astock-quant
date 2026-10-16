# -*- coding: utf-8 -*-
"""配置层：路径、阈值（环境变量可覆盖）、日志、数值工具。

本层是整个包最底层，不依赖包内任何其它模块；并在导入第一时间清空代理环境变量，
保证后续在函数内 import akshare / 访问国内行情接口时强制直连。
"""
import os
import re
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
# ---- 需求二 #38 持仓盯盘定时推送（上午 9:45 / 下午 13:05 各一次）----
HOLD_DROP_PCT = _f("MON_HOLD_DROP_PCT", -7.0)       # 持仓个股大跌预警线%（低于即预警，跌停另标）
HOLD_MAX_SHOWN = int(_f("MON_HOLD_MAX_SHOWN", 10))  # 持仓盯盘最多展示只数（防刷屏）
HOLD_BOARD_STRONG = _f("MON_HOLD_BOARD_STRONG", 2.5)  # 所属板块"当前走强"提示线%
HOLD_BOARD_MAX_MOVES = int(_f("MON_HOLD_BOARD_MAX_MOVES", 10))  # 持仓盯盘"所属板块异动"最多列几条（防刷屏）
# 泛化概念板块过滤开关：默认 "0"=过滤假板块（融资融券/指数成分/机构重仓等）；设 "1" 回到全量不过滤。
MON_INCLUDE_GENERIC = os.environ.get("MON_INCLUDE_GENERIC", "0") == "1"
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


# ---------------------------------------------------------------------------
# 泛化概念板块过滤（板块口径单一事实源）
# ---------------------------------------------------------------------------
# 东财"概念板块"里混着一大批"非题材"标签：它们覆盖几百上千只股票，一开盘就集体
# 同涨同跌，没有任何题材/炒作指向，对"板块异动"是噪声（如融资融券、基金重仓、
# 百元股、破净股、昨日涨停、指数成分、带年份的财报标签）。这些标签会让持仓盯盘
# 一次刷出几十条"所属板块异动"、淹没真信号，故从主监控（em_boards 概念）与持仓
# 盯盘（tx_board_agg 聚合）一并剔除。行业（industry:）板块不在此列，一律保留。
#
# 【保留白名单】以下即使覆盖很大也绝不能滤（真题材/政策主线，写注释固化）：
#   央国企改革、中特估、中字头、一带一路、西部大开发、长江三角、深圳特区、雄安新区、
#   京津冀、成渝特区、海南自贸、粤港自贸、上海自贸、湖北自贸、东北振兴、滨海新区、
#   新型工业化、新型城镇化、乡村振兴、PPP模式、统一大市场、内贸流通、沪企改革、
#   国资云概念、化债(AMC)概念、土地流转、供销社概念、创投、独角兽、并购重组概念、
#   股权转让、IPO受益、知识产权、REITs概念、北交所概念；以及机器人/创新药/半导体/
#   低空经济等真题材、全部真行业。
# 用显式分类名单（而非纯覆盖数硬阈值）：硬阈值会误杀机器人/人工智能/新能源车等大题材。
_GENERIC_CHANNEL = frozenset({
    "融资融券", "深股通", "沪股通", "转融券标的", "转债标的", "AH股", "AB股", "B股", "GDR",
})
_GENERIC_INDEX = frozenset({
    "创业板综", "创业成份", "中证500", "深成500", "深证100R", "上证380", "上证50_", "上证180_",
    "HS300_", "央视50_", "微盘精选", "科创板做市股", "科创板做市商", "茅指数", "宁组合",
})
_GENERIC_HOLDERS = frozenset({
    "富时罗素", "标准普尔", "MSCI中国", "QFII重仓", "基金重仓", "机构重仓", "社保重仓",
    "证金持股", "养老金", "券商金股", "密集调研", "举牌", "东方财富热股", "超级品牌",
})
_GENERIC_STYLE = frozenset({
    "大盘股", "中盘股", "小盘股", "微盘股", "大盘成长", "大盘价值", "中盘成长", "中盘价值",
    "小盘成长", "小盘价值", "权重股", "行业龙头", "趋势股", "题材股", "反转股", "红利股",
    "红利破净股", "价值股", "周期股", "微利股", "科技风格", "消费风格", "金融地产风格",
    "先进制造风格", "医药医疗风格",
})
_GENERIC_PRICE = frozenset({
    "百元股", "低价股", "高市净率", "低市净率", "破净股", "长期破净", "破发股", "破增发价股",
    "高成长股", "贬值受益", "超跌股", "昨日高振幅", "昨日高换手", "昨日涨停", "昨日涨停_含一字",
    "昨日触板", "昨日首板", "昨日炸板", "昨日连板", "昨日连板_含一字", "昨日打二板以上表现",
    "最近多板", "近期新高", "历史新高", "百日新高", "近期摘帽", "次新股", "ST股", "高股息",
})
_GENERIC_EQUITY = frozenset({
    "股权分散", "股权集中", "股权激励", "参股银行", "参股券商", "参股保险", "参股期货",
    "参股新三板",
})
_GENERIC_QUALITY = frozenset({"专精特新"})

# 非年份写法的业绩标签（带年份的由 _RE_EARNINGS 正则覆盖，如"2026中报预增"）。
_GENERIC_EXTRA = frozenset({
    "业绩预增", "业绩预减", "业绩预盈", "业绩预亏", "预盈预增", "预亏预减",
})
_RE_EARNINGS = re.compile(r"^(19|20)\d{2}.*(?:预增|预减|首亏|扭亏|预亏|预盈)")

GENERIC_CONCEPTS = (_GENERIC_CHANNEL | _GENERIC_INDEX | _GENERIC_HOLDERS | _GENERIC_STYLE
                    | _GENERIC_PRICE | _GENERIC_EQUITY | _GENERIC_QUALITY)


def is_generic_concept(name):
    """判断一个概念板块名是否为"非题材泛化标签"（应过滤、不参与异动/盯盘）。

    纯函数，不读环境变量；回滚开关 MON_INCLUDE_GENERIC 在调用处判断。
    None / 非字符串 / 空白一律返回 False（保守不误伤）。
    """
    if not isinstance(name, str):
        return False
    nm = name.strip()
    if not nm:
        return False
    return nm in GENERIC_CONCEPTS or nm in _GENERIC_EXTRA or bool(_RE_EARNINGS.match(nm))
