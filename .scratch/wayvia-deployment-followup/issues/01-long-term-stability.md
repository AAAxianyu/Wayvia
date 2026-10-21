# 01 持续观察探针与自然断连

Status: in_progress
Priority: P1

## 当前证据

已读当前代次约 16 小时 21 分钟历史记录：阿里云 3708 轮、9 次降级，日本 3728 轮、2 次降级。只读诊断证明完整 HTTP 200/正确出口被 relay 关闭异常覆盖，已修复并部署；候选及正式部署后各 100 次业务探针通过。5 次历史 access-only 快速传输失败符合该机制，但无法逐次追溯证明。两端同时 HTTP/DNS 超时仍是真实未解问题，不能宣称全部断连解决。

## 前置条件

无需手机；需要两台已授权服务器的有效 SSH 访问。

## 下一步

保持只读正常观测，按明确 UTC 窗口收集修复后的 monitor 状态、各层失败和 completed-response-close 字段。下一次自然超时需同步记录入口/出口及独立路径证据，区分目标服务、跨境连接、VPN 与主机资源；不注入故障、不改保护阈值。独立供应商备用探针需先定义 TLS、DNS、响应格式和隐私边界，现有两个 IP 仍属同一供应商。

## 完成标准

取得明确时间范围和样本数的长期观察记录；若再发生异常，给出请求失败→保护→恢复的时间线和证据支持的原因。未复现必须标注未复现，不宣称初始故障根因已解决。

## 证据入口

- [本次服务器续查与修复](../../../docs/validation/server-followup-2026-10-20/README.md)
- [阶段报告](../../../docs/validation/stability-fix-2026-10-20/README.md)
- [结构化结果](../../../docs/validation/stability-fix-2026-10-20/summary.json)
- [实现与验收状态](../../../docs/implementation-status.md)

## Comments

2026-10-20：阶段收尾时建立，尚未执行本项续查。

2026-10-20：按用户指示开始不依赖手机的续查；故障与重启测试继续暂缓。

2026-10-20 续查：完成历史复核并修复一个已证明误判；保留 open 给真实超时根因与修复后长期观察。没有创建定时任务。

2026-10-20 长期观察：已启动独立的 24 小时正常采样，UTC 2026-10-21 03:09 至 10-22 03:09，heartbeat `wayvia-24` 每小时只读复核。75 秒预运行正常到期退出，跨境 RTT 约 39 ms、VPN/出口 HTTP/DNS 全通过；阿里云宿主 CF HTTPS 直连超时作为独立对照基线。真实超时根因仍未知，完整窗口未完成。见 [观察报告](../../../docs/validation/stability-observation-2026-10-20/README.md) 和 [接续参数](../../../docs/validation/stability-observation-2026-10-20/run.json)。故障和重启继续暂缓。

2026-10-21 03:48–03:59 UTC：Windows 实网诊断期间只读取证发现阿里云 44 轮中两次自然降级，日本 44/44 ACTIVE。access 为 HTTP_TLS_FAILED/curl 35、HTTP 0，未完成响应，区别于前次已修的 relay 关闭覆盖成功。DEGRADED→ACTIVE 17.20/12.95 秒；承载 TCP RTT 可达约 1.3 秒并伴重传，ICMP 仍约 40 ms，第二次保护前 VPN HTTPS 已超时、DNS 约 1.5 秒。继续排查承载排队/重传及负载关联，未证明具体限速点或唯一根因；不主动注入故障、不调整健康阈值。见 [Windows 诊断与事件窗口](../../../docs/validation/windows-campus-2026-10-20/README.md)。

2026-10-21 04:11 UTC heartbeat：两端采样运行，各新增 133 个样本，最大间隔约 5.002 秒；全窗口 journal 入口 235 轮含已知两次降级、出口 236 轮全部 ACTIVE。比上次已报告结果无新故障，VPN/日本每个 HTTP/DNS 对照新增 22 次均成功；宿主直连 CF 的原有超时保持基线分类。已补存已知两事件前后至少两分钟的两端脱敏采样，观察不延长、不重启。下一次比较入口为 [04:11 review](../../../docs/validation/stability-observation-2026-10-20/heartbeat-20261021T0411Z-review.json)，不重复告知用户已知事件。

2026-10-21 05:15 UTC heartbeat：两端各 1506 样本、单元运行，最大间隔约 5.004 秒；全窗口 journal 入口 473 轮仍只含原两次降级，出口 474 轮全部 ACTIVE。无新增生产失败，VPN/日本每端点新增 126 次对照成功。05:05:19 入口宿主直连 Google DNS 单次约 2 秒超时，下次定时查询恢复；同批 VPN/日本路径正常，已保存两端前后至少两分钟证据，按对照路径孤立失败记录，不能算新代理断连或归因校园网。继续原定观察，下一次比较 [05:15 review](../../../docs/validation/stability-observation-2026-10-20/heartbeat-20261021T0515Z-review.json)。
