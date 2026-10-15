# -*- coding: utf-8 -*-
"""入口层：命令行参数、主循环调度、停机补检、看门狗心跳告警。

顺序严格保持：每轮 扫描 -> sleep -> 看门狗。支持 --once（单次）与 --build-map（只建映射）。
"""
import sys
import traceback
from datetime import datetime, time as dtime

from .config import (POLL_NORMAL, POLL_LATE, POLL_CLOSED, HB_STALL_SEC,
                     CHG_L1, ZT_L1, ZT_L2, log)
from .monitor import Monitor
from .notifier import push
from .datasources import is_trading_time, build_sector_map, current_session_start
from .persistence import (single_instance, load_last_scan, save_last_scan)


class _RunState:
    """主循环可变状态（替代 main 内 nonlocal 闭包变量）。"""
    __slots__ = ("last_success", "last_hb", "down_alerted", "selfcheck_done")

    def __init__(self, last_success):
        self.last_success = last_success
        self.last_hb = None          # 上次宕机告警时间（冷却用）
        self.down_alerted = False    # 是否处于"已告警未恢复"
        self.selfcheck_done = False  # 交易时段首轮启动自检是否已做


def _mark_down(st, title, msg, now):
    push(title, msg)
    st.last_hb = now
    st.down_alerted = True


def _watchdog(st, now):
    """交易时段连续 HB_STALL_SEC 秒没有一次成功扫描 -> 心跳告警；同次宕机按阈值冷却。

    静默基准取「上次成功扫描」与「当前交易时段开盘」中较晚者，
    从而把午休（11:30–13:00）、隔夜、节假日的休市停扫排除在静默之外，
    避免开盘瞬间把上一时段的旧时间戳误判成宕机（I22）。
    """
    sess_start = current_session_start(now)
    if sess_start is None:
        return
    baseline = max(st.last_success, sess_start)
    stall = (now - baseline).total_seconds()
    if stall >= HB_STALL_SEC and (st.last_hb is None
                                  or (now - st.last_hb).total_seconds() >= HB_STALL_SEC):
        _mark_down(st, "⚠️ 盘中监控心跳告警",
                   f"已连续约 {int(stall // 60)} 分钟没有完成一次成功扫描，"
                   f"期间板块异动可能漏推，请立即检查。\n"
                   f"日志：data/_workspace/intraday_monitor.log", now)


def _parse_last_success(prev_state):
    """从持久化的上次扫描时间恢复 last_success；解析失败用当前时间。"""
    if prev_state:
        try:
            return datetime.strptime(prev_state, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now()


def _gap_check(m, st, prev_state):
    """距上次正常扫描超过阈值且当前在交易时段，立即补检一次，避免停机窗口漏推。"""
    gap_min = 0.0
    if prev_state:
        try:
            gap_min = (datetime.now()
                       - datetime.strptime(prev_state, "%Y-%m-%d %H:%M:%S")
                       ).total_seconds() / 60.0
        except Exception:
            gap_min = 0.0
    if gap_min < 5 or not is_trading_time():
        return
    log(f"[停机补检] 距上次扫描 {gap_min:.0f} 分钟（上次 {prev_state}），中间未实时监测，"
        f"立即补检当前市场状态")
    try:
        src = m.scan_once()
        if src:
            save_last_scan()
            st.last_success = datetime.now()
            st.selfcheck_done = True
        else:
            _mark_down(st, "⚠️ 盘中监控启动自检失败",
                       "停机补检时所有板块数据源均不可用，监控可能无法产出信号，"
                       "请立即检查网络/数据源。", datetime.now())
            st.selfcheck_done = True
    except Exception:
        log("停机补检异常：\n" + traceback.format_exc())
        _mark_down(st, "⚠️ 盘中监控启动自检异常",
                   "停机补检首轮扫描即抛异常，期间可能漏推，请立即检查日志 "
                   "data/_workspace/intraday_monitor.log", datetime.now())
        st.selfcheck_done = True


def _poll_once(m, st):
    """主循环单轮：交易时段扫描并处理成功/失败/异常，返回本轮应 sleep 的秒数。"""
    now = datetime.now()
    try:
        if not is_trading_time(now):
            return POLL_CLOSED
        src = m.scan_once()
        if src:
            save_last_scan()
            st.last_success = now
            if st.down_alerted:
                push("✅ 盘中监控已恢复", f"扫描恢复正常（本轮数据源：{src}）。")
                st.down_alerted = False
        elif not st.selfcheck_done:
            _mark_down(st, "⚠️ 盘中监控启动自检失败",
                       "首轮扫描所有板块数据源均不可用，监控可能无法产出信号，"
                       "请立即检查网络/数据源。", now)
        st.selfcheck_done = True
        late = now.time() >= dtime(14, 30)
        return POLL_LATE if late else POLL_NORMAL
    except Exception:
        log("主循环异常：\n" + traceback.format_exc())
        if not st.selfcheck_done:
            _mark_down(st, "⚠️ 盘中监控启动自检异常",
                       "首轮扫描即抛异常，期间可能漏推，请立即检查日志 "
                       "data/_workspace/intraday_monitor.log", now)
        st.selfcheck_done = True
        return POLL_NORMAL


def main(argv=None):
    argv = sys.argv if argv is None else argv
    once = "--once" in argv
    build_only = "--build-map" in argv
    if build_only:
        build_sector_map(datetime.now().strftime("%Y%m%d"), background=False)
        return
    single_instance()
    m = Monitor()
    log(f"启动 板块异动监测  CHG>{CHG_L1}% L1涨停>={ZT_L1} L2涨停>={ZT_L2} "
        f"常规{POLL_NORMAL}s/尾盘{POLL_LATE}s once={once}")
    if once:
        m.scan_once()
        save_last_scan()
        log("单次扫描结束。")
        return
    _prev_state = load_last_scan()
    st = _RunState(_parse_last_success(_prev_state))
    _gap_check(m, st, _prev_state)

    while True:
        secs = _poll_once(m, st)
        import time
        time.sleep(secs)
        _watchdog(st, datetime.now())


if __name__ == "__main__":
    main()
