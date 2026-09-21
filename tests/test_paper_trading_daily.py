#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟盘日常推进的端到端自检（不依赖 pytest，直接 python tests/test_paper_trading_daily.py）
=========================================================================================
用【临时账户副本】+ 在真实面板尾部追加两个假想交易日，验证：
  1. 信号日(9-21)当天绝不成交（未来函数防线）；
  2. 下一交易日(9-22)以开盘价成交 15 只、收盘盯市、记一条净值；
  3. 同日重复运行幂等（不重复成交、不重复记账）；
  4. 月末(9-30)盘后无待执行信号时，产出下月新信号。
真实 data/paper/account.json 不被修改。
"""

import sys
import json
import shutil
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.quant import data as D  # noqa: E402

# 保存原始加载器：给 R.D.load_price_panel 打补丁会污染共享的 src.quant.data
# 模块（R.D 与本测试的 D 是同一模块对象），构造夹具必须始终走原始函数。
_ORIG_LOAD = D.load_price_panel

TMP = ROOT / "data/_workspace/paper_selftest"
REAL_ACCT = ROOT / "data/paper/account.json"

# 以文件路径加载 scripts/run_paper_trading.py（scripts 非包）
spec = importlib.util.spec_from_file_location(
    "run_paper_trading", ROOT / "scripts/run_paper_trading.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def fake_panel_after(real_codes, extra_dates):
    """真实面板 + 尾部追加假想交易日（价格沿 9-21 收盘每日 +1%）。"""
    panels = _ORIG_LOAD(real_codes)
    last = panels["close"].index[-1]
    base_close = panels["close"].loc[last]
    prev = base_close
    new_rows = {}
    for i, d in enumerate(extra_dates, start=1):
        c = prev * (1.0 + 0.01 * i / len(extra_dates))
        new_rows[d] = {
            "close": c, "open": prev, "preclose": prev,
            "tradestatus": pd.Series(1, index=c.index),
            "isST": pd.Series(0, index=c.index),
            "volume": pd.Series(1e6, index=c.index),
        }
        prev = c
    for f in ["close", "open", "preclose", "tradestatus", "isST", "volume"]:
        add = pd.DataFrame({d: new_rows[d][f] for d in extra_dates}).T
        add.index = pd.DatetimeIndex(extra_dates)
        panels[f] = pd.concat([panels[f], add]).sort_index()
    return panels


def main():
    assert REAL_ACCT.exists(), "先跑首跑 scripts/run_paper_trading.py 再做本自检"
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    acct_path = TMP / "account.json"
    shutil.copy(REAL_ACCT, acct_path)
    R.ACCT = acct_path
    R.OUT = TMP

    real_codes = D.load_constituents()
    real_panels = _ORIG_LOAD(real_codes)

    # ---- 场景 1：数据仍停在信号日 9-21，重跑不得成交 ----
    R.D.load_price_panel = lambda codes=None: real_panels
    R.main()
    a = json.loads(acct_path.read_text())
    assert len(a["nav_history"]) == 1, f"信号日重跑不应记账，得到 {len(a['nav_history'])} 条"
    assert a["holdings"] == {}, "信号日当天不得成交"
    assert len(a["pending_target"]) == 15, "信号日当天 pending 不得丢失"
    print("✅ 场景1：信号日当天不成交、不记账，pending 保留 15 只")

    # ---- 场景 2：出现下一交易日 9-22，应开盘成交；9-23 继续盯市 ----
    # 注：夹具面板最后一天会被月末逻辑当作月末（真实日历不会），
    # 故本场景只验证成交与盯市；月末信号由场景 4 专门验证。
    p2 = fake_panel_after(real_codes,
                          [pd.Timestamp("2026-09-22"), pd.Timestamp("2026-09-23")])
    R.D.load_price_panel = lambda codes=None: p2
    R.main()
    a = json.loads(acct_path.read_text())
    assert a["last_exec"] == "2026-09-22", f"应在 9-22 成交，实际 {a['last_exec']}"
    assert len(a["holdings"]) == 15, f"应成交 15 只，实际 {len(a['holdings'])}"
    assert [r["date"] for r in a["nav_history"]] == ["2026-09-21", "2026-09-22", "2026-09-23"], \
        f"净值日期应为 9-21/22/23，实际 {[r['date'] for r in a['nav_history']]}"
    assert a["nav_history"][1]["value"] > 0, "9-22 持仓市值应>0"
    assert a["cash"] < 1_000_000.0, f"买入后现金应减少，cash={a['cash']}"
    print(f"✅ 场景2：9-22 开盘成交 {len(a['holdings'])} 只，现金 {a['cash']:,.0f}，"
          f"9-23 市值 {a['nav_history'][-1]['value']:,.0f}，净值 {a['nav_history'][-1]['nav']:,.0f}")

    # ---- 场景 3：再跑一次，无新交易日必须幂等 ----
    R.main()
    a = json.loads(acct_path.read_text())
    assert len(a["nav_history"]) == 3, f"重复运行不得重复记账，得到 {len(a['nav_history'])} 条"
    assert a["last_exec"] == "2026-09-22"
    print("✅ 场景3：无新交易日重复运行幂等，仍为 3 条净值")

    # ---- 场景 4：月末 9-30 盘后应出新信号（次月开盘成交）----
    p3 = fake_panel_after(real_codes,
                          [pd.Timestamp("2026-09-22"), pd.Timestamp("2026-09-30")])
    # 重置账户到首跑后状态再演场景4
    shutil.copy(REAL_ACCT, acct_path)
    R.D.load_price_panel = lambda codes=None: p3
    R.main()
    a = json.loads(acct_path.read_text())
    assert a["last_exec"] == "2026-09-22", "9-22 应先成交"
    assert a["pending_signal_date"] == "2026-09-30", \
        f"9-30 月末应出新信号，实际信号日 {a['pending_signal_date']}"
    assert len(a["pending_target"]) == 15, "月末新信号应为 15 只"
    assert len(a["nav_history"]) == 3, f"应记录到 9-30 共 3 条，实际 {len(a['nav_history'])}"
    print(f"✅ 场景4：9-30 月末产出新信号 15 只（10 月开盘成交），净值 {len(a['nav_history'])} 条")

    shutil.rmtree(TMP)
    R.D.load_price_panel = _ORIG_LOAD
    print("\n全部通过：T+1 开盘成交、盯市、月末信号、幂等均正确。")


if __name__ == "__main__":
    main()
