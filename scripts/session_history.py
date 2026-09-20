#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地会话轨迹(trajectory.jsonl)查询工具——上下文被压缩后，回查原始记录。

豆包桌面端把每次会话的逐字记录按 session 落盘在本地：
  ~/Library/Application Support/Doubao/<Profile>/.doubao/agent_mode/workspace/.sessions/<sid>/agents/<aid>/system/trajectory.jsonl
每行一个 JSON：role=user/assistant/tool，content 为内容。

用法:
  python3 scripts/session_history.py where              # 打印当前命中的轨迹路径(确认找对会话)
  python3 scripts/session_history.py stats             # 统计(轮数/角色/字数/首末条)
  python3 scripts/session_history.py search 股息率     # 在当前会话的 用户+助手 消息里搜关键词
  python3 scripts/session_history.py search --all 决策 # 跨全部历史会话搜(不只当前)
  python3 scripts/session_history.py export out.md     # 导出可读对话(跳过 tool)

注意：trajectory 含系统提示、工具返回、token 等敏感内容——只本机自用，不粘进 git/文档/对外。
"""
import sys, json, glob, os, time
from pathlib import Path


def all_trajectories():
    home = str(Path.home())
    pat = os.path.join(home, "Library/Application Support/Doubao/*/.doubao/agent_mode/workspace/.sessions/*/agents/*/system/trajectory.jsonl")
    return sorted(glob.glob(pat), key=os.path.getmtime)


def latest():
    c = all_trajectories()
    return c[-1] if c else None


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def fmt_path(p):
    mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    return f"{p}  (更新于 {mt})"


def cmd_where():
    p = latest()
    print(fmt_path(p) if p else "未找到 trajectory")


def cmd_stats(path):
    rows = load(path)
    import collections
    c = collections.Counter(r.get("role", "?") for r in rows)
    total = sum(len(str(r.get("content", ""))) for r in rows)
    users = [r for r in rows if r.get("role") == "user"]
    print("轨迹:", fmt_path(path))
    print(f"共 {len(rows)} 条 | 用户 {c.get('user',0)} | 助手 {c.get('assistant',0)} | 工具 {c.get('tool',0)} | 约 {total/10000:.1f}万字")
    if users:
        print("首条用户:", str(users[0].get("content", ""))[:60])
        print("末条用户:", str(users[-1].get("content", ""))[:60])


def cmd_search(paths, kw):
    hits = 0
    for p in paths:
        rows = load(p)
        for r in rows:
            if r.get("role") not in ("user", "assistant"):
                continue
            txt = str(r.get("content", ""))
            if kw in txt:
                hits += 1
                i = txt.find(kw)
                snip = txt[max(0, i - 70):i + 110].replace("\n", " ")
                tag = "用户" if r.get("role") == "user" else "助手"
                print(f"[{tag}] ...{snip}...")
                if hits >= 40:
                    print("...(更多省略，缩小关键词)")
                    print(f"共命中 {hits}+ 条")
                    return
    print(f"共命中 {hits} 条")


def cmd_export(path, out):
    rows = load(path)
    with open(out, "w") as f:
        for r in rows:
            if r.get("role") not in ("user", "assistant"):
                continue
            c = str(r.get("content", "")).strip()
            if not c:
                continue
            f.write(f"\n### {'用户' if r.get('role')=='user' else '助手'}\n\n{c}\n")
    print(f"已导出: {out}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "stats"
    if cmd == "where":
        cmd_where()
    elif cmd == "stats":
        cmd_stats(latest())
    elif cmd == "search" and len(args) >= 2:
        kw = args[1]
        paths = all_trajectories() if "--all" in args else [latest()]
        cmd_search(paths, kw)
    elif cmd == "export" and len(args) >= 2:
        cmd_export(latest(), args[1])
    else:
        print(__doc__)
