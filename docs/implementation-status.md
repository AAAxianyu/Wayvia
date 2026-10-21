# Wayvia 实现与验收状态

## 2026-10-20 Clash Verge 指定核心订阅校验

应用户要求为 Clash Verge v1.7.2 / Mihomo v1.19.13 签发额外完整订阅链接，沿用现有已启用设备凭据；HTTPS 200、TLS 验证通过，外部下载约 6 秒。官方摘要验证的 Mihomo v1.19.13 在无网络一次性 Linux 容器中校验未经修改的订阅成功，国内直连/默认 Wayvia 路由和 TLS 校验保持。用户桌面 GUI、TUN 及完整分流尚未实测。入口保持原事务代次 ACTIVE，日本 SSH 复用连接失效后重新认证，确认同代次 ACTIVE、采样仍运行，24 小时观察配置未变；源码/数据面未改、故障和重启仍暂缓。见[指定版本验证](validation/clash-verge-172-2026-10-20/README.md)。

## 2026-10-20 长期正常运行观察（进行中）

按用户要求继续真实超时归因，两端已启动独立 24 小时采样，UTC 2026-10-21 03:09 至 10-22 03:09，到期自动结束。每 5 秒采集跨境 RTT、承载 TCP/接口和资源，每 30 秒对照宿主/VPN 的 HTTP 与两个供应商 DNS；不参与生产健康决策。每小时 heartbeat 只读复核。75 秒正常预运行两端均到期退出，跨境中位约 39 ms，VPN 与日本出口 HTTP/DNS 成功；阿里云宿主 CF HTTPS 直连超时作为对照基线，不能当成代理断连。

软件 369 项、模拟演示、10 项真实配置检查通过；完整隔离故障/恢复实验与真实故障/重启仍按用户要求暂缓，未测试手机。现有生产包、服务 PID/激活时间、默认路由未变。01 为 in_progress；**全天观察尚未完成，历史真实超时根因仍未确定**。证据、自动停止条件和后续流程见[观察报告](validation/stability-observation-2026-10-20/README.md)。

## 2026-10-20 无手机服务器续查

按用户要求完成稳定性只读排查、订阅提速和安装权限审查，故障与重启测试继续暂缓。约 16 小时 21 分钟的两端日志显示阿里云 9 次、日本 2 次降级；实际 HTTP/DNS 超时仍有未确定原因。已复现并修复“HTTP 200 且出口正确，随后 relay 关闭异常覆盖成功响应”的探针误判，已部署两端，候选及正式部署后各 100 次新鲜业务探针全部通过。历史 5 次 transport-only 事件缺少 curl 原结果，只能列为疑似同机制；不能宣称长期断连全部解决。

Mihomo 使用安全 C YAML 序列化器并保留安全回退。相同完整订阅每组 3 次，同机 TLS 首字节中位数从 12.97 秒降到 4.22 秒；阿里云自身访问公网地址从 13.49 秒降到 4.33 秒，完整下载从 15.15 秒降到 6.39 秒。前后 12 个响应逐字节相同，令牌/期限/设备状态检查和 no-store 保留，没有响应缓存，未重测手机。

正式安装器限定公开运行文件的子进程 umask，强制同版本包重装，并校验服务账户可执行。一次性无外部网络 Linux 容器在 077 下的新装、同版本重装和非 root API 首次启动通过；秘密仍为 0700/0600。真实两端权限只读审查通过。软件 357 项、演示、10 项真实配置检查通过；完整 `make test-lab` 因用户暂缓未执行，不能算本补丁完整隔离验收。仅订阅 API 重载，数据面/agent/monitor/既有 nginx 未重启，默认路由及运行代次不变。

证据及剩余边界见[本次报告](validation/server-followup-2026-10-20/README.md)。本次在 `codex/server-followup-20261020` 工作，主工作区既有未提交内容保留。待办 08/09 完成，01 保持 open，05/06 仍 deferred。

后续工作入口：[双云部署待办](../.scratch/wayvia-deployment-followup/spec.md)。手机已由用户移除，故障与重启测试继续暂缓。

## 2026-10-20 探针稳定性修复与性能续查

已在两台授权服务器部署有总期限的备用出口身份探针，移除日本健康检查绕回阿里云的额外跨境依赖；入口仍通过真实认证业务链路验证日本出口。网络超时可在原总期限内尝试备用端点，TLS/身份异常立即失败，全部失败仍保护并恢复。两端点同属 Cloudflare，不是跨供应商容灾；monitor 状态机未改。OpenVPN TCP 两端和探针中继启用 TCP_NODELAY，国内直连、VPN-over-proxy、服务器秘密和 fail-closed 保持不变。

两端新事务 ACTIVE、已重新显式发布；最终观察窗口和逐轮结果见[稳定性修复报告](validation/stability-fix-2026-10-20/README.md)。原最初网络超时的底层原因尚未证明，本次修复已确认的额外依赖和单端点失败放大问题，短时健康不能当作长期稳定验收。手机随后停止由用户确认是手动操作，保持停止状态并排除出断连统计。

约 14 分 40 秒观察中，两端各 56 轮、392 项分层检查全部健康；阿里云一次入口认证探针传输失败后，备用尝试 207 ms 验证成功，该轮保持 ACTIVE，未触发重建。该自然事件不代表已确认某个外部端点故障。

同一路径服务器新建 HTTPS 中位数约 259→171 ms，日本直连仍约 64 ms；手机本轮 Wi-Fi 新建约 493 ms、复用约 71 ms，前轮为 LTE，不能纯归因于补丁。完整链路下载仍约 1.64 Mbps。日本直接上传约 1.85–1.87 Mbps，两路并行总吞吐约 1.85 Mbps，支持出站共享容量瓶颈；提供商公开页面有 2 Mbps 档位，但账户未登录，实际购买套餐仍未确认。带宽低不能独自解释空闲首次请求延迟；负载时承载 TCP RTT 从最小约 42 ms 升至约 153 ms，另有排队影响。国内本轮镜像 403 未计作测速，完整手机策略验收仍未完成。

本补丁 `make check` 最终 347 项通过（3 项已有警告），`make demo` 和 10 项 `make test-configs` 通过。完整 `make test-lab` 含故障注入和恢复，按用户要求暂缓；真实故障和整机重启亦未做。旧基线完整实验通过不代表本补丁通过。部署源码摘要、版本、短时服务器验收与未完成项分别记录于报告、`compatibility.yaml` 和原始证据中。以下早期记录保留为历史经过。

## 2026-10-20 实际双云部署与正常路径验收

两台授权 Ubuntu 24.04/amd64 主机已安装固定版本软件和本地控制面，完成相同逻辑配置、分角色 VPN 私钥、生产 IP 证书与两端 staging 续期预演；四小时续期 timer 已启用。日本旧 CentOS/旧 VPN 由用户自行重装后再继续部署。阿里云已有网站与 Docker 业务保持运行，原虚拟主机摘要未变，两台宿主默认路由均保持不变。

用户放行 `8443/TCP` 和 `9443/TCP` 后，两端重新生成计划并完成真实 apply/confirm，均为 **ACTIVE**，七层 monitor 检查健康。相同 revision 经 SSH 传递新鲜出口证据后显式发布，Android 独立设备凭据已同步，私人 HTTPS 订阅下载返回 200，内容与完整导出一致。

在一次性 Linux 容器中，以锁定 sing-box 1.14.3 运行**未经修改的终端配置**，通过公网连接真实服务器：受控探针和国外 HTTPS 均返回日本出口；国内百度 HTTPS 返回 200，容器物理网卡抓到直达目标的连接；国外 UDP DNS 正常解析且没有直接发往国外解析器的物理 UDP 包，国外 AAAA 返回空答案，受保护 IPv6 请求失败。服务器连接观测同时确认 OpenVPN 只连接承载 SOCKS，跨主机承载连接再进入日本回环 VPN 服务，VPN-over-proxy 顺序保持。

**M6 正常服务器路径与外部 Linux 客户端通过；实体 Android 的 Clash Meta URL 导入、LTE 下选定国内目标直连和国外日本出口也已通过。** 手机短时下载已测：国内中位数 14.235 Mbps，国外经手机核心 1.360 Mbps，手机浏览器复核 1.678 Mbps。阿里云通过现有业务链路下载约 1.65 Mbps，日本服务器直接下载 8.906 Mbps，表明代理吞吐偏慢且能在服务器链路复现；具体根因仍待隔离。完整分流/DNS/IPv6 矩阵、全部应用 TUN、Wi-Fi/蜂窝切换、校园 DNS、上传和长期性能及费用仍待验证；真实故障及整机重启按用户要求另行安排。续期 dry-run 通过且 timer 已启用，但尚未经历未来真实自动续期周期。国内规则按固定域名表与 APNIC CIDR 匹配，不能视为实时地理识别；本次阿里云公网 IP 不在国内 CIDR 中，其探针端口按默认代理处理，入口端口有独立直连例外。

本轮手机测试保持中国移动 LTE 和现有规则配置。浏览器国内查询看到移动出口、国外查询看到日本出口，Clash 目标日志分别为 `DIRECT` 与 `Wayvia[Wayvia-entry]`。每组 3 次 2 MiB 下载；国内浏览器镜像请求返回 403，未计作吞吐，国内速度来自 USB 发请求、手机核心联网的测量。详见[实体 Android 分流与下载测试](validation/android-routing-speed-2026-10-20/README.md)，其中保留失败、重试、测量范围与服务器对照。

延迟补测：手机至入口平均 51.2 ms，阿里云至日本承载 TCP 建连平均 41.7 ms，现有 VPN 对端 RTT 平均 46.6 ms，日本至 Cloudflare 1.1.1.1 平均 3.2 ms。手机国外 HTTPS 复用连接首字节中位数 86 ms、小响应完成 120 ms；新连接中位数 605 ms，其中到目标 TLS 完成累计约 485 ms。另观察到 09:25 UTC 一次真实探针超时：日本、阿里云先后进入 DEGRADED 并由现有监控恢复，从日本出口停止至阿里云 ACTIVE 约 19 秒，最初超时原因未确认。正常路径通过不能代表长期稳定，本轮没有主动故障注入或重启。[延迟样本与异常时间线](validation/android-routing-speed-2026-10-20/latency.md)保留了测量边界和恢复证据。

完整结果与证据见 [双云部署验收](validation/cloud-deployment-2026-10-20/README.md)，此前安装、证书和端口阻塞经过保留在 [双云准备记录](validation/cloud-preparation-2026-10-20/README.md)。原主工作区已有改动保留，本轮记录位于独立 `codex/deployment-readiness-20261020` 工作区。

用户询问 NekoBox/Clash 后，已用相同发布 revision 和 Android 设备凭据额外生成完整 Mihomo YAML，真实 `mihomo 1.19.32 -t` 退出 0，见 [Clash 配置校验](validation/cloud-deployment-2026-10-20/clash-config-check.json)。随后应用户链接导入请求，为同一设备签发独立 Mihomo 订阅，公网 HTTPS 200、完整文件与已校验 YAML 一致，读取链接有效至 2026-11-19 08:47:19 UTC，见 [订阅验证](validation/cloud-deployment-2026-10-20/clash-subscription-validation.json)。数据面配置与设备密码未改变。NekoBox 尚未按具体版本适配。

用户报告手机导入转圈后，通过 USB 确认其原客户端为旧 Clash 2.5.12 Premium。安装官方、摘要校验通过的 Clash Meta 2.11.35，同时仅为专属订阅位置启用 gzip；传输量从 9.2 MB 降至 1.15 MB，外部下载从约 34.6 秒降至 15.5 秒，解压后配置不变。实体手机在当前 LTE 网络约 22 秒完成 URL 导入，随后显示规则模式运行，浏览器自有 HTTPS 探针观察到日本出口。未单独证明旧版无限等待的唯一原因；服务端生成仍约 14 秒，手机完整策略矩阵仍待验证。详见 [Android 导入排查与实机结果](validation/android-import-2026-10-20/README.md)。

## 2026-10-20 双云部署准备复核

完整重跑已通过：`make test-lab` 退出 0，用时 463 秒，取得全部模块及 23 项正式安装事务的新鲜结果；`make clean-lab` 退出 0。本次四项门禁现已全部通过，首次 480 秒中止记录继续保留，见 [重跑命令](validation/deployment-review-2026-10-20/commands-retry.json) 与 [正式事务](validation/deployment-review-2026-10-20/retry-latest-lab/transaction-result.json)。

用户已授权两台指定主机部署，实际故障和整机重启测试另行安排；无域名，采用 IP 证书方案。日本原 CentOS 7/旧 VPN 由用户自行重装，随后核实 Ubuntu 24.04.1，443 空闲。阿里云 Ubuntu 24.04 存在其他网站与 Docker 业务，部署保持这些服务，使用独立端口。此处记录部署前复核，当前实网结果以上节为准。

以下为首次检查经过（后续完整重跑结果以上文为准）：

检查 `0e5bbbc`，独立 `codex/deployment-readiness-20261020` 工作区；生产源码与测试不变，实验仅隔离 Docker 名称。`make check` 325 项通过（3 项已有弃用警告），`make demo` 通过，`make test-configs` 10 项真实校验通过。`make test-lab` 完成 23 项链路、7 项协议、6 项 DNS、12 项冲突、TUN、executor 和 2 项 systemd 恢复后，在正式事务阶段达到本次复验设置的 480 秒总时限，被 SIGINT 中止；make 进程返回 -2，子目标报告 130。Ubuntu 快照下载与重试占据大部分运行时间。本轮没有新鲜正式事务结果，不能算完整门禁通过，也不能据此认定功能回归。`make clean-lab` 退出 0，本轮容器与网络无残留。

重新通过 GitHub API 查询确认同一提交的 CI run 38850714713：software、amd64、arm64 均成功；这是已存在的 CI 结果，本次没有触发新 CI。软件实现和隔离验证已有通过证据，实网 M6 与 OpenWrt M7 状态不变。完整本地复验需在足够运行时间与依赖可用条件下完成，真实部署前仍须主机、域名、证书和手机输入。

[检查证据与双云部署方案](validation/deployment-review-2026-10-20/README.md)包含源版本、命令、组件版本、时限中止及清理记录。主工作区原有未提交内容未修改；本次记录只位于独立审查工作区。

更新：2026-10-15（America/Los_Angeles）。以 QOKIK `cb2676b` 合入 main 后的源码为基础，补齐 v2rayA 启动等待与 arm64 APT 快照冷索引问题；修复在 `codex/qokik-main-integration-20261015` 开发，交付目标为 main 和同步后的 QOKIK。

## 2026-10-15 交付结论（历史）

nftables 基线已忽略实时计数和对象句柄，同时拒绝未知顶层对象；完整回退快照保持不变。v2rayA 的首次应用和恢复共用有截止时间的只读就绪检查，错误响应与版本不符立即失败，配置写入不自动重试。Ubuntu 快照保留 `20261014T064100Z` 和 systemd `.17` 精确版本，新增 `ports.ubuntu.com` 快照映射，解决 arm64 派生镜像清空 APT 索引后找不到包的问题。

本轮 `make check` 325 项通过（3 项已有弃用警告），格式、lint、sdist/wheel 与 CLI help 通过；`make demo` 通过；`make test-configs` 10 项通过。使用新构建的 arm64 基础/systemd 镜像，`make test-lab` 完整退出 0：23 项核心链路、23 项正式事务、7 项协议、6 项 DNS、12 项内核冲突、2 项独立恢复，以及 TUN 与 nft 执行器回归通过。真实 v2rayA 进程四次延迟约 4.001–4.009 秒，仍完成 `443→8443→9443→8443→443` 两次更新及连续两次回退；容器安装代码摘要与本次源码一致。

已把隔离 Linux CI 扩展至 amd64 和 arm64，每次均执行完整实验和冷索引镜像构建。推送后的 CI 结果以对应提交的 GitHub Actions run 为准。本轮第一次完整验收的失败记录、修正后的全部日志、版本和隔离清理证明见[合并与补充修复验收](validation/main-carrier-readiness-2026-10-15/README.md)。历史 316 项软件测试及 WSL 构建记录保留，不能当作本轮结果。

上述是软件和一次性 Linux 容器验收；M6 真实服务器与设备部署尚未执行，OpenWrt M7 尚未实现／验收。

## 2026-10-15 首次 APT 快照修复记录（历史）

GitHub Actions 的 `software` job 通过；原 `isolated-linux` 在 systemd 实验镜像干净重建时因 Ubuntu 在线依赖版本不一致而失败。已在基础实验镜像 Dockerfile 中固定 Ubuntu APT 快照 `20261014T064100Z`，派生 systemd 与事务镜像继承该设置；关键包精确版本 pin 保留。2026-10-15 干净基础/systemd 镜像构建通过，完整隔离实验从 WSL `/tmp` 启动退出码为 0。根因跟进发现：容器向 D 盘 bind mount 写验证证据后，WSL 的 `/mnt/d` cwd 路径解析失效；从 Linux 文件系统目录启动可通过。详见[APT 快照锁定摘要](validation/apt-snapshot-lock-2026-10-15/summary.md)。

## 2026-10-12 历史审查（已由本次修复更新）

检查基线提交的 [CI run 38347888025](https://github.com/AAAxianyu/Wayvia/actions/runs/38347888025) 软件任务通过、隔离 Linux 任务失败：Docker 自动子网拒绝指定静态 `--ip`。本次交付已纳入改用实际分配地址的修复和事务诊断增强，新的 amd64 CI 结果仍待验证。功能代码位于 `codex/m0-m5-delivery`；GitHub 默认 `main` 尚为文档版本，未合并该开发分支。真实阿里云／日本服务器没有可确认的部署验收记录，M6 尚未执行；OpenWrt M7 尚未实现／验收。

此前 15 项审计问题（4 项 P1、10 项 P2、1 项 P3）的修复及通过记录仍保留于 [历史命令记录](validation/fixes-2026-10-12/commands.json)。这些记录只代表当时的软件和受控实验结果。

[修复追踪](validation/fixes-2026-10-12/README.md) 逐项对应 4 项 P1、10 项 P2、1 项 P3，保留 [修复前审计](validation/review-2026-10-12/README.md)。此外，正式安装集成发现并修复了首次 pending 阻断、CLI 参数不一致、共享运行目录被删除、账户创建中断恢复等交付问题。

## 2026-10-12 检查记录与未关闭项（历史快照）

检查采用当前源码副本，保留原工作区改动和既有验证文件。为避免干扰已有实验，只在副本中替换 Docker 容器、网络和镜像名称；生产源码与测试断言未改变。记录见 [名称隔离](validation/status-review-2026-10-12/lab-namespace.json) 和 [源码摘要](validation/status-review-2026-10-12/source.json)。初次 uv 沙箱启动错误及 Docker 不共享 `/private/tmp` 的环境错误均保留，环境调整后重新执行对应检查。

| 命令 | 本轮结果 |
| --- | --- |
| `make check` | 退出 0；298 项测试、格式、静态检查、wheel/sdist 与 CLI help 通过；3 项依赖弃用警告 |
| `make demo` | 退出 0；明确的 simulated 控制面演示 |
| `make test-configs` | 退出 0；10 项真实组件校验通过 |
| `make test-lab` | 退出 2；23 项路径、7 项协议、12 项内核冲突、6 项 DNS、TUN、executor 和 2 项独立恢复通过；正式安装事务完成 8 项后在入口多代更新失败 |
| `make clean-lab` | 退出 0；本轮专用容器和网络无残留，既有实验未操作 |

续查已确认 baseline 把 `nft list table` 的实时 packets/bytes 计数纳入摘要。使用原生产执行器在一次性 Linux 容器中，真实被阻断连接仅使计数增加、未改变规则结构，也会触发 `BASELINE_CHANGED`；见 [真实内核复现](validation/status-review-2026-10-12/continuation/kernel-baseline-result.json)。随后仅在临时副本添加脱敏观测，正式事务在第三代入口更新时再次失败：计划与 apply 之间仅命名空间 guard 计数从 packets 1 / bytes 48 变为 packets 2 / bytes 96，配置、服务和路由未变。拒绝发生在新事务创建与网络变更之前，上一代仍为 ACTIVE。见 [根因与变化字段](validation/status-review-2026-10-12/continuation/root-cause.json)。原先全量失败未保存成对 baseline，其具体代次不能从原记录断定；此前“第二代”的表述已修正为入口多代更新。

临时修正候选仅在 baseline 摘要中排除 nft 实时计数，完整 capture/rollback 快照和其它冲突检查不变。真实规则变化仍被拒绝，且 [候选正式事务](validation/status-review-2026-10-12/continuation/candidate-transactions/transaction-result.json) 的 23 项全部通过，覆盖多代更新、连续回退、两层故障保护与恢复、独立恢复、重装、撤销和卸载。候选尚未应用到仓库生产代码，因此不能把候选通过当成当前代码门禁通过。可审核修改见 [候选补丁](validation/status-review-2026-10-12/continuation/baseline-candidate.patch)，完整续查记录见 [续查汇总](validation/status-review-2026-10-12/continuation/summary.json)。下一步应用正式修复、补回归检查并重跑四项门禁，同时复验本次交付的 amd64 CI。

当时的检查未修复生产代码。现有 `RECOVERY_REQUIRED` 历史失败及后续专项通过记录继续保留于 `validation/fixes-2026-10-12/ci-follow-up/`，不能用单次重跑覆盖失败证据。

## 阶段状态

| 阶段 | 已完成的软件与专项验证 | 验证边界 |
| --- | --- | --- |
| M0 | Python 3.12、uv.lock、CLI、严格配置 Schema；sdist 明确边界，排除共享工作区无关内容 | 最终 make check 见命令记录；软件测试不要求主机网络接管 |
| M1 | 策略编译、7511 APNIC CIDR、110185 国内域名；sing-box/mihomo 完整配置；mihomo 真实 6 项重叠 DNS 行为通过 | 校园真实规则和手机应用导入仍属 M6 |
| M2 | SQLite/私密凭据、设备新增轮换撤销、私人订阅；轮换中断可重试；显式双角色版本发布与持久快照 | 正式 API→订阅→真实入口已验证；不宣称跨主机原子提交 |
| M3 | 真实代理/VPN适配、固定 Linux 动作、事务日志、current/predecessor、凭据检查点、回退与恢复、带期限诊断 | 时间边界及崩溃注入用软件回归；真实 timer/内核/协议另有隔离证据 |
| M4 | 本轮 23 项双层路径、23 项正式事务、7 项协议、12 项资源冲突、6 项 DNS、TUN、executor、timer/recover 全部通过；实际 4 秒延迟下两次更新和连续回退通过 | 软件与 arm64 隔离门禁通过；跨版本升级、真实整机重启和手机实网仍需专门验收 |
| M5 | 安装 journal、账户创建意图、生命周期锁、所有权清理、备份保留、统一 wayvia-system/控制目录和运维文档 | 实际安装/重装/卸载在专用临时容器执行，未操作真实服务器 |
| M6 | 2026-10-20 两端 ACTIVE，正常公网路径、外部 Linux 客户端、Android Clash Meta 导入和基本 HTTPS 出口通过 | 手机完整策略与切网、真实故障及完整重启、性能费用尚待验收，见本文顶部最新记录 |
| M7 | 未实现／未验收 | 指定 OpenWrt 设备、固件、透明接管与避免重复代理 |

## 本轮关键闭环

- 正常 apply、回退和恢复都采用最新设备检查点；正式实验确认撤销后重放旧计划、回退均不能重新使用旧密码。
- 角色 current 和前驱链覆盖三代部署；真实入口端口 443→8443→9443→8443→443，旧监控不会停止待确认新入口。出口角色另外验证同配置三代运行归属。
- 运行故障先保护再重建，VPN/carrier 分别注入故障、monitor 恢复；凭据保护持续到获授权且校验成功的协调。首次未部署只保存有效检查点并保持 pending。
- confirm 具有总时间预算与提交复核；残留旧 timer 不回退当前代次。独立 systemd timer 在原控制器退出后完成回退。
- 入口探针真实完成 TLS、Trojan 认证与业务请求；承载探针验证 VPN 服务端身份；VPN 运行状态从私有管理 socket 查询。出口单独报告 component_readiness / ready，入口报告 datapath / healthy。
- 探针与 monitor 证据绑定 revision、generation、观测时间、耗时、90 秒有效期。陈旧、模拟、错代次和格式异常证据不能作为发布依据；旧端口型探针须重新计划。
- 发布快照只有在同 revision 双端确认和新鲜观测后更新。修改 desired config 或失败部署不会把未确认内容发给订阅；设备撤销仍实时执行。
- TUN 超时定位为实验启动竞态：SOCKS 就绪时内核 TUN 策略尚未完成，连接提前选择物理源地址并被保护规则阻断。新增明确内核就绪门禁，没有增加业务请求重试或修改 sing-box。见 [完整因果证据](validation/fixes-2026-10-12/tun/README.md)。

## 上一轮修复检查（历史记录）

最终命令退出码与日志以 [commands.json](validation/fixes-2026-10-12/commands.json) 为准。`make demo` 始终是明确的 simulated 控制面演示；`make test-configs` 是 10 项真实组件解析／加载；`make test-lab` 串联全部隔离模块，缺少 Docker、网络命名空间或真实 systemd 会非零退出。

| 命令 | 最终结果 |
| --- | --- |
| `make check` | 退出 0；291 项测试通过、3 项依赖弃用警告；格式、静态检查、wheel/sdist 构建和 CLI help 通过 |
| `make demo` | 退出 0；明确标记的模拟演示通过 |
| `make test-configs` | 退出 0；10 项真实配置解析／加载通过 |
| `make test-lab` | 退出 0；23 项路径、7 项协议、12 项内核冲突、6 项 DNS、TUN 就绪、executor、独立 timer/recover 和 23 项正式事务全部通过 |
| `make clean-lab` | 退出 0；任务实验容器、临时网络和生成凭据已清理 |

最后一轮检查中的测试契约调整也保留记录：有效的初始凭据协调现在写入“无当前事务、无阻断”的 runtime 状态，旧测试仍要求文件不存在；已改为核验运行语义，未弱化凭据或主机命令约束。

正式事务门禁还发现重装后 systemd 进程已启动、agent socket 尚未就绪的实验脚本竞态。已等待真实 agent 状态接口与控制 API 就绪后，再单次核验 ACTIVE 和代次；不重试业务失败。原失败及最终全量通过记录均保留于修复证据目录。

旧提交 `d408163` 的 amd64 [CI run 38241319117](https://github.com/AAAxianyu/Wayvia/actions/runs/38241319117) 是历史基线；本次本地隔离测试架构为 arm64，不能把旧 CI 冒充本次修复的远端结果。

## 仍需实网输入

M6 需要两台明确授权主机的地域、系统、接口、资源、带宽，域名和 TLS/VPN 证书条件，Android/iPhone 具体 App 版本，校园域名/网段/DNS、UDP 应用与 Wi-Fi/蜂窝测试条件。M7 另需 OpenWrt 型号和固件。

代理 IPv6 及 UDP 底层 VPN 仍未支持；隧道内 TCP/UDP、国内 IPv6 有受控证据。mihomo DNS 等价不代表完整 mihomo TUN 或所有手机 App 已验收。证书续期、真实跨地域性能、费用和生产 SLA 均未验证。

本轮没有修改开发机路由、全局防火墙、既有 VPN 或未授权服务器。所有实验使用明确标记的一次性 Linux 容器/命名空间，正式双角色连接本任务创建并清理的 Docker internal 网络；无关项目未被修改或纳入提交。
