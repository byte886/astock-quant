# launchd 后台任务（macOS 常驻/定时）

本目录放本机 `~/Library/LaunchAgents/` 下 launchd 任务的**权威模板**，便于换机复现与交接。
实际运行的 plist 是这里的副本（`cp` 到 `~/Library/LaunchAgents/` 后 `launchctl bootstrap`）。
任务统一用 `~/bin/A股量化-*` 包装脚本 `exec` 到项目 `.venv/bin/python`，使日志/工作目录/解释器固定。

## 任务清单（2026-10-13 起）

| Label | 类型 | 触发 | 作用 | 状态 |
|---|---|---|---|---|
| `com.astock-quant.intraday-monitor` | 常驻 KeepAlive | 全天 | 外脑：盘中板块异动监控（9:30–11:30 / 13:00–15:00 扫描并企微推送） | 长期 |
| `com.astock-quant.incremental-worker` | 常驻 KeepAlive | 全天，盘后 15:05 起补数 | 量子：每日行情增量（核心池+全市场日线+分钟线滚动） | 长期 |
| `com.astock-quant.daily-after-close` | 定时 | 周一至周五 18:30 | 量子：收盘后推进模拟盘（run_paper_trading） | 长期 |
| `com.astock-quant.build-sector-map` | 定时 | 周一至周五 09:13 | 外脑：盘前预建当日板块成分映射（本目录模板） | 长期 |
| `com.astock-quant.holdings-check` | 定时·跑完即退 | 周一至周五 09:45 / 13:05 | 外脑：持仓盯盘快照推送（现价/涨跌幅/量比/涨速、涨跌停/大跌标注、3号池⭐、所属板块异动，需求二 #38，本目录模板） | 长期 |
| `com.astock-quant.download-data` | 保活·一次性 | 历史下载完成即退出 | 量子：全市场历史行情下载（断点续传） | **已停用归档**（个股日线/5分钟 4888/4888、ETF 日线/5分钟 1588/1588 全 100%、0 失败） |
| `com.astock-quant.download-monitor` | 保活·一次性 | 每 15 分钟巡检 | download-data 的看门狗 | **已停用归档**（随上者） |
| `com.astock-quant.fundamentals` | 保活·一次性 | 基本面下载完成即退出 | 量子：中证800 基本面（ROE/成长/分红） | **已停用归档**（707/约800≈88%、无失败；将来跑月度多因子前需续跑 `scripts/download_fundamentals.py` 补约 93 只） |
| `com.astock-quant.open-check` | 定时（写死 2026-09-28） | 一次性 | 9-28 开盘检查 | **已退役归档**，被 build-sector-map 替换 |

> 长期任务 5 个：常驻 KeepAlive 2 个（intraday-monitor、incremental-worker），定时跑完即退 3 个
> （daily-after-close 18:30、build-sector-map 09:13、holdings-check 09:45/13:05）。2026-10-13 已将三个一次性下载任务与 open-check `bootout` 停用，plist 归档在
> `~/Library/LaunchAgents/_archived_20261013/`（可恢复；脚本 `scripts/download_*.py` 与 `~/bin/A股量化-*` 包装保留，补数据时手动续跑即可）。
> 日常行情增量由常驻 `incremental-worker` 负责，历史下载任务无需常驻。

## 为什么 5 个任务不合并（2026-10-16 评估，结论：维持现状）

曾考虑把常驻/定时任务合并以减少数量，评估后认为当前拆分是**有意的故障域与主线隔离**，不是冗余：

- **两个常驻 KeepAlive 分属两条主线、时间正交**：`intraday-monitor`（外脑，盘中 9:30–15:00 实时行情）与 `incremental-worker`（量子，盘后 15:05 起批量下载）。架构契约（import-linter，见 pyproject）要求 `src.monitor` 与 `src.quant/src.execution` 互不依赖；若合并为一个进程，一边崩溃/卡死会被 KeepAlive 连带重启另一边，盘中实时监控的可用性被盘后下载拖累。
- **三个定时任务时间点与职责互不重叠**：`build-sector-map`（09:13 数据准备，开盘前必须完成）、`holdings-check`（9:45/13:05 推送持仓快照，跑完即退）、`daily-after-close`（18:30 推进模拟盘）。合成一个 plist 要在单脚本内部分别判断多个时刻、各自 try/catch，反而降低可读性与可独立重启性；launchd 对"一个 Label 一个 cron 时刻"本就是最自然的建模。
- **判据**：任务合并只在"同主线、同数据源、同时间窗、且一个失败不影响另一个 SLA"时才做；当前 5 个任务没有一对同时满足这四条，故维持 5 个。

## 盘前建映射任务为什么需要

`intraday-monitor` 启动首轮扫描需要"股票→所属板块"成分映射。该映射若等开盘后才现抓，
要逐板块串行请求数百次（含重试与节流），历史上拖到 09:46 才建好，造成 9:30–9:46 开盘盲区。
两道保险：

1. **扫描侧不阻塞**（`datasources.build_sector_map(background=True)`）：当日缓存缺失时，
   立即返回最近一个交易日的历史映射兜底盯盘，同时后台单飞（single-flight）构建当日映射，
   下一轮扫描自动切换。
2. **盘前预建**（本任务 09:13 `--build-map`，同步）：开盘前把当日映射写入缓存，
   开盘即读，不做任何网络抓取。

## 部署 / 管理

```bash
UID_NUM=$(id -u)
# 首次部署
cp scripts/launchd/com.astock-quant.build-sector-map.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/com.astock-quant.build-sector-map.plist

# 立即手动触发（验证）
launchctl kickstart gui/$UID_NUM/com.astock-quant.build-sector-map

# 查看 / 停用
launchctl list | grep astock
launchctl bootout gui/$UID_NUM/com.astock-quant.build-sector-map
```

### 持仓盯盘任务（holdings-check）

仓库模板 `com.astock-quant.holdings-check.plist` 里的 `WECOM_BOT_WEBHOOK` 是占位符
（`REPLACE_WITH_YOUR_WECOM_BOT_WEBHOOK`，**凭证不入库**）。首次部署时复制后先填入真实
webhook（可从本机 `~/Library/LaunchAgents/com.astock-quant.intraday-monitor.plist` 复制同一值），
再 bootstrap：

```bash
cp scripts/launchd/com.astock-quant.holdings-check.plist ~/Library/LaunchAgents/
# 编辑 ~/Library/LaunchAgents/com.astock-quant.holdings-check.plist 把占位符换成真实 webhook
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.astock-quant.holdings-check.plist
```

盘中监控红线：9:30–11:30 / 13:00–15:00 不重启 intraday-monitor；重启/部署只在午休或收盘后。
