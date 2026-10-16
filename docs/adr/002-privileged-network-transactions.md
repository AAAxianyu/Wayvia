# ADR 002：固定 Linux 资源与可恢复网络事务

日期：2026-10-12。状态：采用。

Wayvia 使用独立 `wayvia-egress` 网络命名空间承载业务 SOCKS 桥与 OpenVPN 客户端。终端入口留在宿主网络；入口回包与管理连接保留宿主路径。宿主不改默认路由。`wayvia-host` 与 `wayvia-ns` 使用 `10.89.0.0/30`，两种 SOCKS 分别监听 `10.89.0.2:1081`（业务）与 `10.89.0.1:1080`（VPN 承载）。命名空间先装输出默认阻断，仅允许 loopback、已建立连接、IPv4 TUN 出站及固定 carrier。OpenVPN 启动前的 namespace 默认路由只为计算 `net_gateway`；保护规则禁止其作为业务 fallback。入口 SOCKS UDP relay 的动态端口仅允许来自内部宿主 veth 地址。

日本代理使用固定 loopback `127.0.0.1:1194/TCP` 白名单和最后拒绝，OpenVPN 服务端监听相同端点。NAT 仅匹配 manifest 中的私有 VPN 子网与明确指定的物理出口接口。日本 IPv4 转发要求安装者在授权主机明确启用；事务不会暗改现有全局 sysctl。自有 nft 表不执行 `flush ruleset`。

计划使用版本化 Pydantic frozen 模型，固定动作和字段、有效期、SHA-256 摘要、目标资源与宿主主路由基线。agent 在执行前重建完整角色动作序列，拒绝缺少阻断或顺序被改的自封装计划。核心 JSON 也校验固定入站与出站拓扑，禁止 host TUN、透明代理、任意配置字段及文件路径；OpenVPN 禁止脚本、插件、嵌套配置与无关文件引用。服务 TLS/VPN 材料仅从 root 所有、权限 0600 的命名 `/etc/wayvia/secrets` 文件读取，不允许符号链接。全部实际引用的证书与密钥必须与 manifest 一致；应用前执行真实 OpenSSL 到期检查及公钥配对，失败时尚未安装 timer 或修改网络。现有接口子网和监听端口也做只读冲突检测，占用端口须归属相应 Wayvia systemd cgroup。

CLI/API 只访问本机 Unix socket。agent 通过 Linux `SO_PEERCRED` 核实 root 或明确允许的 UID，消息大小限制 2 MiB；不接受任意 shell、程序路径或 systemd 单位。设备同步是单独的固定操作，仅替换既定 Trojan 入口用户，真实核心校验后重启入口以关闭旧会话。校验或重启失败则停止入口。最新设备凭据 revision 单调持久化，所有启动恢复和配置回滚覆盖旧事务中嵌入的 users，避免复活撤销凭据。

每个动作执行前 fsync 前态日志；部分成功的动作同样记录并回滚。应用前用 `systemd-run --on-active` 安装独立回退 timer，默认确认窗口 120 秒。完整探针通过后仍为 `AWAITING_CONFIRM`；显式确认前重新验证探针及截止时间。新确认的配置将旧事务标记 `SUPERSEDED`，重启不重放旧配置。失败逆序恢复自有资源；无法恢复时 `RECOVERY_REQUIRED` 并阻断入口。运行时故障进入 `DEGRADED`，保留重连和阻断资源。启动恢复与定时监控由独立 systemd 单元运行，API 死亡不会取消保护。

动态数据面单位由恢复过程在保护资源就绪后启动，安装者不独立 enable 这些单位。单位不依赖正在执行同步恢复的 oneshot，避免恢复等待服务、服务又等待恢复的循环。状态只保留组件与层级错误码，命令输出、密码与业务正文不进入公开状态。恢复 journal 包含必要前态及配置，目录 0700、文件 0600，不作为日志导出。

v2rayA 使用专属 Wayvia 实例，版本 2.5.10、匹配的 `v2raya_core` 2.5.10。通过官方公开 `/api/version`、`account/login`、`ports`、`setting`、`import`、`outboundConnections`、`v2ray` 操作；不修改数据库。必须检查响应 `code=SUCCESS`，HTTP 200 不能代替成功判断。关闭透明代理和 DNS 劫持；离线 geoip/geosite 数据来自已锁定、校验过的资产。官方该版本要求自己的同版本 core，不能替换成 v2fly-core 或 xray。域名 carrier 通过配置中的独立 numeric UDP/TCP bootstrap resolver 显式解析，然后导入数字 IP，同时保留原有 TLS SNI。IP carrier 无 DNS 依赖；当前该适配器不支持 HTTPS bootstrap，明确返回 unsupported，绝不退回系统 DNS。

2026-10-15 启动等待修正：systemd 的 active 不代表 v2rayA 管理 API 已监听。首次应用和恢复均先在 30 秒单调时钟预算内轮询只读 `/api/version`，单次 HTTP 各阶段超时不超过 2 秒且按剩余预算收缩；成功响应也必须在截止前到达。仅连接／传输不可用可以重试，错误响应、非法 JSON、版本／核心不兼容立即失败；就绪后才执行账户和配置写入，写入不自动重试。超时继续抛出 `V2RAYA_UNAVAILABLE`，由原事务恢复和阻断逻辑处理。软件回归覆盖延迟、永久不可用、慢读和终止错误；正式安装实验通过 systemd ExecStart 包装器延迟真实进程 4 秒，覆盖两次入口更新及连续两次回退。

真实 sing-box 使用固定 1.14.3 的 `check -c`。OpenVPN 2.6.19 无 TLS 配置 dry-run；`--test-crypto` 不证明 TLS 配置有效。完整 TLS 校验要求明确的 `wayvia-validation` 隔离 namespace 和受控对端，以真实初始化标记为证据，停止并回收验证进程。普通配置读取与计划预览不启动 VPN。

缺少探针输入时计划保留明确 `unsupported` 条目，apply 拒绝执行。每层只能用相应实际探针：接入 TCP、承载 TCP、TUN 路由、实际出口 HTTP 响应、DNS 查询、IPv6 保护、宿主回程路由。七个层标签不能都用 service-active 冒充网络健康；这些部署探针仍与一次性全链实验及手机实网验收分别记录。

官方依据：[v2rayA 2.5.10 API 路由](https://github.com/v2rayA/v2rayA/blob/v2.5.10/service/server/router/index.go)、[匹配 core 检查](https://github.com/v2rayA/v2rayA/blob/v2.5.10/service/kernel/v2ray/service/service.go)、[OpenVPN 2.6 手册](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html)、[Unix socket 与 SO_PEERCRED](https://man7.org/linux/man-pages/man7/unix.7.html)。对应代码在 `agent/`、`adapters/`；契约与逐步骤故障测试在 `tests/contract`、`tests/unit/test_transactions.py`。真实 namespace/nft/veth 及回滚证据由一次性 Linux 实验另行保存。`tests/lab/recovery_probe.py` 还在显式隔离 systemd PID1 容器内验证真实独立 timer 和控制进程退出后的恢复；这不代替真实云主机重启验收。
