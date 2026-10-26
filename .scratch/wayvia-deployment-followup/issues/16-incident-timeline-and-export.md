# 16 统一故障时间线与脱敏证据导出

Status: done
Scope: software_only; production_sampling_and_fault_acceptance_deferred
Priority: P2

## 缺口

现有 journal、事务状态、设备审计和临时诊断分散，调查依赖手工选窗口与关联。当前还不能一键交付具备版本、覆盖率和归因边界的故障证据。

## TODO 与验收

- [x] 为自然故障生成事件标识，串联两端请求失败、保护动作、服务停启及恢复；关联实际部署版本、配置 revision/generation 和维护标记。
- [x] 保存 UTC 与显式时区，支持按设备、时间、层级查询；终端身份和版本注明用户确认/实际采集来源，不能仅靠相同订阅名称认定物理设备。
- [x] 提供只读、字段白名单的诊断导出，含日志覆盖率、缺失窗口和完整性校验；证据不完整时保留 unknown。
- [x] 区分管理 SSH 失败、探针刻意结束的短连接、真实业务失败和维护重启；不将所有 TLS/reset 日志计成用户断连。
- [x] 导出不包含凭据、完整订阅、业务响应、原始计划或未授权终端日志；明确访问权限及保留期限。

## Comments

2026-10-25 America/Los_Angeles 软件交付：新增只读双端时间线、UTC/本地时区、角色/层/设备筛选，关联请求、保护、服务和恢复事实及维护/SSH/内部探针独立事件。设备来源、版本缺失与时钟偏差保持明确未知；ZIP 白名单导出含来源、覆盖率、缺口、摘要与有效期。终端 typed 事件仅在显式授权开关下导入，原始日志和凭据不入库。 参见[ADR 009](../../../docs/adr/009-rolling-diagnostics-and-incident-export.md)、[操作说明](../../../docs/diagnostic-evidence.md)和[验证记录](../../../docs/validation/rolling-incidents-2026-10-25/README.md)。done 仅表示本次软件范围完成；生产部署、实网采样及故障/重启测试继续暂缓。

2026-10-25 America/Los_Angeles：公共证据 v1 已随 15/13 落地，包含事件标识、时间/启动周期、角色/配置代次、源码摘要和显式未知的时钟信息。仅完成字段基础；事件关联、查询、导出和双端完整性仍待实现。见 [ADR 008](../../../docs/adr/008-observation-evidence-and-lifecycle.md)。

2026-10-25 America/Los_Angeles：本次手工事件报告可作为输出范例，但不是已实现的产品能力。仅登记，未启动开发。
