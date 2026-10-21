# 双云部署准备记录

2026-10-20。源码基线 `0e5bbbc63b59031377b698311f0cb6fda44c5401`，工作分支 `codex/deployment-readiness-20261020`。用户授权在指定两台主机部署，真实故障与整机重启测试另行安排；没有域名，优先使用 IP 证书。

本目录保留放行端口前的准备快照。用户随后放行端口，双端 ACTIVE、配置发布和公网正常路径已通过，最新结果见 [双云部署验收](../cloud-deployment-2026-10-20/README.md)。

## 放行端口前的结果与边界

两端软件安装、私密材料、IP 证书、续期预演及本地控制面已完成。**数据面尚未 apply/confirm，尚未发布配置，也未签发终端凭据。** 两端 `status` 的事务列表均为空，不能报告服务器链路或手机验收通过。结构化结果见 [summary.json](summary.json)，版本和状态见 [versions-and-state.json](versions-and-state.json)。

阻塞发生在阿里云公网入站：独立 HTTPS 监听 `9443/TCP` 的本机 TLS 验证成功，日本和开发网络访问均超时；现有 `443/8080` 可达，主机 `iptables INPUT` 为 ACCEPT。已请用户在阿里云安全组放行 `8443/TCP`（Trojan 接入）及 `9443/TCP`（HTTPS 订阅和受控探针），并检查云防火墙。没有通过修改既有网站来绕开这个条件。`8443` 的 Wayvia 数据面尚未启动，不能把它记为已经完成监听验证。

## 已执行工作

- 日本原系统是 CentOS 7，存在旧 OpenVPN。用户自行重装后，通过新 SSH 端口登录，确认 Ubuntu 24.04.1、amd64、出口接口 `ens17`，原 VPN 已不存在，443 空闲。没有声称替用户完成旧系统备份。
- 阿里云为 Ubuntu 24.04.3、amd64、出口接口 `eth0`。存在 Nginx 与其他 Docker 业务，保留其监听和配置；Wayvia 使用独立端口。Nginx 配置检查、既有虚拟主机文件摘要和服务状态检查通过。
- 两端安装同一个 SHA256 校验通过的软件包，外部组件由安装器再次对照锁文件验证。包摘要见 [bundle.json](bundle.json)。OpenVPN 固定 `2.6.19-0ubuntu0.24.04.4`，uv 固定 `0.9.7`。没有执行全系统升级。
- Wayvia 的 API、agent、recover 和 monitor 已启动，API 仅绑定 `127.0.0.1:8787`。独立 HTTPS 端点只代理 `/subscriptions/`，关闭访问及错误日志；`/api/v1/devices` 的本机 TLS 请求返回 404。
- 两台主机采用相同逻辑配置：国内直连、国外默认代理、代理 IPv6 阻断、VPN-over-proxy 顺序保持不变。VPN 子网为 `10.77.0.0/24`，阿里云业务隔离网段为 `10.89.0.0/30`。校园 DNS/规则尚未取得，默认配置不能算校园验收。
- VPN CA 与签发材料放在日本 root 私有目录；客户端私钥在阿里云生成，服务端私钥在日本生成。服务器间承载凭据和 tls-crypt 密钥经 SSH 在内存中传输，没有进入终端配置或此证据目录。VPN 叶证书有效期 365 天，后续需要独立更新计划。
- 日本显式开启 IPv4 转发，持久化到专属 sysctl 文件。专属 nft 表仅限制承载 `443/TCP` 的来源，允许阿里云及本机探针；SSH 和其他端口不受此表限制。两端宿主默认路由保持不变。
- 数据面计划已生成供审核。执行时必须重新读取 baseline 并重新生成计划，不能使用等待期间过期的计划。辅助脚本见 [activate-role.py](activate-role.py)，它不绕过 Wayvia 的 apply/confirm 检查。

## IP 证书与续期

两端分别在独立 ACME 目录使用 Certbot `5.8.0`，先通过 staging，再取得生产证书。证书 SAN 与对应公网 IP 一致；日本证书有效期至 **2026-10-26 23:11:25 UTC**，阿里云至 **2026-10-26 23:18:07 UTC**。既有网站的证书及续期账户没有替换。

日本使用 standalone HTTP-01；阿里云使用已有通用 ACME challenge webroot，只临时创建自己的随机 challenge 文件，不改原站点配置。两端独立续期 dry-run 均通过。`wayvia-certificate-renew.timer` 每四小时检查一次并随机延迟最多五分钟；Certbot 自带随机休眠已关闭，避免与 systemd 超时冲突。未来真实自动续期尚未经历一个周期，不能将 dry-run 当成该周期的证据。

[renew-deploy.py](renew-deploy.py) 在部署前验证公网证书链、IP SAN、剩余有效期及证书/私钥配对，持有对应 Wayvia 角色锁后更新 root 私有文件，只重启已活跃的本角色入口，不启动被阻断或未启用的数据面服务，不清除 agent 安全状态。阿里云证书更新后先检查 Nginx 配置再 reload。

Let’s Encrypt 已支持短期 IP 证书，相关依据见 [IP 证书正式可用说明](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) 和 [Certbot IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。本次结论同时有真实生产签发与 staging 续期结果支持。

## 保留的失败与修复记录

阿里云 Wayvia 安装成功后，Certbot 依赖下载长时间停滞。仅终止该下载进程，原组合安装命令因此退出 143；随后使用与锁文件散列相符的 18 个离线 wheel 安装成功。没有终止已运行的业务服务。

初次续期单元检查发生在阿里云 Certbot 尚未安装完时，因可执行文件不存在而失败；完成离线安装后再次检查通过。日本首次 dry-run 进入 Certbot 的约 440 秒随机等待，终止该等待后以 `--no-random-sleep-on-renew` 重跑通过。失败退出码保留在 [operations.jsonl](operations.jsonl) 和相应日志中。

## 当时的后续部署计划（执行结果见最新验收）

1. 用户放行云端端口后，从日本再次访问阿里云 `9443` 探针，确认返回实际日本出口地址。若实际出口与已知公网 IP 不同，按真实观测修正预期并保留依据。
2. 日本 exit 生成新计划、apply、立即 confirm；阿里云 ingress 随后执行同样流程。确认失败由现有事务机制保护和回退，不伪造探针结果。
3. 选定终端后创建独立设备，协调凭据，等待双方新鲜 monitor 证据，经受限 SSH 传递日本状态后显式 publish。
4. 验证完整入口请求、双层封装、DNS/IPv6、UDP及国内直连，之后交付完整客户端配置。手机导入、Wi-Fi/蜂窝切换、校园规则、性能及流量仍需各自验收。
5. 真实故障注入和整机重启继续等待另行安排。本轮故障与恢复证据只来自已通过的隔离实验。

此目录对服务器 IP、主机名作脱敏，保留版本、摘要和退出码。实际配置、私钥、承载密码、管理员令牌与未发布计划均留在受限部署目录，不提交 Git。
