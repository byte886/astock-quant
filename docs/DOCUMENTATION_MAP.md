# 文档地图 · A股量化交易实验室

> 文档类型：Reference（参考资料 — 文档索引）
> 更新频率：每次新增/删除/移动文档时
> 读者：AI代理（快速定位文档）和人类（查找文档时）
> 最后更新：2026-09-21

> 本文档是项目所有文档的导航入口，告诉AI和人"先读什么、去哪里找什么"。

---

## 快速入口（按场景）

### 新介入 / 第三方咨询（第一次了解这个项目）
1. 先读 `docs/HANDOFF.md` —— 项目日志 + 需求同步：一路怎么讨论和决策的、现在在纠结什么、产物在哪

### 开始新任务前
1. 先判冷启动还是续接，按 `AGENTS.md` 第5章读对应文档
2. 当前到哪/下一步：`03_进行中的任务/TASK_STATUS.md` + `ISSUES.md`
3. 项目事实（技术栈/数据来源/约定）：`AGENTS.md` 第3节
4. 跨会话稳定结论：（工程记忆暂未启用，需要时查 ADR）

### 数据下载与管理
1. 数据下载：`scripts/download_data.py`（baostock 日线/5分钟，断点续传）+ `scripts/download_monitor.py`（launchd 自愈巡检）；流程见 `docs/WORKFLOW.md` 阶段①，口径见 `docs/数据资产清单.md`
2. 数据来源与凭证：`AGENTS.md` 第3节
3. 目录结构与存储分工：`docs/DIRECTORY_STRUCTURE.md` 第三节

### 框架选型与对比
1. 开源量化框架对比：`01_结论与产出/`（待产出）
2. AI 舆情因子方案对比：`01_结论与产出/`（待产出）
3. 需求文档（唯一）：`docs/REQUIREMENTS.md` — 已合并原 BRD，包含背景/需求/验收/开放问题

### 策略研发与回测
1. 工作流：`docs/WORKFLOW.md`（阶段③④）
2. 策略代码：`src/strategies/`
3. 回测引擎：`src/backtest/`
4. 回测结果：`results/`（不入库）

### 遇到问题/异常
1. `grep -rn "关键词" docs/ 09_调研底稿与素材/` — 搜索相关文档
2. `AGENTS.md` 第3章 — 工具用法与坑
3. `03_进行中的任务/ISSUES.md` — 已知问题清单
4. `AGENTS.md` 第3节 — 工具用法与坑（含已验证做不通的方向）

### 项目维护/文档更新
1. **怎么沉淀、怎么维护、新窗口怎么接手**：`docs/项目维护SOP.md`（沉淀收拢路由 + 维护节奏 + 变更矩阵 + 冷启动验收六问）
2. 提交前体检（断链/空目录/登记覆盖/数据误入git）：`.venv/bin/python scripts/doc_health_check.py`
3. 目录结构：`docs/DIRECTORY_STRUCTURE.md`
4. ADR决策记录：`02_决策记录/`（做重要决策前先查历史）
5. 治理卡：`02_决策记录/治理卡.md`
6. 工作流：`docs/WORKFLOW.md`

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
| 待讨论需求 | `docs/待讨论需求清单.md` | 还没聊清楚的需求点，聊完更新到 REQUIREMENTS |
| 数据资产清单 | `docs/数据资产清单.md` | A股证券类型盘点+数据需求+下载状态（待用户确认） |
| 工作流 | `docs/WORKFLOW.md` | 阶段流水线 + 各阶段校验门 + SOP链接（§0 是 AI 项目经理阶段路由） |
| 项目日志与需求同步 | `docs/HANDOFF.md` | 面向咨询方的活文档：讨论过程(日志倒序)+关键决策+当前纠结点+产物地图 |
| 参与者画像 | `docs/参与者画像.md` | 项目里都有谁、各自角色/风格/怎么协作（含新增参与者SOP） |
| 操盘手交易判断采集 | `docs/操盘手交易判断采集清单.md` | 外脑采集：按打法五要素设计问题，日常对话中把操盘手隐性判断采出来，建专属交易档案 |
| 外脑落地SOP | `docs/外脑落地SOP.md` | 分阶段（阶段0-4）让炒股客用上"懂他的AI"：怎么一步步采、每次产出什么、怎么验收，与量子系统并行节奏 |
| 操盘手会话增量采集SOP | `docs/外脑-操盘手会话增量采集SOP.md` | 增量、记忆触发地扫操盘手其它任务窗口，游标续读、去重、归档编号、结构化提炼 |
| 项目维护SOP | `docs/项目维护SOP.md` | 沉淀收拢路由、维护节奏、文档变更矩阵、文档体检、新窗口冷启动验收六问 |
| 目录结构 | `docs/DIRECTORY_STRUCTURE.md` | 存储分工、各目录职责、命名规则 |
| ETF轮动策略 | `docs/strategies/ETF轮动策略_v0.1.md` | MVP策略设计稿（候选池/调仓/选股/风控） |
| 多因子选股策略 | `docs/strategies/多因子选股策略_v0.1.md` | 价值(股息率)/成长(YOYNI+ROE)因子、月度调仓、回测口径（G2：价值 Conditional Go、成长 Hold） |
| 舆情因子与模拟盘 | `docs/strategies/舆情因子与模拟盘设计_v0.1.md` | 股吧情绪反向指标+历史数据验证方案+模拟盘设计（单维度已验证不稳健，C2 挂起） |
| 3号池扫描报告 | `01_结论与产出/3号池扫描/` | 外脑抓手 pool3_scan 的人读评级报告（定稿归档；明细 CSV 在本地 results/） |

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
| 框架选型报告 | `01_结论与产出/`（待产出） | 开源量化框架横向对比 + 选型建议 |
| AI因子方案报告 | `01_结论与产出/`（待产出） | AI 舆情因子方案对比 + 选型建议 |

### 三、调研底稿与素材（只读）

| 类别 | 路径 | 说明 |
|---|---|---|
| 竞品视频 | `09_调研底稿与素材/竞品视频/` | 竞品需求输入文档 + 转写稿 |
| 操盘手经验 | `09_调研底稿与素材/操盘手经验/` | 经验沉淀文档 + raw/ 130个原始文件 |
| 已有讨论整理 | `09_调研底稿与素材/已有讨论整理.md` | 量化讨论归纳 |
| 参考资料 | `09_调研底稿与素材/references/` | 引用链接、参考资料 |

### 四、工程文档（docs/）

| 文档 | 路径 | 用途 |
|------|------|------|
| 工作流 | `docs/WORKFLOW.md` | 四阶段流水线 + 校验门 + SOP链接 |
| 需求 | `docs/REQUIREMENTS.md` | 功能需求清单 + 验收标准 |
| 目录结构 | `docs/DIRECTORY_STRUCTURE.md` | 存储分工 + 各目录职责 |
| 文档地图 | `docs/DOCUMENTATION_MAP.md` | 本文档 |

---

## 工具清单（快速索引）

> 统一用项目 venv 运行：`.venv/bin/python scripts/<x>.py`。新增/删除脚本必须回本表登记。

| 分组 | 脚本 | 用途 |
|---|---|---|
| 数据下载 | `scripts/download_data.py` | Baostock 个股日线/5分钟，前复权、断点续传、防限流（launchd 后台单例） |
| 数据下载 | `scripts/download_monitor.py` | launchd 每 15 分钟自愈巡检、失败补下 |
| 数据下载 | `scripts/download_fundamentals.py` | 中证800 财务三表（profit/growth/dividend） |
| 数据下载 | `scripts/download_csi800_daily.py` | 中证800 成分日线补齐 |
| 数据下载 | `scripts/download_etf.py` / `download_etf_akshare.py` / `download_etf_playwright.py` | ETF 行情多通道（baostock/akshare/浏览器兜底） |
| 数据探测 | `scripts/probe_fundamental_data.py` | 财务数据字段/可用性探测 |
| 回测 | `scripts/run_multifactor_backtest.py` | 多因子回测（`--factor value/growth/both --top 15 --start`） |
| 回测 | `scripts/backtest_etf_rotation.py` | ETF 轮动回测 |
| 模拟盘 | `scripts/run_paper_trading.py` | 价值策略模拟盘（次日开盘成交、盘后信号） |
| 外脑 | `scripts/pool3_scan.py` | 3号池扫描器（量比/TRIX/位置/突破/试盘，1/2号评级） |
| 外脑 | `scripts/scan_operator_sessions.py` | 操盘手其它任务窗口增量采集（游标续读，见增量采集SOP） |
| 外脑 | `scripts/session_history.py` | 单会话历史导出（早期版，跨会话优先用 local-trajectory-recall 技能） |
| 舆情(C2挂起) | `scripts/fetch_guba_sentiment.py` / `analyze_guba_sentiment.py` / `verify_sentiment_contrarian.py` | 股吧发帖采集/情绪指数/反向指标验证（单维度已证不稳健） |
| 项目维护 | `scripts/doc_health_check.py` | 文档健康度体检（断链/空目录/登记覆盖/数据误入git），提交前跑 |
| 自动化 | `scripts/auto_run_when_ready.py` | 数据就绪后自动触发回测/扫描 |

---

## 外部资源

| 资源 | 链接/路径 | 说明 |
|---|---|---|
| 高顿 CPA 知识库 | `~/Doubao/chats/2026-08-26/new-chat/gaodun-course-knowledge-base/` | 方法论参考 |
| 内容流水线 | `~/Desktop/multiplatform-content-pipeline/` | 内容采集+知识提取参考（含 domains/stock/） |
| 项目管理技能 | `~/Doubao/skills/project-manager/` | 本项目结构和模板来源 |
| Baostock | http://baostock.com | A股免费数据源 |
