# ADR 008：统一证据字段、请求阶段与观察器生命周期

日期：2026-10-25 America/Los_Angeles。状态：软件实现；生产部署、实网采样和故障/重启验收暂缓。

## 范围与既有决策

本次落实待办 15、13 和待办 16 的公共字段基础，待办 12 仅形成[恢复决策表](../recovery-decision-table.md)。不实现常驻双端采集、事件导出、历史设备统计或通知渠道，不启动任何观察任务。

继承 ADR 002、004、006：国内直连、VPN-over-proxy、服务器秘密隔离、凭据撤销、显式阻断和 fail-closed 不变。HTTP 成功仍要求新鲜的受验证响应、HTTP 200、正确出口身份和总期限内完成。保留当前分支已有的 curl 35 窄化传输错误候选；本次不扩大备用条件，也不将该未部署候选算作生产修复。

## 公共证据 v1

`wayvia.evidence` 是探针、观察器和观察摘要的字段白名单。新记录携带下列字段；旧记录读取时缺项保持 null/unknown，不补造事件时间、同步状态或新鲜度。

| 字段 | 语义 |
| --- | --- |
| `schema_version`, `event_id` | 当前为 1；每条观测唯一标识。事件关联与聚合另由 16 实现 |
| `observed_at`, `monotonic_ms` | UTC 时间及进程所在启动周期的单调时钟；请求/控制探针使用开始时间 |
| `role`, `revision`, `generation` | 角色、配置摘要及事务代次；生产探针由事务管理器绑定 |
| `run_id` | 有限观察任务标识；生产健康探针没有独立观察任务时为 null |
| `boot_id` | Linux 启动标识；平台不支持或读取失败为 null，不跨启动周期相减 |
| `software_version`, `producer_digest` | 软件包版本及证据生产相关源码摘要；观察脚本自身摘要另存于 run contract |
| `clock_sync`, `clock_offset_ms` | 当前均为 unknown/null；未测量时钟偏差，不能据此声称双端时间已精确对齐 |

探针原有 `duration_ms`、`valid_until`、`scope` 和健康判断继续使用原语义。新证据字段不进入网络计划或摘要计算，不破坏旧计划验证。脱敏投影保留原 event_id/时间，不生成“更新鲜”的记录。

## 请求阶段与失败来源

每个端点 attempt 追加 `connect_ms`、`tls_ms`、`first_byte_ms`、`total_ms`。这些值是从本次 curl 开始至对应里程碑的累计毫秒，不能直接当作各阶段独占耗时；零值、缺失和非有限值不证明阶段完成，投影为 null。

`connect_scope=local_relay` 表示 curl 连接的是本地 SOCKS 中继；这不证明入口或目标已经连通。独立 `relay` 字段记录中继自身开始至入口 TCP、TLS、Trojan 请求发出后的里程碑与失败阶段。`tunnel_ready_ms` 不证明目标已认证；业务身份仍由完整响应验证。

`failure_stage` 表示观测停止在哪一阶段，不定位具体网络节点。`failure_class` 区分证书材料/验证、其他 TLS、身份、超时、连接关闭及请求格式等类别。`termination` 分别记录 curl 正常退出或自身超时、外层 curl 进程期限、helper 期限及整轮验证期限。请求完成状态与中继清理结果独立保存；完成响应后的普通连接关闭保持原判定规则，证书/TLS 错误仍失败。

curl 只输出固定 write-out 字段。subprocess 超时时，尝试解析已取得的 write-out；仍判失败，即使这些字节含正确响应。若进程在 write-out 前被终止，目标阶段为 unknown，只保留已经取得的中继里程碑。无法保证硬终止时恢复所有 curl 内部进度。curl 与其外层进程共享剩余 attempt 预算，备用端点继续共享原总预算，不增加重试、阈值或宽限。

不导出 URL、原始 stderr、响应正文、凭据、计划或完整配置。只用错误文本在内存中保留 ADR 006 已有的窄化分类。新增字段贯穿 runtime → verify → 事务证据 → 观察器投影。

字段依据：[curl 官方 write-out 文档](https://curl.se/docs/manpage.html#-w)。该文档说明 connect 可能测量代理连接，TLS 和首字节时间均为累计值。本次离线回归使用固定输出夹具；真实链路时序准确性仍待后续允许的验收。

## 有限观察任务的状态与文件

`ObservationRun` 将不可变 contract、运行状态、显式停止意图和分段证据分开：

| 文件 | 用途 |
| --- | --- |
| `run.json` | 原始截止时间、角色/配置/代次、目标输入摘要、源码摘要、频率和容量限制；恢复必须完全匹配 |
| `run.lock` | 持有全生命周期的排他锁，防止两个采集进程同时写入 |
| `stop.json` | 显式用户停止标记；启动/续接永不删除或忽略它 |
| `state.json` | 可恢复运行状态及当前 boot 的单调截止时间 |
| `samples.jsonl`, `samples-NNNNNN.jsonl` | 首段及续接段；只新建，不截断或改写旧段，包括末尾半行 |
| `result-NNNNNN.json`, `result.json` | 不可变的各段结果，以及可更新的最新结果指针 |

新建任务默认不允许同名续接。`--resume-interrupted` 明确允许恢复仍在期限内、contract 一致的 running/interrupted 任务；可在原启动命令中配置，以支持系统维护后的同参数调用。用户停止、到期、容量耗尽、采集错误、代次/配置变化均为终态，不能通过该开关重新打开。旧观察目录缺少新 run metadata 时拒绝迁移和续接，已结束历史任务保持结束。

SIGTERM/SIGINT 只记录 `interrupted`，不猜测来源。**用户永久停止必须使用 `--stop --run-id RUN`，不能仅发送信号。** 此命令先持久化停止意图，运行进程在下次预算检查时退出；后续维护调用也无法恢复。没有配置 resume 开关时，普通信号中断也不会隐式续接。

命令模板仅用于未来获授权的观察，不是本次已执行操作：

```sh
/opt/wayvia/.venv/bin/python scripts/observe_stability.py --stop --run-id RUN
/opt/wayvia/.venv/bin/python scripts/observe_stability.py --role ingress \
  --generation GENERATION --peer-ip AUTHORIZED_PEER --interface INTERFACE \
  --run-id NEW_RUN --until UTC_DEADLINE --resume-interrupted
```

脚本现在使用已安装的 Wayvia 包，必须配套相同源码版本的解释器/包。维护协调通过显式续接及状态证据完成；本次不改 needrestart、自动更新策略或生产 systemd 单元，也不添加自动重启服务。

数据文件为 0600、目录为 0700，检查所有者、文件类型、符号链接与硬链接。续接前检查旧段完整行；仅末尾半行可保留并报告，其他损坏拒绝续接。96 MiB 样本容量按全部段累计，最多 1024 段；结果及元数据另有小量开销。终态段结果优先于可能落后的可变 state，避免中断窗口复活任务。

运行中同时检查原 UTC 截止时间与单调期限，同 boot 续接不能延长单调期限。时钟倒退至已记录时间之前时拒绝续接；跨 boot 单调时间不可比较，仅使用原 UTC 期限并保留 boot 标识，真实时钟同步仍未知。所有外部命令按剩余预算收缩；到期/停止后不启动新命令。正在运行的命令可在其已分配的短预算内退出，清理结束时间不等于采样窗口延长。

摘要读取全部段，单列续接空洞与半行；不跨段计算 CPU/单调采样间隔或给出跨启动 TCP 总差值。观察结束导致的对照任务未完成标为 unknown，不计为新增业务失败。完整双端覆盖率、磁盘告警及事件导出继续由 14/16/18 实现。
