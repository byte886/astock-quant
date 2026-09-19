#!/usr/bin/env python3
"""
下载监控脚本：自动管理数据下载流程
1. 监控 ETF 5分钟线下载，挂了自动重启
2. ETF 5分钟线完成后，自动切换到个股下载
3. 个股下载优先补失败的，再继续未完成的
"""
import json
import subprocess
import time
import os
import sys
import atexit
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent
PYTHON_BIN = str(PROJECT_ROOT / ".venv" / "bin" / "python")
STATUS_DIR = PROJECT_ROOT / "data" / "_workspace"
LOG_FILE = PROJECT_ROOT / "data" / "_workspace" / "download_monitor.log"
LOCK_FILE = PROJECT_ROOT / "data" / "_workspace" / "download_monitor.lock"

# ========== 单例锁：确保只有一个监控进程运行 ==========
def acquire_lock():
    lock_path = Path(LOCK_FILE)
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            os.kill(old_pid, 0)  # 信号0=只检查进程是否存在
            print(f"[MONITOR] 已有监控进程运行中 (PID: {old_pid})，本实例退出", flush=True)
            sys.exit(0)
        except (OSError, ValueError):
            lock_path.unlink()  # 旧进程已死，清理锁文件
    lock_path.write_text(str(os.getpid()))
    atexit.register(lambda: lock_path.unlink(missing_ok=True))

acquire_lock()
# =====================================================

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [MONITOR] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def is_process_running(process_name):
    """检查进程是否在运行"""
    try:
        result = subprocess.run(["pgrep", "-f", process_name], capture_output=True, text=True)
        return result.returncode == 0
    except:
        return False

def get_etf_minute_progress():
    """获取ETF 5分钟线进度"""
    f = STATUS_DIR / "download_status_etf_minute.json"
    if not f.exists():
        return None
    d = json.load(open(f))
    completed = len(d.get("completed", []))
    failed = len(d.get("failed", []))
    return {
        "completed": completed,
        "failed": failed,
        "total": completed + failed,
        "pct": completed / 1588 * 100
    }

def get_stock_progress():
    """获取个股下载进度"""
    results = {}
    for freq, name in [("daily", "日线"), ("minute", "5分钟线")]:
        f = STATUS_DIR / f"download_status_{freq}.json"
        if not f.exists():
            results[freq] = None
            continue
        d = json.load(open(f))
        completed = len(d.get("completed", []))
        failed = len(d.get("failed", []))
        results[freq] = {
            "completed": completed,
            "failed": failed,
            "total": completed + failed,
            "pct": completed / 4889 * 100
        }
    return results

def start_etf_minute_download():
    """启动ETF 5分钟线下载"""
    log("启动ETF 5分钟线下载...")
    cmd = [PYTHON_BIN, "scripts/download_etf.py", "--freq", "minute"]
    subprocess.Popen(cmd, cwd=PROJECT_ROOT,
                     stdout=open(PROJECT_ROOT / "data/_workspace/etf_minute_stdout.log", "a"),
                     stderr=subprocess.STDOUT)

def start_stock_download():
    """启动个股下载（both模式：日线+5分钟线）"""
    log("启动个股下载（both模式）...")
    cmd = [PYTHON_BIN, "scripts/download_data.py", "--freq", "both"]
    subprocess.Popen(cmd, cwd=PROJECT_ROOT,
                     stdout=open(PROJECT_ROOT / "data/_workspace/stock_stdout.log", "a"),
                     stderr=subprocess.STDOUT)

def main():
    log("=" * 60)
    log("下载监控脚本启动")
    log("=" * 60)

    # 阶段：0=ETF分钟线, 1=个股下载
    phase = 0
    check_interval = 1800  # 30分钟检查一次

    while True:
        try:
            if phase == 0:
                # 阶段0：ETF 5分钟线下载
                progress = get_etf_minute_progress()
                if progress:
                    log(f"ETF 5分钟线进度：{progress['completed']}/1588 ({progress['pct']:.1f}%), 失败{progress['failed']}")

                    # 检查是否完成
                    if progress["completed"] >= 1588:
                        log("✅ ETF 5分钟线下载完成！切换到个股下载阶段")
                        phase = 1
                        time.sleep(5)  # 等待进程退出
                        start_stock_download()
                        continue

                    # 检查进程是否在运行
                    if not is_process_running("download_etf.py"):
                        log("⚠️ ETF 5分钟线下载进程未运行，自动重启")
                        start_etf_minute_download()
                else:
                    log("⚠️ ETF 5分钟线状态文件不存在，启动下载")
                    start_etf_minute_download()

            elif phase == 1:
                # 阶段1：个股下载
                progress = get_stock_progress()
                if progress.get("daily") and progress.get("minute"):
                    d = progress["daily"]
                    m = progress["minute"]
                    log(f"个股日线进度：{d['completed']}/4889 ({d['pct']:.1f}%), 失败{d['failed']}")
                    log(f"个股5分钟线进度：{m['completed']}/4889 ({m['pct']:.1f}%), 失败{m['failed']}")

                    # 检查是否完成（完成+失败都算处理完）
                    if d["total"] >= 4889 and m["total"] >= 4889:
                        log("✅ 个股下载全部处理完成！")
                        if d["failed"] > 0:
                            log(f"⚠️ 仍有{d['failed']}只日线、{m['failed']}只5分钟线失败，下次可重试")
                        break

                    # 检查进程是否在运行
                    if not is_process_running("download_data.py"):
                        log("⚠️ 个股下载进程未运行，自动重启（断点续传）")
                        start_stock_download()
                else:
                    log("⚠️ 个股状态文件不存在，启动下载")
                    start_stock_download()

        except Exception as e:
            log(f"❌ 监控脚本出错：{e}")

        time.sleep(check_interval)

    log("=" * 60)
    log("下载监控脚本结束")
    log("=" * 60)

if __name__ == "__main__":
    main()
