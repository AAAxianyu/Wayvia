# Wayvia 双云部署后续待办

Status: open
Updated: 2026-10-20

本阶段已收尾。本目录按仓库 issue tracker 约定逐项记录未完成工作；不创建定时任务，不自动恢复手机测试，也不授权购买带宽或故障注入。

## 下次从这里继续

1. 先读[最新实现状态](../../docs/implementation-status.md)和[阶段报告](../../docs/validation/stability-fix-2026-10-20/README.md)，再检查 Git 工作区。主工作区原有未提交的进度检查记录独立保留，不能覆盖或误算成本次提交。
2. 优先处理 01 稳定性只读复核；手机就绪时处理 02/04，提供商账号资料可用时处理 03。无需手机的软件工作可从 08、09 的本地审查开始。
3. 手机由用户手动停止并移除，临时 ADB 转发已清空；这不是断连。重新测试前确认设备和客户端状态，不自动改用旧私密缓存中的链接或凭据。
4. 故障和重启测试继续暂缓，包括 05 的完整隔离目标及 06 的真实测试。开始这些项前先确认用户恢复授权和具体范围。
5. SSH 密码、私钥、设备认证和订阅 token 不在仓库。按授权渠道取得当前凭据；忽略的部署缓存只是本机辅助材料，不能作为可迁移凭据来源或重放脚本直接执行。

## 当前可依赖的结果

- 两端已部署有期限的备用出口身份探针和 TCP_NODELAY，原保护状态机不变。
- 约 14 分 40 秒观察，两端各 56 轮全部通过；一次传输失败由备用新鲜身份验证成功接续。长期稳定性及最初瞬时网络异常根因仍未验收。
- 服务器新建 HTTPS 中位数约 171 ms；手机本轮 Wi-Fi 新建约 493 ms、复用约 71 ms。手机前后网络不同，不能直接归因。
- 完整链路下载约 1.6 Mbps，日本出站约 1.85 Mbps；实际购买额度未核实。
- 本补丁软件 347 项、演示和 10 项真实配置检查通过；完整隔离实验暂缓。软件、隔离路径、真实部署验收分别记录。

## 待办索引

`open` 表示可开始独立工作，`blocked` 表示缺少明确输入，`deferred` 表示用户暂缓。所有条目尚未完成。

| 待办 | 优先级 | 状态 |
| --- | --- | --- |
| [01 持续观察探针与自然断连](issues/01-long-term-stability.md) | P1 | open |
| [02 拆分手机首次建连延迟](issues/02-phone-cold-connection-latency.md) | P1 | blocked |
| [03 核实日本公网出站额度与吞吐瓶颈](issues/03-egress-bandwidth.md) | P1 | blocked |
| [04 补齐实体 Android 分流与协议矩阵](issues/04-mobile-routing-acceptance.md) | P1 | blocked |
| [05 补跑当前补丁完整隔离实验](issues/05-current-patch-isolated-lab.md) | P1 | deferred |
| [06 安排真实故障与整机重启验收](issues/06-real-fault-reboot-acceptance.md) | P1 | deferred |
| [07 观察真实证书续期与订阅生命周期](issues/07-renewal-and-subscription-lifecycle.md) | P2 | open |
| [08 降低订阅生成首字节等待](issues/08-subscription-generation-speed.md) | P2 | open |
| [09 复核严格 umask 下的安装与重装权限](issues/09-install-permissions.md) | P2 | open |
| [10 补充 NekoBox 与 iPhone 客户端适配](issues/10-client-compatibility.md) | P3 | blocked |

## 工作边界

在 `codex/` 分支继续。保留国内直连、VPN-over-proxy、服务器承载秘密和 fail-closed。不得改变开发机路由、全局防火墙、既有 VPN 或未授权主机。实验仅限明确一次性 Linux 环境。核验已有门禁是否仍对应当前源码；变化后按范围补跑，缺少环境须记录非零结果。

本目录是可提交的接续清单。每项完成后更新该票状态、证据和 `docs/implementation-status.md` / `compatibility.yaml`；不要把当前待办的存在当成已完成验收。
