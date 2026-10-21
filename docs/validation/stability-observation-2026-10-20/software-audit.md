# Wayvia 软件侧排查与阶段交接（2026-10-21 UTC）

用户要求不联系平台、检查 Wayvia 自身，随后要求阶段性收尾。本轮没有提交工单、发送外部消息、修改生产软件或网络、人工重启服务、制造故障或操作终端。主动排查到此暂停，原有 24 小时只读观察按既定期限继续。

## 已确认的软件行为及影响

1. **一次探针失败会扩大为整链恢复。** `TransactionManager.monitor()` 将任意必需层失败转为 DEGRADED，`LinuxExecutor.protect()` 停止入口；下一轮 `_rebuild()` 重放完整计划，依次重启 carrier、VPN、bridge、ingress，没有按失败层缩小恢复范围。既有连接会随进程退出中断。代码与五次自然事件的明确 systemd 单元记录一致。此处解释后续断连扩大，不解释最早的请求失败。
2. **curl 35 被统一归类为终止性 TLS 错误。** 四次生产失败分别只执行主端点一次，尝试耗时 5028、5024、5061、5027 ms，均 HTTP 0 / curl 35，配置中的备用端点没有执行。curl 35 也可能是握手途中 EOF/传输中断，不能据此断言证书错误。历史探针没有保留这类细分证据，无法追溯证明四次都是相同错误。
3. **内部连接预算与外层探针预算不同。** 生产入口 SOCKS 和 bridge direct 出站均未显式指定 `connect_timeout`；锁定 sing-box 1.14.3 的默认 TCP 拨号预算为 5 秒。Wayvia HTTP 探针总预算 12 秒，扣除外层余量后两个端点各约 5.75 秒。约 5 秒失败与内部拨号期限吻合，但本轮限定日志没有取到对应目标的拨号错误，仍只是待验证解释，不能写成已证实根因。未调整任何期限。
4. **业务和业务探针共用同一条 OpenVPN TCP 承载。** 09:06/09:09 的既有证据显示自然带载时承载 TCP RTT/重传上升，而 ICMP、日本宿主对照正常。共享承载可使探针与业务一起等待；TCP 多层承载的具体贡献尚未通过对照量化。08:27/08:28 日本宿主 HTTP/DNS 与跨境 ICMP 同时异常，不能由 Wayvia 的探针分类或恢复动作单独解释。

源码摘要核对确认两端已安装 `http_probe.py`、`probe_runtime.py`、`transactions.py` 与修改前审查源码一致。配置只提取类型、标签、超时和绑定接口，日志只输出固定分类及生命周期时间，未导出计划、凭据、业务目标或正文。见[入口核查](software-audit-timeouts-ali.json)、[日本核查](software-audit-timeouts-jp.json)与[只读采集源码](inspect-software-timeouts.py)。

| 事件 UTC | 入口停止至下一次启动记录 | DEGRADED 至 ACTIVE |
| --- | ---: | ---: |
| 03:52 | 14.695 秒 | 17.199 秒 |
| 03:58–03:59 | 11.415 秒 | 12.948 秒 |
| 08:27 | 10.257 秒 | 12.090 秒 |
| 08:28 | 12.538 秒 | 14.162 秒 |
| 09:09 | 7.079 秒 | 8.865 秒 |

systemd 的 started 仅表示进程启动，不等于业务已可用；这些时间不能当作完整用户断流时长，也不能全算作可消除的额外延迟。原始失败早于保护。逐项关联见[恢复时间线](software-recovery-timeline.json)。

## 候选修复与验证边界

本地 `http_probe.py` 仅对 curl 35 中明确的 OpenSSL `SSL_ERROR_SYSCALL` / `unexpected eof while reading` 传输中断标记允许尝试已有备用端点。若同时出现证书、x509、verify 或 TLS alert 标记，则继续终止；未知错误也保持原行为。输出固定 `HTTP_TLS_TRANSPORT_FAILED`，不记录 stderr。新响应仍必须通过 TLS 校验、HTTP 200、新鲜出口身份及原总期限。证书错误、错误出口、无响应、总预算用尽都不能被当成健康；没有不安全 TLS 重试或缓存成功。

这是对 [ADR 006](../../adr/006-bounded-egress-probe-redundancy.md) 原先笼统 curl 35 分类的窄化修正，未改整链恢复策略或任何生产阈值。修改前专项 34 项中 5 项按预期暴露旧行为；修改后专项 58 项通过，随后补充总预算耗尽回归并纳入全量检查。最终 `make check` 376 项通过（3 项既有警告）、`make demo` 通过（simulated）、`make test-configs` 10 项真实核心配置通过。配置检查使用已有一次性 Linux 容器及其预检，不是完整故障/恢复实验。[本轮门禁](software-audit-gates.json)和[配置结果](software-audit-config-result.json)保留证据；软件通过不能代替实网接受度。

**候选尚未部署，不能宣称真实断连已修复。** 当前没有运行隔离故障/恢复实验或生产故障/重启测试，均按用户指示暂缓。没有修改远端长期观察脚本；本地观察器只增加新错误码白名单供将来部署使用。

## 下次接续

先读[待办 12](../../../.scratch/wayvia-deployment-followup/issues/12-software-timeout-recovery.md)。核查候选代码和门禁，保留证书及出口身份失败的终止语义；补足明确的分阶段失败证据，再评估候选在自然流量下的效果。不要根据历史 curl 35 猜测底层错误。

整链恢复另行设计按失败层恢复的方案，需要保留独立 fail-closed 保护、凭据撤销及显式阻断，不能简单删除 `protect()` 或跳过健康校验。该行为尚未修改，故障与重启测试未恢复授权。

不再以供应商答复作为软件侧工作的前置条件，也不联系平台。现有低流量共同异常的最初原因仍未知；完整观察到期后按原流程收尾，未证明的原因继续标未解决。

技术依据：[sing-box 1.14.3 超时常量](https://github.com/SagerNet/sing-box/blob/v1.14.3/constant/timeout.go)、[默认拨号器](https://github.com/SagerNet/sing-box/blob/v1.14.3/common/dialer/default.go)、[curl 错误码说明](https://curl.se/libcurl/c/libcurl-errors.html)。
