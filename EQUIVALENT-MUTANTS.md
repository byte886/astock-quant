# 等价变异体人工核验清单

按 clean-code-gauntlet「存活变异体 = 0（等价变异体经人工核验 + 清单登记后可豁免）」执行。
范围：`src/monitor/`（盘中板块异动监控拆包版）。以下变异体经人工逐条核验，**改动后对外可观测行为完全不变**，登记豁免。

- 核验日期：2026-10-12（I16 修复后同日复核）
- 核验轮次：初版全量第四轮 Total 328 / Killed 266 / Survived 13 / Uncovered 49；**I16 修复（aggregator 剥离市场前缀 + codes 920 归 bj）后全量复核 Total 331 / Killed 267 / Survived 15 / Uncovered 49**——新增 2 个存活均位于本次新增的前缀剥离行 aggregator.py:22，经手动注入变异 + 裸码/前缀用例交叉验证确认为等价体，登记后存活=0。
- 核验方式：逐条重读源码 + 推导可达路径 + 现有/新增测试交叉确认（aggregator.py:22 两条额外做了"临时改源码跑测试"的变异实证）

| 位置 | 变异 | 等价性说明 | 核验人 | 日期 |
|------|------|-----------|--------|------|
| `src/monitor/aggregator.py:19` `if zdf is None or pd.isna(zdf): return False` | `or` → `and` | zdf=None 时两子句都为 True（`None is None` 与 `pd.isna(None)` 均 True），and 仍命中 return False；zdf=NaN 时首子句 False 使 and 短路、不提前 return，但后续 `NaN>=19.9`、`NaN>=9.9` 均为 False，函数末尾仍 return False。两条路径返回值相同。（I16 修复加剥离前缀分支后本行由 13 位移至 19，逻辑未变） | 人工核验 | 2026-10-12 |
| `src/monitor/aggregator.py:22` `if len(c) > 6 and c[:2] in ("sh","sz","bj"): c = c[2:]`（剥离市场前缀） | `>` → `>=` | 有效 code 只有两类：6 位纯数字裸码（c[:2] 为 60/30/00/68 等数字，绝不为 sh/sz/bj）、或"2 字母前缀+6 位数字"恒长 8 的带前缀码。`>=6` 与 `>6` 的唯一差别在 len 恰为 6，但此时第二子句 `c[:2]∈{sh,sz,bj}` 对纯数字裸码必为 False，`and` 整体仍 False、不剥离；len=7 的有效码不存在。两版剥离决策完全相同。已手动把 `>6` 改成 `>=6` 后跑全部裸码/前缀用例（17 条）全过，实证不可区分。 | 人工核验 | 2026-10-12 |
| `src/monitor/aggregator.py:22` 同上剥离条件 | `and` → `or` | `or` 与 `and` 产生差异需"`len(c)>6` 为 False 但 `c[:2]∈{sh,sz,bj}` 为 True"，即 len≤6 却以 sh/sz/bj 开头的串；有效裸码恒为 6 位纯数字、不存在此种串，带前缀码恒长 8（第一子句也为 True）。故两版结果一致，同样经手动变异 + 17 条用例全过实证。 | 人工核验 | 2026-10-12 |
| `src/monitor/config.py:63` `print(line, flush=True)` | `True` → `False` | flush 只控制 stdout 缓冲刷新时机，不改变是否打印与打印内容；日志另由独立文件写入落盘，运行期/退出时缓冲都会输出，无可观测差异。 | 人工核验 | 2026-10-12 |
| `src/monitor/datasources.py:168` `ok = False` | `False` → `True` | ok 仅在 `if names: ok=True` 置位。两类 names 全空时 `_collect_em_cons` 执行 0 次、c2b 必为空，结尾 `len(c2b) < 100` 为 True 仍触发新浪兜底；任一 names 非空时 ok 正常也被设为 True。初值取 False/True，最终兜底决策一致。 | 人工核验 | 2026-10-12 |
| `src/monitor/monitor.py:166` `max(int(st.get("peak_zt") or 0), sig["zt"])` | `or` → `and` | 该表达式只在「同波无台阶静默」分支求值，此时 st 必已由 `begin_new_wave` 建立并含 `peak_zt=zt_n`（达标时 ≥3，真值），`st.get("peak_zt")` 必命中真值，`or 0` 分支不可达，max 结果不变。 | 人工核验 | 2026-10-12 |
| `src/monitor/monitor.py:166` 同上 `st.get("peak_zt", 0)` | `0` → `1` | 同上：st 必含 peak_zt 键且为真值，get 的默认值（0 或 1）永不可达。 | 人工核验 | 2026-10-12 |
| `src/monitor/monitor.py:178` `int(st.get("fire_count", 0)) + 1` | `0` → `1` | 到达 `_emit_alert` 的 st 都经过 `begin_new_wave`，其初始化字典显式写入 `"fire_count": int(old.get("fire_count", 0))`，键必存在，get 默认值不可达。 | 人工核验 | 2026-10-12 |
| `src/monitor/monitor.py:182` `if level == "L1": st["fired_l1"] = True` | `==` → `!=` | fired_l1 在 `begin_new_wave` 已按 `cur_lvl=="L1"` 初始化。level 为 L1 时它已是 True，本行只是冗余再赋 True；level 为 L2 时能升到 L2 必先经历 L1 首波（fired_l1 已为 True），变异后 `!=` 分支赋 True 结果仍是 True。所有路径 fired_l1 相同。 | 人工核验 | 2026-10-12 |
| `src/monitor/notifier.py:24` `emsg = (resp.text or "")[:80]` | `or` → `and` | 仅用于 HTTP 响应 JSON 解析失败时拼接异常日志文本，不参与推送成功与否的返回值；即便 resp.text 为 None 使 and 产生 None 并在切片时二次抛错，也被外层 `try/except` 捕获归为推送失败（return False），与原路径结果一致，仅异常日志文本不同。 | 人工核验 | 2026-10-12 |
| `src/monitor/notifier.py:50` `(resp.text or '')[:100]`（失败日志） | `or` → `and` | 同上：只影响 Server 酱失败分支的日志字符串，函数返回的成功布尔不依赖它；二次异常同样被外层捕获归为失败（return False）。 | 人工核验 | 2026-10-12 |
| `src/monitor/persistence.py:135` `json.dumps({"last_scan": ts}, ensure_ascii=False)` | `False` → `True` | ts 恒为 `%Y-%m-%d %H:%M:%S` 时间戳（数字与 `-`、`:`、空格，纯 ASCII），ensure_ascii 取 True/False 对纯 ASCII 字符串的输出字节完全相同，`json.loads` 读回一致。 | 人工核验 | 2026-10-12 |
| `src/monitor/reconcile.py:58` `abs_diffs[min(n - 1, int(n * 0.9))]` | `-` → `+`（n-1 → n+1） | n≥1 时 `int(n*0.9) ≤ n-1` 恒成立，min 恒取 `int(n*0.9)`；上界写成 n-1 还是 n+1 都不改变 min 结果。 | 人工核验 | 2026-10-12 |
| `src/monitor/reconcile.py:58` 同上 `n - 1` | `1` → `0`（n-1 → n） | 同上：`int(n*0.9) < n` 恒成立，上界写成 n-1 还是 n，min 都取 `int(n*0.9)`，P90 取值不变。 | 人工核验 | 2026-10-12 |
| `src/monitor/statemachine.py:24` `if up and down is not None and (up + down) > 0` | `>` → `>=` | 到达末个条件需首子句 `up` 为真（up 为 ≥1 的整数），down 非负，故 up+down ≥ 1 恒成立，永远不会恰好等于 0；`>0` 与 `>=0` 判定结果恒一致。 | 人工核验 | 2026-10-12 |
