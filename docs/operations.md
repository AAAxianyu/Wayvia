# Wayvia 安装与运维

本手册面向仓库软件和明确授权的独立 Linux 环境。真实云服务器的地址、地域、系统、域名、证书和主机授权属于 M6 输入；已授权部署与软件验证分别记录，最近一次保留数据升级见[记录](validation/runtime-upgrade-2026-10-26/README.md)。不要在 macOS 或现有 VPN 所在的开发机上执行 Linux 网络计划。

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

本节的 `uv run` 命令仅操作开发目录 `.wayvia`。系统安装后使用下文的 `wayvia-system` 命令，它与安装的 API 共用同一个状态目录和服务身份；不要用开发目录管理同一个生产 agent。

默认 production 不会回退到模拟。通过本机 agent 将设备列表应用到真实 Trojan 入口，配置校验、进程重启和运行状态通过后才把设备标为 active。入口重启会关闭全部旧会话，轮换或撤销时会影响当前入口上的其他连接。已有入口同步失败时停受影响入口，并保留 pending/degraded；修复后执行 `device reconcile`。首次部署前，只有 agent 明确返回 `INGRESS_NOT_DEPLOYED` 时才保持 pending 而不新增入口阻断，所需凭据仍已持久化；其它失败继续尝试保护入口。

以下生产 `profile export` 和 `subscription issue` 命令要求设备已 active，并完成下文双角色确认后的 `config publish`。只创建设备或只生成计划不会满足发布条件。

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

安装器支持调用方使用 `umask 077`：明确设置公开运行父目录 `/opt/wayvia` 为 0755，仅创建 Python 运行环境的两个 uv 子进程使用 022；调用方及后续秘密文件仍保留原 umask。安装会强制重装 Wayvia 包，以免相同版本号的新 wheel 被跳过，并以 `wayvia` 服务账户执行版本检查。私密配置和状态目录继续保持 0700/0600。已存在的错误权限 venv 不会被宽泛递归放权；出现 `runtime_unreadable` 时，先在停服维护范围内核对拥有权、备份和公开运行环境，再定向修复或重建。不要对 `/etc/wayvia`、控制面或密钥目录执行 `chmod -R`，也不要用继承 077 的外部 uv 重装脚本替代正式安装器。

Mihomo 完整订阅优先使用 PyYAML 的 `CSafeDumper`，不可用时回退 `SafeDumper`；两者都只输出安全 YAML 数据。每次请求仍执行令牌、期限、设备状态、发布快照和当前凭据检查，不缓存含凭据的最终响应。性能对照和严格权限验证见[服务器续查报告](validation/server-followup-2026-10-20/README.md)。

安装器在第一次软件文件变更前写入受限事务日志。安装或升级中断后，重新执行同一安装命令会先恢复上次文件前态再安装；也可先显式运行 `python3.12 deploy/install.py --system --recover`，stage 模式对应 `--stage DIR --recover`。若中断后管理员又改动相关文件，恢复会拒绝覆盖并保留日志，需要先审核该文件。提交后的 manifest 是完成边界；系统运行环境安装失败时状态保持 `runtime_install_pending`，重新执行安装命令继续完成依赖和服务账户配置。旧版本删除的文件只在摘要未变时自动清理；管理员修改过的旧文件继续记录所有权并报告保留。

系统安装、恢复和卸载使用独立操作锁串行执行，锁覆盖账户、软件布局、venv 与完成标记。首次创建账户前记录预留组身份；建组后中断可安全重试，已有但没有本项目创建记录的同名独立组会被拒绝，不会被自动接管。

系统运维使用安装的 venv 和 `wayvia` 账户。以下函数可在管理员 shell 定义；`--system` 固定状态、配置和 socket 路径，并拒绝错误身份或路径覆盖：

```sh
wayvia-system() {
  sudo -u wayvia /opt/wayvia/.venv/bin/wayvia --system "$@"
}
wayvia-system validate /var/lib/wayvia/control/config.yaml
wayvia-system device list
wayvia-system status --json
```

API 与该 CLI 共用 `/var/lib/wayvia/control/config.yaml`、`metadata.db`、`secrets/` 和 `/run/wayvia/agent.sock`。修改系统配置时保留 `wayvia` 所有权及 0600 权限。API 在启动时加载配置；修改后按发布流程处理版本，并重新启动 API 以载入新逻辑配置。API 管理令牌位于 `/var/lib/wayvia/control/secrets/admin-token`，仅从受限管理路径读取。

先完成证书、真实入口、规则、探针与 IPv4 转发的目标主机设置，再由管理员启用 `wayvia-recover.service`、`wayvia-agent.service`、`wayvia-api.service`、`wayvia-monitor.timer`。动态数据面单元由事务和恢复流程启动，不能单独 enable，以免绕过保护建立顺序。只在已授权的日本出口主机显式开启 IPv4 转发，事务和安装器均不会暗改全局 sysctl。

## Linux 双角色部署

按日本 exit → 阿里云 ingress → 终端发布的顺序部署。两台主机分别维护事务，不能宣称跨主机原子提交。日本代理只转交本机 OpenVPN TCP 端点；OpenVPN 不对公网监听。阿里云入口在宿主保留接入回包，业务 SOCKS 桥接和 OpenVPN 在 wayvia-egress 内运行，OpenVPN 底层经独立 v2rayA/core SOCKS，代理核心从物理网络连接日本。

准备服务器的证书文件在 `/etc/wayvia/secrets/`：入口 entry-tls.crt/key，代理 carrier-tls.crt/key，VPN vpn-ca.crt、vpn-client.crt/key 或 vpn-server.crt/key；名称以渲染 manifest 的 required_secret_files 为准。证书身份应匹配 server_name，VPN 服务证书必须有 serverAuth 用途，客户端有 clientAuth 用途；不关闭校验。保留服务器间共享 carrier-credential 到控制面的受限密钥文件，不能发给终端。

```sh
wayvia-system secret import carrier-credential /var/lib/wayvia/control/incoming/carrier-password
wayvia-system render /var/lib/wayvia/control/config.yaml --role exit --output /var/lib/wayvia/control/generated/exit
wayvia-system render /var/lib/wayvia/control/config.yaml --role ingress --output /var/lib/wayvia/control/generated/ingress
wayvia-system status --json
wayvia-system doctor
```

`incoming/carrier-password` 由管理员预先放入本机控制目录，目录 0700、文件 0600 且归 `wayvia` 所有。两台服务器分别执行自己的角色命令，服务器 TLS/VPN 私钥仍位于 root 管理的 `/etc/wayvia/secrets/`。

渲染只生成文件，不更改网络。生成物和计划包含服务器秘密，必须放在受限、已忽略的目录，不能提交到 Git。部署配置、组件路径和独立管理账号只用于专属 Wayvia 实例，不能操作已有 v2rayA 实例。域名 carrier 节点通过配置的 UDP/TCP bootstrap DNS 解析为数字地址后导入，TLS 仍校验原 SNI；当前该适配器不支持 HTTPS/TLS bootstrap，配置在执行前会明确拒绝。

计划输入包括 Linux agent 的当前 baseline、角色、渲染 revision、明确的出口接口及真实探针。`--baseline auto` 从本机 agent 读取计划资源基线；离线预览可显式传 baseline，但应用时必须与目标一致。`examples/ingress-probes.json` 和 `examples/exit-probes.json` 仅提供字段模板，必须替换已授权探针地址、实际出口响应、客户端地址和接口后使用。计划有效期和摘要会在执行时再次检查；有缺失层的探针为 unsupported，不能确认 ACTIVE。

```sh
wayvia-system plan /var/lib/wayvia/control/config.yaml --role exit --baseline auto \
  --exit-interface ETH_INTERFACE --probes /var/lib/wayvia/control/exit-probes.json \
  --output-file /var/lib/wayvia/control/exit-plan.json
wayvia-system plan /var/lib/wayvia/control/config.yaml --role ingress --baseline auto \
  --probes /var/lib/wayvia/control/ingress-probes.json \
  --output-file /var/lib/wayvia/control/ingress-plan.json
wayvia-system apply /var/lib/wayvia/control/ingress-plan.json
wayvia-system confirm TRANSACTION_ID
wayvia-system rollback TRANSACTION_ID
```

探针模板的 access 地址/端口必须匹配实际入口 listener（0.0.0.0 使用 127.0.0.1，:: 使用 ::1）；入口的 access.url / expected 必须与 egress.target / expected 一致。出口状态 ready 表示组件准备完成，不表示手机业务已走通。旧 tcp/route 形式的 access/carrier/vpn 不再满足确认条件，升级时重新渲染和生成计划。OpenVPN 单元在 /run/wayvia 下创建仅 root 可用的 management socket；内部入口探测凭据由 agent 管理，不可当作设备凭据分发。

日本主机应用并确认自己的 `exit-plan.json`，阿里云主机随后应用并确认自己的 `ingress-plan.json`；不要在同一主机同时执行两种角色。设备管理同样使用系统入口：

```sh
wayvia-system device add phone --platform android
wayvia-system device reconcile
wayvia-system device list
```

第一次部署可先创建设备：若 agent 明确确认尚无入口部署，则设备保持 pending，凭据供入口计划加载；网络事务成功后再 reconcile，并检查设备已 active。reconcile 会使已有探针证据失效，发布前需等待 monitor 完成新一轮真实验证。网络探针使用独立的内部凭据。终端配置发布是单独步骤：两台服务器使用相同逻辑配置 revision，各自确认并获得新鲜监控证据后，在日本主机执行 `wayvia-system status --json`，通过可信管理通道将结果保存为阿里云的 `/var/lib/wayvia/control/exit-status.json`（归 `wayvia`，0600）。然后在阿里云执行：

```sh
wayvia-system config publish --exit-status /var/lib/wayvia/control/exit-status.json
wayvia-system config publication
wayvia-system profile export DEVICE_ID --format sing-box --output-file /var/lib/wayvia/control/generated/phone.json
wayvia-system subscription issue DEVICE_ID --format sing-box
wayvia-system device rotate DEVICE_ID
wayvia-system subscription revoke SUBSCRIPTION_ID
wayvia-system device revoke DEVICE_ID
```

发布检查两端的确认版本和不超过 90 秒的路径证据；过期时重新采集，不编辑状态文件伪造通过。首次未发布返回 `configuration_unpublished`。后续修改逻辑配置、计划未确认或应用失败时，订阅继续使用上次发布快照；只有再次显式 publish 才更新终端入口与策略。管理 API 的 `POST /api/v1/config/publication` 接收 `{"exit_status": ...}`，同样要求管理员认证与幂等键；GET 只返回发布修订元数据。完整状态文件应通过受限管理路径传输，它不是公开订阅内容。

agent 只接受固定文件、服务单元和网络资源动作，通过 Unix socket SO_PEERCRED 限定本机 UID。系统级安装时配置专属服务用户、socket 所有者和 allowed_uid，管理 API 没有任意 shell/ip/nft/systemctl 接口。需要的 systemd 单元在 deploy/systemd；独立恢复任务必须先于对外入口恢复。正常事务安装独立确认超时回退后才改网络，默认 120 秒；CLI/API 退出不取消回退。

## 故障、恢复、升级和卸载

确认前的变更失败或超时按日志恢复本事务拥有的资源，恢复失败是 RECOVERY_REQUIRED。确认后链路故障是 DEGRADED，保护规则保持阻断代理业务。不能把 VPN 失效处理为业务物理直出。检查按入口、carrier、VPN、最终出口、DNS、IPv6、回程分层进行；unknown/unsupported/skipped 都不表示健康。

升级前备份 SQLite（使用 SQLite backup API 或停 API 后备份）、配置、受限密钥和 agent journal；先在隔离实验中运行全部门禁。开发环境的 `uv sync --locked` 只同步依赖，不执行数据库迁移。系统升级应在全部 Wayvia 单元停止后，使用新源码、锁定组件和新 wheel 重跑 `deploy/install.py --system`；它更新 `/opt/wayvia/.venv`，不能用仓库内 `uv sync` 代替系统升级。安装完成后先执行 `wayvia-system status --json`：数据库在控制面初始化时运行显式迁移；读取到比当前软件更新的数据库版本会拒绝启动。然后启动恢复、agent、API 与 monitor 单元并检查实际状态。升级会修改当前 venv，降级需要用备份中的旧 wheel、源码锁文件与组件重新安装旧运行环境，并恢复升级前数据库，不能用旧软件直接覆盖新 Schema。对两台服务器分别滚动更新和确认，通过前不发布新终端配置。

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

## 保留数据的滚动升级工具

`deploy/upgrade.py` 只面向已授权的现有安装，默认预检，`--apply` 才停服；先推送并在目标主机 pull 精确 commit，再给出角色、commit、arch、uv、components 和 wheel 路径。要求干净的 tracked Git checkout、无安装文件漂移、组件与 wheel/source 一致。按 exit → ingress 运行，并把整个升级进程放进受限 systemd 临时作业（MemoryHigh 640M、MemoryMax 768M、CPUQuota 100%、RuntimeMaxSec 600）。不要在小内存生产机运行完整 ASGI/大规则软件回归。

工具冻结现有服务并备份本次当前状态，迁移后比较身份、密钥和累计统计，复用原恢复/监控并检查角色 ACTIVE 与 API 就绪。失败先停止新版本、保留新状态，再恢复本次备份及旧运行时；不能用旧 wheel 直接打开已迁移的新 DB。不会启用独立观察或告警投递。全量升级后不保留 schema 3 的 API ExecStart/PYTHONPATH 覆盖；Wayvia 反向代理透传完整 Host（含非默认端口），API 仍只监听本机，公网仅公开私人订阅读取路径。

## 订阅入口限流

用户确认的入口方案为每来源平均 5 次/分钟，允许突发 5 次且不排队；整个订阅路径同时最多 2 个请求，包括信息页，超限返回 429。首次空桶可立即通过 6 次请求，后续额度按约 12 秒一次恢复；设备共享 NAT 时也共享来源额度。限流不记录完整 URL 或令牌，不主动断开现有代理连接，也不能阻止带宽层面的分布式攻击。

`deploy/subscription_limits.py` 对固定的 Wayvia Nginx 配置添加限流，`--apply` 要求原配置和已隔离验证候选的精确 SHA256；变更前保存 root-only 备份，执行 `nginx -t` 与服务重载。校验或重载失败时尝试恢复原配置；只有在实时配置仍为工具写入的候选版本时，`--restore BACKUP_DIRECTORY` 才恢复，避免覆盖其他人的改动。回退工具必须使用当前经审查的仓库副本；它不会自动部署到服务器。具体部署结果和备份位置见[验证记录](validation/subscription-limits-2026-10-27/README.md)。

隔离测试使用 `scripts/test_subscription_limits.py --original PATH`：给定经过授权取得的 Wayvia Nginx 原配置，在一次性、禁止外网的 Linux 容器里替换为合成证书和合成订阅，检查正常下载、拒绝和恢复。不要把真实订阅、设备密码、证书私钥或完整 URL 传入实验容器或证据。生产环境只做已授权的正常订阅就绪检查，不执行超限/故障实验。

## 私有日志与管理员平台

新增日志采集、受限两端传输、一次性登录码、元数据查询、导出和安全回退见[独立运维说明](logging-admin.md)。仓库实现不自动安装或启动生产采集；未来切换必须先验证 Nginx 覆盖可信请求 ID，再启用信任该头的公开 API 路径。软件验收与生产验收分别记录。
