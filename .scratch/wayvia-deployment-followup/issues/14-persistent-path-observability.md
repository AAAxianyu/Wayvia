# 14 持续低开销的双端路径诊断

Status: done
Scope: software_only; production_sampling_and_fault_acceptance_deferred
Priority: P1

## 缺口

已有有限观察脚本，但专项窗口结束后，生产日志不能回溯故障瞬间的双端 TCP/接口/主机状态。10 月 26 日事件缺少同期数据，十分钟资源平均值不足以判断秒级排队或重传。

## TODO 与验收

- [x] 设计可显式启停、容量有界的诊断模式，持续记录承载 RTT/重传、Send-Q/Recv-Q、接收窗口、接口吞吐及 CPU/内存/I/O 压力；只采集已授权路径。
- [x] 使用有限滚动缓冲，在自然异常时固化两端前后至少两分钟；记录时间同步、版本、代次、缺测、采样频率和自身资源开销。
- [x] 保留文件轮转、磁盘预算和过期删除策略；诊断失效不得修改生产健康判定或阻断/放行业务。
- [x] 明确常态诊断与有截止时间的观察任务的区别，不自动重开旧窗口；与 13 的生命周期规则一致。

## Comments

2026-10-25 America/Los_Angeles 软件交付：新增有原期限的被动持续模式，指定 peer/port 与接口，容量有界的 SQLite 滚动缓冲、双端同 ID 触发文件通道、前后至少两分钟固定窗口、过期清理、缺测及采样负担。迟到请求可前移起点，恢复后窗回传，长事件拆分不缩短既有窗口。未配置对端通道明确 unavailable，软件例子使用合成双端记录。 参见[ADR 009](../../../docs/adr/009-rolling-diagnostics-and-incident-export.md)、[操作说明](../../../docs/diagnostic-evidence.md)和[验证记录](../../../docs/validation/rolling-incidents-2026-10-25/README.md)。done 仅表示本次软件范围完成；生产部署、实网采样及故障/重启测试继续暂缓。

2026-10-25 America/Los_Angeles：用户要求停止根因排查并记录 TODO。本项未实施，不构成启动常驻采样的授权。见[功能总览](../TODO.md)。
