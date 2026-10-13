# ADR 004：配置发布快照与有时效的角色证据

状态：接受。日期：2026-10-12。

设备凭据已被接入核心接受，不等于当前逻辑配置的入口地址、端口、DNS 或双层拓扑已经部署。原先导出直接读取当前配置，会把未确认修改立即发给终端。因此配置发布与设备凭据同步分开处理。

## 发布契约

- SQLite 显式迁移 2 增加 `config_publications`，保存逻辑配置快照、两端确认记录及发布时间；`settings` 持久记录 desired 和 published revision。
- `wayvia config publish --exit-status FILE` 是显式管理操作。它读取本机 agent 的 ingress 状态，以及管理员经可信管理通道从 exit 主机导出的状态。两者必须属于各角色当前运行代次、处于 ACTIVE、对应当前逻辑配置的完整 SHA256 revision，并具有新鲜验证和监控证据。
- ingress 需要 `verification_scope=datapath` 的完整链路证据。exit 在对端部署之前只能证明自己的组件就绪，其 `component_readiness` 证据标为 ready；与 ingress 的完整链路确认一起允许发布，不能单凭出口主机的本地就绪宣称端到端健康。
- 生产 `profile export`、订阅签发和订阅读取仅使用已经发布的快照。首次发布前拒绝导出；加载新的 desired 配置、变更失败或者重启 API 都不会隐式替换已有快照。设备密码仍从当前设备的独立 secret_ref 读取，撤销和轮换即时沿原有设备生命周期执行。
- 两台服务器仍各自提交事务。发布记录保存双方 transaction id、generation 与 revision，不宣称跨主机原子性。远端状态文件是管理员经 SSH 等可信路径获取的运维输入，不是自签名的远程认证机制；公网订阅访问者不能提交它。HTTP 发布接口需要管理员认证与幂等键。
- 已发布快照在 agent 暂时不可用时保留，避免配置服务重启丢失发布状态；这不代表链路健康。新发布必须重新获取合格证据。显式 test/demo 保持清楚标注的本地演示导出，不可用来证明生产发布完成。

## 证据契约

每份探针结果绑定 `revision`、`generation` 和 `scope`，携带 `observed_at`、`duration_ms` 与 `valid_until`。有效期最多 90 秒，允许最多 5 秒的时钟偏差。修改事务 `updated_at` 不能刷新探针证据。

周期 monitor 单独提供 `monitored_at` 和 `monitor_valid_until`；确认事务不会冒充 monitor 心跳。doctor 只接受当前运行代次，过期、缺失、代次不一致或范围不一致的证据返回 unknown，并给出具体原因。新鲜 monitor 证据仅表示最近观察到监控，不声称已实时检查 monitor 进程仍存活。exit 的独立检查显示 ready，完整数据链路健康须由 ingress 实证。

## DNS 和轮换的一致性修复

mihomo 使用域名 trie，后序更具体的域名可能改变统一策略的优先级。编译器剔除被前序后缀覆盖的后序条目，保留前序子域与后序父域的组合。精确 bootstrap 域名与同名后缀同时存在时，后缀使用仅匹配子域的 `.domain`，防止 `+.domain` 的精确节点覆盖 bootstrap。

设备轮换的新 secret_ref 增加独立随机后缀。secret fsync 后、数据库提交前中断留下的未引用文件不再占用下一次轮换的确定名称；数据库提交后的中断由 reconcile 使用已经提交的同一凭据恢复，不能凭未提交的孤立文件自动切换凭据。

## 系统操作入口

安装系统使用 `sudo -u wayvia /opt/wayvia/.venv/bin/wayvia --system ...`。该模式固定 production、`/var/lib/wayvia/control`、同目录配置和 `/run/wayvia/agent.sock`，要求 wayvia 服务身份，并拒绝混用另一套目录参数。开发目录仍使用显式本地参数。API systemd 单元与系统 CLI 共用同一控制面目录。

## 验证边界

测试覆盖发布前拒绝、旧快照跨重启保留、双角色 revision 和证据校验、发布接口权限与幂等、轮换三个中断边界、诊断新鲜度、系统 CLI 路径，以及六种真实 mihomo DNS 查询。DNS 实验在显式一次性容器内运行固定核心与三个本地 UDP 解析器；仅关闭 TUN 以隔离验证 DNS 策略，不将该结果扩大为 mihomo 全设备接管或手机导入验收。
