# 兼容版本与验证边界

版本与下载摘要由 [compatibility.yaml](../compatibility.yaml)、[组件锁](../deploy/lab/components.lock.json) 维护。初始化及部署不可使用 `latest` 替代锁定版本。下载器只使用官方固定发布 URL，并拒绝 SHA256 不匹配的文件；隔离执行生产适配器的 Python 依赖也固定版本和 wheel SHA256。

15 项审计问题的代码与专项回归已完成，详见 [修复追踪](validation/fixes-2026-10-12/README.md)。最终 `make check`、`make demo`、`make test-configs`、`make test-lab` 均退出 0：软件回归 291 项通过，另有 3 条锁定依赖弃用提示，Ruff、wheel/sdist 构建及 CLI 帮助均通过；真实配置门禁为 10 项，统一实验包含全部隔离模块。命令退出码和日志记于本轮 `validation/fixes-2026-10-12/commands.json`。这些结果确认软件交付及当前隔离环境验证，实网验收仍单独记录。

| 组件 | 固定版本 | 当前已取得的证据 |
| --- | --- | --- |
| Ubuntu | 24.04，基础镜像固定 digest | arm64 一次性 Docker 容器；双角色正式事务使用独立 systemd 容器和 internal 网络 |
| Python | 工程 3.12；实验 Ubuntu 3.12.3 | 非特权回归、生产适配器与安装后锁定 venv |
| sing-box | 1.14.3 | 正式服务端/终端配置 check；真实 Trojan 认证、TUN、SOCKS、TCP/UDP及双层路径 |
| mihomo | 1.19.32 | 正式完整终端配置 `-t`；6 类真实 DNS 查询；未据此声称完整 mihomo TUN 或手机导入通过 |
| v2rayA | 2.5.10 | 生产适配器真实账户、设置、导入、连接与 core 启动 API，正式事务中的承载故障及恢复 |
| v2raya_core | 2.5.10，基于 xray-core 26.7.28 | v2rayA 管理的真实 SOCKS 承载 OpenVPN TCP |
| OpenVPN | 2.6.19，Ubuntu包 2.6.19-0ubuntu0.24.04.4 | CA/EKU/服务端身份验证、当前 management 会话、TCP over SOCKS及隧道业务 |
| iproute2 / nftables | 6.1.0 / 1.0.9 | 真实内核资源、保护/NAT、12 项冲突检查、回退与无关资源保留 |
| systemd | 255.4-1ubuntu8.17 | 私有 cgroup2、真实 timer 独立回退；安装后双角色 API/agent/monitor、独立恢复和同版本重装 |
| V2Ray | 5.53.0 | 固定发布包提供 geoip/geosite 文件；不是 v2rayA 2.5.10 运行核心 |

v2rayA 2.5.10 要求配套同版本 v2raya_core，不能套用旧版“v2fly 或 xray 可任选”的约定。部署提供固定发布包中的 geodata 并指定资产目录，隔离运行不依赖启动时隐式下载。

OpenVPN 包摘要来自 Ubuntu 认证 APT Release/Packages 元数据。基础镜像按 digest 固定，OpenVPN 按准确包版本安装；辅助 Ubuntu 库仍跟随其安全仓库，安装版本记录于环境证据。若包仓库删除固定版本，构建会失败，需重新验证后显式更新锁，不能自动换版。没有宣称整个操作系统字节级可重复构建。

## 本轮隔离证据

- [23 项双层路径](validation/fixes-2026-10-12/tun/full-lab-after.json)验证国内直连、国外双层链路、原生 TUN、UDP/DNS、回程与故障保护；[协议 7 项正负向](validation/fixes-2026-10-12/protocol-result.json)拒绝错误 Trojan 密码、错误 TLS/VPN 身份和普通 TCP 伪端点。端口可连接不再足以证明组件健康。
- [mihomo DNS 6 项](validation/fixes-2026-10-12/mihomo-dns-result.json)使用固定核心及 3 个真实 UDP 解析器，验证同名/父子后缀优先级和精确 bootstrap。该专项关闭 TUN，验证范围限于 DNS 行为。
- [真实内核冲突 12 项](validation/fixes-2026-10-12/preflight-conflicts-result.json)检查 TUN 所有权及所有路由表的冲突，并验证自有内核资源、无关 VPN/nft 和默认路由的边界；它不证明业务组件健康。
- [正式安装事务 23 项](validation/fixes-2026-10-12/transaction-result.json)运行真实安装的 API/CLI、agent、systemd 和 monitor：入口端口 `443→8443→9443→8443→443`、旧 monitor 交错、出口同配置三代、两层故障与认证恢复、发布/订阅、轮换撤销、旧计划及回退不复活权限、独立恢复、同版本重装和两端卸载。同版本重装不是跨版本升级，独立恢复不是整机重启。

原生 TUN 首次超时已定位：SOCKS 就绪早于内核 TUN 接口和策略就绪，提前创建的 socket 选择物理源地址，随后被保护规则阻断。修复的是实验就绪门禁，没有修改 sing-box，也没有增加业务请求重试。[因果记录](validation/fixes-2026-10-12/tun/README.md)保留原始失败、socket 选源和 netlink 时间；[分阶段反例](validation/fixes-2026-10-12/tun/readiness-stages.json)验证不完整策略不能提前放行，[12 次独立冷启动](validation/fixes-2026-10-12/tun/cold-starts-after.json)另行存证。采样通过没有扩大为所有客户端、平台或调度时序的稳定性保证。

本轮本地隔离实测架构是 arm64。旧提交 `d4081636c15ea2c0530b2e53ff529c0a1a6a3cc2` 的 [CI run 38241319117](https://github.com/AAAxianyu/Wayvia/actions/runs/38241319117) 曾通过 amd64 的 software 和 isolated-linux，属于历史基线，不能作为本次修复或新提交的远端 CI 结果。原始审计与失败门禁保留在 [历史审计](validation/review-2026-10-12/README.md)。macOS 只承担非特权开发和普通测试，不作为待接管网关。

## 仍未验收的范围

完整导出格式为 sing-box JSON 和 mihomo YAML；格式解析及 DNS 行为通过不等于 iPhone/Android 的某个 App 已导入成功。代理 IPv6 仍阻断，国内 IPv6直连及隧道内 TCP/UDP 有受控证据；UDP 底层 VPN 和其他协议未支持。

真实云服务器、生产域名证书与续期、手机系统/浏览器私人 DNS、实际 App 导入与 Wi-Fi/蜂窝切换、整机重启、跨地域性能和费用均未执行 M6 验收。OpenWrt M7 尚未实现/验收。实验未修改开发机路由、全局防火墙、既有 VPN 或未授权主机。

运行方式、模块和证据边界见 [实验说明](../deploy/lab/README.md)、[M4 验证记录](validation/m4-controlled-lab.md) 和 [实现状态](implementation-status.md)。
