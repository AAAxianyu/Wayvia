# M4 受控链路验证记录

日期：2026-10-12（America/Los_Angeles）。原始组件日志使用 UTC；这不是两台真实云服务器或手机的验收记录。

## 本次实际运行

实验在 Colima 的 Docker 29.5.2 / aarch64 / Linux 6.8.0-117-generic 上执行。Ubuntu 24.04 容器中实际 Python 为 3.12.3，iproute2 6.1.0（包6.1.0-1ubuntu6.4），nftables 1.0.9（包1.0.9-1ubuntu0.1），OpenVPN 2.6.19。数据链路容器没有 systemctl；另外使用仅有容器私有 cgroup2 挂载的 systemd PID 1 容器验证了真实独立计时器及恢复进程，版本255.4-1ubuntu8.17。该证据不等于云主机整体重启验收。

默认命令重新校验固定官方二进制/数据资产以及测试 Python wheel 的 SHA256，构建一次性镜像，随后在 `--network none` 的专属 privileged 容器内运行。只挂载项目源代码、实验脚本、临时生成产物与证据目录，不挂载宿主网络、全局防火墙、现有 VPN、Docker socket 或真实凭据。

| 门禁 | 结果与证据 |
| --- | --- |
| `make test-configs` | 见 `latest-configs/config-result.json`：5份 sing-box 真实解析、1份 mihomo 真实解析、2份 OpenVPN 实际隔离启动，以及2份不支持协议负向拒绝，共10项 |
| `make test-lab` | 见 `latest-lab/lab-result.json`：22项真实路径/权限/故障/握手测试；随后生产 LinuxExecutor 真实资源执行与逆序回退（`executor-result.json`），以及真实 systemd timer / 独立恢复进程两项（`recovery-result.json`） |
| `make clean-lab` | 只定位本实验名称和所有权标签，实验容器结束时已 `--rm`；具体执行退出码见 `commands.json` |

脚本开发阶段也直接运行过 `.venv/bin/python scripts/test_configs.py --no-build`、`scripts/lab.py --no-build`，这些开发命令不能取代最后按 Makefile 运行的约定门禁。命令摘要与最终退出码写入 `commands.json`。

## 证据说明

产物来自正式 WayviaConfig、ProfileRenderer、ServerRenderer。受控规则明确分类10.71/24及fd72:1::/64为国内，10.74/24为国外。终端保留完整 TUN/DNS/TLS/分流产物，只增加本地 SOCKS 探针入站；原生 TCP 与 UDP socket 两项证明未指定代理的程序也被 TUN 接管。

阿里云 v2rayA 使用生产 V2rayAAdapter.configure_carrier 通过正式账户、配置、导入、outboundConnections 和启动 API 管理同版本 core。OpenVPN 底层 TCP 经 SOCKS、Trojan TLS 到日本回环 VPN；健康快照中的 socket 路径、TUN 计数和两端 OpenVPN TLS/证书校验日志支持该顺序。日本只允许指定 VPN 地址/端口，任意网站转发请求被拒绝，VPN物理接口1194连接被拒绝。

国外 TCP、UDP、DNS目标看到日本物理地址10.74.0.1；国内 TCP/DNS看到终端10.71.0.2，国内 IPv6看到fd72:1::2。国外 IPv6被明确规则阻断。管理探针沿10.73/24物理链路回包。UDP端到端回包通过真实跨namespace SOCKS relay。

实验直接使用生产 guard_rules 的阿里云 host与出口 input/output规则，日本 NAT通过生产 LinuxExecutor 的 typed exit_nat生成。为了验证故障保护本身，实验给出口配置了同生产的物理default，并让隔离阿里云能够物理forward/NAT到国外目标：未安装保护时，目标实际看到阿里云10.73.0.1；装保护后同一个直接请求失败。随后分别停VPN/日本代理，SOCKS及原生TUN业务均失败，国内及管理请求继续成功；重启组件后业务恢复。

设备权限通过真实入口删除用户并受控重启撤销，新连接被拒绝，旧会话被终止；订阅令牌单独撤销及控制面权限边界由服务集成测试覆盖。订阅撤销与设备权限撤销不是同一个动作。

实验最后停止原VPN客户端，将同一个受保护出口namespace临时别名为wayvia-validation；生产 OpenVPNAdapter.validate 对正式生成的客户端文件完成真实隔离握手。独立新容器中生产 LinuxExecutor 创建namespace、保护表、veth、host保护表，逆序回退后这些资源全部消失，默认路由前后相同。

另一个隔离容器以systemd为PID 1、`--cgroupns private`运行，不绑定宿主cgroup。子控制进程先创建真实SystemdTimer并持久化APPLYING journal，再执行4个真实kernel动作，然后退出。10秒后独立systemd-run恢复服务将事务回退到ROLLED_BACK，ExecMainStatus=0，资源移除，默认路由不变。第二例在新的控制进程退出后，另一个独立 `wayvia.agent.recovery` 进程读取未确认journal并执行相同真实回退。它模拟控制进程/恢复进程重启边界，没有伪造healthy探针或ACTIVE状态，也没有声称整个云主机已经重启验证。

## 修复与边界

测试暴露并修复了v2rayA配套core版本要求、缺失离线geodata导致启动下载、管理密码32字符上限、IPv6 DAD启动竞争、跨namespace UDP relay输入放行、veth重命名down/up删除提前添加default的问题。国内DNS探针改用domestic.lab并验证对应解析器地址，避免把被正确按域名转到国外DNS的查询误写成国内通过。

阿里云日本节点使用carrier.lab，通过生产适配器指定的10.73.0.1启动DNS解析为日本IP，并保留TLS名称校验；终端入口使用数值IP。系统/浏览器私人DNS行为、Android/iPhone客户端导入、Wi-Fi/蜂窝切换、真实云服务器带宽/性能/费用及OpenWrt仍为独立M6/M7输入。systemd真实计时器与恢复在独立容器中通过；目标云主机完整重启仍待M6。固定核心版本、Python依赖和基础镜像可重现；辅助Ubuntu安全仓库包仅记录已测版本，未宣称整套系统字节级可重复构建。
