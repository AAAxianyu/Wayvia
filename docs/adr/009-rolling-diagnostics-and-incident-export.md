# ADR 009：持续诊断、双端窗口与脱敏导出

日期：2026-10-25 America/Los_Angeles。状态：本次软件范围实现；生产、实网采样和故障/重启验收暂缓。

## 授权与边界

沿用 ADR 008 的证据 v1、0600/0700 文件边界、原截止时间、排他锁、显式续接和持久用户停止。本次交付待办 14/16 的仓库实现与软件验证。没有启用生产采集器、部署服务、修改路由/防火墙/VPN、启动实网观察、恢复 heartbeat 或通知渠道。

新 `wayvia diagnostics` 命令提供被动持续采集、事件查询、对端触发文件接收与脱敏证据包。持续模式指在显式授权租期内运行、滚动保存；仍继承每个 run 最长 24 小时的期限，不自动续期或重开已结束任务。原 `observe_stability.py` 有限专项观察继续保留，主动 HTTP/DNS 对照不会隐式加入新的被动采集器。

保护、恢复与发布规则继续遵守 ADR 002/004/006。事务管理器仅新增有限结构化事实：保护请求/动作返回/失败、恢复开始/完成/失败及依赖服务启动。保护动作包装仍执行原有方法，异常继续沿原逻辑传播；证据输出失败被隔离。`protection_applied` 表示原保护命令已返回，不增加新的防火墙证明或健康判定；service started 不等于业务已恢复。待办 12 的恢复范围仍未实施。

## 被动来源与字段

| 来源 | 保存内容 | 明确限制 |
| --- | --- | --- |
| 已授权 carrier TCP | RTT、RTO、重传、收发队列、窗口及字节计数；连接标识加 run 盐后散列 | 仅指定 peer/port，区分 ingress 目标端口与 exit 源端口；无原始地址、PID、目标列表 |
| 物理/TUN 接口 | 数值计数与间隔吞吐；接口索引用于重置检测 | 启动变化、索引/计数器重置、较大间隔时不推算负吞吐；不保存 MAC 等属性 |
| 主机资源 | CPU、内存、load、CPU/内存/I/O pressure | 主机级量不能直接归因某设备或 carrier；缺字段为 null |
| 采集负担 | 整轮读取/写入前的 wall/CPU 耗时、RSS 高水位与序列化负载大小 | 不声称是完整块设备 I/O 计费；真实持续开销尚未验收 |
| 时钟 | 只读 NTP 同步状态、UTC、boot_id、单调时间 | 偏移量未测得仍为 null；同步标志不证明双端精确对齐 |
| 软件版本 | 运行中的 Wayvia 版本/源码摘要；每 60 秒有界查询已安装核心版本 | 组件版本标注 `installed_binary_query; running_process_version_unknown`，不将安装文件版本倒推为历史进程版本 |
| systemd journal | 固定单位、固定消息 ID、Wayvia 结构化事件和 monitor 白名单证据 | 原始 MESSAGE、stderr、计划、命令行、正文和 URL 不入库；截断/错误不冒充完整读取 |
| 设备审计（可选） | 既有 SQLite audit 的设备/订阅操作及稳定账号 ID | 只读、不迁移，不读取 secret_ref 或 token_hash；账号不等于物理设备，旧审计缺版本保持 unknown |

`peer_receive_window_bytes` 来自 snd_wnd；`receive_window_bytes` 仅在内核实际提供 rcv_wnd 时记录。rcv_space 是接收缓冲自动调节量，不冒充线上 advertised window。[ss 手册](https://man7.org/linux/man-pages/man8/ss.8.html)给出 TCP 信息和 rcv_space 的语义。

systemd 服务消息采用固定 ID，且要求 PID 1/固定 UNIT；其中 failed 是启动 job 失败。维护单位只提供维护标记，不凭时间接近推断 needrestart 因果。管理 SSH、明确内部探针身份的短连接和业务证据使用不同 kind，普通 TLS/reset 文本不计为用户断连。ID 和 job 语义来自 [systemd 官方消息定义](https://github.com/systemd/systemd/blob/main/src/systemd/sd-messages.h)。

## 有界存储与双端窗口

`telemetry.store` 使用独立私有 SQLite 文件，与控制面 metadata.db 分开。默认每 5 秒采样、保留 900 秒滚动记录；故障窗口至少前 120 秒、后 120 秒，成功恢复可延长后窗。buffer 至少为 pre + 120 秒，给检测和对端传输留余量。

失败事件自动生成 incident_id；同一窗口内的后续事件合并，迟到的请求起始证据可以向前扩展窗口而保持 ID。双端共享该 ID、path_id、revision 及触发起点，通过显式配置的私有文件通道传递；重放幂等、范围/身份冲突拒绝、旧副本不能缩小窗口。两端可回传扩展后的窗口。单事件最长 1800 秒，继续故障或窗口扩展超过上限会拆成新事件，保留旧窗口。

触发时固定已有前窗，并继续固定后续记录。只有未固定滚动记录或已到期事件可以删除。默认事件保留 7 天、最多 32 个未到期事件，策略范围由 `Policy` 校验。SQLite 页数上限为总逻辑预算扣除 2 MiB 后的一半，为 rollback journal 和生命周期/触发元数据预留空间；不使用 WAL 或 VACUUM。默认总逻辑预算 128 MiB，文件系统分配粒度、宿主磁盘占用及独立导出文件另计。

容量或持久化失败会结束诊断并保留已有证据，不删除未过期窗口来伪造持续采集。运行时自动按期限清理；任务结束后使用显式 `prune` 清理，不为清理重新启动任务。缺测由每角色的采样槽位、来源读取状态、续接 gap 和窗口边界报告；未完成的后窗为 pending，缺对端为 source_available=false。采样覆盖率不是业务可用率。

文件通道不提供远端身份认证。inbox/outbox 必须为管理员明确配置的、私有且专用的目录，外部传输由获授权的管理通道负责；软件不会主动 SSH 或联系任何主机。未配置通道时仍可本端采集，peer 来源明确 unavailable，不能宣称双端窗口完整。`import-trigger` 也可在明确传入文件后固定仍存在的对端滚动数据，不会启动采样。

## 时间线、身份与导出

`timeline` 支持最多 8 个源数据库，按 UTC 窗口或 incident_id 查询，附 IANA 时区本地时间；支持角色、层级和设备 ID。跨端 incident_id 明确关联窗口，时间接近不作为根因证明。共享链路探针默认不归属任何设备；按设备查询只返回已有明确设备 ID 的事件。`identity_source` 区分 credential_only、user_confirmed、device_observed，客户端版本为有限数字版本格式；接入同一订阅名不能替代物理设备确认。

终端日志不会自动读取。`ingest --authorize-client-logs` 才允许明确提供的 typed client/business 事件，仍执行字段白名单，原始日志和正文不保存。导入先投影和授权检查，随后按记录提交；失败可按 event_id 幂等重试，不声明整文件原子提交。

查询/导出以 SQLite mode=ro + query_only 运行，不迁移、修订、清理或更新源证据。单次窗口最多 24 小时、每源最多 20000 行，累计读取/报告限 16 MiB。无对应采样的旧事件不能补造历史指标。UTC/本地时间并列显示，时钟偏差未知时明确保留 unknown。

导出 ZIP 固定包含 manifest.json、events.jsonl、samples.jsonl、coverage.json；不提取源文件或复制原日志。0600 文件、0700 目录，拒绝覆盖和不安全路径。manifest 包含逐文件 SHA-256、访问/保留策略和到期时间；verify 检查成员、容量、校验和与有效期。校验和用于完整性，不是远端签名认证；篡改 manifest 后重算摘要不构成可信来源。

独立导出包到期后 verify 明确拒绝；文件由管理员依约清理，不安装隐式清理任务。采集数据库的 `prune` 不删除用户独立导出文件。完整产品告警渠道及设备分钟历史仍由 18/17 跟踪。

## 命令与验证

详细命令见 [诊断操作说明](../diagnostic-evidence.md)。软件测试使用合成双端事件、临时私有数据库、命令输出夹具与真实文件锁；[离线例子](../validation/rolling-incidents-2026-10-25/README.md)不运行采样器、不发网络请求。真实内核字段差异、维护重启、两端时钟/管理传输和自然故障覆盖均待后续获授权验收。
