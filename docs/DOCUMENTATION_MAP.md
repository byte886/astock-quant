# 文档地图 · A股量化交易实验室

> 文档类型：Reference（参考资料 — 文档索引）
> 更新频率：每次新增/删除/移动文档时
> 读者：AI代理（快速定位文档）和人类（查找文档时）
> 最后更新：2026-10-13

> 本文档是项目所有文档的导航入口，告诉AI和人"先读什么、去哪里找什么"。

---

## 两条主线对照总表（先看这张，判清你在动哪条线）

> 项目有**两条相互独立、最终汇于一个交易 Agent** 的主线。三个代码包**互不 import（物理隔离）**，`pyproject.toml` 的 import-linter 契约守护边界——改一条线不影响另一条。

| 维度 | 量子系统 | 交易外脑（外脑） |
|---|---|---|
| **一句话目标** | 多因子选股 + 模拟盘验证"能否复现超额"，先模拟后小资金、人工扣扳机 | 克隆操盘手（魏强）的判断，盘中实时盯板块异动并推送，替他省盯盘复盘 |
| **代码包** | `src/quant/`（data/factors/engine/metrics/viz）＋`src/execution/`（paper_trading 模拟盘） | `src/monitor/`（cli/monitor/datasources/statemachine/aggregator/notifier/reconcile/persistence/codes/config） |
| **入口脚本** | 回测/因子脚本在 `scripts/`；模拟盘 `scripts/run_paper_trading.py`；盘后编排 `scripts/daily_after_close.sh` | `scripts/intraday_sector_monitor.py`（薄入口，逻辑在包内）、`scripts/query_alerts.py` |
| **数据目录** | `data/raw`（baostock 日线/5分钟/财务）、`data/paper`（模拟盘账户）、`results/paper_trading`（净值） | `data/_workspace`（板块映射/快照/日志/双源对账）、`data/intraday_alerts`（预警留痕） |
| **常驻后台任务** | `incremental-worker`（盘后 15:05 增量补数）、`daily-after-close`（周一至五 18:30 推进模拟盘） | `intraday-monitor`（9:30–15:00 盯盘企微推送）、`build-sector-map`（周一至五 09:13 预建板块映射） |
| **运行时段** | 盘后/月度（T+1 开盘成交、月度调仓），不盘中运行 | 盘中实时（轮询 60s，14:30 后 30s；集合竞价不盯） |
| **需求章节** | `REQUIREMENTS.md` §5.1–5.7（数据/因子/回测/选股/工作流/风控/AI）、§8.3 | `REQUIREMENTS.md` §5.8（盘中实时板块异动监测器） |
| **核心文档** | `docs/WORKFLOW.md` 四阶段、`00_项目总纲.md` G2/G3 | `docs/板块异动监测_上线说明.md`（v2）、`docs/盘中监控重构报告_*.md`、`02_决策记录/ADR-007` |
| **当前阶段** | G2 已过（价值年化 13.0%、Conditional Go；成长 Hold），G3 模拟盘跟踪中（2026-09-21 起） | 盘中监控 v2 已上线（2026-10-12 模块化重构、10-13 I18 开盘盲区加固）；3 号池扫描器 v2 待操盘手校准（T30） |

> 两者的关系：外脑负责"盘中实时盯盘、找当下板块异动"，量子系统负责"按多因子模型盘后选股、模拟盘验证"；终态汇入一个交易 Agent，**人工扣扳机、永不自动下单**。

---

## 快速入口（按场景）

### 新介入 / 第三方咨询（第一次了解这个项目）
1. 开新任务窗口是**固定 SOP**（用户说"开新窗口/换新窗口"即触发）：老窗口先收口（HANDOFF/TASK_STATUS 更新、体检 0 ERROR、提交推送），再让用户"选项目文件夹 `~/Desktop/astock-quant` + 发标准句"，全套见 `docs/新窗口接手开场白.md`
2. 先读 `docs/HANDOFF.md` —— 项目日志 + 需求同步：一路怎么讨论和决策的、现在在纠结什么、产物在哪

### 开始新任务前
1. 先判冷启动还是续接，按 `AGENTS.md` 第5章读对应文档
2. 当前到哪/下一步：`03_进行中的任务/TASK_STATUS.md` + `ISSUES.md`
3. 项目事实（技术栈/数据来源/约定）：`AGENTS.md` 第3节
4. 跨会话稳定结论：（工程记忆暂未启用，需要时查 ADR）

### 常驻后台任务（launchd）
> **当前长期任务 5 个（2 个常驻 KeepAlive + 3 个定时跑完即退），权威清单见 [`scripts/launchd/README.md`](../scripts/launchd/README.md)**（含部署/停用命令）。AI 接手先 `launchctl list | grep astock` 核对实际进程。
1. `intraday-monitor`：外脑盘中板块异动监控（常驻 KeepAlive），9:30–11:30/13:00–15:00 扫描并企微推送
2. `incremental-worker`：量子系统盘后 15:05 起每日行情增量补数（常驻 KeepAlive）
3. `build-sector-map`：盘前周一至五 09:13 预建当日板块成分映射（定时，防开盘盲区）
4. `holdings-check`：周一至五 09:45/13:05 持仓盯盘快照推送（定时，需求二 #38）
5. `daily-after-close`：周一至五 18:30 收盘后推进模拟盘（定时，仅模拟盘，数据增量已交 worker）
6. 已停用归档（2026-10-13）：`download-data`/`download-monitor`/`fundamentals`/`open-check`，plist 在 `~/Library/LaunchAgents/_archived_20261013/`，脚本保留可手动续跑（fundamentals 尚差约 93 只，跑月度多因子前补）

### 数据下载与管理
1. 历史补下载：`scripts/download_data.py`（baostock 日线/5分钟，断点续传）——历史已 100% 下完、launchd 任务已归档停用；流程见 `docs/WORKFLOW.md` 阶段①，口径见 `docs/数据资产清单.md`
2. 日常增量：由常驻 `incremental-worker` 每日盘后追加，不再用一次性下载脚本
3. 数据来源与凭证：`AGENTS.md` 第3节
4. 目录结构与存储分工：`docs/DIRECTORY_STRUCTURE.md` §〇；文件/目录命名规范：同文档 §五（唯一事实源）

### 框架与因子方案（已定，留档）
1. 量化框架：已决策**自研 pandas 轻量引擎**（`src/quant/`），不引入 qlib/vn.py，理由见 `docs/HANDOFF.md` §2 与 ADR
2. AI 舆情因子：单维度股吧发帖量验证不稳健，**C2 挂起**（攒数据，见 ISSUES I2 / `docs/strategies/舆情因子与模拟盘设计_v0.1.md`）
3. 需求文档（唯一）：`docs/REQUIREMENTS.md` — 已合并原 BRD，包含背景/需求/分阶段总表/开放问题

### 策略研发与回测
1. 工作流：`docs/WORKFLOW.md`（阶段③④）
2. 策略/因子代码：`src/quant/factors.py`（因子）、策略规格 `docs/strategies/`
3. 回测引擎：`src/quant/`（data/factors/engine/metrics/viz）
4. 回测结果：`results/multifactor_backtest/`（不入库）

### 遇到问题/异常
1. `grep -rn "关键词" docs/ 09_调研底稿与素材/` — 搜索相关文档
2. `AGENTS.md` 第3章 — 工具用法与坑
3. `03_进行中的任务/ISSUES.md` — 已知问题清单
4. `AGENTS.md` 第3节 — 工具用法与坑（含已验证做不通的方向）

### 项目维护/文档更新
1. **怎么沉淀、怎么维护、新窗口怎么接手**：`docs/项目维护SOP.md`（沉淀收拢路由 + 维护节奏 + 变更矩阵 + 冷启动验收六问）
2. **聊需求/新想法时怎么走**：`docs/需求讨论SOP.md`（五步收口、准入清单、防需求蔓延预警、变更六问；触发词见 AGENTS §8）
3. 提交前体检（断链/空目录/登记覆盖/数据误入git）：`.venv/bin/python scripts/doc_health_check.py`
4. 目录结构：`docs/DIRECTORY_STRUCTURE.md`
5. ADR决策记录：`02_决策记录/`（做重要决策前先查历史）
6. 治理卡：`02_决策记录/治理卡.md`
7. 工作流：`docs/WORKFLOW.md`

---

## 文档完整清单

### 零、根目录标准文档

| 文档 | 路径 | 用途 |
|------|------|------|
| 项目介绍 | `README.md` | 项目目标、定位、目录结构、快速开始 |
| AI操作手册 | `AGENTS.md` | 目录约定、工具用法、禁止事项、恢复顺序、提交前检查 |
| 项目总纲 | `00_项目总纲.md` | 目标/期限/成功标准/当前状态/治理卡指针 |
| 文档地图 | `docs/DOCUMENTATION_MAP.md` | 本文档 |
| 需求文档 | `docs/REQUIREMENTS.md` | 功能需求、验收标准、不做什么 |
| 待讨论需求 | `docs/待讨论需求清单.md` | 早期讨论记录，**已冻结**（头部声明）；新需求走需求讨论SOP，待拍板项看 TASK_STATUS |
| 需求讨论SOP | `docs/需求讨论SOP.md` | 每次聊需求/新想法的固定动作：五步收口、准入清单（说不清的后果）、MoSCoW 定级、蔓延预警、收尾模板、变更六问 |
| 数据资产清单 | `docs/数据资产清单.md` | A股证券类型盘点+数据需求+下载状态（待用户确认） |
| 外部工具与数据源调研 | `docs/外部工具与数据源调研_2026-09-22.md` | 5 个外部项目尽调（KHQUANT/public-apis/tickflow-stock-panel/灵汐AI/kronos）：横向汇总+逐对象详评+行动清单；结论=tickflow 只读代码借鉴3号池形态时序(I12)、Alpha Vantage 待探针，灵汐AI/Khy-quant 高风险拉黑；star 等以报告所标来源与日期为准 |
| 外部代码精读·tickflow | `docs/外部代码精读_tickflow扫描器借鉴_2026-09-22.md` | 只读精读 tick-stock-panel v0.2.2：三层架构（Parquet按日分区→Polars `.over(symbol)`算指标→numpy宽矩阵算信号）、策略声明式契约、13个内置策略↔001/002/003阈值对照（候选值待T30校准）、量比标准口径、不借清单、ADR-005基准输入；克隆在 gitignore 工作区 |
| 工作流 | `docs/WORKFLOW.md` | 阶段流水线 + 各阶段校验门 + SOP链接（§0 是 AI 项目经理阶段路由） |
| 开发规划与版本路线图 | `docs/开发规划与版本路线图.md` | 开工三闸门（三策略规则结构/扫描器MVP范围/模拟盘每日工作流）+ M0–M3 版本路线、并行时序、决策责任矩阵；回答"定哪些就能开工"，实时进度仍看 TASK_STATUS |
| 001/002/003 可计算规格 | `docs/策略可计算规格_001-002-003_v0.1.md` | **M0 闸门①**：三策略逐条翻成"业务原话→所需字段→计算逻辑→参数"；A/B/C/D 可计算分级、三个量能口径分离（池子60日/信号20日/对账5日）、001四条件/002五条件/离场四信号、003自动排雷+人工定性分工、参数总表、正负样本回放验收集；v0.1 待操盘手/用户判读结构 |
| 策略与业务集中收口问卷 | `docs/策略与业务集中收口问卷_操盘手沟通版_v0.1.md` | **M0 闸门配套·一次集中沟通收口**：A 部分 Q1-Q28 请操盘手定策略/业务规矩（每日交易定义、两线合一、选股漏斗、买卖点、仓位风控、验收标准，选择题为主）、B 部分 U1-U4 请用户拍边界（自主交易红线/Agent 形态/2-3 万等比模拟盘/技术决策机制）、C 部分为 AI 技术底座自决备案；答完升规格 v1.0 |
| 项目日志与需求同步 | `docs/HANDOFF.md` | 面向咨询方的活文档：讨论过程(日志倒序)+关键决策+当前纠结点+产物地图 |
| 参与者画像 | `docs/参与者画像.md` | 项目里都有谁、各自角色/风格/怎么协作（含新增参与者SOP） |
| 操盘手交易判断采集 | `docs/操盘手交易判断采集清单.md` | 外脑采集：按打法五要素设计问题，日常对话中把操盘手隐性判断采出来，建专属交易档案 |
| 外脑落地SOP | `docs/外脑落地SOP.md` | 分阶段（阶段0-4）让炒股客用上"懂他的AI"：怎么一步步采、每次产出什么、怎么验收，与量子系统并行节奏 |
| 操盘手会话增量采集SOP | `docs/外脑-操盘手会话增量采集SOP.md` | 增量、记忆触发地扫操盘手其它任务窗口，游标续读、去重、归档编号、结构化提炼 |
| 项目维护SOP | `docs/项目维护SOP.md` | 沉淀收拢路由、维护节奏、文档变更矩阵、文档体检、新窗口冷启动验收六问 |
| 新窗口接手开场白 | `docs/新窗口接手开场白.md` | 开新任务窗口固定 SOP：触发词、老窗口收口清单、本机两步开窗（选文件夹+标准句）、六问验收、他人/网页兜底整段 |
| 目录结构与命名规范 | `docs/DIRECTORY_STRUCTURE.md` | 存储分工、各目录职责；§五 文件与目录命名规范（唯一事实源） |
| ETF轮动策略 | `docs/strategies/ETF轮动策略_v0.1.md` | MVP策略设计稿（候选池/调仓/选股/风控） |
| 多因子选股策略 | `docs/strategies/多因子选股策略_v0.1.md` | 价值(股息率)/成长(YOYNI+ROE)因子、月度调仓、回测口径（G2：价值 Conditional Go、成长 Hold） |
| 舆情因子与模拟盘 | `docs/strategies/舆情因子与模拟盘设计_v0.1.md` | 股吧情绪反向指标+历史数据验证方案+模拟盘设计（单维度已验证不稳健，C2 挂起） |
| 盘中板块异动监测方案 | `docs/盘中板块异动监测方案_AB双轨_v1.0.md` | 盘中全板块异动监测的 A/B 双轨**调研选型（留档，已落地）**；阈值/通道结论已回填文首与 §4，实现以《上线说明》v2 为准 |
| 盘中板块异动监测上线说明 | `docs/板块异动监测_上线说明.md` | **v2（2026-10-12）**："代码怎么跑"的单一事实文档——src/monitor 分层包+薄入口、config 阈值事实源、增强状态机（宽限/波次/标签）、数据源主备与双源对账、涨停口径（I16/I17）、防静默三道、推送回执、部署红线、六道门、已知局限 |
| 盘中监控重构报告 | `docs/盘中监控重构报告_gauntlet质量改造_2026-10-12.md` | 巨石单文件（核心函数 CC=86）→ src/monitor 分层包的 gauntlet 六道门质量改造：发现并修复 I13/I15/I16/I17、行为守恒固定种子差分、部署门禁与关机核验、质量数据；决策见 ADR-007 |
| 等价变异体清单 | `EQUIVALENT-MUTANTS.md` | gauntlet 变异测试 15 个"改了但行为不变"存活体的逐条豁免证据（含手动改坏源码反证）；merge 门 `--equiv-ok` 的放行依据，随代码入库；阶段门日志/签收单在本地 `target/quality/`（gitignore，不入库） |
| 3号池扫描报告 | `01_结论与产出/3号池扫描/3号池扫描报告_2026-09-18.md` | 外脑抓手 pool3_scan 的人读评级报告（定稿归档；明细 CSV 在本地 results/） |

### 一、项目治理（六槽位）

| 文档 | 路径 | 用途 |
|------|------|------|
| 任务状态 | `03_进行中的任务/TASK_STATUS.md` | 当前进度、下一步（单一进度真相） |
| 问题清单 | `03_进行中的任务/ISSUES.md` | 已知问题、阻塞项、Won't Fix |
| ADR决策 | `02_决策记录/` | 决策记录（只增不改） |
| 治理卡 | `02_决策记录/治理卡.md` | 决策权限、暂停重评条件、碰节奏 |

### 二、结论与产出

| 文档 | 路径 | 用途 |
|------|------|------|
| 需求文档 | `docs/REQUIREMENTS.md` | 唯一需求文档（已合并原 BRD）：背景/需求清单/验收/开放问题 |
| 框架选型结论 | 见 `docs/HANDOFF.md` §2 + `02_决策记录/` ADR | 已决策自研 pandas 引擎，不再产出重型框架对比报告 |
| 舆情因子结论 | 见 ISSUES W1/I2 + 舆情策略文档 | 单维度验证不稳健、C2 挂起；精细化方向留档，暂不产出选型报告 |

### 三、调研底稿与素材（只读）

| 类别 | 路径 | 说明 |
|---|---|---|
| 竞品视频 | `09_调研底稿与素材/竞品视频/` | 竞品需求输入文档 + 转写稿 |
| 操盘手经验 | `09_调研底稿与素材/操盘手经验/` | 经验沉淀文档 + 编号归档（已到 130，持续追加；127/128 原文存 `raw/`；129 为荀玉根 DDM 大盘研判、130 为概率论/期望值/资金管理方法论（数学通识，落点见该篇）；原始素材只读） |
| 已有讨论整理 | `09_调研底稿与素材/已有讨论整理.md` | 量化讨论归纳 |
| 参考资料 | `09_调研底稿与素材/references/` | 引用链接、参考资料 |

### 四、工程文档（docs/）

| 文档 | 路径 | 用途 |
|------|------|------|
| 工作流 | `docs/WORKFLOW.md` | 四阶段流水线 + 校验门 + SOP链接 |
| 需求 | `docs/REQUIREMENTS.md` | 功能需求清单 + 验收标准 |
| 目录结构与命名规范 | `docs/DIRECTORY_STRUCTURE.md` | 存储分工 + 各目录职责；§五 命名规范唯一事实源 |
| 文档地图 | `docs/DOCUMENTATION_MAP.md` | 本文档 |

---

## 工具清单（快速索引）

> 统一用项目 venv 运行：`.venv/bin/python scripts/<x>.py`。新增/删除脚本必须回本表登记。

| 分组 | 脚本 | 用途 |
|---|---|---|
| 数据下载 | `scripts/download_data.py` | Baostock 个股日线/5分钟，前复权、断点续传、防限流（历史已 100% 下完，launchd 任务 2026-10-13 归档停用，手动续跑用） |
| 数据下载 | `scripts/download_monitor.py` | download_data 的看门狗（launchd 自愈巡检），随主任务 2026-10-13 归档停用 |
| 数据下载 | `scripts/download_fundamentals.py` | 中证800 财务三表（profit/growth/dividend）；已下 707/约800，launchd 任务归档，跑月度多因子前补约 93 只 |
| 数据下载 | `scripts/download_etf.py` / `download_etf_akshare.py` | ETF 行情手动补数（baostock 全量 / akshare 东财完整历史并在东财失败时自动回退新浪源、失败返回 rc=1）；日常增量由 incremental-worker 内联复刻 baostock 口径，不经这两个脚本 |
| 数据探测 | `scripts/probe_external_data.py` | akshare 外部通道探测（东财 datacenter/push2、新浪资金流/ETF、同花顺行业概念；SIGALRM 超时，报告 results/external_data_probe/） |
| 回测 | `scripts/run_multifactor_backtest.py` | 多因子回测（`--factor value/growth/both --top 15 --start`） |
| 回测 | `scripts/backtest_etf_rotation.py` | ETF 轮动回测 |
| 研究验证 | `scripts/attribute_growth_drawdown.py` | T28 成长策略 -60% 回撤归因+改进变体回测（A估值/B质量/C择时/D估值+择时/E质量+择时），产物 results/growth_attribution/ |
| 研究验证 | `scripts/bench_scan_engines.py` | 扫描引擎三方案基准（pandas/DuckDB/Polars，在自有日线 Parquet 上测 L1捞数/L2分组指标/L3形态时序/L4横截面，独立子进程测耗时内存并交叉校验），ADR-005 证据，结果落 results/engine_benchmark/ |
| 模拟盘 | `scripts/run_paper_trading.py` | 价值策略模拟盘（建仓首跑 + 日常增量推进：T日盘后信号、T+1开盘成交、收盘盯市、月末新信号） |
| 模拟盘 | `src/execution/paper_trading.py` | 模拟盘账户/撮合/成本引擎（佣金万2.5+卖出印花税千1+滑点千1） |
| 测试 | `tests/execution/`（test_paper_trading 15 例 + flow 5 例）、`tests/quant/`（test_data_calendar 12 例 + test_data_loading 25 例） | 模拟盘撮合/现金/100股整手/月末口径与数据层行为刻画测试；paper_trading.py 与 data.py 覆盖率 100%、变异 0 存活（engine/factors/metrics/viz 欠账见 I24） |
| 自动化 | `scripts/daily_after_close.sh` | 盘后编排（**仅推进模拟盘**；数据增量已统一交常驻 incremental-worker，避免同账号 baostock 并发挂起 W3），launchd 周一至五 18:30 触发 |
| 外脑 | `scripts/pool3_scan.py` | 3号池扫描器（量比/TRIX/位置/突破/试盘，1/2号评级） |
| 外脑 | `scripts/scan_operator_sessions.py` | 操盘手其它任务窗口增量采集（游标续读，见增量采集SOP） |
| 外脑 | `scripts/session_history.py` | 单会话历史导出（早期版，跨会话优先用 local-trajectory-recall 技能） |
| 舆情(C2挂起) | `scripts/fetch_guba_sentiment.py` / `analyze_guba_sentiment.py` / `verify_sentiment_contrarian.py` | 股吧发帖采集/情绪指数/反向指标验证（单维度已证不稳健） |
| 项目维护 | `scripts/doc_health_check.py` | 文档健康度体检（断链/空目录/登记覆盖/数据误入git），提交前跑 |
| 盘中监控 | `scripts/intraday_sector_monitor.py`（**薄入口**，逻辑在 `src/monitor/` 包） | 盘中板块异动监测（launchd 常驻，`--once` 试跑/`--build-map` 建映射）：东财 push2(curl) 主 → 腾讯个股聚合兜底 → 东财涨停池 → 新浪备份，L1/L2 两级 + 波次状态机触发企微推送；含增强状态机/防静默三道/双源对账；已接交易日历自动休眠；留痕 `data/intraday_alerts/`、快照 `data/_workspace/snapshots/`、对账 `data/_workspace/source_diff/`；测试 `tests/monitor/`；**实现与坑见《上线说明》v2，质量门见 ADR-007** |
| 盘中监控 | `scripts/query_alerts.py` | 盘中异动预警留痕查询（`--date/--level/--board`） |

---

## 外部资源

| 资源 | 链接/路径 | 说明 |
|---|---|---|
| 高顿 CPA 知识库 | `~/Doubao/chats/2026-08-26/new-chat/gaodun-course-knowledge-base/` | 方法论参考 |
| 会计仓（高配参考） | `~/Desktop/accounting-kb/` | 项目管理高配实例（命名规范落地版 §9、自动检查脚本），本仓 §五 蓝本之一 |
| 内容流水线 | `~/Desktop/content-pipeline/` | 内容采集+知识提取参考（含 domains/stock/） |
| 项目管理技能 | `~/Doubao/skills/project-manager/` | 本项目结构和模板来源 |
| Baostock | http://baostock.com | A股免费数据源 |
