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
| `com.astock-quant.download-data` | 保活·一次性 | 历史下载完成即退出 | 量子：全市场历史行情下载（断点续传） | 已下完，待停用归档 |
| `com.astock-quant.download-monitor` | 保活·一次性 | 每 15 分钟巡检 | download-data 的看门狗 | 随上者停用 |
| `com.astock-quant.fundamentals` | 保活·一次性 | 基本面下载完成即退出 | 量子：中证800 基本面（ROE/成长/分红） | 下完停用 |
| `com.astock-quant.open-check` | 定时（写死 2026-09-28） | 一次性 | 9-28 开盘检查 | **已退役**，被 build-sector-map 替换 |

> 长期保留 4 个；三个一次性下载任务在历史/基本面补齐后 `bootout` 停用、plist 归档保留（可恢复）。

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

盘中监控红线：9:30–11:30 / 13:00–15:00 不重启 intraday-monitor；重启/部署只在午休或收盘后。
