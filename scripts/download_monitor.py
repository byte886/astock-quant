#!/usr/bin/env python3
"""
下载监控脚本（常驻，由 launchd 任务 com.astock-quant.download-monitor 管理）。

架构（关键）：
- 下载进程 download_data.py 是【独立的 launchd 任务】com.astock-quant.download-data，
  由 launchd 直接管理（RunAtLoad 开机自启 + KeepAlive 崩溃自动重启）。
  不要再用 subprocess.Popen 在监控脚本里派生孙子进程——launchd 作业里 Popen
  出来的子进程会异常退出且无输出，这是踩过的坑。
- 本监控只做两件事：
  1) 以【磁盘实际有效 csv 文件】为准统计进度（只看文件大小，秒级），不信 failed 计数；
  2) 若仍有缺失、而下载任务没在跑（跑完一遍 exit0 但还有网络失败股 / 被停 / 开机后没起），
     用 `launchctl kickstart` 把独立下载任务重新拉起。
- 全部覆盖后正常退出 exit0（launchd 配置 SuccessfulExit=false，不再被重启）。
- 单例锁防止监控重复运行。
"""
import csv
import json
import subprocess
import time
import os
import sys
import atexit
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = str(PROJECT_ROOT / ".venv" / "bin" / "python")
STATUS_DIR = PROJECT_ROOT / "data" / "_workspace"
LOG_FILE = STATUS_DIR / "download_monitor.log"
LOCK_FILE = STATUS_DIR / "download_monitor.lock"
STOCK_LIST_FILE = STATUS_DIR / "stock_list.csv"
DATA_RAW = PROJECT_ROOT / "data" / "raw"

DATA_LABEL = "com.astock-quant.download-data"
DATA_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{DATA_LABEL}.plist"
ETF_TARGET = 1588
CHECK_INTERVAL = 900          # 每15分钟巡检一次
STALL_LIMIT = 20             # 连续20轮(约5小时)无增长则告警
MIN_VALID_BYTES = 2048       # 有效csv最小字节（表头仅约200字节）

# ========== 单例锁 ==========
def acquire_lock():
    lock_path = Path(LOCK_FILE)
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            os.kill(old_pid, 0)
            print(f"[MONITOR] 已有监控进程运行中 (PID: {old_pid})，本实例退出", flush=True)
            sys.exit(0)
        except (OSError, ValueError):
            lock_path.unlink()
    lock_path.write_text(str(os.getpid()))
    atexit.register(lambda: lock_path.unlink(missing_ok=True))

# 注：加锁放在 __main__ 主入口，避免 import 本模块复用 kickstart 等函数时误触发单例退出。
# =============================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [MONITOR] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def _uid():
    return os.getuid()

def is_download_running():
    """下载进程是否在跑（pgrep，排除监控自身）"""
    try:
        r = subprocess.run(["pgrep", "-f", "download_data.py"], capture_output=True, text=True)
        if r.returncode != 0:
            return False
        me, parent = os.getpid(), os.getppid()
        for tok in r.stdout.split():
            try:
                pid = int(tok)
            except ValueError:
                continue
            if pid not in (me, parent):
                return True
        return False
    except Exception:
        return False

def ensure_data_job_loaded():
    """确保下载 launchd 任务已加载（kickstart 找不到服务时兜底 load）"""
    svc = f"gui/{_uid()}/{DATA_LABEL}"
    r = subprocess.run(["launchctl", "print", svc], capture_output=True, text=True)
    if r.returncode != 0 and DATA_PLIST.exists():
        log("下载任务未加载，执行 launchctl load 装载")
        subprocess.run(["launchctl", "load", str(DATA_PLIST)], capture_output=True, text=True)

def kickstart_download():
    """通过 launchd 启动（或重启）独立下载任务，天然单例"""
    ensure_data_job_loaded()
    svc = f"gui/{_uid()}/{DATA_LABEL}"
    r = subprocess.run(["launchctl", "kickstart", svc], capture_output=True, text=True)
    err = (r.stderr or r.stdout or "").strip()
    if r.returncode == 0:
        log(f"✅ 已通过 launchctl kickstart 拉起下载任务：{svc}")
        return True
    # already running 之类不算失败
    if "already" in err.lower() or "running" in err.lower():
        log("下载任务本就在运行")
        return True
    log(f"⚠️ kickstart 失败(rc={r.returncode})：{err}")
    return False

# ---------- 进度（以磁盘文件为准）----------
def load_target_codes():
    if not STOCK_LIST_FILE.exists():
        return None
    codes = []
    with open(STOCK_LIST_FILE, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            c = (row.get("code") or "").strip()
            if c:
                codes.append(c)
    return sorted(set(codes))

def disk_has_data(code, freq):
    subdir = "minute" if freq == "minute" else "daily"
    exchange = code.split(".")[0]
    p = DATA_RAW / subdir / exchange / f"{code}.csv"
    try:
        return p.exists() and p.stat().st_size > MIN_VALID_BYTES
    except OSError:
        return False

def load_skipped(freq, targets):
    f = STATUS_DIR / f"download_status_{freq}.json"
    if not f.exists():
        return set()
    try:
        d = json.load(open(f))
        return set(d.get("skipped", [])) & set(targets)
    except Exception:
        return set()

def get_progress(targets):
    out = {}
    for freq in ("daily", "minute"):
        done = {c for c in targets if disk_has_data(c, freq)}
        skipped = load_skipped(freq, targets)
        covered = done | skipped
        out[freq] = {"done": len(done), "skipped": len(skipped), "total": len(targets),
                     "remaining": len(targets) - len(covered),
                     "pct": len(done) / len(targets) * 100}
    return out

def etf_done():
    f = STATUS_DIR / "download_status_etf_minute.json"
    if not f.exists():
        return False
    try:
        return len(json.load(open(f)).get("completed", [])) >= ETF_TARGET
    except Exception:
        return False

def main():
    log("=" * 60)
    log("下载监控脚本启动（launchd 双任务版 v3）")
    log("=" * 60)
    if not etf_done():
        log(f"⚠️ ETF 5分钟线尚未完成（目标{ETF_TARGET}），请先运行 download_etf.py；本监控暂只守护个股")
    else:
        log(f"✅ ETF 5分钟线已完成 {ETF_TARGET}/{ETF_TARGET}")

    stall_rounds = 0
    last_done = -1

    while True:
        try:
            targets = load_target_codes()
            if not targets:
                log("⚠️ 股票列表不存在，尝试拉起下载任务以生成列表")
                kickstart_download()
            else:
                prog = get_progress(targets)
                d, m = prog["daily"], prog["minute"]
                log(f"个股日线：{d['done']}/{d['total']} 文件 ({d['pct']:.1f}%)，"
                    f"跳过{d['skipped']}，待补{d['remaining']}")
                log(f"个股5分钟线：{m['done']}/{m['total']} 文件 ({m['pct']:.1f}%)，"
                    f"跳过{m['skipped']}，待补{m['remaining']}")

                if d["remaining"] == 0 and m["remaining"] == 0:
                    log("🎉 个股日线与5分钟线已全部覆盖，下载任务彻底完成，监控退出(exit 0)")
                    break

                running = is_download_running()
                cur_done = d["done"] + m["done"]
                if cur_done == last_done:
                    stall_rounds += 1
                else:
                    stall_rounds = 0
                    last_done = cur_done
                if stall_rounds >= STALL_LIMIT:
                    log(f"🚨 连续{STALL_LIMIT}轮（约{STALL_LIMIT * CHECK_INTERVAL // 3600}小时）"
                        f"无增长，可能被限流或网络长期不可用；强制 kickstart 重启下载任务一次")
                    kickstart_download()
                    stall_rounds = 0
                elif not running:
                    log("⚠️ 下载任务未运行且尚未完成，launchctl kickstart 重新拉起")
                    kickstart_download()
                else:
                    log("下载任务运行中，本轮无需干预")
        except Exception as e:
            log(f"❌ 监控脚本出错：{e}")

        time.sleep(CHECK_INTERVAL)

    log("=" * 60)
    log("下载监控脚本结束（全部完成，exit 0，launchd 不再重启）")
    log("=" * 60)
    sys.exit(0)

if __name__ == "__main__":
    acquire_lock()
    main()
