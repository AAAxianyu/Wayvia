# 15 请求分阶段诊断

Status: done
Priority: P1
Scope: software_only; production_and_fault_acceptance_deferred

## 缺口

当前 HTTP 探针已有端点序号、总耗时、curl/HTTP 状态和错误分类，但 subprocess 超时可能只剩 HTTP_TIMEOUT 与空状态，无法区分连接、TLS、首字节或接收阶段。内部中继关闭与请求结果也必须独立记录。

## TODO 与验收

- [x] 在原总预算内记录连接、TLS、首字节、总耗时、响应完成状态和有限错误分类；无相应阶段记录时标 unknown，不推断成功。
- [x] 区分 curl 自身超时、外层进程期限、连接关闭、证书/身份失败和中继清理错误；在安全的范围内保留超时前已经取得的阶段数据。
- [x] 不输出 stderr 原文、目标 URL、凭据、原始计划或响应体，不能因新增日志改变身份验证、备用策略及 fail-closed。
- [x] 验证记录准确性、超时预算及脱敏；生产验收与软件回归分别记录，不凭错误名称认定具体网络节点。

## Comments

2026-10-25 America/Los_Angeles 软件交付：统一证据 v1，增加 curl 与中继分阶段时间、有限失败类别、curl/进程/helper/整轮期限区分，超时已有 write-out 尽可能保留，缺失为 unknown。请求结果与清理错误分开；身份、备用条件与 fail-closed 不放宽。硬终止前未输出的 curl 进度不可恢复，已明确记录限制。 软件回归与配置校验见[验证记录](../../../docs/validation/probe-observer-evidence-2026-10-25/README.md)，契约见[ADR 008](../../../docs/adr/008-observation-evidence-and-lifecycle.md)。本票 done 仅表示本次授权的软件范围完成；生产、维护重启及故障验收继续暂缓。

2026-10-25 America/Los_Angeles：从 12 的观测缺口拆出独立实现票；12 继续负责恢复范围。仅登记，未修改或部署探针。
