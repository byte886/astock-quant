# A股量化交易实验室

> 基于开源框架的 A 股量化交易研究与实践项目，融合 AI 大模型做舆情/文本因子。

## 项目定位

- **市场**：A 股（含 ETF）
- **策略方向**：中低频多因子选股 / ETF 轮动（待对比确定）
- **技术栈**：纯开源框架（vn.py / QuantDinger / qlib / Hikyuu 待选型）
- **AI 增强**：大模型舆情打分（豆包 API / Grok / 本地开源模型 待对比）
- **执行方式**：盘后信号 + 人工下单 起步，后续评估自动下单
- **数据源**：Baostock（免费，1990年至今）

## 当前阶段

📋 **调研期** — 竞品调研完成、框架选型进行中、数据下载准备中。

## 新会话快速恢复

1. 读 `AGENTS.md` — AI 操作规则（禁止事项、目录约定、恢复顺序）
2. 读 `00_项目总纲.md` — 项目目标、期限、当前状态
3. 读 `docs/DOCUMENTATION_MAP.md` — 文档地图（按需沿链接深读）
4. 读 `docs/WORKFLOW.md` — 四阶段工作流（了解当前在哪个阶段）
5. 读 `03_进行中的任务/TASK_STATUS.md` — 当前在做什么、下一步

## 工程文档（docs/）

| 文档 | 用途 |
|---|---|
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | 四阶段流水线 + 各阶段校验门 + SOP链接 |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 功能需求清单 + 验收标准 + 开放问题（v0.1草稿） |
| [docs/DIRECTORY_STRUCTURE.md](docs/DIRECTORY_STRUCTURE.md) | 存储分工 + 各目录职责 + 命名规则 |
| [docs/DOCUMENTATION_MAP.md](docs/DOCUMENTATION_MAP.md) | 所有文档的快速入口（按场景索引） |

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
├── docs/                   # 工程文档（WORKFLOW/REQUIREMENTS/DIRECTORY_STRUCTURE/DOCUMENTATION_MAP）
├── src/                    # 源代码
│   ├── data/               # 数据获取与清洗
│   ├── features/           # 因子工程
│   ├── strategies/         # 策略实现
│   ├── backtest/           # 回测引擎
│   ├── execution/          # 执行/下单
│   └── risk/               # 风险管理
├── config/                 # 配置文件（default_*.py 入库，local_*.py 不入库）
├── scripts/                # 可执行脚本（数据下载等）
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
# 数据下载（全量A股，Baostock，防限流+断点续传）
python scripts/download_data.py

# 数据下载（只下载增量，从上次断点继续）
python scripts/download_data.py --incremental

# 数据质量检查
python scripts/download_data.py --check
```

## 调研产出

- [需求文档 v0.2](docs/REQUIREMENTS.md) — 唯一需求文档（已合并原 BRD）：背景/需求清单/验收/开放问题
- [竞品需求输入文档](09_调研底稿与素材/竞品视频/竞品需求输入文档.md) — B站+抖音两个竞品视频拆解
- [操盘手经验沉淀](09_调研底稿与素材/操盘手经验/操盘手经验沉淀与需求输入.md) — 130个豆包链接归纳

## 免责声明

本项目仅用于个人学习与研究，不构成任何投资建议。量化策略存在亏损风险，回测结果不等于未来收益。
