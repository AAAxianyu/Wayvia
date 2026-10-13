# Wayvia 实现与验收状态

更新：2026-10-12（America/Los_Angeles）。分支 `codex/m0-m5-delivery`。本轮检查基线 `739eef73d6185e6cad7b436b23a09a4be8d3dc60` 加已有未提交的 Wayvia 修复；此前修复基线为 `d4081636c15ea2c0530b2e53ff529c0a1a6a3cc2`。

## 当前结论

M0–M5 主要软件模块已实现，但当前全量交付门禁未通过。本轮 `make check` 通过 298 项测试，`make demo` 和 10 项真实配置校验通过；`make test-lab` 在入口多代更新时返回 `BASELINE_CHANGED`，make 退出 2。当前结论以 [本轮检查记录](validation/status-review-2026-10-12/summary.json) 为准，不能沿用此前“全部通过”的结论。

检查基线提交的 [CI run 38347888025](https://github.com/AAAxianyu/Wayvia/actions/runs/38347888025) 软件任务通过、隔离 Linux 任务失败：Docker 自动子网拒绝指定静态 `--ip`。本次交付已纳入改用实际分配地址的修复和事务诊断增强，新的 amd64 CI 结果仍待验证。功能代码位于 `codex/m0-m5-delivery`；GitHub 默认 `main` 尚为文档版本，未合并该开发分支。真实阿里云／日本服务器没有可确认的部署验收记录，M6 尚未执行；OpenWrt M7 尚未实现／验收。

此前 15 项审计问题（4 项 P1、10 项 P2、1 项 P3）的修复及通过记录仍保留于 [历史命令记录](validation/fixes-2026-10-12/commands.json)。这些记录只代表当时的软件和受控实验结果。

[修复追踪](validation/fixes-2026-10-12/README.md) 逐项对应 4 项 P1、10 项 P2、1 项 P3，保留 [修复前审计](validation/review-2026-10-12/README.md)。此外，正式安装集成发现并修复了首次 pending 阻断、CLI 参数不一致、共享运行目录被删除、账户创建中断恢复等交付问题。

## 本轮检查与未关闭项

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

本轮未修复生产代码。现有 `RECOVERY_REQUIRED` 历史失败及后续专项通过记录继续保留于 `validation/fixes-2026-10-12/ci-follow-up/`，不能用单次重跑覆盖失败证据。

## 阶段状态

| 阶段 | 已完成的软件与专项验证 | 验证边界 |
| --- | --- | --- |
| M0 | Python 3.12、uv.lock、CLI、严格配置 Schema；sdist 明确边界，排除共享工作区无关内容 | 最终 make check 见命令记录；软件测试不要求主机网络接管 |
| M1 | 策略编译、7511 APNIC CIDR、110185 国内域名；sing-box/mihomo 完整配置；mihomo 真实 6 项重叠 DNS 行为通过 | 校园真实规则和手机应用导入仍属 M6 |
| M2 | SQLite/私密凭据、设备新增轮换撤销、私人订阅；轮换中断可重试；显式双角色版本发布与持久快照 | 正式 API→订阅→真实入口已验证；不宣称跨主机原子提交 |
| M3 | 真实代理/VPN适配、固定 Linux 动作、事务日志、current/predecessor、凭据检查点、回退与恢复、带期限诊断 | 时间边界及崩溃注入用软件回归；真实 timer/内核/协议另有隔离证据 |
| M4 | 本轮 23 项双层路径、7 项协议、12 项资源冲突、6 项 DNS、TUN、executor、timer/recover 通过；历史正式安装事务有 23 项通过记录 | 本轮全量在第二代入口更新返回 BASELINE_CHANGED，M4 门禁未通过；跨版本升级与整机重启仍需专门验收 |
| M5 | 安装 journal、账户创建意图、生命周期锁、所有权清理、备份保留、统一 wayvia-system/控制目录和运维文档 | 实际安装/重装/卸载在专用临时容器执行，未操作真实服务器 |
| M6 | 未执行 | 两台授权主机、生产域名证书、手机导入与切网、完整重启、性能费用 |
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
