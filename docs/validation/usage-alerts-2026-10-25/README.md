# 17/18 设备历史与观测告警软件验证

日期：2026-10-25 America/Los_Angeles（执行日志 2026-10-26 UTC）。分支 `codex/probe-observer-evidence`，集成基线 `633daf3`。

用户授权仅为仓库开发和软件验证。生产部署、实网采样和故障/恢复/维护/重启测试继续暂缓；本轮未启动旧观察、heartbeat、automation 或通知作业。没有访问生产主机或改变开发机路由、防火墙及 VPN。

## 先确认统计基线

`codex/device-usage-20261021` 的 `08695f2110662437c9b28a64763eae5c7a4fc2cc` 基于共同祖先 `20e33ab860c3a1ef7421093f7f785211355631c1`，缺少当前 13–16。精确 cherry-pick 这一提交为 `633daf3174deffd25f6470551a10b962c333e4fa`；当前诊断实现保留，唯一文档冲突保留两边历史。开始新功能前 37 项统计/诊断回归通过。可核对 [baseline.json](baseline.json) 与该集成提交。

该提交带来的 `device-usage-2026-10-21` 和 compatibility 部署段均为此前证据，不是本次重新执行或部署。

## 门禁

最终结果与版本/源码摘要见 [summary.json](summary.json)。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `make check` | 退出 0；492 项通过、3 项既有弃用警告，格式/静态检查/构建/CLI 通过 | [check.log](check.log) |
| `make demo` | 退出 0，simulated 控制面演示 | [demo.log](demo.log) |
| `make test-configs` | 退出 0，10 项真实组件配置检查 | [config-result.json](config-result.json)、[日志](test-configs.log) |
| 离线历史/告警例子 | 退出 0，未开始采样、发送通知 0 次，源库摘要不变 | [结果](example-private/result.json)、[日志](offline-demo.log) |
| `make test-lab` | **未执行：用户继续暂缓故障/恢复测试**；exit_code=null | 不记通过或环境缺失 |

配置门禁使用明确 disposable、`--network none` Linux 容器；附带 namespace/guard 前置预检，未执行完整链路故障/恢复实验。OpenVPN 配置启动结果 `tunnel_established=false` 不改写成真实建隧道验收。缺失实验环境本应记实际非零；本次配置环境可用，而完整故障实验是用户明确暂缓。

uv 使用已有 `/Users/dev2/Documents/project/Wayvia/.cache/progress-check-tools/bin/uv`，通过 make 的 `UV=` 指定。归档日志只规范行尾空白/末尾空行，不改变退出结果。最终文档完成后另做打包，见 [final-build.log](final-build.log)。

## 有意义的验证

- `tests/integration/test_usage_history.py`：分钟与小时、初始快照/未分配流量、跨分钟覆盖、单侧/双侧计数器归零、重启/断采、乱序/重复、独立设备 ID、轮换/撤销、保留期限及幂等清理、旧迁移不回填；订阅计数与实体在线分开。管理查询/导出要求鉴权，public-only 无路由，原始 UA/目标/正文/令牌不进入结果。
- `tests/integration/test_observability_alerts.py`：正常无通知、共同 incident 双端合并、持久去重/水位、缺源不能恢复、短暂故障及恢复不丢失、持续故障跨有界窗口保持一个告警事件；停止/原截止优先、异常终止、源绑定/权限/坏输入、采样/日志空洞、心跳、存储与轮转、证据到期跟踪、时钟未知/失步估计。通知覆盖私有文件幂等、失败后相同 ID 重试、禁止重定向、满队列回滚、过期不重放和显式清理。
- HTTPS 仅 `httpx.MockTransport`，没有真实 DNS/HTTP 通知。测试中的缺盘、不可读源、异常终止、计数器归零是临时文件/对象夹具，不是生产或隔离链路故障注入。chrony 只用官方格式夹具，不实际查询或校时。
- `tests/unit/test_telemetry.py` 与 `test_transactions.py`：监控完整健康事实和快照原时间；不会为重读的旧检查制造新鲜时间。旧 v1 无偏移扩展字段的记录重投影/重导入保持幂等；原保护、凭据、发布、备用与生命周期回归继续纳入全量门禁。
- `scripts/demo_usage_alerts.py` 仅生成临时合成库和导出。例子中可定位上传增量 70 字节，初始/归零/缺测量另列，缺失分钟为 unknown；正常 pending=0、降级=1、重复仍为 1、恢复=2，投递为 0。

## 可审阅结果与限制

[usage.json](example-private/usage.json)、[降级](example-private/opened-alert.json)、[恢复](example-private/recovered-alert.json) 都标注 simulated；source/reference 与指标是夹具，不含真实设备身份、主机地址或秘密。生成时是 0600/0700，Git 不保留这些模式，重新生成可检查实际权限。生成器拒绝覆盖旧目录。

设计见 [ADR 010](../../adr/010-device-history-and-observation-alerts.md)，命令/告警处理见[操作说明](../../usage-history-and-alerts.md)。SQLite 与轮转上限、源只读、通知语义和鉴权属于软件结论。实际采集开销、历史数据规模、双端时钟精度、独立调度/真实通知可靠性及生产故障行为均未验收；JXY 的最初超时根因没有新结论。软件完成不表示新能力已在生产启用。
