# 决策记录 #004 · 本地行情存储采用 Parquet 并引入 DuckDB 只读查询

- 日期：2026-09-22 ｜ 项目：astock-quant ｜ 状态：Accepted

## 背景

T13 把数据范围从中证800日线扩到**全市场 4889 只的日线 + 5 分钟线**，本地数据从几百 MB
增长到约 44G、分钟线近 4 亿行；与此同时，3号池扫描器要**每个交易日全市场取数**、
多因子/成长归因要**反复加载面板回测**、未来还要做多源交叉对账。原始的纯 CSV 方案在
中证800日线阶段是合理选择（人眼可读、Excel 可开、零依赖、跟着 baostock 教程最短路径），
但在"全市场 + 高频读取"下暴露三个问题：每次都整文件全列读取、无类型（`src/quant/data.py`
靠大量 `to_numeric/to_datetime` 事后补救，pandas 3 下还出现文本列被误推断成数值而丢内容）、
占用空间大。

本地实测（pyarrow，样本 sh.600000）：

| 数据 | CSV | Parquet/zstd | 读取对比 |
|---|---|---|---|
| 5分钟线 78096 行 | 8.7 MB | 1.35 MB（约 15.5%） | 读全列 69.5ms→4.1ms；只读 close 列 1.3ms |
| 日线 6512 行 18 列 | 0.98 MB | 0.64 MB（约 65%） | 读全列 7.5ms→1.8ms |

全市场分钟线据此推算约 44G CSV → 7~8G Parquet。Parquet 是列式文件格式（不是数据库），
收益全部在**下载完成之后的读取/扫描/回测/对账**：列裁剪、行组统计的谓词下推（块裁剪）、
同构列高压缩、二进制类型化；它**不加快下载**，下载速度只取决于 baostock 网络与并发。

多源与现成数据集的评估结论（2026-09 查证）：baostock 无整市场压缩包、只有逐只 API 且按
IP 限流；akshare 分钟仅近几日、不能承担全量历史，Tushare 长历史需付费积分；通达信
（mootdx/pytdx）在本机当前网络环境协议层取不到 K 线、同账号多进程并发也在登录后挂起，
两条下载提速旁路均已验证放弃（见 ISSUES W3/W4），下载维持 baostock 串行；最接近的现成
全集 financial-analyst（HuggingFace，约14G、5500+只、含5min）其 K 线是 **微软 Qlib
`.bin` 而非 Parquet**、个人维护、快照式，不满足口径一致与持续更新。

## 决策

1. **CSV + Parquet(zstd) 双轨**：CSV 继续作为 baostock 一手原始底稿保留（只读、不改不删、
   可复现）；Parquet 是同目录、同名（`.parquet`）的读取加速副本，由
   `scripts/csv_to_parquet.py` 幂等生成，可用该脚本随时整体重建。
2. **读取层 Parquet 优先、CSV 回退**：`src/quant/data.py` 新增 `_read_table()`，同名
   Parquet 存在则读 Parquet，否则读 CSV；模块常量 `PREFER_PARQUET=False` 可整体退回 CSV。
   现有回测/研究代码无需改动即获得加速。
3. **Parquet schema 约定**（与 CSV 同列同序、一只一文件）：
   - `date` 及各 `*Date` 财务日期列存日期时间类型；
   - `code`、`time`（baostock 17 位毫秒时间戳原文）、`dividCashStock` 等文本列存字符串；
     税后分红 `dividCashPsAfterTax` 形如 `0.495或0.5225`（按持股期限分档的两个税后值）
     必须按文本无损保留，不转数值；
   - `adjustflag/tradestatus/isST` 存可空整数；OHLCV、估值、财务比率存数值；
   - 未识别列一律按字符串无损落盘，不猜测类型；压缩用 zstd。
4. **转换安全机制**：只读 CSV；对最近 120 秒（`--stable-seconds`）内被写过的文件判为
   "可能正在下载"而本轮跳过；先写 `.parquet.tmp-<pid>` 再原子 rename；每只做三向对账
   （CSV 原文 ↔ 拟写 ↔ Parquet 读回：行数、列集合、每列非空计数、数值列 sum/min/max、
   日期 min/max、文本列逐值），任一不符判 failed 且不产出正式 Parquet；可重复执行、
   数据下到哪转到哪。
5. **复权口径本阶段不变**：维持 baostock 前复权（adjustflag=2）落盘，Parquet 只改存储
   格式、不改口径，与既有数据和 ADR-002 回测保持连续。未来真正接入多源时再演进为
   "不复权 + 复权因子"，届时需重落盘并另开 ADR。
6. **引入 DuckDB 作为只读分析/对账工具**：直接对一批 Parquet 跑 SQL（自动列裁剪与谓词
   下推），零建库、零常驻服务；定位是全市场扫描取数与跨源对账的查询引擎，**不做主数据
   存储、不引入常驻数据库**。为什么选 DuckDB 而非 SQLite/Polars/ClickHouse 等，见下文
   「查询引擎选型」（2026-09-22 补查，含来源与"自有数据复测"前置）。
7. **CSV 退役是后续、可选项**：需满足"Parquet 双读稳定运行 + 每日增量直写 Parquet +
   跨源对账通过 + 用户确认"后才评估停写/删除；当前一律保留 CSV。
8. 后续工单（本 ADR 不含实现）：`download_data.py` 每日增量同步直写 Parquet；3号池
   扫描器读取层切到 Parquet/DuckDB。

## 备选（被放弃的）

- **维持纯 CSV**：在中证800日线阶段够用，但全市场分钟线 + 每日全市场扫描下读取慢、占约
  44G、类型不安全；不作为目标态，但 CSV 仍作为原始底稿保留。
- **gzip 压缩 CSV**：体积接近 Parquet，却仍是整行流、不能列裁剪/块裁剪，读取反而更慢。
- **直接下载第三方 Parquet/Qlib 全集替换**：复权/单位/退市处理口径不明、快照不持续更新、
  正确性与幸存者偏差无担保，破坏一手可复现性；第三方数据集只可作第二来源做校验或补缺，
  不替换主数据。
- **PostgreSQL/ClickHouse/数据湖等重型方案**：个人项目、单机、以只读分析为主，属过度
  工程；DuckDB + Parquet 已能满足全市场扫描与对账。
- **现在就改成"不复权 + 复权因子"**：需重下全量并冲击在跑的下载与既有回测，推迟到多源
  接入时统一做。

## 查询引擎选型：为什么是 DuckDB（2026-09-22 补查）

定 ADR 时只否掉了重型数据库，未与同量级的嵌入式引擎正面对比；2026-09-22 补做一轮横向
调研。**外部基准随硬件/版本/负载变化，只作参考、不替代在本项目自有数据上的复测。**

| 方案 | 形态 / 范式 | 对本项目的判断 |
|---|---|---|
| pandas（现状） | 行式 DataFrame、内存内 | 全市场需逐只 read 再 concat，瓶颈在文件数与 DataFrame 拼接（现拼日线面板 8.97s）；大聚合吃内存 |
| SQLite | 嵌入式、**行存**、OLTP | 擅点查/增删改/事务，逐行执行；大范围扫描聚合比列存慢一到两个数量级，方向不符 |
| Polars | 嵌入式、**列存**、Rust DataFrame（表达式 API、lazy/streaming） | 性能与 DuckDB 同档、也能直接扫 Parquet，是最强同量级对手；但范式是 Python DataFrame 转换而非 SQL。列为"未来重型特征工程/ETL 的并存候选"，当前因子计算仍用 pandas，暂不引入 |
| **DuckDB** | 嵌入式、**列存**、SQL，就地查 Parquet | **选中**：一句 SQL + glob 跨上千文件扫描/join/对账，自动列裁剪+块裁剪+join 顺序优化；内存超限可 spill 落盘；零建库零常驻、文件即数据 |
| ClickHouse / PostgreSQL / Spark / 数仓 | 独立服务器或集群 | 需 daemon/端口/运维/复制；个人单机、只读分析为主，属过度工程 |

**选择理由（针对本项目负载＝跨 4889 个 Parquet 文件做全市场扫描、聚合、跨源对账、回测取数）**：

1. **范式匹配**：核心动作是多文件 SQL 聚合/join/对账，DuckDB 的 glob 就地查询加查询优化器
   直接替代 pandas 逐只读取拼接；公开的多文件分区裁剪测试中内存占用显著更低（1 亿行约
   0.7GB，对比 pandas 8GB+ 易 OOM、Polars 1–2GB）。
2. **嵌入式零运维**：与 SQLite 同为进程内库、无端口/无守护进程/无 client-server 往返，却
   列存向量化；ClickBench 上 100GB 数据导入约 119s（无服务器架构反而最快）、43 条分析查询
   中胜出 10 条，居嵌入式第一梯队。
3. **内存安全**：数据量大于内存时可 spill 到磁盘，适合上亿行分钟线，不易 OOM。
4. **不建库、文件即数据**：直接查 Parquet，不另存一份、无需同步，契合双轨与可重建原则。

**诚实边界（不写成"DuckDB 绝对最快"）**：窗口函数与部分中小负载 Polars streaming 更快，
单行点查 pandas/SQLite 更快，2TB 级超大 Parquet 上两者互有胜负；决定因素是**范式匹配与
内存稳**，不是绝对速度。业界常见做法是 DuckDB（SQL/多文件聚合）与 Polars（Python 内转换/
特征工程）同进程并存、共享 Arrow 内存。本项目当前只引入 DuckDB，Polars 留作后续瓶颈出现
时的候选，不提前增加依赖。

**落地前置（已挂台账 T29）**：扫描器读取层切换到 Parquet/DuckDB 前，必须在**本项目自有
Parquet** 上做一次 DuckDB vs Polars vs pandas 的小基准再定稿，不直接采信外部榜单。

参考来源（2026-09 检索）：

- [DuckDB 官方 Why DuckDB（向量化 vs SQLite/PostgreSQL 逐行）](https://duckdb.org/why_duckdb)
- [ClickHouse 官方 ClickBench：最快 OLAP 数据库 2026（含 DuckDB 嵌入式排名）](https://clickhouse.com/resources/engineering/fastest-olap-databases)
- [DuckDB vs Polars in 2026（含 codecentric 2GB–2TB Parquet 基准引述）](https://www.danilchenko.dev/posts/duckdb-vs-polars/)
- [DuckDB vs Polars：SQL 引擎还是 DataFrame 库](https://fastero.com/blog/duckdb-vs-polars-which-dataframe-engine)
- [DuckDB Parquet 分区裁剪与内存对比](https://duckdblab.org/en/post/duckdb-parquet-partition-pruning/)
- [SQLite 与 DuckDB 的部署/存储差异](https://spice.ai/learn/duckdb)
- [pandas/Polars/DuckDB 分项基准（Parquet 读过滤、点查、join）](http://pythondatabench.com/de/article/duckdb-python-pandas-sql-analytik-2026)

> 注：duckdblab.org 属 DuckDB 生态向站点，其"A 股本地回测快 10–20 倍"说法仅作佐证、非中立
> 基准，故未作为选型关键依据。

## 已知局限（诚实清单）

- Parquet 不可变、不能改单行；行情是只追加冷数据，恰好适配；若要修订历史需重写该文件
  （转换脚本幂等，可整库重建）。
- **双轨过渡期 CSV 与 Parquet 可能短暂不一致**：下载进程追加了 CSV 但 Parquet 尚未重转时，
  Parquet 优先会读到稍旧的副本。缓解：转换幂等 + 稳定窗口 + 全量下完后与每日增量统一
  重转；待后续增量直写 Parquet 后彻底消除。当前全量下载未完成，这是预期内的临时状态。
- 复权口径相关的历史局限沿用 ADR-002（如股息率分母前复权的横截面偏差及 v0.2 修正方向），
  本 ADR 只固化存储、不改变复权口径。
- baostock 5 分钟线实测只回溯到 **2020-01（约 5.8 年）**，并非下载脚本配置所写的 2006；
  换 Parquet 不增加历史深度，该口径订正另行落到下载任务文档。
- 通达信备源/实时源与 baostock 多进程并发这两条**下载提速旁路，均已验证当前形态做不通并
  放弃**（2026-09-22，ISSUES W3/W4）：通达信是 TCP 7709 可握手、路由走物理网卡 en0
  直连，但 TDX 协议层取不到 K 线（真因未定、未擅改代理）；多进程是 spawn 子进程登录后
  挂起、且有同 IP 限流封禁风险。故下载维持 baostock 串行，本 ADR 的读取提速不依赖下载侧
  加速。通达信属"本机当前环境不通"而非源本身不可用，换网络环境且确有第二源刚需时凭 W4 重开。
- profit/growth/dividend 等小表行数少，Parquet 受元数据开销影响体积可能略大于 CSV，
  但换取类型安全与统一读取，且总量很小，可接受。
- DuckDB 不适合作并发写入/高频点更新；本方案只用于只读查询，不涉及该场景。

## 后果 / 何时重新评估

- 全市场下载完成后，重跑一次全量转换并执行 `--verify-duckdb` 跨文件总量校验，对账应
  100% 通过；任何 failed 都要先查清、不允许带失败进入后续。
- 后续把每日增量写入切到 Parquet 后，重新评估是否停写/删除 CSV。
- 若将来重开第二来源（通达信需先按 ISSUES W4 换网络环境排障；akshare 仅适合近期/补充
  数据），统一 schema 与 `data_source/ingested_at` 标记，并就多源架构与复权因子另开 ADR。
- 若未来需要 Tick 级数据或盘中实时更新，Parquet 的不可变特性不再适合，需另行选择
  （如 DuckDB 原生表或专用时序库）。
