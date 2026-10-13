# Wayvia 途序产品开发文档

更新于 2026 年 10 月 12 日

本开发文档把 [网络设计方案](design.md) 转化为工程结构、模块契约和可验证的开发任务，供开发者与编码代理在 Wayvia 仓库内执行。文档中的目录、接口和命令是待实现的开发约定。

当前仓库已有设计与产品介绍，尚无业务实现。本次开发目标是交付可运行的项目、真实功能模块、Linux 部署适配和可复现的验证流程。产品介绍采用面向用户的表述；工程完成情况以代码、测试与验收记录为准。

## 1 架构约束

以下约束继承设计方案，所有模块与测试均需保持：

1. 国内、校内与局域网流量在终端或网关直连。国外及未分类目标进入阿里云。
2. 国外数据路径固定为：终端 → 阿里云接入服务 → 阿里云 OpenVPN 客户端 → 阿里云代理客户端 → 日本代理服务端 → 日本 OpenVPN 服务端 → 目标服务。
3. 阿里云先封装 VPN，再由代理承载 VPN 连接；日本按相反顺序解封装。
4. 日本代理与 VPN 部署在同一台服务器，代理仅转交指定本机 VPN 端点。
5. 手机配置只包含阿里云入口及终端自己的凭据，服务器之间的代理和 VPN 凭据保留在服务器侧。
6. 指定走代理的业务在链路故障时保持阻断；国内直连不受该故障影响。
7. 阿里云接入回包、SSH 管理和代理到日本公网端点的通信具有明确的独立路径。
8. 项目独立实现与交付。Air 仅提供架构参考，不构成运行依赖。

默认规则不设置优先于国内直连的强制海外出口例外。调整架构约束需要新的明确产品决策；实现方式的选择在 ADR 中说明即可。

## 2 开发范围与交付层次

### 本次核心范围

- 可安装的 Python 工程、命令行工具、配置和规则模型。
- 设备凭据管理、配置导出、私人订阅与配置版本管理。
- 阿里云入口、日本代理与 VPN、v2rayA 适配及 Linux 路由规则。
- 网络操作的预览、应用、确认、回退、状态与故障诊断。
- 真实组件配置校验、隔离网络实验和交付文档。
- Android 与 iPhone 的配置适配接口，以及已经选定格式的可用实现。

不自行编写加密协议或 VPN 内核。数据面复用代理核心与 OpenVPN；Wayvia 管理其配置、权限、生命周期和证据。

### 后续扩展接口

电脑配置和 OpenWrt 网关沿用同一规则模型。Web 管理界面、原生手机 App、多出口调度、计费套餐与复杂多租户系统不作为本次框架完成的必要条件。

若具体 iPhone 客户端尚未选定，应完成格式适配接口、能力清单与现有可验证格式。设备实际导入和行为验收保留为独立项目，不以某份 YAML 能解析代替手机兼容性。

## 3 工程技术基线

以下是本开发文档给出的默认实现选择。若发现明确的兼容性问题，可通过 ADR 调整同一职责内的实现，并保持第 1 节架构约束。

| 层次 | 默认选择 | 使用方式 |
| --- | --- | --- |
| 主语言 | Python 3.12 | 固定项目 Python 版本，后续按兼容矩阵扩展 |
| 依赖管理 | uv 与 uv.lock | 锁定依赖，开发与 CI 使用相同解析结果 |
| 配置模型 | Pydantic 2 与 YAML | 严格校验、拒绝未知字段，输出版本化 JSON Schema |
| 命令行 | Typer | 使用 wayvia 命令，提供 JSON 输出和稳定退出码 |
| 配置与订阅 API | FastAPI | 管理接口绑定本机，单独发布订阅读取端点 |
| 持久化 | SQLite 与显式迁移 | 保存设备、策略、订阅及计划元数据 |
| 密钥引用 | 受权限保护的文件存储 | 数据库记录 secret_ref，敏感值按职责读取 |
| 网络执行 | Linux network namespace、nftables、iproute2、systemd | 隔离业务出口，仅修改 Wayvia 所有的资源 |
| 终端入口与日本代理 | sing-box 候选实现 | 锁定版本并验证相应协议与配置 |
| 阿里云代理客户端 | v2rayA 与匹配的代理核心 | 通过版本适配器管理本地代理能力 |
| VPN | OpenVPN | 配置与生命周期交由 Wayvia 管理 |
| 检查工具 | Ruff、pytest、构建检查 | 重点覆盖规则、凭据、事务与真实数据路径 |

uv 提供项目锁文件与同步工作流；FastAPI 提供类型驱动的 API；Python 自带 SQLite 接口，可满足当前单入口管理元数据的需要。[uv 项目文档](https://docs.astral.sh/uv/guides/projects/) · [FastAPI 文档](https://fastapi.tiangolo.com/) · [Python SQLite 文档](https://docs.python.org/3.12/library/sqlite3.html)

生产网络适配先限定在 Ubuntu 24.04 LTS 的受支持环境。macOS 可执行配置生成、API、存储和单元测试；真实网络实验在隔离的 Linux 环境中运行。

初始化时创建 compatibility.yaml，记录系统、内核、Python、代理核心、v2rayA、OpenVPN 和客户端格式的具体版本、来源、校验值与验证状态。不得在部署时依赖未锁定的 latest，也不得把未经实测的版本组合标为支持。

## 4 推荐目录

```text
Wayvia/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
├── Makefile
├── compatibility.yaml
├── src/wayvia/
│   ├── cli.py
│   ├── config/             # 配置模型、加载、版本迁移
│   ├── domain/             # 设备、策略、计划、事务与状态模型
│   ├── policy/             # 规则编译、解释、冲突检查
│   ├── render/             # 客户端与服务端配置渲染
│   ├── services/           # 设备、订阅、计划、诊断应用逻辑
│   ├── storage/            # SQLite 仓储与迁移
│   ├── secrets/            # secret_ref 解析与权限检查
│   ├── api/                # FastAPI 与认证
│   ├── agent/              # 本机特权执行协议与事务调度
│   └── adapters/
│       ├── singbox/
│       ├── v2raya/
│       ├── openvpn/
│       ├── linux/
│       └── fake/           # 仅测试和显式演示模式
├── schemas/                # 导出的配置和操作计划 Schema
├── examples/               # 脱敏示例及受控实验配置
├── deploy/
│   ├── systemd/
│   └── lab/                # 一次性 Linux 实验环境定义
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   └── lab/
├── docs/
│   ├── design.md
│   ├── product-overview.md
│   ├── development.md
│   ├── development-goal.md
│   ├── implementation-status.md
│   ├── operations.md
│   ├── compatibility.md
│   ├── adr/
│   └── validation/
└── .github/workflows/
```

按功能建立目录，避免先生成大量空模块。AGENTS.md 应简短指向设计约束、本开发文档、验证命令和进度记录。所有生成物、数据库、运行日志、真实凭据和私有配置加入忽略规则。

## 5 进程与权限分工

| 进程 | 权限与网络边界 | 职责 |
| --- | --- | --- |
| wayvia CLI | 普通用户，按需访问本机管理端点 | 校验、导出、提交计划、查看状态 |
| wayvia API | 独立服务用户 | 设备与订阅管理、配置导出、读取运行状态 |
| wayvia agent | 本机受限特权进程 | 执行允许的 Linux 操作、管理事务与恢复 |
| 代理核心与 OpenVPN | 按组件最小权限运行 | 承担数据传输 |
| systemd 回退与恢复任务 | 与网络执行职责一致 | 在控制进程退出或系统重启时恢复状态 |

管理 API 默认只监听本机，使用管理员凭据认证。HTTPS 反向代理只公开订阅读取路径；管理路由、运行时控制端点和特权 agent 均不公开。

agent 使用 Unix socket，以本机用户或用户组校验调用方。请求是带版本的操作计划，使用固定动作类型和结构化参数；不接受任意 shell、任意脚本路径或未经限制的文件写入。

CLI、API 和 agent 共享领域契约。API 不拥有直接执行 nft、ip 或 systemctl 的通用接口。

## 6 数据面实现建议

### 业务出口使用独立命名空间

采用 wayvia-egress 命名空间承载阿里云的业务出口。入口服务的客户端连接留在入口所在网络环境；入口服务把新建业务连接交给命名空间内的受限 SOCKS 出口桥接服务。桥接服务发往目标的连接通过 OpenVPN TUN。

```text
阿里云入口所在网络
  接入核心
    ├─ 客户端连接及回包 → 当前入口路径
    └─ 新建业务连接 → 受限内部 SOCKS 桥接
                         │
                 wayvia-egress
                   业务出口桥接
                         ↓
                   OpenVPN TUN
                         ↓
                   OpenVPN 客户端
                         ↓ 本地 SOCKS 代理
                   v2rayA / Core
                         ↓ 仅到日本代理端点
                 物理网络 → 日本
```

图中的两个 SOCKS 接口职责不同：业务出口桥接承载解开终端接入后的业务；v2rayA 的 SOCKS 接口承载 OpenVPN 底层连接。两者使用不同监听端点和权限，不得相互递归。

业务桥接同时验证 TCP 与 UDP 转发，包括 UDP relay 返回的地址能够从入口所在网络到达。不能把跨命名空间不可达的回环地址交给入口，也不能用仅 TCP 的桥接替代 UDP 业务支持。业务目标解析经 VPN 出口，启动解析使用独立的受限路径。

桥接服务只接受指定入口进程或接口的连接，不暴露公网。其入站连接回包通过明确的内部路由返回；其到目标的新连接只能从 TUN 发出。Linux network namespace 可隔离设备、路由和网络规则，具体边界需通过实验验证。[Linux 网络命名空间说明](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)

### 优先建立可验证的代理承载基线

OpenVPN 支持通过 SOCKS5 连接远端，可优先使用该能力把底层连接交给 v2rayA 提供的本地代理。日本代理把该连接转交同机 VPN 服务。[OpenVPN SOCKS5 说明](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html)

受控实验先建立 OpenVPN TCP 加 SOCKS5 的功能基线，并验证隧道内的 TCP 与 UDP 业务。底层使用 TCP 不代表隧道内只能传输 TCP。该组合的性能取舍必须记录；UDP 底层承载在端到端支持确认后增加，不能仅切换一个配置字段便声明可用。

VPN 的远端逻辑地址由日本代理解释。若使用日本本机监听地址，必须验证该地址由日本代理实际连接，避免错误连接到阿里云本机。v2rayA 的实际 API、配置方式和端口以锁定版本为准，不猜测接口或直接改写其数据库。

如果所选版本不支持该基线，可在保持双层顺序的前提下实现 TPROXY 适配；需要新增 ADR 与等价的数据路径测试，不能改为 OpenVPN 直连作为完成方式。

### 最小放行与回程

- 命名空间到物理网络仅允许指定代理核心访问日本代理端点，以及明确列出的启动解析流量。
- 业务桥接的目标连接只能使用 VPN TUN；VPN 失效后由独立规则阻断物理出站。
- 日本代理只允许指定的本机 VPN 地址、端口和协议；VPN 服务不作为终端入口。
- 日本 VPN 启用对应转发、NAT 和回程规则。
- 宿主机默认路由与其他服务规则保持独立。Wayvia 使用带前缀的命名空间、链、表、接口和服务单元，变更前检查名称和路由表冲突。
- 自动分配的私有网段需检测与宿主、VPN 及局域网现有路由的冲突。
- 管理回包、业务桥接回包、VPN 底层连接和代理公网连接分别有路径测试。

这是建议的工程分解，不表示该命名空间方案已经通过验证。只有真实网络实验成功后，才将对应适配标为已验证。

## 7 领域模型与配置契约

### 必要模型

| 模型 | 关键字段与约束 |
| --- | --- |
| Deployment | id、role、配置版本、期望状态；role 为 ingress 或 exit |
| Device | id、名称、平台、状态、凭据版本；创建后先 pending |
| CredentialRef | 类型、secret_ref、版本、失效时间；不在普通 API 返回敏感值 |
| Subscription | 令牌摘要、device_id、格式、有效期、撤销状态 |
| RuleSet | 来源、版本、摘要、域名与 IPv4/IPv6 规则 |
| Policy | 国内直连、内网例外、默认代理、DNS 与 IPv6 策略 |
| Plan | id、role、输入摘要、基线摘要、有序动作、资源清单 |
| Transaction | plan_id、状态、回退期限、操作日志、结果证据 |
| ProbeResult | 层次、时间、结果、错误码、耗时与脱敏详情 |
| AuditEvent | 操作主体、动作、对象、结果与时间 |

SQLite 保存控制面元数据。agent 的特权执行日志与恢复状态保存在独立、受限目录，通过本机协议提供脱敏查询。

secret_ref 只能指向按职责配置的密钥目录，校验文件所有者、权限及符号链接。普通 API 不允许提交任意文件路径，特权执行端不得借密钥引用读取无关的系统文件。

### 配置示意

下例说明字段职责，具体 Schema 在实现时固定；示例域名不用于真实部署。

```yaml
schema_version: 1
deployment:
  id: wayvia-demo

entry:
  endpoint: entry.wayvia.example:443
  protocol: trojan
  tls_certificate_ref: entry-tls

carrier:
  endpoint: exit.wayvia.example:443
  protocol: trojan
  credential_ref: carrier-credential

vpn:
  transport: tcp
  remote_on_exit: 127.0.0.1:1194
  client_certificate_ref: vpn-client
  server_certificate_ref: vpn-server

policy:
  domestic_rules_ref: domestic-rules
  local_rules_ref: local-rules
  default_action: proxy
  proxy_ipv6: block_until_supported

dns:
  domestic_resolver_ref: domestic-dns
  internal_resolver_ref: internal-dns
  proxy_resolver_ref: exit-dns
  bootstrap_resolver_ref: bootstrap-dns
```

同一份逻辑配置按 role 编译为阿里云与日本的不同部署产物。终端配置是第三类产物，只包含阿里云入口及设备凭据。渲染器检查角色隔离，防止把服务器私钥或日本节点凭据写入手机订阅。

配置错误返回字段路径和错误码。未知字段、无效协议组合、冲突网段、失效证书引用和缺失的必填输入应在 apply 前拒绝。

## 8 模块接口与真实功能

### 配置与规则

- ConfigLoader.load：解析并校验输入，输出带版本的领域配置。
- PolicyCompiler.compile：编译国内直连、内网例外、DNS、IPv6 与默认代理规则。
- PolicyExplainer.explain：给定域名、IP、协议和端口，返回动作、命中规则及规则版本。
- ProfileRenderer.render：按设备和格式生成完整配置，拒绝不支持的能力组合。
- ServerRenderer.render：按角色生成代理、VPN、nftables、systemd 和资源清单。

渲染结果必须可由对应真实组件解析。不能只用自家解析器校验自家生成的配置，也不能通过只导出节点地址来冒充完整分流配置。

### 设备与订阅

- DeviceService.create：签发独立代理凭据并创建 pending 设备，数据面接受配置后才成为 active。
- DeviceService.rotate：生成新凭据，更新数据面并记录新旧版本的生效边界。
- DeviceService.revoke：撤销设备代理权限与相关订阅，验证新连接被拒绝。
- SubscriptionService.issue：生成高熵随机令牌，普通数据库仅存令牌摘要。
- SubscriptionService.fetch：验证令牌、设备状态与格式，生成对应完整配置。
- SubscriptionService.revoke：停止使用该链接获取更新。

撤销订阅链接不会自动使已导入的代理凭据失效。设备停用必须同步作用到接入核心；若目标核心不能定向关闭旧会话，应采用可控重载并说明影响范围。实时配置应用失败时，状态保留为 pending 或 degraded，并采取受影响入口的阻断措施，不能报告撤销已生效。

订阅响应与访问日志不缓存或记录令牌、密码及配置正文。服务端渲染所需的设备密码存入受限 secret_ref 文件，不以“数据库只存哈希”为由丢失生成配置所需的信息。

订阅响应设置 Cache-Control: no-store；反向代理、应用访问日志和错误上报统一隐藏 URL 中的令牌。撤销后的链接不能从缓存继续获取配置。

### 网络适配

定义统一的适配器契约：

| 方法 | 行为 |
| --- | --- |
| inspect | 只读获取组件版本、资源与状态 |
| validate | 调用真实组件校验候选配置 |
| plan | 返回带前置条件和回退动作的结构化步骤 |
| apply | 执行允许的步骤，记录已改变资源 |
| verify | 返回实际探针和路径证据 |
| rollback | 仅恢复本事务修改的资源 |
| stop | 按当前模式有序停止，保持约定的业务保护 |

fake 适配器只用于单元测试和显式 demo。生产模式拒绝 fake；未实现的能力返回明确错误，不能返回成功或空结果掩盖缺失功能。

## 9 命令行和 HTTP 契约

以下是待实现的命令界面。读取、校验、导出与 plan 默认不修改宿主机网络。

| 命令 | 预期结果 |
| --- | --- |
| wayvia init | 在指定目录创建脱敏模板和本地元数据 |
| wayvia validate CONFIG | 校验输入，输出字段级错误 |
| wayvia policy explain DOMAIN | 解释分流动作与规则来源 |
| wayvia device add 或 list 或 revoke 或 rotate | 管理设备与实际生效状态 |
| wayvia profile export DEVICE --format FORMAT | 导出可用的完整客户端配置 |
| wayvia subscription issue 或 revoke | 签发与撤销订阅令牌 |
| wayvia render CONFIG --role ROLE --output DIR | 生成指定角色产物和清单 |
| wayvia plan CONFIG --role ROLE | 生成不可变操作计划及摘要 |
| wayvia apply PLAN | 将明确指定的计划交给本机 agent |
| wayvia confirm TRANSACTION | 确认探针通过的事务 |
| wayvia rollback TRANSACTION | 恢复本事务改变的状态 |
| wayvia status --json | 返回配置版本、组件状态及最近探针 |
| wayvia doctor | 分层诊断并给出脱敏结果 |
| wayvia serve | 启动非特权管理与订阅 API |
| wayvia agent | 启动本机特权执行端 |

退出码至少区分成功、输入错误、凭据错误、环境不支持、运行故障和待确认；数值映射在实现时固定并写入 CLI 文档。

| HTTP 路径 | 认证边界与职责 |
| --- | --- |
| GET /healthz | 最小存活信息，不代表完整数据面可用 |
| GET /api/v1/status | 管理认证，查看分层运行状态 |
| GET 或 POST /api/v1/devices | 管理认证，设备查询或创建 |
| POST /api/v1/devices/{id}/revoke | 管理认证，发起并跟踪实际撤销 |
| POST /api/v1/devices/{id}/rotate | 管理认证，轮换凭据 |
| POST /api/v1/subscriptions | 管理认证，签发设备订阅 |
| DELETE /api/v1/subscriptions/{id} | 管理认证，撤销订阅链接 |
| GET /subscriptions/{token}/{format} | 订阅令牌，只返回该设备的配置 |

暂不通过公网 HTTP 开放任意网络 apply 操作。管理写操作返回 job_id 或状态对象，明确区分已接受、正在应用和已生效。重复请求使用幂等键，防止重试重复签发设备或令牌。

错误统一包含 code、message、request_id 和脱敏 details。状态模型中 unknown、unsupported 和 skipped 不得被归为 healthy。

## 10 网络事务与故障恢复

正常变更的状态顺序为：

```text
DRAFT → VALIDATED → PLANNED → APPLYING
      → VERIFYING → AWAITING_CONFIRM → ACTIVE
```

验证失败或确认超时进入 ROLLING_BACK；成功恢复为 ROLLED_BACK，恢复失败为 RECOVERY_REQUIRED。已确认链路运行中故障进入 DEGRADED，继续阻断对应代理业务并保留重连所需路径。

每次变更按以下步骤执行：

1. 获取目标角色的变更锁，检查计划摘要、有效期和宿主基线是否一致。
2. 将资源清单、前态与恢复日志持久化。
3. 在执行网络变更前安装独立回退计时器，默认确认窗口 120 秒。
4. 先建立必要的阻断和管理放行，再按依赖顺序启动组件。
5. 运行接入、代理、VPN、最终出口、DNS、IPv6 和回程探针。
6. 探针通过后等待显式确认；确认前不把事务记为 ACTIVE。
7. 失败或超时恢复本事务资源，保留原有其他服务及其规则。

回退计时器不能依赖 API 进程存活。重启发现未确认事务时执行恢复；已确认配置重启时先建立保护，再重建与验证链路。

两台服务器分别维护事务，不宣称具备跨主机原子提交。部署按“日本端就绪 → 阿里云出口就绪 → 发布终端配置”排序。记录双方配置 revision，失败时停止发布新配置并恢复受影响一侧。

### 10.1 运行代次与凭据一致性

角色 runtime 指针与事务日志共同记录 current generation、predecessor、blocked_reason 和 device_revision。申请新事务时先检查原计划 digest / baseline，再将持久化的最新设备检查点覆盖到入口配置，记录 submitted_digest 和 effective_digest。回退同样采用最新检查点，设备撤销优先于旧网络计划。入口内部认证探测用户由 agent 单独生成并保持在受限文件中，不能从设备接口创建、导出或覆盖。

新计划成为 current 后，旧 ACTIVE 代次的 monitor 和残留确认计时器不得再操作当前服务。连续部署回退按前驱逐代恢复；恢复失败保持 RECOVERY_REQUIRED 和阻断，成功恢复后原前驱重新成为当前受监控代次。运行故障与明确的安全阻断分别持久化：普通链路故障可由 monitor / recover 重建；显式阻断或凭据同步失败持续到下一次获授权且校验成功的凭据协调。尚未部署入口时，可在验证设备、单调修订并 fsync 检查点后解除凭据保护阻断，但仍明确返回 pending / INGRESS_NOT_DEPLOYED，不宣称数据面 active。进程在 stop、恢复指针或重启之间中断后，独立恢复任务根据日志继续。

确认期限同时约束探针总预算和最终 ACTIVE 提交，探针执行期间越期也必须回退。主机预检覆盖非自有 tun0、接口地址、所有路由表的静态／策略路由；只豁免可证明归属本角色的资源，不因接口名称相同而接受冲突。

### 10.2 可确认的探针与发布

| 层 | 入口角色 | 出口角色 |
| --- | --- | --- |
| access | trojan_http：实际 TLS、Trojan 凭据、受控业务请求 | trojan_vpn：实际 TLS、Trojan 凭据和受限 VPN 目标证书 |
| carrier | vpn_handshake：经 10.89.0.1:1080 校验 VPN 服务端身份 | vpn_handshake：本机受限 VPN listener 身份 |
| vpn | vpn_session：私有管理 socket 的已认证运行会话 | vpn_session：服务端已初始化及 peer 数量 |
| egress / DNS / IPv6 / return | namespace 内出口、DNS 与阻断，以及宿主物理回程 | 出口组件本机对应条件，仅表示 component_readiness |

access 的地址和端口必须绑定实际渲染 listener；入口 access 的 URL 和 expected 必须与 egress 探针一致。仅 TCP 端口打开、路由存在或宿主 curl 成功不能代替入口数据路径。旧 tcp / route 探针模板必须重新生成计划。证据包含 observed_at、duration_ms、valid_until、revision、generation、scope；diagnostics 和 publish 同时检查 90 秒有效期限及独立 monitor 心跳。

`config publish --exit-status FILE` 由管理员显式提交同 revision 双端当前 ACTIVE 证据，控制面 migration 2 保存不可变配置快照和发布指针。export、subscription issue / fetch 只使用发布快照；初次未发布明确失败。desired config 后续变化不会悄悄改变已发布订阅，设备权限和撤销仍实时生效。远端回执由可信管理通道传输，当前协议不提供跨主机签名或分布式事务。

凭据轮换使用唯一 secret_ref，文件 fsync 成功后在数据库事务中切换引用；进程中断遗留的无引用私密文件不能阻塞重试，也不能被当成当前凭据。系统 CLI 使用 `wayvia-system`（以 wayvia 身份、固定 /var/lib/wayvia/control），与 API 共用数据库、配置及同步检查点。

### 10.3 安装事务与交付隔离

安装器对每个写入预先持久化 journal，在 manifest 提交前可恢复旧状态；manifest 提交后按新归属完成收尾。重跑与显式 --recover 都处理残留安装。升级删除未修改的过时自有文件，保留被修改的文件及其归属记录，避免后续卸载遗漏。sdist 只包含明确列出的 Wayvia 源码、锁文件、文档和验证资源，不能遍历共享工作区的其他项目。


## 11 测试策略与开发命令

测试以具体风险为中心，不以覆盖率数字或大量快照作为完成依据。

| 层次 | 必测内容 | 证据 |
| --- | --- | --- |
| 单元 | 规则优先级、域名与 IP 冲突、无效配置、角色凭据隔离 | 明确输入与期望行为 |
| 服务集成 | SQLite 迁移、设备生命周期、订阅隔离、令牌撤销、API 认证 | 请求与持久化结果 |
| 适配器契约 | 版本探测、真实配置校验、非零退出、超时、失败步骤回退 | 组件版本与命令结果 |
| Linux 实验 | 双层数据路径、NAT 回程、DNS、IPv6、UDP、进程故障；真实安装/API/正式事务/连续回退/monitor/独立恢复；真实 mihomo 重叠 DNS | 受控目标日志、路由与规则计数、必要的抓包 |
| 实际环境 | 两台云服务器、客户端导入、Wi-Fi 与蜂窝切换、网关 | 带环境和版本的人工或自动验收记录 |

必须有负向路径证据：关掉 OpenVPN 后代理业务不能从阿里云直接访问测试目标；关掉日本代理后 OpenVPN 不能直连日本 VPN；日本代理不能被用于任意目标转发。仅验证出口 IP 不足以证明双层顺序。

受控实验包括终端、国内测试目标、阿里云角色、日本角色和国外测试目标。使用独立 Linux VM 或等效隔离环境模拟这些位置；“国内与国外”通过测试规则集区分，不依赖实际跨境位置。

实验生成临时测试 CA 和凭据，并在结束时清理。测试密钥与真实密钥分别管理。宿主机不得被当作待接管的真实网关。

需要实现以下统一命令：

| 命令 | 作用 |
| --- | --- |
| uv sync --locked | 安装锁定依赖 |
| make check | 格式、静态检查、非特权自动测试与包构建 |
| make demo | 在临时数据目录启动可交互演示，明确标记 simulated |
| make test-configs | 使用锁定版本的真实核心校验生成配置 |
| make test-lab | 在明确指定的一次性 Linux 环境运行数据路径测试 |
| make clean-lab | 只清理本次实验拥有的资源 |

测试需要 Linux、root 或缺失的组件时返回明确的环境缺失结果。make test-lab 不得静默跳过后以成功退出。普通本地检查与实验门禁分开报告，CI 也分别显示。

## 12 开发里程碑

| 阶段 | 必交付内容 | 完成门禁 |
| --- | --- | --- |
| M0 工程基础 | 包结构、依赖锁、CLI、日志、错误模型、CI、状态文档 | 从干净目录可安装、运行帮助并构建包 |
| M1 配置与分流 | Schema、规则编译和解释、配置渲染、格式适配接口 | 国内直连和默认代理测试通过，角色凭据隔离成立 |
| M2 设备与订阅 | SQLite、设备生命周期、API 认证、配置导出、私人订阅 | 设备创建到导出到撤销闭环，错误与权限路径通过 |
| M3 网络执行 | 真实组件适配、Linux 动作计划、独立回退、状态机、服务单元 | 各失败点有恢复测试，真实配置校验通过 |
| M4 双层实验 | 一次性 Linux 实验、两角色部署与故障注入 | 正向与负向数据路径均有证据 |
| M5 工程交付 | 安装升级卸载、运维文档、版本清单、故障诊断、验收报告 | 能从文档重建软件与实验，剩余实网项目明确 |
| M6 实网验收 | 真实阿里云和日本部署、Android 与 iPhone 导入、性能测量 | 按设计文档逐条取得实际环境证据 |
| M7 网关扩展 | OpenWrt 适配与多设备测试 | 未装客户端的设备可用，已装客户端不重复代理 |

实施采用纵向闭环：先让一个配置完成“校验 → 设备 → 导出 → API”，再接通真实 Linux 执行和数据路径。M0 完成后继续 M1 至 M5，不能以目录和空接口作为本次目标的交付结果。

M0 至 M5 的软件目标与 M6、M7 的外部环境验收分别记录。缺少服务器账号、域名或手机时，继续完成所有可独立开发内容。缺少 Linux 实验环境时，M4 标为环境阻塞，先完成其他可执行任务；不能将未运行的实验改为已通过。

## 13 完成定义与进度记录

docs/implementation-status.md 必须维护：

- 模块状态：未开始、实现中、已实现、已验证或环境阻塞。
- 对应代码、运行命令、版本、结果和证据路径。
- 下一项可执行任务、具体阻塞及所需的最小输入。
- 软件实现、受控实验、实际环境验收三个独立结论。

### 软件交付完成

必须同时满足：

1. M0 至 M3 和 M5 的代码与文档齐全，核心功能不存在占位实现。
2. make check 通过，配置由锁定版本的真实组件校验；不能仅依赖 fake 或自定义解析器。
3. CLI 与 API 可以完成真实持久化的设备、配置和订阅闭环。
4. Linux 适配器具有可执行实现、错误处理、事务日志与恢复机制。
5. M4 在可用的隔离 Linux 环境中执行；若环境确实不可获得，保留可运行实验和环境缺失证据，并明确标为“软件交付完成，Linux 链路待验收”。
6. 变更、测试结果、兼容矩阵和剩余输入均可从仓库重现或核查。

只完成框架、只让演示模式运行、把错误分支写成成功，均不满足软件交付完成。

### 数据链路验证完成

只有 M4 全部通过后，才能声明受控双层链路已验证。M6 未通过前不宣称真实云服务器、手机或目标网络已经可用。对外文案不作为测试证据。

## 14 实施授权与开发约定

本次 /goal 面向仓库工程实现和一次性测试环境。可以创建文件、安装项目依赖、下载并校验官方组件、运行本地服务与测试，以及在当前任务允许的范围内使用隔离 Linux 实验环境。

真实阿里云、日本服务器与现有网络的部署需要实际输入和明确的目标主机授权；本次指令不包含未指定主机的远程网络变更。不得修改开发机默认路由、全局防火墙或现有 VPN 来替代实验环境。

以已有用户决策和 design.md 的架构为依据，使用本开发文档完成实现。技术细节由开发者合理选择并记录 ADR。真正影响架构约束的问题及时提出，同时推进不依赖答案的工作。

保持对外产品介绍的正式产品文案风格。新增实现状态只写入开发和验证文档，不在产品介绍中回填“首版”或“开发中”措辞。

开发分支使用 codex/ 前缀。提交只包含项目相关文件，不覆盖他人的未提交改动。远端推送及 PR 按当前任务已有授权执行。

配套指令见 [Wayvia 开发 Goal 指令](development-goal.md)。
