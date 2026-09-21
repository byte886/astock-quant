#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档健康度体检（astock-quant 项目维护 SOP §5 的自动项）。

在仓库根目录运行：
    .venv/bin/python scripts/doc_health_check.py   # 或 python3 scripts/doc_health_check.py

扫描 git 跟踪 + 工作区未跟踪的 markdown（提交前也能覆盖新文件），
并用 git ls-files 判定 data/ results/ 凭证是否误入版本库。
检查：核心文件齐备 / markdown 相对链接与正文路径式引用断链 / 空目录 /
冷启动链可达 / 入库文档在 DOCUMENTATION_MAP 的登记覆盖 /
data·results·凭证是否误入 git / “待创建·待补充·TODO”占位残留。
退出码：有 ERROR 为 1，仅 WARN 为 0。
"""
import os
import re
import subprocess
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

# 冷启动链 + 治理骨架必须存在（对应 AGENTS.md §5 与维护 SOP）
CORE_FILES = [
    "README.md", "AGENTS.md", "00_项目总纲.md",
    "docs/DOCUMENTATION_MAP.md", "docs/DIRECTORY_STRUCTURE.md", "docs/HANDOFF.md",
    "docs/WORKFLOW.md", "docs/REQUIREMENTS.md", "docs/项目维护SOP.md",
    "03_进行中的任务/TASK_STATUS.md", "03_进行中的任务/ISSUES.md",
]
# MAP 逐篇登记可豁免的目录（原始素材 / 逐篇 ADR / 逐份定稿报告，目录级登记即可）
MAP_EXEMPT_PREFIX = ("09_调研底稿与素材/", "02_决策记录/", "01_结论与产出/")
SKIP_WALK = {".git", ".venv", "venv", "node_modules", "data", "results",
             "__pycache__", ".pytest_cache"}
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#]+)(?:#[^)]*)?\)")
BACKTICK_MD_RE = re.compile(r"`([^`\n]+?\.md)`")
PLACEHOLDER_RE = re.compile(r"(待创建|待补充|TODO|FIXME)")


def _placeholder_exempt(rel):
    """这些文件出现占位词是合理的：只读原始素材、模板/治理卡、描述检查规则本身的维护SOP。"""
    base = os.path.basename(rel)
    if rel.startswith("09_"):
        return True
    if "治理卡" in base or "TEMPLATE" in base:
        return True
    if rel.replace("\\", "/") == "docs/项目维护SOP.md":
        return True
    return False
SECRET_RE = re.compile(r"(^|/)(\.env(\.|$)|.*(secret|token|credential|\.pem|\.key)$)", re.I)


def git_ls(folder=None):
    args = ["git", "ls-files"]
    if folder:
        args.append(folder)
    out = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        return []
    return [l for l in out.stdout.splitlines() if l]


def _norm_ref(ref):
    return urllib.parse.unquote(ref.strip().strip("`").strip())


def path_exists(base_dir, ref):
    """两种基准都试：相对当前 md 所在目录（正确解析 ../）、相对仓库根。"""
    ref = _norm_ref(ref)
    if ref.startswith("~"):
        return os.path.exists(os.path.expanduser(ref))
    if ref.startswith("/") or ref.startswith("$"):
        return True  # 绝对路径/环境变量，不在仓库内判
    p1 = os.path.normpath(os.path.join(base_dir, ref))
    p2 = os.path.normpath(os.path.join(ROOT, ref))
    return os.path.exists(p1) or os.path.exists(p2)


def top_level_exists(ref):
    """路径式引用的顶层目录/文件是否在仓库根存在。

    家目录/环境变量/绝对路径、以及 ../、./ 相对引用交 path_exists 处理（视为可解析）；
    仅当顶层是一个仓库根下却不存在的目录（如 09/ 这类简写）时，才视为外部/示意引用跳过。
    """
    ref = _norm_ref(ref)
    if ref.startswith(("~", "$", "/", "..", ".")):
        return True
    top = ref.split("/")[0]
    return os.path.exists(os.path.join(ROOT, top))


def workspace_md_files():
    """git 跟踪的 md + 工作区未跟踪的 md（提交前也要能扫到新文件），去重。"""
    tracked = set(f for f in git_ls() if f.endswith(".md"))
    found = set(tracked)
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_WALK and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".md"):
                rel = os.path.relpath(os.path.join(dp, fn), ROOT)
                found.add(rel)
    return sorted(found)


def main():
    errors, warns = [], []

    # 1) 核心文件
    for f in CORE_FILES:
        if not os.path.exists(os.path.join(ROOT, f)):
            errors.append(f"[核心文件缺失] {f}")

    md_files = workspace_md_files()

    # 2) 断链（markdown 链接严格；反引号路径式引用仅在顶层目录真实存在时校验）
    for rel in md_files:
        fp = os.path.join(ROOT, rel)
        base_dir = os.path.dirname(fp)
        try:
            with open(fp, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    refs = [(u, "链接", True) for u in LINK_RE.findall(line)]
                    refs += [(u, "路径", False) for u in BACKTICK_MD_RE.findall(line)]
                    for url, kind, strict in refs:
                        url = url.strip()
                        if url.startswith(("http://", "https://", "#", "mailto:")):
                            continue
                        if "<" in url or ">" in url or "*" in url:  # 占位模板 / 通配泛指
                            continue
                        if not strict and _norm_ref(url).startswith(("~", "$")):
                            continue  # 家目录/环境变量引用，不在仓库内判
                        # 裸文件名（无目录分隔）只在仓库根判断，避免泛指误报
                        if "/" not in url:
                            if not os.path.exists(os.path.join(ROOT, url)):
                                continue
                            continue
                        if not strict and not top_level_exists(url):
                            continue  # 简写/外部引用
                        if not path_exists(base_dir, url):
                            errors.append(f"[断链·{kind}] {rel}:{i} -> {url}")
        except OSError as e:
            warns.append(f"[读不了] {rel}: {e}")

    # 3) 空目录（工作区，跳过忽略目录）
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_WALK and not d.startswith(".")]
        if not dns and not fns:
            rel = os.path.relpath(dp, ROOT)
            if rel != ".":
                warns.append(f"[空目录] {rel}")

    # 4) MAP 登记覆盖
    map_path = os.path.join(ROOT, "docs/DOCUMENTATION_MAP.md")
    map_text = ""
    if os.path.exists(map_path):
        map_text = open(map_path, encoding="utf-8").read()
    for rel in md_files:
        if rel.startswith(MAP_EXEMPT_PREFIX) or rel in CORE_FILES and "DOCUMENTATION_MAP" in rel:
            continue
        base = os.path.basename(rel)
        if rel in ("AGENTS.md", "README.md", "00_项目总纲.md"):
            continue
        if base not in map_text and rel not in map_text:
            warns.append(f"[未在文档地图登记] {rel}")

    # 5) data/results/凭证 是否误入 git
    for rel in git_ls():
        low = rel
        if low.startswith(("data/", "results/")):
            errors.append(f"[数据/结果误入git] {rel}")
        if SECRET_RE.search(low.replace("\\", "/")):
            errors.append(f"[疑似凭证明文入库] {rel}")

    # 6) 占位残留（“待创建”等可能意味着文档过时）；模板/原始素材/维护SOP 豁免
    for rel in md_files:
        if _placeholder_exempt(rel):
            continue
        fp = os.path.join(ROOT, rel)
        try:
            for i, line in enumerate(open(fp, encoding="utf-8"), 1):
                if PLACEHOLDER_RE.search(line):
                    warns.append(f"[占位/过时?] {rel}:{i}: {line.strip()[:60]}")
        except OSError:
            pass

    print("=" * 60)
    print("文档健康度体检 · astock-quant")
    print("=" * 60)
    if errors:
        print(f"\n❌ ERROR {len(errors)} 处（提交前必须处理）：")
        for e in errors:
            print("  " + e)
    if warns:
        print(f"\n⚠️  WARN {len(warns)} 处（人工甄别，可能是过时/泛指）：")
        for w in warns:
            print("  " + w)
    if not errors and not warns:
        print("\n✅ 未发现问题。")
    print(f"\n汇总：ERROR={len(errors)}  WARN={len(warns)}  扫描 md={len(md_files)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
