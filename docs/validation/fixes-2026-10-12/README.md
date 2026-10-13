# 15 项审计问题修复与验证

修复基线：`d4081636c15ea2c0530b2e53ff529c0a1a6a3cc2`；分支 `codex/m0-m5-delivery`。日期为 America/Los_Angeles 2026-10-12，原始实验日志使用 UTC 2026-10-13。

本记录对应 [原始 15 项审计](../review-2026-10-12/README.md)。15 项全部完成修复，四项约定检查全部退出 0。保留原始失败条件与证据，不以新结果覆盖历史；最终退出码、日志摘要和验证范围见 [commands.json](commands.json)。

## 最终统一门禁

| 命令 | 结果与证据 |
| --- | --- |
| `make check` | 291 项通过、3 项第三方依赖弃用警告；格式、静态检查、源码包／wheel 构建及 CLI help 通过，见 [日志](make-check.txt) |
| `make demo` | 明确模拟模式通过，见 [日志](make-demo.txt) |
| `make test-configs` | 10 项真实组件校验通过，见 [报告](final-configs/config-result.json) 和 [日志](make-test-configs.txt) |
| `make test-lab` | 全部模块通过，见 [日志](make-test-lab.txt) 与 `final-lab/` 原始报告；正式安装事务 [23 项](final-lab/transaction-result.json) 通过 |
| `make clean-lab` | 清理通过，见 [日志](make-clean-lab.txt)；随后按任务标签检查，容器和临时网络剩余均为零 |

真实路径 23 项、协议 7 项、内核资源冲突 12 项、mihomo DNS 6 项及独立 timer/recover 2 项分开记录；TUN 分阶段拒绝和生产 LinuxExecutor 回退也在同一门禁中执行，不把这些不同层次简单合并成单一业务覆盖率。

最终门禁之前的两次修正完整保留：初始凭据检查点回归从“文件不存在”改为检查有效的空运行状态，见 [失败日志](make-check-before-final-assertion.txt)；正式实验重装检查增加 agent/API 就绪等待，避免将尚未绑定的 socket 当业务失败，见 [原始报告](final-lab-before-agent-readiness/transaction-result.json)。等待只用于服务启动，ACTIVE、代次和业务请求仍单次断言。

## 修复追踪

| 问题 | 修复行为 | 主要验证位置 |
| --- | --- | --- |
| R01 · P1 | apply、回退、恢复均在角色锁内覆盖最新设备检查点，分别记录提交与实际生效摘要，旧计划不能复活撤销用户 | `tests/unit/test_transaction_revisions.py`；正式事务实验的撤销后旧计划重放、真实旧密码请求 |
| R02 · P1 | 持久化故障原因；运行故障 DEGRADED 可在 guard 保留时重建并重新验证；显式和凭据阻断不能自动解除 | 同上；正式 VPN/carrier 分别故障、monitor 恢复及独立 recover |
| R03 · P1 | 每角色 current 指针和 predecessor 链；回退同时恢复资源、运行归属与监控，支持逐代回退至卸载 | 三代状态机与正式 A→B→C→B→A；独立恢复和卸载 |
| R04 · P1 | monitor 只作用于当前代次，旧 ACTIVE 及残留 timer 无权停止待确认的新入口 | 多代／变端口状态机回归；正式入口变端口与旧 monitor 交错 |
| R05 · P2 | 确认前、探针总预算、确认提交点统一检查期限；超时后回退 | 确认越期和探针预算回归；独立真实 systemd timer 另验 |
| R06 · P2 | 不再豁免所有 tun0；核验角色所有权及 kernel connected/local 路由，检查所有表的静态／策略路由冲突 | `tests/contract/test_resource_conflicts.py`；真实隔离内核冲突测试 |
| R07 · P2 | mihomo DNS 后序规则不能覆盖前序；精确 bootstrap 域名不被后序后缀覆盖、不扩大到子域 | 渲染回归；`mihomo-dns-result.json` 的 6 项真实 resolver 计数 |
| R08 · P2 | 轮换使用唯一 secret_ref，密钥先持久化、数据库再切换引用，中断遗留文件不阻塞重试 | `tests/integration/test_publication_recovery.py`；正式 API 轮换与旧密码拒绝 |
| R09 · P2 | migration 2 保存发布快照；双角色同 revision、当前 ACTIVE 和新鲜证据才允许显式 publish；export/issue/fetch 使用发布版本 | 控制面发布与失败恢复回归；正式确认→发布→订阅→真实请求 |
| R10 · P2 | access 绑定渲染端点，实际 Trojan TLS/认证请求；carrier 校验 OpenVPN 身份；vpn 查询当前运行会话；出口单独只能报告 ready | `protocol-result.json` 真实 7 项正负向；正式双层入口业务；旧 journal 的端口探针拒绝重新变 healthy |
| R11 · P2 | 每条证据绑定时间、耗时、revision、generation、scope；90 秒期限及独立 monitor 心跳，陈旧/模拟/格式异常证据不健康 | `tests/contract/test_diagnostics.py`、管理 socket 协议回归、发布拒绝回归 |
| R12 · P2 | 安装变更前写持久 journal，按 manifest 提交点恢复；系统运行环境失败可重试 | 安装逐写入中断回归；正式 system install 和锁定软件重装 |
| R13 · P2 | 系统 CLI/API 共用 wayvia 身份及 `/var/lib/wayvia/control`；文档参数与 CLI 一致；首次 pending 有独立语义 | CLI 回归；正式 API 新增、CLI 读取、轮换撤销 |
| R14 · P2 | sdist 明确 only-include，不遍历共享目录其他项目 | `tests/integration/test_packaging.py`；当前工作区实际 wheel/sdist 构建 |
| R15 · P3 | 升级清理未修改的过时自有文件，修改过的文件继续跟踪并保留 | 真实 staging 升级→卸载和修改文件保留回归 |

## 正式协议观测

入口内部探测用户仅由 root agent 管理，不进入设备列表、订阅、日志或公开事务状态。日本端通过 Trojan 仅访问固定 OpenVPN 目标，证书探测不携带真正客户端证书，不抢占现有会话。入口实际认证 HTTP 响应必须与 namespace 出口目标一致。

探针 helper 使用单独进程组；总预算超时会停止它创建的 OpenVPN/curl 后代。真实父、子、孙进程测试另行验证该约束。OpenVPN management 使用顺序请求/END 响应，不把历史 CONNECTED 或 status 文本当当前状态；TLS 转发处理非阻塞控制报文，避免 TLS 1.3 session ticket 阻塞另一方向。

`protocol-result.json` 包含真实 OpenVPN 服务端身份、实时管理状态、真实 Trojan→VPN 正向，以及错误 Trojan 密码、错误 TLS 名称、错误 VPN 名称、普通 TCP 伪端点的负向结果。负向案例中的 `healthy: false` 是预期结果，案例 `status: passed` 表示拒绝成立。

## TUN 超时根因

见 [TUN 完整记录](tun/README.md)。失败连接比 TUN 接口 UP 和策略路由安装早约 322 ms，已经选定物理源地址。保护规则将其阻断，SOCKS 已通不等于原生 TUN 就绪。新增内核接口、双阶段路由和源地址检查，业务请求仍只执行一次；真实分阶段反例、12 次冷启动和完整链路分别记录。修复的是实验的就绪条件，没有修改 sing-box，也没有将采样成功当作所有平台稳定性的保证。

## 验证边界

软件回归、真实组件/内核、正式安装事务和实网验收是不同层次。状态机的临界时间与崩溃注入主要由受控软件回归证明；真实 systemd timer、网络资源和业务路径有独立实验。软件重装验证不能冒充跨版本数据库降级或目标主机完整重启。

所有网络实验仅运行于明确标记的临时 Linux Docker 容器/命名空间；正式双角色通过本任务的 Docker internal 网络连接受控目标。没有更改开发机路由、全局防火墙或既有 VPN，没有登录生产服务器。未读取或修改同工作区的无关项目。

M6 真实云服务器、域名证书续期、Android/iPhone 实际 App 导入与切网、整机重启、性能与费用仍未验收；OpenWrt M7 仍未实现。当前证据也不把 mihomo DNS 等价扩大为完整 mihomo TUN 或所有手机客户端验收。
