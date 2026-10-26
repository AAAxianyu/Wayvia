# 设备历史统计与可操作告警

本说明对应待办 17/18 的软件实现，未启用生产采集、调度或通知。统计集成基线与语义见 [ADR 010](adr/010-device-history-and-observation-alerts.md)，诊断时间线/租期仍见[14/16 操作说明](diagnostic-evidence.md)。以下命令只供后续明确授权后使用；本次验证全部采用临时合成数据。

## 历史查询

既有 `wayvia device usage` 仍返回近期连接和累计代理负载。历史默认 5 秒采样，分钟 30 天、小时 365 天；迁移不会把累计流量回填到过去分钟。

```sh
wayvia --system device history DEVICE_ID --since UNIX_SECONDS --until UNIX_SECONDS --grain minute
wayvia --system device history DEVICE_ID --since UNIX_SECONDS --until UNIX_SECONDS --grain hour --output /PRIVATE_NEW_DIRECTORY/usage.json
wayvia --system device history-prune
```

`since`/`until` 为 UTC Unix 秒，返回半开区间所涉及的完整桶，并给出原查询/桶窗口及边界标记。最多 10000 桶和 10000 条客户端声明，过长区间需缩短或选小时。`unallocated_*` 是起点未知的累计核对量，不能当作所在分钟吞吐；连续增量按采样结束桶归属，跨桶有标记。`covered_seconds`/`coverage_fraction` 表示采样连续性，不是连接完整度或用户在线概率。缺测桶为 unknown/null，采样失败后不判断离线。历史 `last_used_at` 仅来自可定位的流量增量。

`subscription_fetches` 与 `subscription_client_claims` 单独展示；不含原始头、IP、token，声明不能认证实体设备或客户端。每分钟每家族只留最后版本、合计次数；客户端详情 30 天后到期，小时拉取计数保留 365 天。原生累计统计的口径仍是凭据活动和尽力计数的代理 payload；不含国内直连或承载开销，不用于计费。

管理 API（已有 Bearer 鉴权）：

- `GET /api/v1/devices/DEVICE_ID/history?since=...&until=...&grain=minute`
- `GET /api/v1/devices/DEVICE_ID/history/export?since=...&until=...&grain=hour`

导出响应禁止缓存；公开 subscriptions-only 监听器没有这些路由。CLI 导出新建 0600 文件、要求 0700 父目录，拒绝覆盖和符号链接。查询对已过期明细返回 expired；物理删除由写入或 `history-prune` 执行，停止采集不会凭空触发后台作业。

## 独立评估

私有 JSON 配置必须 0600，父目录 0700。固定指向两个原始 run 和其 `rolling/diagnostics.db`；缺失源也须声明。示意配置中的路径必须换成明确指定的证据位置，不据此连接服务器或恢复旧观察：

```json
{
  "schema_version": 1,
  "watches": [{
    "id": "declared-pair",
    "path_id": "declared-pair",
    "sources": [
      {"role": "ingress", "database": "/PRIVATE/ingress/rolling/diagnostics.db", "run_directory": "/PRIVATE/ingress"},
      {"role": "exit", "database": "/PRIVATE/exit/rolling/diagnostics.db", "run_directory": "/PRIVATE/exit"}
    ],
    "window_seconds": 300,
    "stale_seconds": 20,
    "evidence_fresh_seconds": 120,
    "min_coverage": 0.8,
    "min_free_bytes": 67108864,
    "require_clock_offset": false,
    "max_clock_offset_ms": 100,
    "max_offset_spread_ms": 250,
    "required_sources": ["tcp", "interfaces", "host", "clock", "journal"]
  }]
}
```

这些阈值只决定告警展示，不修改健康判定或恢复策略。可选 `usage_database` 指向同机私有 control.db，只读检查统计心跳，不执行迁移。audit/peer 默认为可选；若该部署承诺这些来源，可加入 required_sources。每个 watch 必须配置两个角色，共最多 16 个 watch。改变 watch 合约须使用新状态库，不能静默改变旧告警范围。

```sh
wayvia alerts evaluate /PRIVATE/config.json /PRIVATE/alerts/alerts.db
wayvia alerts list /PRIVATE/alerts/alerts.db
wayvia alerts list /PRIVATE/alerts/alerts.db --output /PRIVATE_NEW_EXPORT/alerts.json
wayvia alerts watchdog /PRIVATE/alerts/alerts.db --max-age 60
```

评估只读已声明证据，单次退出；不发送通知，也不创建任何后台工作。常态没有新消息；同一事件/原因保持去重，最新列表仍更新覆盖率与证据年龄。健康和完整性分别显示。已停止/到期的 run 不发新完整性告警，已知健康异常不会因为没有样本就“恢复”。watchdog 心跳陈旧、未来时间或缺失返回非零；明确创建的独立监督者才能在评估停止时检查它，本次没有安装监督者。

| 原因 | 后续操作 |
| --- | --- |
| `fresh_degraded_or_recovery_failed` | 按 incident ID 查看诊断时间线，核对保护和完整健康/恢复事实；业务影响另需终端证据 |
| `collector_unexpected_stop` / `collector_heartbeat_stale` | 先核对原 lease、state/result/stop，再查采集进程和持久化结果；不自动恢复结束任务 |
| `sample_coverage_low` / `source_gap_journal` 等 | 查看缺失时段/来源；核对 journald 保留及读取权限，不把日志空洞解释为业务空闲 |
| `health_evidence_expired` / `usage_observation_stale` | 核对独立 monitor/usage 收集是否仍在承诺时窗内运行，不推断设备离线 |
| `local_evidence_disk_low` / `evidence_quota_near_limit` / `evidence_rotation_overdue` | 检查本地证据卷空间、私有目录权限和 TTL/页预算；保留未过期事件，按既有策略清理 |
| `evidence_persistence_failed` / `evidence_source_unreadable` | 核对 state 原因、权限/源绑定、SQLite 错误；源不可读期间完整性与业务状态分别未知 |
| `incident_evidence_expired` | 证据超过原保留期；已清理的证据不能凭空恢复，核对是否有此前授权导出 |
| `clock_*` / `reported_ntp_offset_spread_excessive` | 核对时间同步和测量来源；偏移差是报告估计值，不是精确跨机误差 |

事件里有 role/run/revision/generation 和 incident ID、时间窗、coverage 与 uncertainty；用配置里的对应 source 路径运行 `wayvia diagnostics timeline --source ... --source ... --incident INCIDENT_ID` 查看证据。没有 incident 的完整性事件用给出的时窗查询。时间相邻不证明共同根因；管理 SSH、探针传输错误也不等于业务故障。

偏移测量默认关闭。后续新建且获授权的诊断 lease 可显式加 `--measure-clock-offset`；只读已有 chrony tracking，不安装或校时。旧证据/无 daemon/未知格式保持 null，require_clock_offset=true 可将缺测提升为完整性提醒。更改该输入不能复用旧 lease。

## 投递是独立显式动作

配置省略 channel 时不能发送。可设 `{"kind":"directory","destination":"/PRIVATE_NEW_CHANNEL"}`，或 `{"kind":"https","destination":"https://EXPLICIT_RECEIVER/PATH","authorization_file":"/PRIVATE/channel-secret.json"}`。秘密文件 0600，只含 `bearer_token`；URL 不允许内嵌认证、query 或 fragment。这里没有真实渠道配置。

```sh
wayvia alerts deliver /PRIVATE/config.json /PRIVATE/alerts/alerts.db --authorize-send
```

每次最多 8 条、每条一次；HTTP 超时有界且不跟随重定向、不使用环境代理，不保留远端响应。失败返回非零并保留同一 event ID；显式重试，接收方按 event ID 去重。目录采用每 event ID 一个独占私有 JSON，同内容可幂等重试。至少一次语义不承诺崩溃场景恰好一次。

队列/状态满或磁盘持久化失败返回固定非零错误，不丢弃新鲜待发记录；外部监督需要检查退出码和心跳。投递 TTL 7 天，过期消息不重发，列表保留 `outbox_expired`，投递返回非零；审阅/导出后可显式执行 `wayvia alerts discard-expired /PRIVATE/alerts/alerts.db` 清除过期待发项。它不清除新鲜待发项，也不启动采集。

## 离线验证

`uv run --locked python scripts/demo_usage_alerts.py --output NEW_PRIVATE_DIRECTORY` 生成合成历史、去重降级/恢复和结果 JSON。不会调用 sampler、SSH、真实原生 API、服务控制或通知渠道。见[本次验证](validation/usage-alerts-2026-10-25/README.md)。生产部署、实网采样和故障/维护/重启验收继续暂缓。
