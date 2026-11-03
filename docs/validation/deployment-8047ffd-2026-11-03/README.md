# 保留数据的生产升级：8047ffd（2026-11-03 UTC）

用户要求把所有分支合并为 `main`、本地与远端只保留 `main`，并把 `main` 部署到两台服务器。

## 分支整理

`origin/main`（`0e5bbbc`）是 `codex/probe-observer-evidence`（`0873d61`）与 `codex/congestion-resilience`（`8047ffd`）的祖先，三者为一条直线。`main` 快进到 `8047ffd` 并推送，没有合并提交或冲突；随后删除本地两个 `codex/` 分支和远端 `codex/probe-observer-evidence`。两台服务器代码检出原为只跟踪已删除分支的单分支克隆，改为跟踪 `main` 并删除本机旧分支。

## 部署

按 [保留数据的滚动升级工具](../../operations.md) 的流程，exit → ingress：

1. 服务器 `/opt/wayvia/repository` 切换到 `origin/main` 精确提交 `8047ffd`，工作区干净。
2. 上传本地由同一源码构建的 wheel，两端 SHA256 `d3e8f8d7723e0361b29e77ae07a733aaa7b08e36b7383c4087edfcf67778bd05`；组件使用 10-20 部署时已校验的锁定归档，`uv` 为 `/opt/wayvia/uv-bootstrap-0.9.7/bin/uv`。
3. `deploy/upgrade.py` 在受限 systemd 临时作业中运行（MemoryHigh 640M、MemoryMax 768M、CPUQuota 100%、RuntimeMaxSec 600）：先预检（`prepared`），再 `--apply`。

| 角色 | 维护时段（UTC） | 结果 | 存储 | 备份 |
| --- | --- | --- | --- | --- |
| 日本出口 | 02:14:48–02:15:42 | deployed，ACTIVE | schema 4，原数据保留 | `/var/backups/wayvia/upgrade-8047ffd37a65-exit` |
| 阿里云入口 | 02:16:34–02:17:21 | deployed，ACTIVE（同一代次） | schema 4，原数据保留 | `/var/backups/wayvia/upgrade-8047ffd37a65-ingress` |

两端均报告未启用新观察器或通知、未改全局路由/防火墙、未重启主机。维护时段应从自然故障与性能归因中排除。

## 升级后核对

- 日本：agent、API、exit、vpn-server、monitor.timer 均 active；monitor 已输出紧凑摘要，60 秒内只写 1 条健康事件（心跳逻辑生效）；`ens17` 仍为默认 `fq_codel`，**整形未启用**。
- 阿里云：agent、API、ingress、bridge、carrier、vpn-client、usage、monitor.timer、Nginx 均 active；monitor 七层全部 healthy（access 0.44 s、carrier 0.44 s、vpn 0.26 s、egress 0.44 s、dns 0.08 s），即设备 → 阿里云 → 日本 → 外网整条受保护路径可用。
- 数据：3 个设备记录（2 个 active）、5 个订阅记录，与 10-26 记录一致；分钟流量历史继续写入。
- 同机其他业务（miaoban API/worker/PostgreSQL）保持运行，未受影响。

## 本次生效与未生效

- 生效（随软件）：ADR 014 的失败分类与连续确认、承载进展判定、同一次降级中前两次立即重建且此后失败才退避、monitor 紧凑输出与健康心跳、诊断批量写入与日志库分批清理。
- 未生效（需配置与用户决定）：日本出口整形与隧道公平队列（`shaping`）、`policy.proxy_domains`、journald 上限、日志/管理平台与持续诊断采集器。

门禁见[报告](../../congestion-resilience-report.md)：`make check` 629 项、`make demo`、`make test-configs` 11 项、`make test-lab` 整体退出 0（事务 23/23）。没有在生产机运行软件回归。
