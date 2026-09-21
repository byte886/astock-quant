# A股量化交易实验室

> 两条主线：**①量子系统**——A 股（含 ETF）中低频量化，多因子选股跑赢沪深300；**②交易外脑**——持续克隆职业操盘手（nero6688/炒股客）说不清的交易判断。数据用免费 Baostock，回测自研 pandas 轻量引擎。

## 项目定位

- **市场**：A 股个股 + ETF（剔科创板、不要北交所）；基准沪深300
- **策略方向**：主线多因子选股——价值（TTM 股息率+连续分红）/ 成长（YOYNI+ROE），月度调仓、15 只等权；ETF 轮动为早期 MVP（回测年化约 2%、已搁置）
- **技术栈**：自研 pandas 轻量引擎（`src/quant/`：data/factors/engine/metrics/viz）+ 模拟盘（`src/execution/`），不引入 qlib/vn.py 等重型框架
- **AI 增强**：股吧散户情绪反向因子（单维度发帖量已验证不稳健，C2 挂起攒数据）；外脑侧沉淀操盘手 1/2/3 号交易规则
- **执行方式**：盘后出信号 → 模拟盘先行 → 人工下单，**不自动下单**
- **数据源**：Baostock（免费，日线 1990 起、5 分钟线 2006 起、财务三表覆盖中证800）

## 当前阶段

🧪 **G2 验证门已过（2026-09-20）**：价值策略年化 13.0%、最大回撤 -35%、夏普 0.62、对沪深300超额 +9.0% → **Conditional Go，待进模拟盘**；成长策略年化 8.0%、回撤 -60% → **Hold**。外脑第一个抓手「3号池扫描器 v1」已出评级、待操盘手校准。下一步见 `03_进行中的任务/TASK_STATUS.md`。

## 新会话快速恢复

> 只给仓库路径就能接手。读完按 `docs/项目维护SOP.md` §6「冷启动六问」自测，答不上先修文档再动手。

1. 读 `AGENTS.md` — AI 操作规则（红线、目录约定、恢复顺序、提交清单）
2. 读 `00_项目总纲.md` — 项目目标、期限、当前状态
3. 读 `docs/DOCUMENTATION_MAP.md` — 文档地图（按需沿链接深读）
4. 读 `docs/WORKFLOW.md` — 四阶段工作流（§0 判断当前在哪个门）
5. 读 `03_进行中的任务/TASK_STATUS.md` + `ISSUES.md` — 当前在做什么、下一步、坑
6. 第三方/外脑介入先读 `docs/HANDOFF.md`（项目日志+需求同步）

## 工程文档（docs/）

| 文档 | 用途 |
|---|---|
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | 四阶段流水线 + 各阶段校验门 + §0 阶段路由 |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 功能需求清单 + 验收标准 + 开放问题（唯一需求文档） |
| [docs/项目维护SOP.md](docs/项目维护SOP.md) | 沉淀收拢路由、维护节奏、文档体检、冷启动六问 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 项目日志（倒序）+ 需求同步，第三方/外脑介入先读 |
| [docs/DIRECTORY_STRUCTURE.md](docs/DIRECTORY_STRUCTURE.md) | 存储分工 + 各目录职责 + 命名规则 |
| [docs/DOCUMENTATION_MAP.md](docs/DOCUMENTATION_MAP.md) | 所有文档与脚本的快速入口（按场景索引） |

## 目录结构

```
├── 00_项目总纲.md          # 项目章程：目标/成功标准/期限/治理
├── AGENTS.md               # AI 操作手册（给 AI 看的指令性规则）
├── README.md               # 本文件（项目概览）
├── 01_结论与产出/          # 最终交付物（选型报告、策略文档；需求在 docs/REQUIREMENTS.md）
├── 02_决策记录/            # ADR 关键决策 + 治理卡
├── 03_进行中的任务/        # TASK_STATUS + ISSUES
├── 09_调研底稿与素材/      # 原始资料、视频文稿、引用（只读）
├── 99_复盘与退役总结/      # 项目完成后写
├── docs/                   # 工程文档（WORKFLOW/REQUIREMENTS/项目维护SOP/DIRECTORY_STRUCTURE/DOCUMENTATION_MAP/HANDOFF）
├── src/                    # 源代码
│   ├── quant/              # 量化核心：data/factors/engine/metrics/viz
│   └── execution/          # 执行层：paper_trading 模拟盘
├── config/                 # 配置与池子（default_*、pool3_constituents.csv 入库；local_* 不入库）
├── scripts/                # 可执行脚本（下载/回测/扫描/体检，清单见文档地图）
├── tests/                  # 测试
├── notebooks/              # Jupyter 研究探索
├── data/                   # 数据（不入库）
│   ├── raw/                # 原始数据（只读）
│   ├── processed/          # 处理后数据
│   └── _workspace/         # 运行时过程件（可随时清理）
└── results/                # 回测结果（不入库）
```

## 存储分工

| 内容 | Git仓库 | 本地data/ | 网盘备份 |
|---|:---:|:---:|:---:|
| 代码/文档/配置/脚本/ADR | ✅ 唯一源 | — | — |
| 原始数据（raw/） | ❌ 忽略 | ✅ | ✅（如需） |
| 处理后数据（processed/） | ❌ 忽略 | ✅ | ✅（如需） |
| 运行时过程件（_workspace/） | ❌ 忽略 | ✅ | ❌ |
| 回测/分析结果（results/） | ❌ 忽略 | ✅ | ❌ |
| 知识成品/报告 | ✅（01_结论与产出/） | — | — |

## 常用命令

```bash
PY=.venv/bin/python   # 统一用项目 venv（Python 3.12）

# 数据下载（Baostock 日线/5分钟，防限流+断点续传；后台由 launchd 单例维护）
$PY scripts/download_data.py --check          # 覆盖率/缺口检查
$PY scripts/download_data.py --incremental    # 只补增量

# 多因子回测（价值/成长）
$PY scripts/run_multifactor_backtest.py --factor value --top 15 --start 2015-01-01
# 3号池扫描（外脑）
$PY scripts/pool3_scan.py
# 文档体检（提交前必跑，0 ERROR 才提交）
$PY scripts/doc_health_check.py
```

## 关键产出

- [需求文档](docs/REQUIREMENTS.md) — 唯一需求文档（已合并原 BRD）：背景/需求清单/分阶段总表/开放问题
- [多因子选股策略 v0.1](docs/strategies/多因子选股策略_v0.1.md) — 价值/成长因子与 G2 回测结论
- [3号池扫描报告](01_结论与产出/3号池扫描/) — 外脑 pool3_scan 的人读评级（明细 CSV 在本地 results/）
- [操盘手经验沉淀](09_调研底稿与素材/操盘手经验/操盘手经验沉淀与需求输入.md) — 豆包链接归纳（经验编号持续追加，已到 126）
- [竞品需求输入文档](09_调研底稿与素材/竞品视频/竞品需求输入文档.md) — B站+抖音竞品视频拆解

## 免责声明

本项目仅用于个人学习与研究，不构成任何投资建议。量化策略存在亏损风险，回测结果不等于未来收益。
