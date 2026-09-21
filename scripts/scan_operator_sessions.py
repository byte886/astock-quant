#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_operator_sessions.py · 操盘手任务窗口增量扫描（外脑线 · A 方案）

目的
----
操盘手（nero6688/大海星）在豆包里除"当前会话"外，还散落在很多**工作任务窗口**里
聊行情、做复盘、谈操作。这些会话是 agent 工作任务，会逐字落盘到本机：
  ~/Library/Application Support/Doubao/<Profile>/.doubao/agent_mode/workspace/
      .sessions/<sid>/agents/m_*/system/trajectory.jsonl
本脚本扫描这些 trajectory，**增量**找出与操盘/A股相关的新内容，导出到本地暂存区
（inbox），供 AI 审阅后按规范归档到 09_调研底稿与素材/操盘手经验/，并提炼结构化判断。

设计要点
--------
1. 增量：state 记录每个 sid 已处理到的 jsonl 行号，下次只看新增行，绝不从头重扫。
2. 去重：游标保证同一行不重复导出；inbox 一个会话一个文件、按批次追加并带行号；
   归档结果回写 state.archived，避免重复归档。
3. 只扫主对话（m_ 开头），跳过子 agent（s_/o_）；只取 user/assistant 文本，不碰 tool。
4. 默认排除"当前会话"（正在运行本任务的窗口，其内容已在处理）。
5. 隐私：state 游标与 inbox 原文都写到 data/operator_recall/（已 .gitignore，本机本地，
   绝不入 git）；state 只含 sid/标题/行号无正文，inbox 含原文仅供本机审阅。归档到 09 的
   只应是清洗后的复盘/观点正文，不含系统提示、token、密钥。跨机器去重以 09 里的归档 md 为准。
6. 机器边界：trajectory 只在"产生该会话的那台机器"本地落盘、不跨机同步。本脚本要在
   操盘手实际使用的机器上跑，才能读到他的任务窗口；在开发机上只能扫到开发机自己的会话。

用法（系统 python3，无第三方依赖）
----------------------------------
  python3 scripts/scan_operator_sessions.py                 # 增量扫描（默认排除当前会话）
  python3 scripts/scan_operator_sessions.py --dry-run       # 只报告，不写 inbox / 不推进游标
  python3 scripts/scan_operator_sessions.py --baseline      # 首部署：只建游标不导出（历史视为已读）
  python3 scripts/scan_operator_sessions.py --rescan <sid片段>   # 强制重扫某会话并导出
  python3 scripts/scan_operator_sessions.py --include-current   # 连最新（当前）会话也扫
  # 游标 state 与 inbox 默认都在 data/operator_recall/（已 gitignore，本机本地，不入仓）

后续：见 docs/外脑-操盘手会话增量采集SOP.md
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = REPO_ROOT / "data" / "operator_recall" / "state.json"
DEFAULT_INBOX = REPO_ROOT / "data" / "operator_recall" / "inbox"

# ---- 关键词：命中确定强词 1 个即候选；弱词需 >=3 个；歧义词不单独触发 -------------
STRONG_KW = [
    "复盘", "A股", "大盘", "上证", "深成", "创业板", "科创50", "北证", "沪深300",
    "持仓", "股票池", "买入", "卖出", "加仓", "减仓", "清仓", "仓位", "满仓", "空仓",
    "涨停", "跌停", "量能", "成交量", "操盘", "券商", "个股", "均线", "回调", "反弹",
    "止盈", "止损", "板块轮动", "收盘", "开盘", "北向", "主力资金", "指数",
    "K线", "缠论", "基本面", "技术面", "股息率", "ROE", "多因子", "调仓", "困境反转",
    "支撑位", "压力位", "牛市", "熊市", "洗盘", "散户", "龙头",
]
# 歧义词：git 圈会把"建仓库"简说成"建仓"。命中只展示、不单独触发；必须另有确定强词
# 或弱词达标才算候选（操盘手聊建仓必伴随买入/仓位/个股等）。(?!库) 先排除"建仓库"。
AMBIGUOUS_REGEX = [r"建仓(?!库)"]
WEAK_KW = ["股票", "行情", "板块", "基金", "ETF", "量化", "证券", "股市", "选股", "估值"]

# ---- 噪音会话：首条用户消息命中即跳过（开发/采集/其它项目，不是操盘手观点）--------
NOISE_KW = [
    "微信", "视频号", "res-downloader", "resdownloader", "公众号采集", "公众号后台",
    "高顿", "CPA", "爬虫", "mitm", "clash", "代理", "珠宝", "课程", "多平台", "采集",
    "pipeline", "抖音", "哔哩", "B站", "下载器", "网盘", "百度云", "OCR", "转写",
    "github", "仓库", "SOP", "技能", "trajectory", "飞书", "chrome", "浏览器自动化",
]

HIT_STRONG_MIN = 1
HIT_WEAK_MIN = 3
TITLE_LEN = 40


def trajectories():
    """所有 Profile 下的主对话(m_) trajectory，按修改时间排序。"""
    home = str(Path.home())
    pat = os.path.join(
        home,
        "Library/Application Support/Doubao/*/.doubao/agent_mode/workspace/"
        ".sessions/*/agents/m_*/system/trajectory.jsonl",
    )
    return sorted(glob.glob(pat), key=os.path.getmtime)


def sid_of(path):
    m = re.search(r"\.sessions/([^/]+)/agents/", path)
    return m.group(1) if m else "?"


def load_rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append((ln, json.loads(line)))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def text_of(row):
    c = row.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):  # 容错：多模态 content
        parts = []
        for blk in c:
            if isinstance(blk, dict):
                parts.append(str(blk.get("text", "")))
            else:
                parts.append(str(blk))
        return " ".join(p for p in parts if p)
    return ""


def first_user_title(rows):
    for _, r in rows:
        if r.get("role") == "user":
            t = text_of(r).replace("\n", " ").strip()
            return t[:TITLE_LEN] or "(无用户消息)"
    return "(无用户消息)"


def is_noise(title):
    return any(k.lower() in title.lower() for k in NOISE_KW)


def hit_keywords(text):
    core = {k for k in STRONG_KW if k in text}
    ambiguous = []
    for rx in AMBIGUOUS_REGEX:
        ambiguous += re.findall(rx, text)
    ambiguous = set(ambiguous)
    weak = {k for k in WEAK_KW if k in text}
    # 触发只看确定强词或弱词数量；歧义词（建仓）不单独触发，仅在展示时附上
    ok = len(core) >= HIT_STRONG_MIN or len(weak) >= HIT_WEAK_MIN
    shown = sorted(core | ambiguous)
    return ok, shown, sorted(weak)


def safe_name(s, n=24):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s).strip("_")
    return s[:n] or "untitled"


def load_state(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "updated": None, "sessions": {}}


def save_state(path, state):
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_inbox(inbox_dir, sid, title, path, batches):
    """batches: [(scan_time, start_ln, end_ln, [(role, text)...])]"""
    os.makedirs(inbox_dir, exist_ok=True)
    fname = f"{sid}_{safe_name(title)}.md"
    fp = inbox_dir / fname
    is_new = not fp.exists()
    with open(fp, "a", encoding="utf-8") as f:
        if is_new:
            f.write(
                f"# 操盘手会话候选 · {title}\n\n"
                f"> sid: `{sid}`\n"
                f"> 本机 trajectory: `{path}`\n"
                f"> 用途：本地暂存、待 AI 审阅后归档到 "
                f"`09_调研底稿与素材/操盘手经验/`。**data/ 已 gitignore，勿入 git；"
                f"归档正文前去除系统提示/token/密钥。**\n\n---\n"
            )
        for scan_t, s_ln, e_ln, msgs in batches:
            f.write(f"\n## 批次 {scan_t}（行 {s_ln}–{e_ln}）\n\n")
            for role, txt in msgs:
                tag = "操盘手" if role == "user" else "豆包"
                f.write(f"### {tag}\n\n{txt.strip()}\n\n")
            f.write("---\n")
    return fp


def main():
    ap = argparse.ArgumentParser(description="操盘手任务窗口增量扫描")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--inbox", default=str(DEFAULT_INBOX))
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写 inbox、不推进游标")
    ap.add_argument("--baseline", action="store_true",
                    help="只建游标不导出（把现有会话历史全部标记为已读）")
    ap.add_argument("--rescan", default=None, help="sid 片段：强制从第 0 行重扫该会话并导出")
    ap.add_argument("--include-current", action="store_true",
                    help="默认排除最新(当前)会话；加此参数则不排除")
    ap.add_argument("--this-sid", default=None, help="显式指定要排除的当前会话 sid 片段")
    args = ap.parse_args()

    trajs = trajectories()
    if not trajs:
        print("未发现任何主会话 trajectory（确认豆包桌面端在本机产生过工作任务）。")
        return

    # 确定要排除的"当前会话"
    current_sid = None
    if not args.include_current:
        if args.this_sid:
            current_sid = next((sid_of(p) for p in trajs if args.this_sid in p), None)
        else:
            current_sid = sid_of(trajs[-1])  # 最新 mtime 的主会话≈运行本任务的窗口

    state = load_state(args.state)
    sessions = state.setdefault("sessions", {})

    n_scan = n_newhit = n_skip_noise = n_skip_current = 0
    report = []

    for path in trajs:
        sid = sid_of(path)
        if sid == current_sid:
            n_skip_current += 1
            continue
        rows = load_rows(path)
        if not rows:
            continue
        title = first_user_title(rows)
        total = len(rows)
        prev = sessions.get(sid, {})

        # 决定本次从第几行(ln, 1-based)之后开始读
        rescan_this = bool(args.rescan and args.rescan in sid)
        if rescan_this:
            start_ln = 0                       # 强制全量重扫
        elif args.baseline and sid not in sessions:
            start_ln = total                  # 首次 baseline：历史直接视为已读
        else:
            start_ln = prev.get("processed_lines", 0)
        new_rows = [(ln, r) for (ln, r) in rows if ln > start_ln]

        n_scan += 1
        if is_noise(title) and not rescan_this:
            n_skip_noise += 1
            # 噪音会话也推进游标，避免反复判断
            if not args.dry_run:
                sessions[sid] = {**prev, "path": path, "title": title,
                                 "processed_lines": total,
                                 "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "noise": True}
            continue

        # 关键词命中（在新增文本上）
        joined = "\n".join(text_of(r) for _, r in new_rows
                           if r.get("role") in ("user", "assistant"))
        ok, strong, weak = hit_keywords(joined) if joined else (False, [], [])

        first_seen = prev.get("first_seen") or time.strftime("%Y-%m-%d %H:%M:%S")
        if ok and new_rows and not args.baseline:
            n_newhit += 1
            msgs = [(r.get("role"), text_of(r)) for _, r in new_rows
                    if r.get("role") in ("user", "assistant") and text_of(r).strip()]
            s_ln, e_ln = new_rows[0][0], new_rows[-1][0]
            scan_t = time.strftime("%Y-%m-%d %H:%M:%S")
            fp = None
            if not args.dry_run and msgs:
                fp = append_inbox(Path(args.inbox), sid, title, path,
                                  [(scan_t, s_ln, e_ln, msgs)])
            report.append({
                "sid": sid, "title": title, "lines": f"{s_ln}-{e_ln}",
                "strong": strong[:6], "weak": weak[:6],
                "inbox": str(fp) if fp else "(dry-run)",
            })

        if not args.dry_run:
            sessions[sid] = {
                "path": path,
                "title": title,
                "processed_lines": total,
                "first_seen": first_seen,
                "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                "hits": prev.get("hits", 0) + (1 if ok and new_rows and not args.baseline else 0),
                "inbox": (prev.get("inbox") or
                          (str(Path(args.inbox) / f"{sid}_{safe_name(title)}.md")
                           if ok and new_rows and not args.baseline else None)),
                "archived": prev.get("archived", []),
                "noise": is_noise(title),
            }

    if not args.dry_run:
        save_state(args.state, state)

    # ---- 报告 ----
    mode = "BASELINE(只建游标)" if args.baseline else ("DRY-RUN" if args.dry_run else "增量")
    print(f"=== 操盘手会话扫描 · {mode} ===")
    print(f"主会话总数 {len(trajs)} | 扫描 {n_scan} | 排除当前 {n_skip_current} | "
          f"噪音跳过 {n_skip_noise} | 本次新命中 {n_newhit}")
    if args.baseline:
        print("已把现有会话历史标记为已读；今后只报告新增内容。")
    for it in report:
        print(f"\n● […{it['sid'][-6:]}] 行{it['lines']} {it['title']}")
        print(f"  强词 {it['strong']} 弱词 {it['weak']}")
        print(f"  → {it['inbox']}")
    if not report and not args.baseline:
        print("本次无新增操盘相关内容。")
    print(f"\n游标文件: {args.state}")
    if not args.baseline:
        print(f"待审阅暂存: {args.inbox}（AI 审阅后归档到 09_调研底稿与素材/操盘手经验/）")


if __name__ == "__main__":
    main()
