# 目录结构详细说明

> 文档类型：Reference（参考资料）
> 更新频率：目录结构变更时
> 读者：AI代理 + 人类
> 最后更新：2026-09-21

## 〇、存储分工总表（先看这张）

| 内容 | Git仓库 | 本地data/ | 网盘备份 | 飞书知识库 |
|------|:---:|:---:|:---:|:---:|
| 代码/文档/配置/脚本/ADR | ✅ 唯一源 | — | — | — |
| 原始数据（raw/） | ❌ 忽略 | ✅ | ✅（如需） | ❌ |
| 处理后数据（processed/） | ❌ 忽略 | ✅ | ✅（如需） | ❌ |
| 运行时过程件（_workspace/） | ❌ 忽略 | ✅ | ❌ | ❌ |
| 回测/分析结果（results/） | ❌ 忽略 | ✅ | ❌ | ❌ |
| 知识成品/报告 | ✅（01_结论与产出/） | — | — | ✅（如需同步） |
| 凭证/密钥（环境变量、config/local_*） | ❌ 忽略 | ✅（仅本地，不入库） | ❌ | ❌ |

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
│   ├── quant/                  # 量化核心：data/factors/engine/metrics/viz
│   └── execution/              # 执行层：paper_trading 模拟盘
├── config/                     # 配置（default_*、池子csv 入库；local_* 不入库；密钥走环境变量）
├── scripts/                    # 可执行脚本（下载/回测/扫描/体检，清单见文档地图）
├── tests/                      # 测试
├── notebooks/                  # Jupyter研究（命名加日期前缀）
├── data/                       # 数据（整体gitignore）
│   ├── raw/                    # 原始数据（只读，绝不修改）
│   ├── processed/              # 处理后数据
│   └── _workspace/             # 运行时过程件（可随时清理）
├── results/                    # 回测/分析结果（不入库）
├── docs/                       # 工程文档（完整清单见 DOCUMENTATION_MAP.md）
│   ├── WORKFLOW.md / REQUIREMENTS.md / HANDOFF.md
│   ├── DOCUMENTATION_MAP.md / DIRECTORY_STRUCTURE.md（命名规范见本文 §五）
│   ├── strategies/             # 策略规格（中文人读成品，_vX.Y 版本后缀）
│   └── 外脑/维护类中文 SOP、画像、清单（命名规则见 §五）
└── requirements.txt            # Python依赖（pip install -r requirements.txt）
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
- 按数据类型/市场分目录（实际）：`raw/daily/{sh,sz}/`（个股日线）、`raw/minute/{sh,sz}/`（个股5分钟线）、`raw/etf/{daily,minute}/{sh,sz}/`（ETF 行情）、`raw/fundamentals/{profit,growth,dividend}/`（财务三表）；单文件如 `sh.600519.csv`（前复权、baostock 18 字段）

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

## 五、文件与目录命名规范（唯一事实源）

> 蓝本：`~/Doubao/skills/project-manager` 的 NAMING_CONVENTION 决策树与会计仓 `accounting-kb` 落地版 §9（高配实例），按本仓"六槽位中文治理层 + 工程层英文"的实际裁剪。
> **命名规则只在本节写一次**，AGENTS/README/各 SOP 涉及命名只放指针、不复述（SSOT）。
> 一句话总原则：**工程侧用英文，内容侧用中文；先判性质（给谁看），再定名字。**

### 5.1 分层总表（新增文件先判层，命中即停）

| 层 | 类别（回答什么） | 命名风格 | 本仓实例 |
|---|---|---|---|
| L0 | 平台/工具固定名 | 原样不改 | `README.md`、`AGENTS.md`、`requirements.txt`、`.gitignore` |
| L1 | 工程骨架/规范/台账/索引（"规则、标准、当前状态是什么"） | 英文 UPPER_SNAKE_CASE | docs/ 五骨架 `WORKFLOW`/`REQUIREMENTS`/`HANDOFF`/`DOCUMENTATION_MAP`/`DIRECTORY_STRUCTURE`；`03_进行中的任务/TASK_STATUS.md`、`ISSUES.md` |
| L2 | 方法/操作文档（"怎么做一件事"） | **按读者判语言**（见 5.2） | 中文：`项目维护SOP.md`、`外脑落地SOP.md`；英文 kebab：目前无，纯工程方法文档新增时用（如 `git-workflow.md`），H1 仍中文 |
| L3 | 脚本/代码/配置（`.py/.sh/.csv` 等） | 英文小写 snake_case（PEP 8），禁连字符、禁大写 | `scripts/pool3_scan.py`、`src/quant/factors.py`、`config/pool3_constituents.csv` |
| L4 | 人读内容成品（H1 为中文，操盘手/用户直接打开） | 中文，文件名与 H1 语义对应 | `00_项目总纲.md`、`02_决策记录/治理卡.md`、`docs/strategies/多因子选股策略_v0.1.md`、`01_结论与产出/3号池扫描/3号池扫描报告_2026-09-18.md`、`09_` 全部素材 |
| L5 | 中文过程产物（任务报告/调研/方案） | `{中文类型}_{对象}_{YYYY-MM-DD}.md`，日期 ISO、禁止紧凑 `YYYYMMDD` | `任务报告_数据缺口核查_2026-09-21.md` |
| L6 | 架构决策记录 | `ADR-NNN-中文标题.md`，编号三位、只增不改 | `ADR-003-G2门价值进模拟盘成长挂起.md` |

> L1 收口原则：新的"规则/标准/台账"先考虑往现有五骨架和两份台账里加节，不新造大写文件名。
> 最易混判据："是什么/必须怎样"→ L1 大写；"怎么做"→ L2；给操盘手看的中文成品 → L4；代码脚本 → L3；工具钉死的 → L0。

### 5.2 L2 方法文档的中文/英文分界（本仓项目化选择）

会计仓高配版规定方法文档一律英文 kebab；本仓操盘手是非技术用户、会直接在访达里打开 SOP，且六槽位治理层本身用中文，故按**实际读者**分界：

- **操盘手/用户会直接打开、人 AI 共读**的 SOP、清单、画像、开场白、采集类文档 → **中文文件名**（与 H1 对应）：`项目维护SOP.md`、`外脑落地SOP.md`、`外脑-操盘手会话增量采集SOP.md`、`新窗口接手开场白.md`、`操盘手交易判断采集清单.md`、`参与者画像.md`、`数据资产清单.md`、`待讨论需求清单.md`。
- **只给 AI/开发者看的纯工程方法**（git 工作流、部署、API 用法等，将来若出现）→ 英文小写 kebab-case，H1 仍写中文。
- 判不准时问一句："操盘手会不会自己打开它？"会→中文；不会→英文。

### 5.3 目录命名

- **工程目录英文**（小写、无空格）：`scripts/ src/ config/ data/ results/ tests/ notebooks/ docs/`；data 内桶名英文固定：`raw/{daily,minute,etf,fundamentals}/`、`processed/`、`_workspace/`。
- **六槽位治理/内容目录中文 + 两位数字前缀**：`00_项目总纲.md`、`01_结论与产出/`、`02_决策记录/`、`03_进行中的任务/`、`09_调研底稿与素材/`、`99_复盘与退役总结/`；其下专题子目录同样中文（`操盘手经验/`、`竞品视频/`、`3号池扫描/`）。
- 目录风格与其中文件的风格**相互独立**（中文目录里可以放英文大写台账，如 `03_进行中的任务/TASK_STATUS.md`）。

### 5.4 专项约定

- **操盘手经验归档**（`09_调研底稿与素材/操盘手经验/`）：`NNN_<豆包threadid>_<标题> - 豆包.md`，三位编号顺延（当前最大 126，新档从 127 起），原始素材只读、不改原文。
- **数据文件**：raw 用 `<数据源>_<标的>_<时间范围>.<扩展名>`（如 `baostock_sh.600519_daily_1990-2026.csv`）；processed 用 `<处理步骤>_<标的>_<日期>.<扩展名>`，优先 parquet。
- **notebooks**：`YYYY-MM-DD_中文标题.ipynb`。
- **机器产物 vs 人读成品**：`results/` 下脚本生成的 CSV/JSON/PNG/MD（如 `pool3_scan_2026-09-18.md/.csv`）沿用脚本名英文 snake、不入库；**只有归档进 `01_结论与产出/` 的人读版才改成中文 L4 名、与 H1 对应**（如 `3号池扫描报告_2026-09-18.md`）。
- 股票代码、数据格式等业务口径见 `AGENTS.md` §3.2，本节不重复。

### 5.5 新增/改名检查清单

- [ ] 命中 L0 固定名？命中则原样
- [ ] 脚本/代码/配置是否英文小写 snake_case
- [ ] 人读成品/中文过程产物是否用中文、与 H1 对应、日期 ISO
- [ ] 工程骨架/台账是否 UPPER_SNAKE；L2 方法文档读者判定是否记录在案
- [ ] 无大小写混排、无中英前缀混排、无空格（`09/操盘手经验/` 既有 ` - 豆包.md` 后缀是该目录历史既定格式，仅限此目录）
- [ ] 改名一律 `git mv` 保留历史，并级联更新全部引用、`DOCUMENTATION_MAP.md` 与本文目录树
- [ ] 批量重命名/跨目录移动属高扩散变更：先出"老名/新名/依据/影响面"清单，确认后再执行；改完跑 `.venv/bin/python scripts/doc_health_check.py`，0 ERROR 才提交
