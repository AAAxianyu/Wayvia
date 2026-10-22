# Wayvia 双云部署后续待办

Status: open
Updated: 2026-10-22 UTC

本轮完成不依赖手机的订阅提速、安装权限修复，并取得稳定性续查和误判修复证据。本目录按仓库 issue tracker 约定逐项记录接续工作；本次按用户要求创建有限观察窗口的每小时跟进，不自动恢复手机测试，也不授权购买带宽或故障注入。

## 下次从这里继续

2026-10-22 UTC 收尾：原定正常观察已结束。入口样本覆盖约 80.09%，日本按期退出；生产累计入口 10 次、日本 3 次自然降级，业务根因未解。系统自动更新触发 needrestart 重启一次性观察器，导致入口尾部采样缺失；见[最终报告](../../docs/validation/stability-observation-2026-10-20/final-report.md)。本轮 heartbeat 已暂停，不延长或重新采样。后续先处理待办 12 的软件恢复范围和待办 13 的观察器维护协调；不联系平台，不重放部署脚本。

1. 先读 [24 小时观察与接续流程](../../docs/validation/stability-observation-2026-10-20/README.md)、[最新实现状态](../../docs/implementation-status.md)和[最新续查报告](../../docs/validation/server-followup-2026-10-20/README.md)，再检查 Git 工作区。主工作区原有未提交的进度检查记录独立保留，不能覆盖或误算成本次提交。
2. 01 保留真实超时根因问题；从最终证据接续，新的观察窗口另行安排。优先处理 12/13；手机就绪时处理 02/04。08、09 已完成，禁止恢复供应商联系。
3. 手机由用户手动停止并移除，临时 ADB 转发已清空；这不是断连。重新测试前确认设备和客户端状态，不自动改用旧私密缓存中的链接或凭据。
4. 故障和重启测试继续暂缓，包括 05 的完整隔离目标及 06 的真实测试。开始这些项前先确认用户恢复授权和具体范围。
5. SSH 密码、私钥、设备认证和订阅 token 不在仓库。按授权渠道取得当前凭据；忽略的部署缓存只是本机辅助材料，不能作为可迁移凭据来源或重放脚本直接执行。

## 当前可依赖的结果

- 约 16 小时 21 分钟历史记录：阿里云 3708 轮/9 次降级，日本 3728 轮/2 次降级。已证明并部署完成响应被 relay 关闭异常覆盖的探针修复；真实 HTTP/DNS 超时仍有未确定原因。
- 候选和部署后各 100 次业务探针通过；不能代替修复后长期稳定性验收。最新监控窗口见续查报告。
- 相同完整订阅同机 TLS 首字节中位数 12.97 → 4.22 秒；阿里云自身访问公网地址 13.49 → 4.33 秒，完整下载 15.15 → 6.39 秒。输出逐字节一致，未缓存最终响应。
- 严格 umask 077 下正常新装、相同版本重装、非 root API 首次启动通过；真实两端私钥/控制秘密访问边界审查通过。
- 本补丁软件 357 项、演示和 10 项真实配置检查通过；完整隔离故障/恢复实验仍暂缓。
- 之前完整链路下载约 1.6 Mbps、日本出站约 1.85 Mbps；购买额度未核实。本轮没有手机速度/分流新验收。

## 待办索引

`in_progress` 表示观察进行中，`open` 表示可开始独立工作，`blocked` 表示缺少明确输入，`deferred` 表示用户暂缓。`done` 表示该票本轮约定范围已完成，不代表整个 M6 验收。

| 待办 | 优先级 | 状态 |
| --- | --- | --- |
| [01 持续观察探针与自然断连](issues/01-long-term-stability.md) | P1 | open |
| [02 拆分手机首次建连延迟](issues/02-phone-cold-connection-latency.md) | P1 | blocked |
| [03 核实日本公网出站额度与吞吐瓶颈](issues/03-egress-bandwidth.md) | P1 | blocked |
| [04 补齐实体 Android 分流与协议矩阵](issues/04-mobile-routing-acceptance.md) | P1 | blocked |
| [05 补跑当前补丁完整隔离实验](issues/05-current-patch-isolated-lab.md) | P1 | deferred |
| [06 安排真实故障与整机重启验收](issues/06-real-fault-reboot-acceptance.md) | P1 | deferred |
| [07 观察真实证书续期与订阅生命周期](issues/07-renewal-and-subscription-lifecycle.md) | P2 | open |
| [08 降低订阅生成首字节等待](issues/08-subscription-generation-speed.md) | P2 | done |
| [09 复核严格 umask 下的安装与重装权限](issues/09-install-permissions.md) | P2 | done |
| [10 补充 NekoBox 与 iPhone 客户端适配](issues/10-client-compatibility.md) | P3 | blocked |
| [11 Windows TUN 国外 IPv4 自动使用与回退](issues/11-windows-ipv4-fallback.md) | P1 | open |
| [12 探针错误分类与恢复范围](issues/12-software-timeout-recovery.md) | P1 | open |
| [13 一次性观察器与系统维护的协调](issues/13-observer-maintenance-resilience.md) | P1 | open |

## 工作边界

在 `codex/` 分支继续。保留国内直连、VPN-over-proxy、服务器承载秘密和 fail-closed。不得改变开发机路由、全局防火墙、既有 VPN 或未授权主机。实验仅限明确一次性 Linux 环境。核验已有门禁是否仍对应当前源码；变化后按范围补跑，缺少环境须记录非零结果。

本目录是可提交的接续清单。每项完成后更新该票状态、证据和 `docs/implementation-status.md` / `compatibility.yaml`；不要把当前待办的存在当成已完成验收。
