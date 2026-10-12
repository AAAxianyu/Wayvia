# 兼容版本与验证边界

版本与下载摘要在 `compatibility.yaml`、`deploy/lab/components.lock.json` 中维护。初始化及部署不可使用 `latest` 替代锁定版本。`scripts/fetch_components.py` 只下载官方固定发布 URL，并逐项拒绝 SHA256 不匹配的文件；用于隔离执行生产适配器的 Python 依赖也固定版本和 wheel SHA256。

| 组件 | 固定版本 | 本次证据 |
| --- | --- | --- |
| Ubuntu | 24.04，基础镜像固定 digest | aarch64 一次性 Docker 容器 |
| Python | 工程 3.12；实验 Ubuntu 3.12.3 | 非特权测试与生产 LinuxExecutor 隔离执行 |
| sing-box | 1.14.3 | 正式服务端/终端配置 check；真实 Trojan、TUN、SOCKS、TCP/UDP |
| mihomo | 1.19.32 | 正式完整终端配置 `-t`；设备导入待 M6 |
| v2rayA | 2.5.10 | 生产适配器真实账户、设置、导入、连接与 core 启动 API |
| v2raya_core | 2.5.10，基于 xray-core 26.7.28 | v2rayA 管理的真实 SOCKS 承载 OpenVPN TCP |
| OpenVPN | 2.6.19，Ubuntu包 2.6.19-0ubuntu0.24.04.4 | CA/EKU/服务端名称验证、TCP over SOCKS、隧道业务 |
| systemd | 255.4-1ubuntu8.17 | 容器私有cgroup2、真实SystemdTimer超时回退与独立恢复进程 |
| V2Ray | 5.53.0 | 其固定发布包提供 geoip/geosite 文件；不是 v2rayA 2.5.10 运行核心 |

v2rayA 2.5.10 官方源码要求配套同版本 v2raya_core，不能将旧版“v2fly 或 xray 可任选”的约定套到此版本。缺少 geodata 会触发下载；Wayvia 部署提供固定发布包中的数据并明确指定资产目录，隔离运行不依赖该隐式下载。

OpenVPN 包 SHA256 来自 Ubuntu 认证 APT Release/Packages 元数据。基础镜像按 digest 固定，OpenVPN 按准确包版本安装；辅助 Ubuntu 库仍跟随其安全仓库，安装版本记录于环境证据。包仓库将来删除固定版本时构建会失败，需要重新验证版本后显式更新锁，不能自动换版。

目前实际链路通过的架构是 arm64。amd64 的官方二进制和 Linux wheel 已锁定摘要，但该架构仍需运行相同门禁。macOS 仅承担控制面、存储、配置和普通测试，不用作待接管网关。

支持的完整导出格式为 sing-box JSON、mihomo YAML。iPhone 与 Android 具体 App/版本尚未指定；格式通过核心解析并不等于手机已导入成功。代理 IPv6 先阻断，国内 IPv6 直连已有受控证据。UDP 底层 VPN、其他协议、OpenWrt、手机系统私人 DNS、生产域名/证书、云服务器及实网性能均需单独验收。

重现命令、实验拓扑与证据说明见 [实验说明](../deploy/lab/README.md) 和 [验证记录](validation/m4-controlled-lab.md)。
