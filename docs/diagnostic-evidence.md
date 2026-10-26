# 持续诊断与故障证据操作

本页是新软件能力的运行说明，不表示已经部署或启动。当前生产部署、实网采样和故障测试继续暂缓。

## 查询与导出已有证据

在拥有文件权限的管理员本地执行，`--source` 可重复指定两端经可信渠道取得的私有数据库副本。数据库应命名为 diagnostics.db，目录 0700、文件 0600，源副本也须满足所有者和链接检查。保留必要 SQLite 一致性：从运行数据库取得副本时使用 SQLite backup API 等一致快照，不把复制半个事务当成完整证据。

```sh
wayvia diagnostics timeline --source /private/ali/diagnostics.db \
  --source /private/jp/diagnostics.db --incident INCIDENT_ID \
  --timezone America/Los_Angeles

wayvia diagnostics timeline --source /private/ali/diagnostics.db \
  --since 2026-10-26T00:20:00Z --until 2026-10-26T00:35:00Z \
  --layer egress --role ingress

wayvia diagnostics export --source /private/ali/diagnostics.db \
  --source /private/jp/diagnostics.db --incident INCIDENT_ID \
  --timezone America/Los_Angeles --output /private/export/incident.zip

wayvia diagnostics verify /private/export/incident.zip
```

共享基础设施事件没有设备归属。`--device DEVICE_ID` 只筛选明确归属的账号事件；身份来源和客户端版本缺失时仍为 unknown，不据此宣布设备在线/离线或用户断流。日志覆盖率、各来源 ok 槽位、缺测区间和 clock_alignment 一起阅读；采样齐全不表示业务一直健康。

## 为未来获授权的两端采集做配置

collector 在 Linux 上以 root 运行，只查询指定 carrier peer/port、接口、固定服务 journal 和可选 audit 数据库。它使用自己的 `/var/lib/wayvia/diagnostics/RUN_ID/`；继承原 run 截止时间和停止规则。以下均为模板，不能直接用于未指定主机。

```sh
wayvia diagnostics collect --run-id NEW_RUN_ID --until UTC_DEADLINE \
  --role ingress --generation CURRENT_INGRESS_GENERATION \
  --peer AUTHORIZED_CARRIER_IP --port ACTUAL_CARRIER_PORT \
  --interface ACTUAL_INTERFACE --path-id SHARED_PATH_ID \
  --trigger-inbox /private/ingress-in --trigger-outbox /private/ingress-out

wayvia diagnostics collect --run-id OTHER_NEW_RUN_ID --until UTC_DEADLINE \
  --role exit --generation CURRENT_EXIT_GENERATION \
  --peer AUTHORIZED_INGRESS_IP --port ACTUAL_CARRIER_PORT \
  --interface ACTUAL_INTERFACE --path-id SHARED_PATH_ID \
  --trigger-inbox /private/exit-in --trigger-outbox /private/exit-out
```

两端须为相同配置 revision 和 path_id，generation 各自独立。inbox/outbox 由管理员预先创建为专用 0700 目录；明确授权的管理传输将本端 outbox 同步到对端 inbox，保持 0600 文件及所有权。软件不配置该传输、不自动联系远端。通道缺失、坏文件、范围不符或来不及时，报告显示缺测，不能声称两端共同覆盖。

可用 `--policy-file PRIVATE_JSON` 指定字段；默认 interval_seconds=5、buffer_seconds=900、pre_seconds=120、post_seconds=120、retention_seconds=604800、max_bytes=134217728、max_incidents=32。`--audit-db /var/lib/wayvia/control/metadata.db` 是明确的只读审计输入，不会读取其中配置、凭据或 token 字段。策略和输入摘要写入不可变 contract，续接不能改容量、频率、配置、代次、通道或截止时间。

`--resume-interrupted` 是显式续接开关，可在获授权启动命令中设置。永久停止使用下列命令持久化意图；仅发送 SIGTERM/SIGINT 会记录为来源未知的 interruption，启用续接的任务仍可在原期限内恢复。旧专项观察目录及 terminal run 均不会被新模式打开。

```sh
wayvia diagnostics stop RUN_ID
```

任务到期、用户停止、代次变化或持久化错误会结束采集。旧窗口不自动延长。需要新授权窗口时选择新 run_id，不能沿用已结束 run。

## 触发、导入与保留

```sh
wayvia diagnostics triggers /private/ali/diagnostics.db
wayvia diagnostics import-trigger /private/jp/diagnostics.db /private/trigger.json
wayvia diagnostics prune /private/ali/diagnostics.db
```

触发输出是 JSON 列表；import-trigger 接受其中**单个对象**的私有 JSON 文件。它会固定仍可取得的前窗并等待已有 collector 继续记录后窗；命令本身不会启动采样。使用已结束数据库时，无法取得的后窗保持缺失。

管理员已获授权的终端证据须先转为 typed JSONL：保留 UTC observed_at、稳定 event_id、type=event、受支持 kind/source、必要设备 ID、identity_source 及有限版本字段。原始正文、URL、令牌、密码和任意 message 字段不会入库。

```sh
wayvia diagnostics ingest /private/ali/diagnostics.db /private/client-events.jsonl \
  --authorize-client-logs
```

查询与导出只读。prune 只处理该诊断库的滚动/已过期记录；不会恢复任务或删除独立导出 ZIP。导出默认有效期 7 天，可用 `--retain-seconds` 在允许范围内指定；到期后 verify 拒绝，文件由管理员按保留政策清理。

## 无网络的软件例子

```sh
uv run --locked python scripts/demo_diagnostics.py --output /private/offline-example
```

该命令生成合成的双端样本、一次入口失败/恢复和私有 ZIP；输出明确标为 simulated。它运行真实存储/窗口/查询/导出代码，完全不调用 Linux sampler、journalctl 或任何网络探针，不是实网验收。可核对[本轮验证记录](validation/rolling-incidents-2026-10-25/README.md)。
