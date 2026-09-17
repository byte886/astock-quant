# 目录结构详细说明

> 文档类型：Reference（参考资料）
> 更新频率：目录结构变更时
> 读者：AI代理 + 人类
> 最后更新：2026-09-16

## 〇、存储分工总表（先看这张）

| 内容 | Git仓库 | 本地data/ | 网盘备份 | 飞书知识库 |
|------|:---:|:---:|:---:|:---:|
| 代码/文档/配置/脚本/ADR | ✅ 唯一源 | — | — | — |
| 原始数据（raw/） | ❌ 忽略 | ✅ | ✅（如需） | ❌ |
| 处理后数据（processed/） | ❌ 忽略 | ✅ | ✅（如需） | ❌ |
| 运行时过程件（_workspace/） | ❌ 忽略 | ✅ | ❌ | ❌ |
| 回测/分析结果（results/） | ❌ 忽略 | ✅ | ❌ | ❌ |
| 知识成品/报告 | ✅（01_结论与产出/） | — | — | ✅（如需同步） |
| 加密凭证 .secrets/*.enc | ✅ | ✅ | ❌ | ❌ |

**一句话**：Git 只放"怎么做"的工程资产 + 结论性文档；"原料和过程件"在本地 data/，不入库。

## 一、项目根目录结构

```
astock-quant/
├── 00_项目总纲.md              # 项目章程（目标/期限/成功标准/状态）
├── 01_结论与产出/              # 最终交付物（选型报告、策略文档；需求文档在 docs/REQUIREMENTS.md）
├── 02_决策记录/                # ADR决策记录、治理卡（六槽位模式，不迁移）
├── 03_进行中的任务/            # TASK_STATUS + ISSUES（六槽位模式，不迁移）
├── 09_调研底稿与素材/          # 原始资料、转写稿、引用（只读）
│   ├── 竞品视频/               # 竞品视频文稿+需求输入
│   ├── 操盘手经验/             # 操盘手经验沉淀+raw/原始文件
│   └── references/             # 引用链接、参考资料
├── 99_复盘与退役总结/          # 项目完成后写
├── README.md                   # 项目概览（给人看）
├── AGENTS.md                   # AI操作手册（命令式）
├── .gitignore
├── src/                        # 源代码
│   ├── data/                   # 数据获取与清洗
│   ├── features/               # 因子工程
│   ├── strategies/             # 策略实现
│   ├── backtest/               # 回测引擎
│   ├── execution/              # 执行/下单
│   └── risk/                   # 风险管理
├── config/                     # 配置文件（default_*.py 入库，local_*.py 不入库）
├── scripts/                    # 可执行脚本（数据下载、批量任务等）
├── tests/                      # 测试
├── notebooks/                  # Jupyter研究（命名加日期前缀）
├── data/                       # 数据（整体gitignore）
│   ├── raw/                    # 原始数据（只读，绝不修改）
│   ├── processed/              # 处理后数据
│   └── _workspace/             # 运行时过程件（可随时清理）
├── results/                    # 回测/分析结果（不入库）
├── docs/                       # 工程文档
│   ├── WORKFLOW.md             # 工作流总纲
│   ├── REQUIREMENTS.md         # 需求与验收标准
│   ├── DIRECTORY_STRUCTURE.md  # 本文档
│   └── DOCUMENTATION_MAP.md    # 文档地图
├── .secrets/                   # 加密凭证（待创建，明文不入库）
└── requirements.txt            # Python依赖（待创建）
```

## 二、治理目录位置说明

本项目采用**六槽位管项目治理 + docs/ 管工程文档**的混合模式：

| 治理文件 | 位置 | 说明 |
|---|---|---|
| ADR 决策记录 | `02_决策记录/` | 六槽位模式，不迁移到 docs/project-management/ |
| 治理卡 | `02_决策记录/治理卡.md` | 始终在六槽位 |
| TASK_STATUS | `03_进行中的任务/TASK_STATUS.md` | 六槽位模式，不迁移 |
| ISSUES | `03_进行中的任务/ISSUES.md` | 六槽位模式，不迁移 |
| 工程文档 | `docs/` | WORKFLOW/REQUIREMENTS/DIRECTORY_STRUCTURE/DOCUMENTATION_MAP |
| 工程记忆 | （暂不启用） | 项目长大到需要时在 docs/project-management/memory/ 建立 |

> 选择依据：项目已用六槽位建立治理结构，迁移成本高且无必要；docs/ 只放工程文档，两者分工清晰不重叠。

## 三、data/ 目录说明

### 3.1 raw/ — 原始数据（只读，绝不修改）
- 从数据源下载的原始文件，保持原样
- 命名：`<数据源>_<标的>_<时间范围>.<扩展名>`，如 `baostock_sh.600519_daily_1990-2026.csv`
- 按数据类型分子目录：`raw/daily/`（日线）、`raw/weekly/`（周线）、`raw/financial/`（财务）、`raw/stock_list/`（股票列表）

### 3.2 processed/ — 处理后数据
- 经过清洗/转换/特征工程的数据
- 命名：`<处理步骤>_<标的>_<日期>.<扩展名>`
- 格式优先 parquet（压缩率高、读取快）

### 3.3 _workspace/ — 运行时过程件
- 下载临时文件、中间结果、断点状态、日志
- 可随时清理，不影响成品
- 不传网盘、不同步飞书

## 四、docs/ 目录说明

| 文档 | 职责 | 何时更新 |
|---|---|---|
| WORKFLOW.md | 工作流总纲，各环节SOP的入口和链接 | 流程变更时 |
| REQUIREMENTS.md | 需求定义、功能范围、验收标准 | 需求变更时 |
| DIRECTORY_STRUCTURE.md | 本文档，目录结构与存储分工 | 目录变更时 |
| DOCUMENTATION_MAP.md | 所有文档的快速入口 | 新增/删除文档时 |
