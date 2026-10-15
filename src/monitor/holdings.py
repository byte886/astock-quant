# -*- coding: utf-8 -*-
"""持仓盯盘（需求二 #38）：每个交易日 9:45 / 13:05 各推一次持仓快照。

定位为"独立定时脚本"（launchd 跑完即退），不依赖常驻板块监控进程，任一链路故障
互不影响：
  - 个股快照（现价/涨跌幅/量比/涨速）来自腾讯 spot；
  - 涨停/跌停/大跌判定复用 aggregator 的停板阈值（与板块监控同口径）；
  - 所属板块当前行情用板块成分映射实时聚合（与盘中监控同一口径）；
  - 所属板块"今日是否已异动"读当日真实推送台账（persistence.load_fired_boards），
    不靠实时推断，避免把没报过的板块说成已异动；
  - 推送复用 notifier。

组装与渲染全部拆成小的纯函数（便于单测、控复杂度）；run_once 只负责取数编排。
"""
import os
from datetime import datetime, time as dtime

import pandas as pd

from .aggregator import _is_zt_stock, is_dt_stock, tx_board_agg
from .codes import load_csv_codes, default_holdings_path, default_pool_path
from .config import (HOLD_DROP_PCT, HOLD_MAX_SHOWN, HOLD_BOARD_STRONG, log)
from .datasources import tx_spot, build_sector_map
from .persistence import load_fired_boards
from . import notifier

# 持仓标记的关注优先级：跌停（风险）> 大跌 > 涨停（止盈机会）> 无标记
_FLAG_RANK = {"跌停": 0, "大跌": 1, "涨停": 2}


# ---------------- 基础取值 ----------------
def _spot_index(spot_df):
    """把腾讯 spot 建成 {code: row} 索引；无表/空表返回 {}。"""
    if spot_df is None:
        return {}
    return {r["code"]: r for _, r in spot_df.iterrows()}


def _num(v):
    """腾讯 spot 数值列安全转 float；缺失/NaN 返回 None。"""
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _flag(zdf, code, drop_pct=HOLD_DROP_PCT):
    """持仓个股状态标记：涨停 / 跌停 / 大跌（跌幅≤drop_pct% 但未跌停）/ 无。"""
    if zdf is None:
        return None
    if _is_zt_stock(zdf, code):
        return "涨停"
    if is_dt_stock(zdf, code):
        return "跌停"
    if zdf <= drop_pct:
        return "大跌"
    return None


def _tag_names(tags, prefix):
    """从成分标签取指定前缀（industry:/concept:）的板块名列表。"""
    return [t.partition(":")[2] for t in (tags or []) if t.startswith(prefix)]


def _row_sort_key(row):
    """排序：跌停/大跌/涨停优先，其余按涨幅升序（跌得多的在前，风险优先）。"""
    return (_FLAG_RANK.get(row["flag"], 3),
            row["zdf"] if row["zdf"] is not None else 999.0)


# ---------------- 组装持仓行 ----------------
def _board_view(name, board_by_name, fired):
    """单个所属板块的当前行情 + 今日触发记录视图。"""
    b = board_by_name.get(name) or {}
    return {"name": name, "chg": b.get("chg"), "zt": b.get("zt"),
            "fired": fired.get(name)}


def _stock_boards(tags, board_by_name, fired):
    """该股所属板块：行业全部带当前行情；概念仅在今日已真实推送时带出（避免刷屏）。"""
    views = [_board_view(nm, board_by_name, fired)
             for nm in _tag_names(tags, "industry:")]
    views += [_board_view(nm, board_by_name, fired)
              for nm in _tag_names(tags, "concept:") if nm in fired]
    return views


def _stock_row(code, csv_name, r, in_pool):
    """根据腾讯快照行构造单只持仓行（板块视图随后由 build_holdings 填）。"""
    zdf = _num(r.get("zdf"))
    name = r.get("name") or csv_name or code
    return {
        "code": code, "name": name,
        "price": _num(r.get("zxj")), "zdf": zdf,
        "lb": _num(r.get("lb")), "speed": _num(r.get("speed")),
        "flag": _flag(zdf, code), "in_pool": in_pool, "boards": [],
    }


def build_holdings(holdings, pool, spot_df, mapping, board_agg, fired):
    """组装持仓盯盘行（纯计算），返回 (rows, missing)；取到快照的按风险排序、最多展示上限只。"""
    idx = _spot_index(spot_df)
    board_by_name = {b["name"]: b for b in (board_agg or [])}
    rows, missing = [], []
    for code, csv_name in holdings.items():
        r = idx.get(code)
        if r is None:
            missing.append(csv_name or code)
            continue
        row = _stock_row(code, csv_name, r, code in (pool or {}))
        row["boards"] = _stock_boards((mapping or {}).get(code), board_by_name, fired)
        rows.append(row)
    rows.sort(key=_row_sort_key)
    return rows[:HOLD_MAX_SHOWN], missing


# ---------------- 板块异动汇总 ----------------
def _board_is_notable(board):
    """该板块是否值得提示：今日已真实推送，或当前整体走强。

    "走强"只看板块整体涨幅（≥HOLD_BOARD_STRONG）；板块整体为负、仅个别票涨停
    （如 zt≥3 但 chg<0）多为下跌日的零星脉冲，不叫走强——这与"下跌板块涨速脉冲
    是否该预警"的待确认策略问题保持一致的保守口径（涨停数仍在文案里展示）。
    """
    if board.get("fired"):
        return True
    chg = board.get("chg")
    return chg is not None and chg >= HOLD_BOARD_STRONG


def _merge_boards(rows):
    """把持仓所属板块中"今日已触发或当前走强"的按板块名合并为 {板块名: 汇总}。

    同名板块的 chg/zt/fired 来自同一份板块聚合（_board_view 按名查同一字典），
    多次出现恒等，故只需累积涉及的持仓名 who，无需"回填"字段。
    """
    merged = {}
    for x in rows:
        for b in x["boards"]:
            if not _board_is_notable(b):
                continue
            e = merged.setdefault(b["name"], {"who": [], "chg": b.get("chg"),
                                              "zt": b.get("zt"), "fired": b.get("fired")})
            e["who"].append(x["name"])
    return merged


def _move_tail(e):
    """板块异动文案的现状部分：当前涨幅 + 涨停家数。"""
    tail = f"当前{_fmt(e['chg'], sign=True)}%" if e["chg"] is not None else ""
    if e.get("zt"):
        tail += f"、涨停{e['zt']}家"
    return tail


def _format_move(nm, e):
    """单个异动板块的一行文案。"""
    who = "、".join(dict.fromkeys(e["who"]))
    if e["fired"]:
        head = f"· {nm}（{who}）今日{e['fired']['level']}·{e['fired']['count']}次"
    else:
        head = f"· {nm}（{who}）当前走强"
    tail = _move_tail(e)
    return (head + (" " + tail if tail else "")).rstrip()


def collect_board_moves(rows):
    """汇总持仓所属板块中值得提示的板块，按板块名去重，返回文案行。"""
    return [_format_move(nm, e) for nm, e in _merge_boards(rows).items()]


# ---------------- 渲染 ----------------
def _fmt(v, nd=2, sign=False):
    if v is None:
        return "—"
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"


def _format_stock_line(x):
    """单只持仓的一行：名称 [标记] [⭐] 现价 涨跌幅 量比 涨速。"""
    flag = f" {x['flag']}" if x["flag"] else ""
    star = " ⭐" if x["in_pool"] else ""
    head = f"{x['name']}{flag}{star}"
    chg = f"{_fmt(x['zdf'], sign=True)}%"
    lb_txt = f"量比{_fmt(x['lb'], 1)}"
    speed_txt = f"涨速{_fmt(x['speed'], sign=True)}"
    return f"{head}  {_fmt(x['price'])}  {chg}  {lb_txt}  {speed_txt}"


def session_label(now):
    """当前时段中文标签：上午盘 / 下午盘（按 12:00 划分，仅用于标题）。"""
    return "上午盘" if now.time() < dtime(12, 0) else "下午盘"


def format_holdings(rows, missing):
    """把持仓行渲染成企业微信消息正文（标题由 run_once 经 notifier 单独加粗，此处不重复）；无数据时返回 None。"""
    if not rows and not missing:
        return None
    lines = [f"腾讯快照 · {len(rows)}只持仓", ""]
    lines += [_format_stock_line(x) for x in rows]
    moves = collect_board_moves(rows)
    if moves:
        lines += ["", "🔥 所属板块异动："] + moves
    if missing:
        lines += ["", "⚠️ 未取到快照：" + "、".join(missing)]
    if any(x["in_pool"] for x in rows):
        lines.append("⭐=同时在3号池")
    return "\n".join(lines)


# ---------------- 取数编排 ----------------
def run_once(push=True):
    """跑一次持仓盯盘：取数→组装→（可选）推送。返回 (title, content)，无可推送时 content=None。"""
    now = datetime.now()
    holdings = load_csv_codes(os.environ.get("HOLDINGS_CSV", default_holdings_path()))
    if not holdings:
        log("[holdings] 未配置持仓（config/holdings.csv 为空），跳过持仓盯盘")
        return None, None
    pool = load_csv_codes(default_pool_path())
    spot = tx_spot()
    if spot is None:
        title = "⚠️ 持仓盯盘取数失败"
        content = "腾讯个股快照不可用，本次持仓盯盘未生成；板块监控不受影响。"
        if push:
            notifier.push(title, content)
        return title, content
    mapping = build_sector_map(now.strftime("%Y%m%d"))
    agg = tx_board_agg(mapping, spot)
    fired = load_fired_boards()
    rows, missing = build_holdings(holdings, pool, spot, mapping, agg, fired)
    title = f"📊 持仓盯盘 · {session_label(now)} {now:%H:%M}"
    content = format_holdings(rows, missing)
    if push and content:
        notifier.push(title, content)
    return title, content


def main(argv=None):
    """命令行入口：默认推送；--dry-run 只打印不推送（供手工验证/测试）。"""
    dry = "--dry-run" in (argv or [])
    title, content = run_once(push=not dry)
    if dry and content:
        print(f"**{title}**\n{content}")
    return 0 if content else 1


if __name__ == "__main__":
    raise SystemExit(main())
