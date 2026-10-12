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


def build_sector_map(today):
    """构建 code -> [kind:板块名,...] 映射并缓存；东财不全时追加新浪兜底。空缓存重建。"""
    cached = _cached_sector_map(today)
    if cached is not None:
        return cached
    import akshare as ak
    c2b = {}
    ok = False
    for kind, fn in [("industry", ak.stock_board_industry_cons_em),
                     ("concept", ak.stock_board_concept_cons_em)]:
        names = _em_board_names(kind)
        if names:
            ok = True
        _collect_em_cons(c2b, kind, fn, names)
    if not ok or len(c2b) < 100:
        log("[build_map] 东财成分不完整，追加新浪板块成分兜底")
        try:
            c2b.update(_sina_sector_map())
        except Exception as e:
            log(f"[build_map] 新浪兜底失败：{type(e).__name__}")
    out = {k: sorted(v) for k, v in c2b.items()}
    cache = WORKSPACE / f"sector_map_{today}.json"
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log(f"[build_map] 完成：{len(out)} 只股票映射 -> {cache.name}")
    return out


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
