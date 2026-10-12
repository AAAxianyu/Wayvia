# Wayvia 安装与运维

本手册面向仓库软件和明确授权的独立 Linux 环境。真实云服务器的地址、地域、系统、域名、证书和主机授权属于 M6 输入；本轮没有连接或更改真实服务器。不要在 macOS 或现有 VPN 所在的开发机上执行 Linux 网络计划。

## 安装与本地控制面

固定使用 Python 3.12、uv 0.9.7，Ubuntu 24.04 是 Linux 执行基线。全部 Python 依赖和散列见 uv.lock，外部二进制见 compatibility.yaml 与 deploy/lab/components.lock.json。

```sh
python3 -m venv .uv-bootstrap
.uv-bootstrap/bin/pip install uv==0.9.7
export PATH="$PWD/.uv-bootstrap/bin:$PATH"
uv sync --locked
uv run wayvia --help
uv run wayvia --data-dir .wayvia init
uv run wayvia --data-dir .wayvia validate .wayvia/config.yaml
uv run wayvia --data-dir .wayvia policy explain baidu.com
uv run wayvia --data-dir .wayvia policy explain --ip 223.5.5.5
uv run wayvia --data-dir .wayvia serve
```

初始化创建 0700 本地数据目录、0600 配置与管理员令牌、SQLite 元数据和受限密钥目录。管理员令牌文件为 `.wayvia/secrets/admin-token`，不在常规输出中打印。管理 HTTP 仅绑定回环；服务默认关闭访问日志。`--subscriptions-only` 运行只有健康检查和订阅读取路由的应用，可放在 HTTPS 反向代理之后。任何公网反向代理只能公开 `/subscriptions/`，关闭该位置的访问日志、缓存、错误正文捕获和查询 URL 记录；不能公开 `/api/` 或 agent socket。

默认配置包含脱敏 example 域名，部署前必须换成已授权的地址与真实证书。国内地址规则使用带来源和摘要的 APNIC CN 分配快照；国内域名由固定 dnsmasq-china-list 快照的 110185 条后缀及显式维护的补充表达，运营者应维护 CDN/地理误分类与实际校园公网网段。APNIC 分配地不代表每个地址当前所在地。启动、校园、国内和 VPN 出口 DNS 分别配置；不支持的代理 IPv6 被阻断，国内 IPv6 仍按规则直连。禁止把示例入口当作已部署服务。

## 设备、配置和订阅

默认 production 不会回退到模拟。通过本机 agent 将设备列表应用到真实 Trojan 入口，配置校验、进程重启和运行状态通过后才把设备标为 active。入口重启会关闭全部旧会话，轮换或撤销时会影响当前入口上的其他连接。失败时停受影响入口，并保留 pending/degraded；修复后执行 `device reconcile`。

```sh
uv run wayvia device add phone --platform android
uv run wayvia device list
uv run wayvia device reconcile
uv run wayvia profile export DEVICE_ID --format sing-box --output-file .wayvia/phone.json
uv run wayvia profile export DEVICE_ID --format mihomo --output-file .wayvia/phone.yaml
uv run wayvia subscription issue DEVICE_ID --format sing-box
uv run wayvia device rotate DEVICE_ID
uv run wayvia subscription revoke SUBSCRIPTION_ID
uv run wayvia device revoke DEVICE_ID
```

`device add` 可返回 pending，这代表请求和凭据已持久化，不代表数据面已生效。设备撤销同时撤销相关订阅；单独撤销订阅只拒绝更新链接，已导入的代理密码保持有效。订阅令牌仅存摘要，密码通过 secret_ref 保留在 0600 文件中。订阅每次检查设备状态、令牌期限和格式，响应 `Cache-Control: no-store, private`。

Android/iPhone 的输出格式为 sing-box 和 mihomo，包含入口、TUN、DNS、国内直连、默认代理与 IPv6 防漏规则。真实核心解析通过不等于任一手机应用导入通过；具体 iPhone/Android 客户端及版本需要在 M6 单独验收。

HTTP 管理采用 `Authorization: Bearer <管理员令牌>`。所有写请求需要唯一 `Idempotency-Key`；同键同请求返回同一结果，同键不同输入返回 409。令牌重放响应存在受限密钥文件中，不把明文令牌写入数据库。进程在写操作中途崩溃时，持久预留阻止同键重复签发；返回 request_recovery_required 后先检查设备/订阅状态，再由管理员选新键，不能盲目重试创建。错误统一含 code/message/request_id/details；不返回原始输入。

## 可审核的系统安装布局

安装器先验证官方固定版本 archive 的 SHA256，再解包二进制；不会信任缓存目录中的裸二进制。先构建 wheel，准备软件布局进行审核：

```sh
uv build
uv run python scripts/fetch_components.py --arch arm64 --output .cache/components/arm64
uv run python deploy/install.py --stage .cache/install-review --arch arm64 --api-uid 20001
```

`--stage` 可在普通用户下运行，只写指定目录，状态 prepared 不表示 Linux 服务已运行。amd64 主机用对应的 `--arch amd64` 与下载目录。部署者在明确授权的 Ubuntu 24.04 主机上准备 OpenVPN 2.6.19、iproute2、nftables、OpenSSL、netcat-openbsd、dnsutils、curl、Python3.12、uv0.9.7，停掉本项目既有单元后执行：

```sh
python3.12 deploy/install.py --system --arch arm64 --uv /path/to/uv \
  --wheel dist/wayvia-0.1.0-py3-none-any.whl
```

系统安装创建专属 wayvia 用户、root 与 API 用户分别拥有的运行目录、锁定 venv、软件及 API/agent/恢复/监控单元，然后 daemon-reload；结果 installed_inactive，不自动启动服务或改变网络。生产文件路径固定 `/opt/wayvia`、`/etc/wayvia`、`/var/lib/wayvia`。升级要求先停服务，备份旧清单、配置、密钥、恢复日志与一致的 SQLite 快照；修改过的安装文件需先审核，不能被自动覆盖。网络事务配置的证书私钥属于 root 的 0600 文件；控制面的设备密码属于 wayvia 用户的 0600 文件。

先完成证书、真实入口、规则、探针与 IPv4 转发的目标主机设置，再由管理员启用 `wayvia-recover.service`、`wayvia-agent.service`、`wayvia-api.service`、`wayvia-monitor.timer`。动态数据面单元由事务和恢复流程启动，不能单独 enable，以免绕过保护建立顺序。只在已授权的日本出口主机显式开启 IPv4 转发，事务和安装器均不会暗改全局 sysctl。

## Linux 双角色部署

按日本 exit → 阿里云 ingress → 终端发布的顺序部署。两台主机分别维护事务，不能宣称跨主机原子提交。日本代理只转交本机 OpenVPN TCP 端点；OpenVPN 不对公网监听。阿里云入口在宿主保留接入回包，业务 SOCKS 桥接和 OpenVPN 在 wayvia-egress 内运行，OpenVPN 底层经独立 v2rayA/core SOCKS，代理核心从物理网络连接日本。

准备服务器的证书文件在 `/etc/wayvia/secrets/`：入口 entry-tls.crt/key，代理 carrier-tls.crt/key，VPN vpn-ca.crt、vpn-client.crt/key 或 vpn-server.crt/key；名称以渲染 manifest 的 required_secret_files 为准。证书身份应匹配 server_name，VPN 服务证书必须有 serverAuth 用途，客户端有 clientAuth 用途；不关闭校验。保留服务器间共享 carrier-credential 到控制面的受限密钥文件，不能发给终端。

```sh
uv run wayvia secret import carrier-credential /受限目录/carrier-password
uv run wayvia render .wayvia/config.yaml --role exit --output .wayvia/exit
uv run wayvia render .wayvia/config.yaml --role ingress --output .wayvia/ingress
uv run wayvia status --json
uv run wayvia doctor
```

渲染只生成文件，不更改网络。生成物和计划包含服务器秘密，必须放在受限、已忽略的目录，不能提交到 Git。部署配置、组件路径和独立管理账号只用于专属 Wayvia 实例，不能操作已有 v2rayA 实例。域名 carrier 节点通过配置的 UDP/TCP bootstrap DNS 解析为数字地址后导入，TLS 仍校验原 SNI；当前该适配器不支持 HTTPS/TLS bootstrap，配置在执行前会明确拒绝。

计划输入包括 Linux agent 的当前 baseline、角色、渲染 revision、明确的出口接口及真实探针。`--baseline auto` 从本机 agent 读取计划资源基线；离线预览可显式传 baseline，但应用时必须与目标一致。`examples/ingress-probes.json` 和 `examples/exit-probes.json` 仅提供字段模板，必须替换已授权探针地址、实际出口响应、客户端地址和接口后使用。计划有效期和摘要会在执行时再次检查；有缺失层的探针为 unsupported，不能确认 ACTIVE。

```sh
uv run wayvia plan .wayvia/config.yaml --role exit --baseline BASELINE \
  --exit-interface ETH_INTERFACE --probes .wayvia/exit-probes.json --output-file .wayvia/exit-plan.json
uv run wayvia plan .wayvia/config.yaml --role ingress --baseline BASELINE \
  --probes .wayvia/ingress-probes.json --output-file .wayvia/ingress-plan.json
uv run wayvia apply .wayvia/ingress-plan.json
uv run wayvia confirm TRANSACTION_ID
uv run wayvia rollback TRANSACTION_ID
```

agent 只接受固定文件、服务单元和网络资源动作，通过 Unix socket SO_PEERCRED 限定本机 UID。系统级安装时配置专属服务用户、socket 所有者和 allowed_uid，管理 API 没有任意 shell/ip/nft/systemctl 接口。需要的 systemd 单元在 deploy/systemd；独立恢复任务必须先于对外入口恢复。正常事务安装独立确认超时回退后才改网络，默认 120 秒；CLI/API 退出不取消回退。

## 故障、恢复、升级和卸载

确认前的变更失败或超时按日志恢复本事务拥有的资源，恢复失败是 RECOVERY_REQUIRED。确认后链路故障是 DEGRADED，保护规则保持阻断代理业务。不能把 VPN 失效处理为业务物理直出。检查按入口、carrier、VPN、最终出口、DNS、IPv6、回程分层进行；unknown/unsupported/skipped 都不表示健康。

升级前备份 SQLite（使用 SQLite backup API 或停 API 后备份）、配置、受限密钥和 agent journal；先在隔离实验中运行全部门禁。`uv sync --locked` 安装目标 revision 并运行显式数据库迁移；读取到比当前软件更新的数据库版本会拒绝启动。保留旧环境和备份以供降级，降级数据库用升级前备份，不能用旧软件直接覆盖新 Schema。对两台服务器分别滚动更新和确认，通过前不发布新终端配置。

卸载前停止新订阅发布，撤销设备并验证入口不接受新连接，回退需要删除的 Wayvia 事务，再停专属服务。只移除 Wayvia 的单元、socket、命名空间、veth 和 nft 表；不 flush 全局规则，不改宿主默认路由，不删除其它 VPN。`make clean-lab` 只清理实验脚本自己创建的容器。软件布局卸载使用 `python deploy/install.py --stage DIR --uninstall` 或在已授权且停掉服务的系统上使用 `python3.12 deploy/install.py --system --uninstall`；默认保留控制面数据库、凭据、agent journal、venv 和账号，网络资源必须先经事务回退。生产运行材料由管理员备份或按保留政策删除；本地演示和实验的临时私钥结束后清理。

## 验证和退出码

```sh
make check
make demo
make test-configs
make test-lab
make clean-lab
```

退出码：0 成功；2 输入/配置；3 凭据；4 环境不支持；5 运行失败；6 等待事务确认。设备控制命令返回状态对象时，应同时检查 status，pending/degraded 不是已生效。非特权软件检查、真实配置校验、隔离链路和实网验收分别记录，详见 implementation-status.md 和 validation。实验缺环境必须非零返回，不静默跳过。

## 更新规则

固定来源与 SHA256 后用 `wayvia rules import-apnic`、`import-domains` 或 `import-dnsmasq` 生成 RuleSet，显式替换配置中的相应 rulesets，再 validate、render、plan 与确认；不自动追随 mutable 在线源。`uv run python scripts/fetch_domestic_rules.py --source .cache/domestic-domains.conf` 可从锁定源重现内置域名快照。域名源中的 #disabled 行不会变成直连规则，源自带 DNS 地址不写入客户端。
