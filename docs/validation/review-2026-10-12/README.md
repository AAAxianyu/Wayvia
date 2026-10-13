# Wayvia 实现、详细设计与产品方案复核

> 本文是修复前的历史审计。后续修复和重新验收见 [修复记录](../fixes-2026-10-12/README.md)，下文问题状态按原审计基线保留。

日期：2026-10-12（America/Los_Angeles；部分原始记录为 2026-10-13 UTC）。审计基线：`d4081636c15ea2c0530b2e53ff529c0a1a6a3cc2`，分支 `codex/m0-m5-delivery`。

## 结论

核心软件、真实组件适配和受控双层链路已经落地，有真实测试证据；但本轮发现 **15 项需要处理的实现／交付问题，其中 4 项 P1、10 项 P2、1 项 P3**，另有一次尚未定位原因的 TUN 实验超时。此前“M0–M5 软件全部完成，只剩实网输入”的结论过宽，应改为“主体已实现，软件验收重新打开，修复后重新验证”。这些问题中的大部分不依赖服务器账号或手机，可以继续在仓库和隔离环境内解决。

本轮为审计：没有修复产品代码；新增重现脚本、证据及状态修订。P1 指应优先处理的凭据／运行状态缺陷，P2 指明确正确性或交付缺陷，P3 指较低优先级的安装残留。不将测试替身的状态机重现扩大为真实内核或实网结论。

检查依据为 `docs/design.md`、`docs/development.md`、`docs/development-goal.md`、`docs/product-overview.md` 和 AGENTS.md；覆盖 CLI、配置、策略、渲染、API、存储、密钥、组件适配、特权 agent、事务、诊断、安装、CI 与实验。三条独立审计线交叉核对后汇总。未改开发机路由、防火墙、现有 VPN 或真实服务器。无关工作目录未被人工读取或修改；约定构建命令意外遍历该目录的问题单独记录为 R14。

## 本轮实际门禁

| 检查 | 本轮结果 | 解释与证据 |
| --- | --- | --- |
| `make check` | **退出 2** | Ruff 通过，150 项测试通过、3 个依赖弃用警告；`uv build` 的 sdist 收入无关目录后因外部绝对符号链接失败。见 `make-check.txt`、R14 |
| 干净提交的构建 | 退出 0 | `git archive HEAD` 到临时目录，再运行 `uv build --offline`。见 `clean-head-build-result.json`；仅证明干净基线构建，不替代当前工作区失败 |
| `make demo` | 退出 0 | 临时 SQLite、明确 simulated 数据面；不证明真实服务部署 |
| `make test-configs` | 退出 0 | 10 项真实组件解析／隔离加载和不支持组合拒绝；见 `latest-configs/config-result.json` |
| `make test-lab` 首次 | **退出 2** | 原生 TUN TCP 请求超时；此前 SOCKS 双层请求及国内直连通过。见 `latest-lab/lab-result.json` |
| `make test-lab` 一次复跑 | 退出 0 | 22 项链路检查、LinuxExecutor 内核动作回退及独立 systemd 恢复通过。见 `lab-retry/`、`lab-retry-result.json` |
| `make clean-lab` | 退出 0 | 实验资源清理；只操作本项目的一次性容器 |
| 远端既有 CI | success | 同一提交的 [run 38241319117](https://github.com/AAAxianyu/Wayvia/actions/runs/38241319117)，software 和 isolated-linux 均通过；保存 `ci-run.json`。amd64 已有 CI 隔离执行证据 |

首次实验失败发生在 `foreign_native_tcp_intercepted_by_rendered_tun`，4 秒超时。复跑通过不能抹去首跑失败，也不足以断定是某个已知启动竞争。本轮将其记为开放的稳定性问题，后续应采集失败时的终端路由、TUN 状态、核心进程状态和报文证据。第一次失败后未执行的 executor/recovery 证据没有混入首跑目录；复跑的新证据单独保存。原有 `latest-*` 基线文件保持原样。

## 产品需求与现有交付的对应关系

| 产品／设计要求 | 已有实现和证据 | 本轮判断 |
| --- | --- | --- |
| 国内、校园、局域网在终端直连 | 统一规则编译；APNIC 7511 CIDR、110185 域名后缀；真实国内 TCP/DNS/IPv6 路径 | 主体成立；mihomo 重叠 DNS 规则违反优先级，见 R07；实际校园资源仍需 M6 |
| 国外固定双层顺序 | 终端 → 阿里云入口 → namespace 内业务 bridge → OpenVPN → v2rayA/core → 日本代理 → 日本 OpenVPN → 目标 | sing-box 受控正反向路径成立；不是空接口或演示数据 |
| 终端只获入口和自身凭据 | 渲染显式选字段、服务器角色隔离、secret_ref 权限检查 | 正常导出路径未发现 carrier 密码或 VPN 私钥泄露 |
| 设备独立授权、轮换、撤销 | SQLite、随机密码、生产 agent 同步、订阅与设备权限独立撤销 | 有真实实现；撤销与网络计划并发、轮换崩溃缺陷见 R01/R08 |
| 私人订阅与配置版本 | Bearer 管理认证、令牌摘要、期限、幂等、no-store、完整配置导出 | 订阅权限闭环成立；未绑定已确认部署版本，R09 |
| Android、iPhone、电脑 | sing-box JSON 和 mihomo YAML 完整格式及能力声明 | 格式真实解析通过；具体手机 App 导入、切网、系统接管未验收；mihomo 没有同等完整 TUN 行为证据 |
| 故障时受保护业务阻断、国内继续 | 真实 guard、两层分别断开、物理绕行对照实验 | 阻断机制有证据；生产 monitor 参与后的重连不闭环，R02/R04 |
| 网络变更可确认、回退、重启恢复 | 固定动作、日志、角色锁、真实独立 timer、恢复入口 | 单步故障和未确认回退有证据；多次部署、确认边界、回退后重启存在 R03/R05 |
| 分层连接状态 | status/doctor、七层探针与状态区分 | 普通端口可被判健康、历史结果无过期判断，R10/R11 |
| 安装、升级、卸载 | SHA 锁定组件、stage/system、备份和所有权清单 | 有实现；安装中断、操作目录、过时文件和构建边界未闭环，R12–R15 |
| OpenWrt 网关 | 同一策略和格式可作后续基础 | M7 未实现，不能把“产品方案包含”写成“网关已交付” |
| 真实性能、费用、SLA | 有测量目标，未宣称正式 SLA | 需 M6 实测；不应以容器耗时推断跨地域性能 |

设计的固定约束仍应保留：国内优先直连、VPN over proxy 的封装顺序、日本只转交本机 VPN、服务器凭据不下发终端，以及受保护业务失败时阻断。修复以下问题无需改变这些架构决策。

## 问题清单与修复验收条件

### R01 · P1 · 正常 apply 可重新装回已撤销凭据

位置：`src/wayvia/agent/transactions.py:151`；竞态入口 `src/wayvia/cli.py:342` 和 `:355`。

普通 apply 直接安装计划的 `ingress.json`；最新设备检查点只在恢复和回退时覆盖。CLI 可以先渲染旧用户，并发撤销完成后再取得新 baseline，因而旧 users 与新 baseline 同时出现在合法计划中。重现使用真实 ServerRenderer 生成完整合法产物，拓扑和产物校验通过；检查点 `devices=[]`，交给测试 executor 的安装动作仍包含 `revoked-device`，事务为 AWAITING_CONFIRM。入口在确认前已启动，因此不是单纯的历史元数据问题。

证据：`network/result.json` 的 `apply_does_not_enforce_latest_device_checkpoint`。这是正式事务代码加显式测试 executor 的重现，没有声称让真实已撤销手机重新连接。

验收：在同一角色锁下绑定／验证设备 revision，或把最新设备检查点一致地用于全部应用路径；为渲染→撤销→baseline→apply 的交错次序加测试。真实入口验证旧密码持续拒绝，不能只验证检查点内容。

### R02 · P1 · DEGRADED 后缺少保留保护的恢复路径

位置：`src/wayvia/agent/transactions.py:423`、`:311`、`:125`。

监控失败后调用 protect 停入口并置 DEGRADED。再次监控即使全健康也不恢复 ACTIVE；recover 跳过 DEGRADED，新 apply 又返回 TRANSACTION_PENDING。重现得到“再次健康仍 DEGRADED、recover=[]、新计划拒绝”。现有办法只剩完整回退再重部署，生产的自动／显式重连流程不成立。重启 rebuild 的探针又只执行一轮，未等待握手就降级会扩大这一问题。

证据：`network/result.json` 的 `degraded_recovery_dead_end`。

验收：区分运行故障、凭据撤销、显式安全阻断；仅允许前者在保护规则始终生效时重建、等待握手、重新验证和恢复入口。用真实 monitor 参与的 VPN／代理故障测试验证恢复，显式撤销不得自动解封。

### R03 · P1 · 回退新事务后丢失旧部署的恢复归属

位置：`src/wayvia/agent/transactions.py:261`、`:278`、`:318`。

A 确认→B 确认→回退 B，文件／服务前态被恢复，但 A 仍为 SUPERSEDED，B 为 ROLLED_BACK。重现中 `recover(rebuild_active=True)` 没有任何动作。旧部署因此不再被监控，重启时也不重建；继续 rollback A 被拒绝，原始资源难以通过文档中的卸载流程完整回退。

证据：`network/result.json` 的 `rollback_confirmed_successor`。

验收：记录 predecessor 或持久化独立回退部署代次，恢复资源时也恢复正确的运行／监控归属。覆盖连续两次以上升级、回退、独立恢复进程和最终卸载，检查真实 namespace/veth/nft 残留。

### R04 · P1 · 旧事务监控会停止待确认的新入口

位置：`src/wayvia/agent/transactions.py:423`；`src/wayvia/agent/recovery.py` 的 monitor 分发。

B apply 已安装新配置并等待确认时，A 仍是 ACTIVE。周期监控用 A 的旧探针；若 B 改变入口端口，A 的旧端口失败，protect 就停止 B 正在使用的入口。重现结果：A=DEGRADED、B=AWAITING_CONFIRM，并发生 protect。

证据：`network/result.json` 的 `monitor_old_active_during_pending_successor`。

验收：每个角色持久化当前实际运行代次；新事务接管到确认／回退期间，旧探针不能控制新资源。测试 monitor 与 apply/confirm/rollback 的交错以及变更端口场景。

### R05 · P2 · confirm 可越过确认期限提交

位置：`src/wayvia/agent/transactions.py:206`、`:212`、`:218`。

期限仅在探针前检查；探针期间过期后仍写 ACTIVE。回退任务等待相同角色锁，随后看到 ACTIVE 便不撤回。可控时钟重现得到 `confirmed_at > deadline`。

证据：`network/result.json` 的 `confirmation_passes_deadline_during_probes`。

验收：探针总预算受剩余期限约束，提交 ACTIVE 前再次核验；测临界时间和独立 timer 并发，超时必须回退而非确认。

### R06 · P2 · 冲突预检跳过非项目 tun0 和现有路由

位置：`src/wayvia/adapters/linux/__init__.py:286`。

预检无条件跳过 tun0，只检查接口地址，不检查静态／策略路由。出口 OpenVPN 却在宿主使用固定 tun0。只读 stub 给出未登记所有权且使用计划 VPN 网段的 tun0，预检仍接受。现有活动设备可能在启动时才因占用失败；对持久设备及重叠路由的安全边界没有建立。没有在真实已有 VPN 上测试，也不宣称已观察到其被改动。

证据：`network/result.json` 的 `exit_unowned_tun0_and_subnet_overlap`。

验收：区分 ingress namespace 与 exit 宿主，验证 TUN 归属；检查相关路由表中的网段重叠。在 disposable 环境预置非项目 TUN／路由，apply 前拒绝且对方保持原样。

### R07 · P2 · mihomo 校内 DNS 被国内 DNS 覆盖

位置：`src/wayvia/render/__init__.py:236`。

合法配置允许 `campus.example` 同时出现在 local 和 domestic。解释器返回 internal，但 nameserver-policy 后写覆盖，实际值变成 `223.5.5.5:53#DIRECT`，而非内部 `192.168.1.1:53#DIRECT`。校园分区解析失败，两种格式不再具有相同策略语义。

证据：`control/dns_priority.json`。

验收：编译 DNS 映射时保持完整优先级，覆盖完全相同、父后缀与更具体子域名等重叠；不能只用 setdefault 修复同名后就认为完成。增加真实 mihomo 查询路径测试，解析器接受 YAML 不等于解析策略正确。

### R08 · P2 · 凭据轮换中断后无法重试

位置：`src/wayvia/services/control.py:223`；`src/wayvia/secrets/__init__.py:54`。

轮换先写固定名称 v2 secret，再提交 DB 的版本号。写入 fsync 后中断会留下 v2 文件和 v1 数据库。重启 reconcile 可恢复 active，但再次 rotate 因 O_EXCL 返回 FileExistsError，无法继续轮换。

证据：`control/rotation_crash.json`；真实 SQLite/SecretStore，故障注入与显式模拟数据面。

验收：持久化轮换意图，或使用可恢复的唯一引用；分别在密钥创建、数据库提交、数据面应用前后中断，恢复不丢新凭据、不复活旧凭据且能继续轮换。

### R09 · P2 · 配置发布不受已确认部署版本约束

位置：`src/wayvia/services/control.py:259`；`src/wayvia/adapters/linux/__init__.py:532`。

设备 active 只说明此前凭据应用；export/issue/fetch 使用当前逻辑配置，不检查其 deployment revision 是否已确认。复用旧 active DB、换成未经 apply 的新入口配置，production 导出及订阅立即发布新地址和 8443 端口，即使 agent socket 不存在。测试的初始 active 使用显式 fixture，旧服务配置是渲染快照，不是真实服务器。

证据：`control/publication_revision.json`。

验收：持久化 desired/confirmed/published revision 和发布快照；网络变更未确认或失败时继续提供上一个有效版本，或明确拒绝发布新版本。两台服务器分别确认，通过后显式发布，不能只依赖设备 active。

### R10 · P2 · 端口可达被当作入口／承载层健康

位置：`src/wayvia/agent/probes.py:17`；`src/wayvia/agent/validation.py:validate_probes`。

TCP 探针仅运行 nc；VPN 探针仅检查 tun0 路由。真实本机普通 TCP listener 没有 TLS 或 Trojan，仍被入口探针判为 healthy；access 输入还没有与计划入口端口绑定。出口角色的 HTTP 请求从宿主直接发出，不能验证从 VPN 转发出来的业务。七个标签完整不代表七个层次完成了认证／数据验证。

证据：`diagnostics-result.json` 的 `access_probe`；只打开一个临时 loopback listener。此结果不表示全部七层已实测假阳性，也不否定已有 M4 全链路测试。

验收：探针目标从已应用拓扑绑定；区分 TCP reachability、TLS/协议认证、VPN 会话和真实业务请求。破坏入口 TLS 身份／设备认证、日本代理认证或 VPN 对端时不能报该层 healthy；出口转发需来自 VPN 对侧的证据。

### R11 · P2 · doctor 接受无限期陈旧的 healthy 结果

位置：`src/wayvia/services/diagnostics.py:44`。

dataplane_diagnostic 不校验证据采集时间、运行代次或 monitor 是否仍在运行。传入 2020 年的 ACTIVE 与七个 healthy 结果，仍返回 healthy。若 monitor 停止，连通的 agent 可以一直返回旧健康状态。

证据：`diagnostics-result.json` 的 `stale_dataplane`。

验收：ProbeResult 加采集时间、耗时、revision 与有效期；过期状态返回 unknown/stale，并能显示监控任务不可用。不能用记录被其他操作更新的 updated_at 替代探针采集时间。

### R12 · P2 · 首次安装中断后无法通过安装器恢复

位置：`deploy/install.py:337`、`:357`。

所有权 manifest 在逐文件写完后才提交。第 2 次写入注入失败后已有 1 文件、无 manifest；重试 resource_conflict，卸载 not_installed。M5 不能只验证完整顺序执行。

证据：`install/result.jsonl` 的 `first_install_write_interruption`；stage fixture，不更改系统安装目录。

验收：在落盘前持久化安装事务与所有权，提供重试／回退路径；每个写入阶段注入中断，并证明不覆盖非项目文件。

### R13 · P2 · 系统运维示例使用另一套控制面目录

位置：`docs/operations.md:76`；`deploy/install.py:199`；`src/wayvia/cli.py:76`。

系统 API 固定 `/var/lib/wayvia/control`，CLI 默认 `.wayvia` 且直接操作本地 SQLite。部署文档沿用 uv run 和默认目录，可能使 CLI/API 各维护一份 DB，却向同一 agent 发送不同设备集合和冲突 revision。真实服务代码配模拟 executor 的重现中，第二个目录的设备 pending、REVISION_STALE，并触发 block_ingress。

证据：`install/split-control-result.json`。

验收：系统运维明确使用系统 venv、wayvia 身份、统一 data-dir/config/socket，或让 CLI 统一经管理 API 操作；从文档逐条运行新增、轮换、撤销，API/CLI 必须观察同一设备及 revision。

### R14 · P2 · sdist 缺少项目文件边界

位置：`pyproject.toml:26`（只配置 wheel packages，未限制 sdist）。

当前工作区的无关文件被默认源码包选择收入；约定 `make check` 的构建阶段失败于外部虚拟环境绝对符号链接。干净 HEAD 构建成功，说明不能将它误报为依赖锁或代码无法构建，但工程构建不应收集同目录下无关资料。失败生成的污染 sdist 已移除，未作为交付物保留。

证据：`make-check.txt`、`clean-head-build-result.json`。日志将无关绝对文件路径替换为说明性标记，保留错误类型和退出码。

验收：为 sdist 设置明确 include/exclude；在工作区存在无关文件和外部符号链接时仍能构建，产物仅包含 Wayvia 需要的源码、锁文件、许可证和必要文档。不得以删除他人目录作为修复。

### R15 · P3 · 升级后删除的旧文件失去所有权

位置：`deploy/install.py:352`。

新 manifest 只记录新 payload。旧版本已安装、但新版本删除的文件仍留在磁盘并被移出所有权清单；卸载也不再清理。脚本实际重现的是 fixture 的 obsolete.py；根据版本化 wheel 路径及相同清单逻辑，跨版本 wheel 也可能遗留。

证据：`install/result.jsonl` 的 `removed_source_after_upgrade_then_uninstall`。

验收：计算旧新清单差集；仅自动移除未改动的旧文件，修改过的文件保留并明确报告，或继续跟踪所有权；升级→卸载后无未说明的软件残留。

## 详细设计需要补齐的契约

1. **运行代次与凭据代次分开记录并绑定。** 每角色只能有一个实际运行代次，明确 pending 计划与前任、已确认配置、设备检查点、已发布配置之间的关系。单个 role 锁不足以保证跨 CLI 渲染、凭据同步、monitor 和恢复操作的一致性。
2. **区分故障阻断与显式停用。** DEGRADED 必须有安全的恢复入口；撤销／人工阻断必须持续保持。重新验证、重新开放入口和恢复 ACTIVE 的前置条件需写清，并一直保留 fail-closed 规则。
3. **定义回退的完整语义。** 回退不仅恢复文件，还恢复可监控、可重启、可再次回退的部署记录；超时确认及重启阶段要有明确总预算。
4. **定义发布状态机。** 双机不做原子提交，但记录两端 revision，先 exit→ingress 确认，再发布终端配置；失败时停止新版本发布。订阅输出必须使用发布快照。
5. **把观测级别写入接口。** 端口可达、协议认证、VPN 建立、业务链路成功和证据新鲜度应分别表达。ProbeResult 当前缺少约定中的采集时间和耗时，不应把辅助端口探针称为完整路径验收。
6. **统一控制面入口和安装事务。** 系统 CLI/API 使用相同持久目录及服务身份；安装升级具有自身的持久恢复记录。明确 `uv sync --locked` 只同步依赖，数据库迁移实际由控制面数据库初始化触发。

建议新增完整隔离集成路径：system install → 启动正式 agent/API/systemd 单元 → 新设备 → plan/apply/confirm → 发布订阅 → 真入口认证 → 两次升级/回退 → monitor 故障恢复 → 新恢复进程重建 → 撤销竞态 → 卸载。全部使用一次性 Linux 环境和临时凭据。这属于可独立完成的软件验证，不必等待真实云主机。

## 产品方案需要明确的边界

- 当前交付形态是部署者使用 CLI/API 管理、用户使用兼容第三方客户端的网关软件；原生 Wayvia App、Web 面板、多出口调度、计费和复杂多租户没有列为 M0–M5 必需项。
- “Android/iPhone 支持”需要具体 App 与版本矩阵，分别记录格式解析、导入、更新、TUN 接管、UDP、IPv6、Wi-Fi／蜂窝切换。不能把 sing-box 核心实验推广到所有 App 或 mihomo 行为。
- 国内规则是固定版本数据与管理员维护的校正规则，不是实时地域判断服务；实际校园公网域名、网段、认证门户与 DNS 需要 M6 输入。已确认的国内直连优先级不能被新例外推翻。
- 用户应能区分“订阅链接撤销”和“设备访问撤销”；密码轮换／入口重启影响同入口所有现有会话，交付说明需要给出操作影响及实测失效时间。
- 目前支持隧道内 TCP/UDP，VPN 底层为 TCP over SOCKS/Trojan；国外代理 IPv6 阻断、国内 IPv6 保留。UDP VPN 底层和代理 IPv6 是明确未支持能力。
- 生产 HTTPS 订阅入口、真实域名和证书、证书续期流程、手机应用表现、目标主机完整重启、性能和费用属于 M6；OpenWrt 固件与避免重复代理属于 M7。
- 对外产品介绍可保持正式定位。工程状态、兼容矩阵与验收记录必须明确证据边界。设计／开发文档中的“尚无业务实现”“仍在讨论”是原始基线文字，不应作为当前进度引用。

## 修复顺序与完成门禁

1. 先修 R01–R04：撤销不可复活、运行代次一致、回退可恢复、故障可安全重连。
2. 接着修 R05–R11：期限、资源冲突、两格式 DNS 等价、轮换持久性、发布确认及真实／新鲜诊断。
3. 修 R12–R15 并统一运维示例：安装中断、控制面目录、源码包边界、升级所有权。
4. 将上述失败重现转为有意义的回归测试，增加完整正式事务驱动的隔离集成；定位首跑 TUN 超时并保存失败快照。
5. 重新通过四个约定命令，再更新软件交付结论；之后按明确授权开展 M6，M7 独立排期。

CLI 的 pending/degraded JSON 配成功退出码另存 `control/cli_status_exit_convention.json`：现有手册要求调用方检查 status，本轮将其记为需统一的接口约定，不额外列为确定阻塞项。建议设备变更增加等待生效选项或一致的非零退出规范。CI 建议用 always 上传验证 artifacts，以保存失败证据；不将其缺失混同为 CI 未执行。

## 重现方式与证据限制

在仓库根目录、锁定虚拟环境中运行：

```sh
.venv/bin/python docs/validation/review-2026-10-12/network/reproduce.py
.venv/bin/python docs/validation/review-2026-10-12/control/reproduce.py
.venv/bin/python docs/validation/review-2026-10-12/install/reproduce.py
.venv/bin/python docs/validation/review-2026-10-12/install/split_control_state.py
.venv/bin/python docs/validation/review-2026-10-12/reproduce-diagnostics.py
```

这些重现脚本观测到缺陷时自身可以退出 0，不能误读为缺陷已修好或回归测试通过。network 与安装控制面案例使用显式测试替身隔离外部副作用；配置、SQLite、密钥和事务代码真实执行。诊断案例只临时监听 loopback。真实内核／组件结果来自独立的 make test-configs/test-lab。日志已去 ANSI／行末空白并脱敏无关本地路径；没有保存临时私钥、真实密码或可用订阅令牌。
