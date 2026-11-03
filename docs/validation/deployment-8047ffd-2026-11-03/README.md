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

## 2026-11-03 02:22 UTC 日本出口启用整形

用户确认接受吞吐下降 14–19%，按推荐参数启用 [ADR 014](../../adr/014-congestion-aware-health-and-exit-shaping.md) 的出口整形。

步骤：备份 `/var/lib/wayvia/control/config.yaml` 与 `exit-plan.json`（后缀 `.pre-shaping-20261103T022211Z`）→ 在出口配置追加 `shaping: {egress_kbit: 1800, carrier_kbit: 1600, tunnel_kbit: 1450, notsent_lowat: 131072}` → 以 `wayvia` 身份 `plan --role exit --exit-interface ens17` → `apply`（02:22:33，进入 AWAITING_CONFIRM，120 秒自动回退定时器）→ 核对 → `confirm`（02:23:12，ACTIVE，新代次 `c310db1c826e476da2294562d6b2b950`）。

核对：

- `ens17`：HTB 根、`1:10` 承载类 1600 kbit（可借到 1800）、`1:20` 其他出站 200 kbit（可借到 1800），两个 `fq_codel` 叶子；启用后数分钟内 `1:10` 已发送 153,688 字节、`1:20` 91,557 字节，无丢弃，即真实承载流量按端口 443 正确分类。
- `tcp_notsent_lowat` 131072，Wayvia 状态文件记录原值 4294967295 供回退。
- `tun0`：计划先整形后重启 OpenVPN，重启重建 tun0 使其回到默认 `fq_codel`；确认后下一轮 monitor 记录 `shaping_restored` 并恢复为 HTB 1450 kbit + `fq_codel`。等待确认期间 monitor 不处理该代次，因此 tun0 公平队列在 02:22:33–约 02:23:30 之间缺失。
- 出口七层检查 healthy；阿里云入口 monitor 七层 healthy。出口重启期间入口只记录 1 次 `health_congested`，**没有降级、保护或整链重建**——在原规则下这次出口重启会再触发一次全体断线。

配置 revision：出口配置加入 `shaping` 后 revision 变化，入口配置未改。已发布的订阅与设备管理不受影响；但下次需要 `config publish` 时，两端 revision 必须一致，应先在入口配置加入同样的 `shaping` 段（入口渲染会忽略它）并重新 plan/apply 入口。

回退：恢复备份的 `config.yaml`（或删除 `shaping` 段）后重新 plan/apply/confirm 出口；`shaping` 动作会删除 Wayvia 的 HTB 并恢复原 `tcp_notsent_lowat`。

吞吐与稳定性效果需要在生产负载下观察：建议对比启用前后同时段的降级次数、`health_congested` 次数与设备分钟流量。
