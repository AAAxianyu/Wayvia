# 双云部署与正常公网路径验收

2026-10-20 UTC。源码基线 `0e5bbbc63b59031377b698311f0cb6fda44c5401`，分支 `codex/deployment-readiness-20261020`。服务器、安装版本、IP 证书和续期配置沿用 [准备记录](../cloud-preparation-2026-10-20/README.md)。生产源码未修改。

本目录保留最初部署和配置交付快照。随后已完成实体 Android 的 Clash Meta URL 导入与基本 HTTPS 日本出口验证，并启用订阅压缩，最新手机结果见 [Android 导入排查](../android-import-2026-10-20/README.md)。下文“手机尚未验证”的表述属于当时阶段；手机完整策略矩阵与网络切换仍未完成。

## 结果

用户确认阿里云安全组端口已放行后，日本访问阿里云 HTTPS 探针成功。重新读取各角色 baseline，生成新计划，依次 apply/confirm 出口和入口，均返回 ACTIVE。入口的七层证据为真实 datapath，出口七层为 component_readiness，二者不混同。

| 对象 | 结果 | 证据 |
| --- | --- | --- |
| 日本出口 | ACTIVE；事务 `f5d5a248d3a24b66976382319405d207` | [apply/confirm](jp-apply-confirm.json)、[后续状态](jp-deployed-state.json) |
| 阿里云入口 | ACTIVE；事务 `581c16248ca94897b552ed752d647f51` | [apply/confirm](ali-apply-confirm.json)、[后续状态](ali-deployed-state.json) |
| 双角色发布 | 同 revision、新鲜监测，经 SSH 传递出口收据后显式 publish | [publication.json](publication.json) |
| Android 独立设备 | `android-primary` active，入口同步成功，非模拟 | [summary.json](summary.json) |
| 私人订阅 | 公网 TLS 验证成功，HTTP 200，5,872,116 字节，与导出内容相同 | [subscription-validation.json](subscription-validation.json) |
| 外部完整配置 | sing-box 1.14.3 check 和 TUN 正常路径退出 0，配置未修改 | [public-client-result.json](public-client-result.json) |

逻辑配置 revision 为 `0cfa4377db5ddb2e8f965ebbed8d2d2f8922cb6cc79ba8398a82c68e688df424`。入口 monitor 在 `08:36:42 UTC`、出口在 `08:36:41 UTC` 的快照均为七层 healthy；这些是带有效期的当时证据，不代表无限期健康。

## 外部客户端正常路径

测试使用明确的一次性 Linux Docker 容器 `wayvia-public-client-20261020`，仅授予该容器 NET_ADMIN 与 `/dev/net/tun`，运行原样导出的完整终端配置。没有使用 host 网络，也没有更改开发机路由或全局防火墙。镜像 ID、版本、退出码和清理情况见 summary。脚本见 [test-public-client.py](test-public-client.py)，公开副本以 `WAYVIA_ENTRY_IP`、`WAYVIA_EXIT_IP` 环境变量替换地址；配置以只读 `/handoff/wayvia-android.json` 挂载。

- 在 TUN 建立、策略路由选中 TUN 源地址后开始业务请求。
- 阿里云自有 HTTPS 探针和 Cloudflare HTTPS trace 均观察到日本公网出口，TLS 校验保持开启。
- 百度 HTTPS 返回 200，容器物理 `eth0` 抓到直接发往实际百度目标 443 的 TCP 包，确认该国内域名直连。
- `dig @1.1.1.1 example.net A` 通过终端 DNS 劫持与代理解析返回 NOERROR、两个答案；同期物理网卡发往 `1.1.1.1:53/UDP` 的包为零。
- 对应 AAAA 查询返回 NOERROR、零答案；受保护 IPv6 HTTPS 请求失败，curl 退出 35，与配置的 IPv6 reject 策略一致。没有把这一失败单独当成全面 IPv6 防泄漏证明。
- 客户端容器执行结束后自动删除；测试没有停止或故意破坏任何真实服务器数据面组件。

首次客户端运行把阿里云自有探针 IP 错当成国内 CIDR，断言直连而失败，原始记录保留在 [public-client-initial-failure.txt](public-client-initial-failure.txt)。检查导出规则发现该 IP 不在固定 APNIC CN 列表，入口 8443 的 bootstrap 例外也不覆盖探针 9443，实际走默认代理符合配置。第二次修正测试预期，另用国内域名和物理连接抓包验证直连；生产代码、服务器配置与交付配置均未为此修改。国内覆盖依赖当前规则，不能据此声称所有地理位置在国内的 IP 都能被识别。

核心只报告 `independent_cache` 弃用警告：锁定的 1.14.3 接受该字段，1.16 的迁移须另行验证。本次没有升级组件或忽略校验错误。

## 服务器链路和隔离

[部署后状态](ali-deployed-state.json) 记录入口业务 namespace 的路由、连接和 nft guard：业务 IPv4 经 `tun0`；OpenVPN 的实际 TCP 对端是承载 SOCKS `10.89.0.1:1080`，物理承载连接指向日本 443。日本观测到该承载连接，再由 sing-box 接入回环 `127.0.0.1:1194` OpenVPN。此连接证据与真实探针共同支持 VPN-over-proxy 顺序。

业务 namespace 默认拒绝，允许 VPN 业务路径及指定承载连接；日本专属访问表限制承载端口来源。两端宿主默认路由与激活前快照一致；阿里云 Nginx、Docker 仍 active，原四个站点配置 SHA256 检查全部一致。公网反向代理管理路径返回 404，管理 API 仍只在回环监听。

终端导出仅含阿里云设备密码，TLS 校验开启；没有日本承载凭据、VPN 私钥或 CA 签发私钥。私人订阅与配置保存在 Git 忽略的 `private/android-20261020/`，目录 0700、文件 0600。公开证据对服务器和客户端公网 IP、主机名脱敏，不含订阅 token 或设备密码。

## Android 交付和未完成验收

交付目标为官方 SFA 1.14.3，官方资产元数据见 [sfa-release.json](sfa-release.json)。安装来源为 [官方 Android 客户端页面](https://sing-box.sagernet.org/clients/android/) 与 [1.14.3 发布页](https://github.com/SagerNet/sing-box/releases/tag/v1.14.3)，远程配置格式依照 [官方客户端文档](https://sing-box.sagernet.org/clients/general/)。未接触实体手机，因此不宣称 Android UI 导入、VpnService 和系统权限已通过。

私人文件包含远程订阅地址、完整 JSON、导入说明。订阅读取链接有效至 `2026-11-19 08:33:00 UTC`；订阅到期与设备凭据撤销分别管理，不能把订阅到期当成已有设备断连。

随后按用户关于 NekoBox/Clash 的询问，另以相同发布 revision 与同一 Android 设备密码渲染完整 `wayvia-clash-meta.yaml`（9,207,514 字节），在一次性容器中通过真实 Mihomo 1.19.32 配置解析，详见 [clash-config-check.json](clash-config-check.json)。首次交付为本地配置，没有修改服务器。目标为 Mihomo/Clash Meta 客户端，不能用旧 sing-box JSON 订阅替代 YAML；Mihomo 的完整公网 TUN 或 Android UI 导入仍未验收。

用户进一步要求链接导入后，使用正式 CLI 为同一设备签发独立 `mihomo` 订阅。通过当前网络直接 HTTPS 下载，公网证书验证开启，返回 200、`application/yaml`，内容与上述已校验文件逐字节一致，详见 [clash-subscription-validation.json](clash-subscription-validation.json)。读取链接有效至 `2026-11-19 08:47:19 UTC`；没有改动数据面或轮换设备密码。订阅地址和 `clash://install-config` 导入链接仅存于私密交付目录，不进入公开证据。

依据 [NekoBox 官方配置文档](https://matsuridayo.github.io/nb4a-configuration/)，其 sing-box 订阅导入会解析出站节点，完整自定义配置则另有 GUI 标签、TUN 地址及版本要求；因此节点协议兼容不等于自动保留 Wayvia 全部分流、DNS 和 IPv6 策略。当前尚未对具体 NekoBox 版本做完整适配。

M6 当前只确认正常服务器链路和外部 Linux 客户端。尚待实体 Android 导入与连接、Wi-Fi/蜂窝切换、校园 DNS/规则、性能与流量费用。真实故障注入及整机重启按用户明确要求另行安排。IP 证书两端续期 dry-run 已通过且四小时 timer 已启用；未来真实自动续期与部署尚未经历一个周期，VPN 365 天叶证书也需要独立更新计划。

## 软件和隔离门禁

同一源码的 `make check`、`make demo`、`make test-configs`、`make test-lab` 均已通过，详见 [部署前完整复验](../deployment-review-2026-10-20/README.md)。本次只新增部署及验收记录，没有修改生产源码或测试，未重复运行已通过的完整门禁。此前首次 test-lab 超时和后续成功记录均保留。实际云主机的正常路径结果与容器故障/恢复实验结果分别记录。
