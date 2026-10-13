# -*- coding: utf-8 -*-
"""数据源层（网络 IO，全部内置降级，任一挂掉不崩）：

  1. 东财 push2 板块快照（curl 子进程，绕过 requests TLS 指纹拦截）。
  2. 腾讯个股快照（AkShare stock_zh_a_spot_tx）：涨速/量比/涨跌幅，抗封。
  3. 东财涨停池（AkShare stock_zt_pool_em）：涨停家数、连板、所属行业。
  4. 新浪板块（AkShare stock_sector_spot）：板块涨幅/成交额备份。
另有板块成分映射构建（东财优先、新浪兜底，当日缓存）与交易日历。
"""
import sys
import json
import time
import threading
import subprocess
from datetime import datetime, time as dtime

from .config import (WORKSPACE, TRADE_CAL, EM_HEADERS, num, log)
from .codes import normalize_code


# ---------------- 东财 push2（curl 直连） ----------------
def em_boards(kind="concept"):
    """返回 [{code:BKxx,name,chg,amount,lb,speed,up,down}]；失败返回 None。"""
    fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
    fields = "f2,f3,f6,f10,f12,f14,f22,f104,f105"
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1000&po=1&np=1"
           f"&fltt=2&invt=2&fs={fs}&fields={fields}")
    try:
        out = subprocess.run(["curl", "-s", "--retry", "2", "-m", "15", *EM_HEADERS, url],
                             capture_output=True, text=True, timeout=20).stdout
        d = json.loads(out)
        rows = []
        for r in d["data"]["diff"]:
            rows.append({
                "code": r.get("f12"), "name": r.get("f14"),
                "chg": num(r.get("f3")), "amount": num(r.get("f6")),
                "lb": num(r.get("f10")), "speed": num(r.get("f22")),
                "up": num(r.get("f104")), "down": num(r.get("f105")),
            })
        return rows
    except Exception as e:
        log(f"[em_boards:{kind}] 失败（降级）：{type(e).__name__}")
        return None


# ---------------- 腾讯个股快照 ----------------
def tx_spot():
    """返回腾讯全市场个股快照 DataFrame；失败 None。"""
    try:
        import akshare as ak
        return ak.stock_zh_a_spot_tx()
    except Exception as e:
        log(f"[tx_spot] 失败：{type(e).__name__}")
        return None


# ---------------- 东财涨停池 ----------------
def zt_pool(date):
    """返回 list[dict(code,name,lianban,industry)]；失败 None。"""
    try:
        import akshare as ak
        df = ak.stock_zt_pool_em(date=date)
        out = []
        for _, r in df.iterrows():
            out.append({
                "code": normalize_code(r["代码"]),
                "name": r.get("名称"),
                "lianban": r.get("连板数", 1),
                "industry": r.get("所属行业"),
            })
        return out
    except Exception as e:
        log(f"[zt_pool:{date}] 失败：{type(e).__name__}")
        return None


# ---------------- 新浪板块 ----------------
def sina_boards(indicator="概念"):
    """返回 [{name,chg,amount}]；失败 None。"""
    try:
        import akshare as ak
        df = ak.stock_sector_spot(indicator=indicator)
        rows = []
        for _, r in df.iterrows():
            rows.append({"name": r.get("板块"), "chg": num(r.get("涨跌幅")),
                         "amount": num(r.get("总成交额"))})
        return rows
    except Exception as e:
        log(f"[sina_boards:{indicator}] 失败：{type(e).__name__}")
        return None


# ---------------- 板块成分映射（盘前构建，当日缓存） ----------------
def _collect_sina_indicator(c2b, indicator, kind):
    """收集单个新浪指标（概念/新浪行业）下每个板块的成分，原地并入 c2b。"""
    import akshare as ak
    try:
        spot = ak.stock_sector_spot(indicator=indicator)
    except Exception as e:
        log(f"[sina_map:{indicator}] 板块列表失败：{type(e).__name__}")
        return
    for _, r in spot.iterrows():
        label, nm = r.get("label"), r.get("板块")
        if not label or not nm:
            continue
        try:
            df = ak.stock_sector_detail(sector=str(label))
            codecol = "code" if "code" in df.columns else df.columns[0]
            for c in df[codecol]:
                c2b.setdefault(normalize_code(c), set()).add(f"{kind}:{nm}")
        except Exception:
            continue
        time.sleep(0.05)


def _sina_sector_map():
    """东财成分被限时用新浪板块成分兜底；返回 {code: set(kind:板块名)}，失败/空 {}。"""
    c2b = {}
    for indicator, kind in [("概念", "concept"), ("新浪行业", "industry")]:
        _collect_sina_indicator(c2b, indicator, kind)
    return c2b


def _em_board_names(kind):
    """取东财板块名列表（行业/概念）；失败返回 []。"""
    import akshare as ak
    try:
        names = ak.stock_board_industry_name_em() if kind == "industry" \
            else ak.stock_board_concept_name_em()
        col = "板块名称" if "板块名称" in names.columns else names.columns[1]
        return names[col].tolist()
    except Exception as e:
        log(f"[build_map:{kind}] 东财板块名列表失败（回退新浪）：{type(e).__name__}")
        return []


def _collect_em_cons(c2b, kind, fn, names):
    """遍历东财板块名，把每个板块成分并入 c2b。"""
    for nm in names:
        try:
            df = fn(symbol=nm)
            codecol = "代码" if "代码" in df.columns else df.columns[1]
            for c in df[codecol]:
                c2b.setdefault(normalize_code(c), set()).add(f"{kind}:{nm}")
        except Exception:
            continue
        time.sleep(0.1)


def _cached_sector_map(today):
    """读取当日板块映射缓存；非空才返回，无缓存/空缓存/损坏返回 None（须重建）。"""
    cache = WORKSPACE / f"sector_map_{today}.json"
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data if data else None
    except Exception:
        return None


# 行业（industry）是盘中板块监控的主维度；一份映射至少要覆盖这么多只行业成分股，
# 才算"行业维度完整"。正常东财全市场约 5000 只；设 100 用于区分"完整"与"行业整体缺失"。
MIN_INDUSTRY_COVERED = 100


def _industry_count(data):
    """统计映射中带行业（industry:）标签的股票数。"""
    return sum(1 for tags in data.values()
               if any(isinstance(t, str) and t.startswith("industry:") for t in tags))


def _map_adequate(data):
    """映射是否足以用于盯盘：非空且行业维度覆盖达标。"""
    return bool(data) and _industry_count(data) >= MIN_INDUSTRY_COVERED


# 正在后台构建映射的日期集合（single-flight，防止多轮扫描重复起抓取线程）。
_map_building = set()
_map_build_lock = threading.Lock()


def _build_and_cache_sector_map(today):
    """同步抓取当日板块成分映射并写缓存，返回 dict（盘前预建与后台线程共用）。"""
    import akshare as ak
    c2b = {}
    got = {}
    for kind, fn in [("industry", ak.stock_board_industry_cons_em),
                     ("concept", ak.stock_board_concept_cons_em)]:
        names = _em_board_names(kind)
        got[kind] = bool(names)
        _collect_em_cons(c2b, kind, fn, names)
    # 行业是盘中主维度：行业板块名整体缺失，或成分总量过少，都追加新浪兜底。
    # 概念维度为补充，且新浪概念口径与东财不一致，概念缺失不强制补（行业在即可盯盘）。
    if not got.get("industry") or len(c2b) < 100:
        log(f"[build_map] 东财行业板块缺失或成分不足（行业={got.get('industry')} "
            f"概念={got.get('concept')} 股票={len(c2b)}），追加新浪板块成分兜底")
        try:
            c2b.update(_sina_sector_map())
        except Exception as e:
            log(f"[build_map] 新浪兜底失败：{type(e).__name__}")
    out = {k: sorted(v) for k, v in c2b.items()}
    cache = WORKSPACE / f"sector_map_{today}.json"
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    industry_n = _industry_count(out)
    if industry_n < MIN_INDUSTRY_COVERED:
        log(f"[build_map] ⚠️ 行业映射仍缺失：仅 {industry_n} 只含行业标签，行业板块聚合兜底可能不可用")
    log(f"[build_map] 完成：{len(out)} 只股票映射（行业覆盖 {industry_n}）-> {cache.name}")
    return out


def _latest_stale_map(exclude_today):
    """当日缓存未就绪时，优先返回最近一份"含行业维度"的历史映射兜底；
    若历史映射都缺行业，才降级返回最近一份（至少有概念），全无返回 None。"""
    fallback = None
    fallback_name = None
    for path in reversed(sorted(WORKSPACE.glob("sector_map_*.json"))):
        if path.name == f"sector_map_{exclude_today}.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data:
            continue
        if _map_adequate(data):
            log(f"[build_map] 当日缓存未就绪，先用最近完整历史映射 {path.name} 兜底"
                f"（行业覆盖 {_industry_count(data)}），当日映射在后台构建")
            return data
        if fallback is None:
            fallback, fallback_name = data, path.name
    if fallback is not None:
        log(f"[build_map] 无完整历史映射，降级用最近映射 {fallback_name}（行业维度可能缺失）")
    return fallback


def _spawn_map_build(today):
    """single-flight 后台构建当日映射；已在构建则不重复起线程。"""
    with _map_build_lock:
        if today in _map_building:
            return
        _map_building.add(today)
    log("[build_map] 当日缓存缺失，已在后台单飞构建")

    def _work():
        try:
            _build_and_cache_sector_map(today)
        except Exception as e:
            log(f"[build_map] 后台构建失败：{type(e).__name__}")
        finally:
            with _map_build_lock:
                _map_building.discard(today)

    threading.Thread(target=_work, name=f"build-sector-map-{today}", daemon=True).start()


def build_sector_map(today, background=True):
    """获取板块成分映射（供扫描每轮调用，绝不阻塞开盘）：
    - 当日缓存就绪：直接返回；
    - background=True（盘中默认）：当日缓存缺失时后台 single-flight 构建，本函数立即返回
      最近历史映射兜底（无则空 dict），后续轮次自动读到建好的当日缓存；
    - background=False：同步抓取并写缓存（盘前 --build-map 预建用）。
    """
    if not background:
        return _build_and_cache_sector_map(today)
    cached = _cached_sector_map(today)
    if cached is not None:
        return cached
    _spawn_map_build(today)
    return _latest_stale_map(today) or {}


# ---------------- 交易日历 ----------------
def load_trade_dates():
    """加载 A 股交易日历（新浪接口，本地缓存7天）；失败返回 None（降级仅按周末判断）。"""
    if TRADE_CAL.exists():
        try:
            age = time.time() - TRADE_CAL.stat().st_mtime
            if age < 7 * 86400:
                return set(json.load(open(TRADE_CAL)))
        except Exception:
            pass
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        dates = sorted(set(cal["trade_date"].astype(str).tolist()))
        json.dump(dates, open(TRADE_CAL, "w"))
        return set(dates)
    except Exception as e:
        print(f"[{datetime.now():%F %T}] 交易日历拉取失败，降级仅按周末判断: {e}",
              file=sys.stderr)
        return None


def is_trading_time(now=None):
    """判断给定时刻是否处于 A 股连续竞价时段（含节假日历，失败降级仅按周末）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    cal = load_trade_dates()
    if cal is not None and now.strftime("%Y-%m-%d") not in cal:
        return False
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))
