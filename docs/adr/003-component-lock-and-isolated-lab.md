# ADR 003：真实组件锁与隔离实验

日期：2026-10-12。状态：采用。

服务器与终端候选配置由 sing-box 1.14.3、mihomo 1.19.32 校验。阿里云承载使用 v2rayA 2.5.10 与同一发布的 v2raya_core 2.5.10；该版本明确拒绝其他版本或 v2fly/xray 原版核心。V2Ray 5.53.0 发布包只提供固定版本的 geoip/geosite 数据与可选独立配置解析器，不充当 v2rayA 的运行核心。固定这些资源避免 v2rayA 在隔离实验里尝试下载 latest 数据。

下载源为各项目官方 GitHub release，资产 SHA256 来自官方 GitHub API 并写入 `deploy/lab/components.lock.json`。`scripts/fetch_components.py` 每次核验下载文件摘要。OpenVPN 使用 Ubuntu 24.04 官方认证 APT 仓库的 2.6.19-0ubuntu0.24.04.4 包；包摘要见 `compatibility.yaml`。Ubuntu 基础镜像按 digest 固定。辅助系统库由 Ubuntu 安全仓库安装，其完整安装版本进入验证证据；这些库不被冒充为项目单独支持的核心版本。

M4 在 `--network none`、一次性 privileged Docker 容器里创建终端、国内、阿里云、日本、国外与阿里云出口六个 Linux namespace。所有 veth、路由、nftables、TUN、CA、密码与 OpenVPN 实例均属于该容器。无主机网络挂载，无宿主路由/防火墙/VPN变更。停止容器即销毁测试密钥与网络资源，只有脱敏 JSON 验证证据写回仓库。

OpenVPN 使用 TCP + SOCKS5 基线。隧道内的 TCP/UDP 是不同业务测试；底层 TCP 的重传与队头阻塞可能影响实时业务，实验功能通过不代表性能承诺。代理层只转日本本机 TCP 1194；OpenVPN 不监听日本角色的物理接口。阿里云出口有仅供指定 SOCKS 承载使用的物理默认路由，并先装正式 input/output drop 保护。实验还提供受控的阿里云物理转发基线，证明保护规则能阻断原本可达的绕行路径。正常故障必须使受保护请求失败，国内直连仍成功。

官方依据：

- [sing-box SOCKS 入站](https://sing-box.sagernet.org/configuration/inbound/socks/)
- [sing-box 路由动作](https://sing-box.sagernet.org/configuration/route/rule_action/)
- [OpenVPN 2.6 手册，SOCKS 与 TLS 校验](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html)
- [v2rayA 2.5.10 核心兼容检查源码](https://github.com/v2rayA/v2rayA/blob/v2.5.10/service/kernel/v2ray/service/service.go)
- [v2rayA 2.5.10 HTTP 路由](https://github.com/v2rayA/v2rayA/tree/v2.5.10/service/server)
