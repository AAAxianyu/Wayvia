# Wayvia 一次性 Linux 实验

`make test-lab` 现在统一执行受控双层路径、协议身份负向检查、mihomo DNS 行为、TUN 内核就绪、真实资源回退、真实内核冲突检查、独立 systemd 恢复及正式安装事务集成。任何阶段失败即返回非零；单独路径通过不表示整条交付门禁通过。本轮结果见 [修复证据目录](../../docs/validation/fixes-2026-10-12/)，最终统一命令退出码记于该目录的 `commands.json`；早期 22 项路径结果是历史基线。

## 运行与隔离

在仓库根目录执行：

```sh
uv sync --locked
make check
make demo
make test-configs
make test-lab
make clean-lab
```

直接调用 `uv run python scripts/lab.py --preflight` 只检查实验环境；`uv run python scripts/lab.py` 执行完整门禁。默认重新核验固定资产 SHA256 并构建镜像，`--no-build` 只供已构建镜像的开发迭代，不能把旧镜像结果当作新源码的最终交付结果。Docker 不可用、下载失败、缺少 root/capability、配置被核心拒绝或任何断言失败都会明确非零退出。

路径实验使用 Ubuntu 24.04 容器中的六个 network namespace 模拟终端、国内目标、阿里云入口、阿里云出口、日本与国外目标，容器以 `--network none` 运行。executor、内核冲突、协议、DNS、TUN 就绪和独立恢复辅助容器也使用 `--network none`。正式安装事务实验需要容器间通信，使用带所有权标签的 `--internal` Docker 网络 `wayvia-transaction-disposable`；入口、出口与目标各自独立，两个服务器容器以 systemd 为 PID 1，使用私有 cgroup namespace。

需要网络能力的实验只在明确创建的一次性 privileged Linux 容器内执行；mihomo DNS 辅助容器不需要 privileged。实验不使用 host network，不挂载宿主 `/proc`、`/sys`、cgroup 或 Docker socket，也不操作开发机路由、防火墙、已有 VPN 或未指定服务器。

`make clean-lab` 先核对名称与 `org.wayvia.disposable` 标签，只清理 `wayvia-m4-disposable`、`wayvia-recovery-disposable`、`wayvia-transaction-{ingress,exit,target}` 及上述专属 transaction 网络。临时辅助容器使用 `--rm`。名称相同而标签不同的资源会被拒绝删除。

`components.lock.json` 按 arm64、amd64 锁定官方资产，`python-runtime.lock.json` 固定由 uv.lock 选出的测试依赖及 wheel 摘要。每份实验结果仍须报告实测架构、核心和系统包版本；有另一架构的下载锁不等于该架构本轮验收。

## 统一门禁的范围

| 阶段 | 实际检查 | 默认输出（`docs/validation/latest-lab/`） |
| --- | --- | --- |
| 双层路径 | 23 项：生成配置、TUN 内核就绪、原生/SOCKS TCP与UDP、DNS、国内IPv6、保护与回程、故障重连、入口撤销和 OpenVPN 适配器握手 | `lab-result.json` |
| 生产 executor | 用真实 LinuxExecutor 创建 namespace、veth、保护表并逆序回退，检查默认路由未变 | `executor-result.json` |
| 真实内核冲突 | 12 项：未拥有或地址重叠的 TUN、静态/策略表/blackhole 路由冲突、允许当前自有内核资源、保留无关 VPN/nft 和默认路由 | `preflight-conflicts-result.json` |
| 真实协议 | 7 项：OpenVPN 服务端身份、管理状态、Trojan认证及承载后的VPN身份；错误密码、错误TLS身份、错误VPN身份和普通TCP listener负向拒绝 | `protocol-result.json` |
| TUN 就绪反例 | 故意扣住接口/策略安装阶段，检查门禁不会提前放行，缺少源地址规则最终超时 | `tun-ready-result.json` |
| mihomo DNS | 固定核心、3 个真实本地 UDP 解析器、6 类重叠后缀及精确 bootstrap 查询 | `mihomo-dns-result.json` |
| 独立 systemd 恢复 | 真实 SystemdTimer，以及控制进程退出后的独立恢复进程，使用容器私有 cgroup2 | `recovery-result.json`、`recovery-environment.json` |
| 正式安装与事务 | 23 项：两角色正式安装、真实 API/CLI、计划/确认、发布/订阅、A→B→C 代次与回退、两层故障/恢复、独立恢复、同版本重装、权限与有限卸载 | `transaction-result.json` |

正式事务已取得 [23/23 专项通过证据](../../docs/validation/fixes-2026-10-12/transaction-result.json)，按安装的 venv、服务身份和 Unix socket 执行。入口实际经历 `443→8443→9443→8443→443`，验证旧 monitor 不干扰当前未确认代次及回退后的认证路径；出口验证同配置的三个运行代次。证据还包括 VPN/承载故障保护及认证恢复、独立恢复服务、同版本软件重装、轮换/订阅撤销/设备撤销、旧计划和回退不能复活已撤销权限，以及两端有限卸载。独立恢复不是整机重启，同版本重装不是跨版本升级。真实内核冲突另有 [12/12 专项证据](../../docs/validation/fixes-2026-10-12/preflight-conflicts-result.json)。专项成功与最终统一门禁的退出码分别记录。

## 生成物与流量边界

`scripts/lab_config.py` 使用正式 WayviaConfig、ProfileRenderer、ServerRenderer。规则明确将受控地址分类，不假定 RFC1918 地址的地理归属。路径实验终端保留生成的 TUN、DNS、TLS 与分流规则，只额外添加本地 SOCKS 入站供协议探针使用。原生 TCP/UDP 不指定代理。新就绪门禁检查 TUN 的 UP/LOWER_UP、首次与带源地址的 TCP 路由和实际内核选源；它修正实验的启动次序，不修改 sing-box，也不给失败业务请求增加重试。根因和原始失败见 [TUN 调查](../../docs/validation/fixes-2026-10-12/tun/README.md)。

入口、桥接、日本代理和两个 OpenVPN 文件直接使用 ServerRenderer 产物。v2rayA 通过生产适配器的正式 HTTP API 管理同版本 core。实验 CA、服务证书、设备及管理员凭据随机生成，使用后清理，仓库只保留脱敏证据。mihomo 辅助实验关闭 TUN，仅验证生产 DNS 策略及真实解析器选择，不能据此声称 mihomo 全设备接管通过。

OpenVPN 客户端在 `wayvia-egress` 内先通过 SOCKS 连接；阿里云 core 再通过 Trojan TLS 连接日本代理，日本代理只转交本机 OpenVPN TCP 端点。国外业务在日本解封装并经 NAT 到达目标，国内业务直接从终端到国内目标。实验先证明物理直出可用，再安装正式 drop 保护并确认同一路径被拒绝，使故障时不泄露的断言具有可验证前提。

## 验证边界

真实协议与 management 状态用于证明服务身份和当前连接，端口可以连接不能代替这些证据。出口 `component_readiness` 只表示角色就绪，入口 `datapath` 才验证完整业务路径；配置发布还要求两个角色的相同 revision、当前代次及新鲜监控证据。设备 pending、配置已渲染或事务等待确认均不表示终端配置已发布。

独立 systemd 容器可验证进程退出、真实 timer 和 journal 恢复；正式事务容器可验证安装后的服务与控制流程。两者都不等于真实云主机整体重启。系统/浏览器私人 DNS、Android/iPhone 导入与 Wi-Fi/蜂窝切换、真实吞吐/延迟/费用属于 M6；OpenWrt 型号和固件验收属于 M7。辅助 Ubuntu 仓库包记录实测版本，未宣称整个操作系统字节级可重复构建。
