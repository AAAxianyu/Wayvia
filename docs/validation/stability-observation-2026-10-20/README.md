# 真实超时的 24 小时正常运行观察

日期：2026-10-20 America/Los_Angeles；事件统一记录 UTC。基线 `2b46212fdba2286718da84634acdea06112487e1`，工作分支 `codex/stability-observation-20261020`。用户要求继续真实超时/断连归因，先观察无需手机的长期稳定性，故障与重启测试继续暂缓。

**状态：采样已启动，24 小时观察未完成。历史真实 HTTP/DNS 超时的根因仍未确定。** 本次只增加独立诊断进程；没有更改生产探针期限、保护阈值、恢复逻辑或生产软件包，没有操作手机、注入故障或重启生产服务/主机。

## 最近只读复核

2026-10-21 05:15 UTC 复核（journal 截止 05:14:47）：两端诊断单元仍 active/running，各 1506 个样本，较上次新增 758/759 个；最大采样间隔约 5.004 秒，文件持续增长，未提前结束。入口完整 monitor 473 轮（471 ACTIVE / 已知 2 DEGRADED）、出口 474 轮全部 ACTIVE；无新生产失败或恢复事件，VPN/日本每个 HTTP/DNS 对照新增 126 次均成功。

新增一个宿主直连 DNS 对照事件：入口 05:05:19 向 Google DNS 的查询在单次 2 秒预算内未收到响应，dig 退出 9；30 秒后下次定时查询恢复为 NOERROR / 41 ms。同批宿主 CF DNS、VPN 两个 DNS 和两个 HTTPS、日本对应对照均成功，未触发保护。已保留 05:03:19–05:07:22 的[入口](control-timeout-0505-ali.jsonl)/[出口](control-timeout-0505-jp.jsonl)采样；这次仅宿主直连对照失败不能写作 Wayvia 新断连，原因未确定。没有需用户处理的新业务异常，按静默规则记录，不修改生产或采样配置。[最新比较](heartbeat-20261021T0515Z-review.json)关联本次两端摘要和 monitor；下一次优先与此 review 比较。

2026-10-21 04:11:35 UTC：两端采样单元仍为 active/running，分别累计 748/747 个样本，比上一份 Windows 诊断摘要各增加 133 个；最大采样间隔约 5.002 秒，没有损坏或未完成行，结束文件尚未生成。完整 monitor 日志入口 235 轮（233 ACTIVE / 2 DEGRADED）、出口 236 轮全部 ACTIVE。两次降级均为已向用户报告的 03:52、03:59 事件；自上次复核无新降级或受保护对照失败，新增每端点 22 次 VPN/日本 HTTP/DNS 对照全部成功。阿里云宿主两个 CF HTTPS 各新增 22 次原有基线超时，不计新事故。继续原定窗口，不重复通知、不重启采样。

[本次比较与采样覆盖](heartbeat-20261021T0411Z-review.json)关联[入口摘要](heartbeat-20261021T0411Z-ali.json)、[出口摘要](heartbeat-20261021T0411Z-jp.json)和完整[入口 monitor](heartbeat-20261021T0411Z-monitor-ali.json)/[出口 monitor](heartbeat-20261021T0411Z-monitor-jp.json)。已补存两个已知事件从首个失败前至少两分钟至恢复后至少两分钟的两端完整脱敏采样：[入口](incidents-0352-0359-ali.jsonl)、[出口](incidents-0352-0359-jp.jsonl)。归因仍以[Windows 诊断事件分析](../windows-campus-2026-10-20/README.md)为准；采样中的重复 DEGRADED 证据不算新增事件。此 review 保留为历史检查点。

## 窗口和当前证据

- 阿里云入口：2026-10-21 03:09:19 UTC 左右启动，2026-10-22 03:09:19 UTC 到期。
- 日本出口：2026-10-21 03:09:22 UTC 左右启动，2026-10-22 03:09:22 UTC 到期。
- 对应本机时区：10 月 20 日 20:09 至 10 月 21 日 20:09（America/Los_Angeles）；北京时间为 10 月 21 日 11:09 至 10 月 22 日 11:09。
- 两端独立 systemd 临时服务 `wayvia-observe-stability-20261020`，不启用开机启动、不自动重启。准确起止、摘要、路径和跟进 ID 见 [run.json](run.json)。

补丁部署后的启动前复核：入口 02:38:05–03:01:18 UTC 共 89 个 monitor 记录、出口 02:38:01–03:02:42 UTC 共 94 个记录，均 ACTIVE、没有失败层；入口 3 次完整响应后的连接关闭被正确保留为成功。见 [入口](postfix-initial-ali.json)、[出口](postfix-initial-jp.json)。这些是约 24 分钟的状态采样，不能代替一天观察或手机体验。

两端先各执行 75 秒正常采样，均因 `deadline` 正常退出（不是主动停止/重启测试）。各 15 个 5 秒样本：入口/出口分别记录 6/5 次 monitor，均 ACTIVE。跨境 ICMP 往返中位数分别 38.6/38.7 ms，全部 15 次收到响应。每个对照端点 3 次请求：VPN 内两个 HTTPS、两个 DNS，以及日本宿主同组对照全部成功。短时结果见 [入口](preflight-ali.json)、[出口](preflight-jp.json)。

阿里云宿主直接请求两个 Cloudflare HTTPS 地址均在 4 秒诊断上限超时；同一时段宿主 DNS、VPN 内 HTTP/DNS、日本 HTTP/DNS 成功。这是对照基线中的路径差异，**不能据此认定代理断连，也不足以归因运营商或目标服务**。后续汇总不把这组重复直连超时当成新故障。

正式采样初始结果见 [入口](initial-observation-ali.json)、[出口](initial-observation-jp.json)，两端服务正常运行。启动时的生产服务 PID/激活时间/状态和默认路由摘要均保持一致，见 [入口启动](stability-20261020-ali-launch.json)、[出口启动](stability-20261020-jp-launch.json)。采样服务初始内存峰值约 19/18 MB、累计 CPU 约 0.35/0.31 秒；这是启动短窗口资源读数，不是全天开销或主机负载归因。

正式启动后约 2 分钟复核：两端各 27 个采样，入口 9、出口 10 次 monitor 均 ACTIVE；每个 VPN/出口对照端点 5 次全部成功，跨境 RTT 中位数均 38.7 ms。入口/出口诊断进程累计 CPU 约 1.56/1.38 秒，峰值内存约 20/19 MB。该窗口主机平均 CPU 统计包含生产 monitor 等进程，不是诊断进程单独占用。见 [入口启动复核](startup-check-ali.json)、[出口启动复核](startup-check-jp.json)。两端运行脚本 SHA256 与仓库当前源码完全一致。

## 采样和边界

[采样器](../../../scripts/observe_stability.py)使用 Python 3.12 标准库，每 5 秒读取 CPU jiffies、内存/PSI、主机 TCP 累计计数、指定物理接口/tun0 计数、仅两端间 443 承载连接的 TCP RTT/重传指标、单次跨境 ICMP 和已存在的 monitor 状态。连接标识散列化，不保存地址四元组。主机 TCP/CPU 指标包含其他业务，必须结合承载连接、接口和时间线判断，不能单凭累计重传认定 Wayvia 故障。

每 30 秒发起低频、固定公共目标的对照请求：两端宿主，以及阿里云 `wayvia-egress` namespace。HTTPS 为 `1.1.1.1`/`1.0.0.1` 的 trace，DNS 向 `1.1.1.1`/`8.8.8.8` 查询 `example.com A`。HTTP 无环境代理、校验证书、检查唯一合法出口 IP，受保护路径与日本宿主还检查预期出口身份。两 HTTPS 端点仍属同一供应商；独立供应商 DNS 只是对照，不是新生产健康备用项。请求不含用户流量、账号或订阅凭据，响应体不写盘。

诊断 HTTPS 总上限 4 秒、DNS 单次 2 秒；这些比生产 12 秒期限短，因此单次诊断超时也不能直接当成生产故障。各请求记录开始/完成时间，最多 4 个并发；批次未完成不重叠启动下一批。ICMP 无响应也不能单独证明业务丢包。

只写 `/var/lib/wayvia/observations/` 自有路径，目录 root 0700、文件 0600，字段白名单和固定错误码白名单排除计划、密钥、私密 URL、响应体及原始日志。单机 JSONL 上限 96 MiB；到期、代次变化、采样异常或容量上限均退出并写结束原因，异常退出非零。systemd 额外限制内存 128 MiB、任务数 32、nice 10 和运行上限 24 小时 + 60 秒。没有改生产 units、路由、全局防火墙或 VPN。安装器/生产包没有变更。

预运行完成后仅收紧未知错误码的脱敏策略；正式运行脚本的确切摘要记录于 run.json 和 launch 证据，预运行摘要不同已保留。收紧后的脱敏专项重新通过。采样器不会读设备数据，也不会调用 production monitor/recover/apply。

## 每小时接续流程

当前聊天 heartbeat `wayvia-24` 每小时只读复核，仅在新异常、需要处理或观察完成时汇报。服务器采样独立于本机运行；本机/应用离线期间跟进可能延后，恢复后补收服务器记录。到期完成总结后暂停此 heartbeat，不延长采样。

1. 读取 run.json，核对远端诊断单元 `ActiveState`、`SubState`、`Result`、`ExecMainStatus` 与日志是否持续增长。远端目录 `samples.jsonl` 保存完整脱敏序列，`result.json` 在采样结束时生成；若提前失败，记录原因和覆盖缺口，不自行重启采样。
2. 使用既有可信 SSH 通道，在服务器执行 `python3 <run.json 中的 remote_script_directory>/summarize_observation.py stability-20261020`。也可将仓库同一脚本经 SSH 标准输入执行。它只读取该观察目录。认证材料只从本聊天已授权渠道取得，不写进自动化提示词/仓库，不盲目重放旧部署脚本。SSH 管理连接失败与业务失败分开记录。
3. 保存带采集 UTC 时间的两端摘要。现有 [collect-stability.py](../server-followup-2026-10-20/collect-stability.py) 可从 2026-10-21T03:09:19+00:00 起补齐生产 monitor 日志与服务事件；5 秒当前状态快照可能漏掉短暂状态，不能代替日志完整计数。不要输出原始 journal、plan 或 credentials。
4. 若出现新 DEGRADED 或受保护路径超时，按 `started_at`/`observed_at` 对齐两端事发前后至少 120 秒的脱敏 JSONL，补齐服务/内核事件分类。判断最早请求失败、随后保护动作和恢复的先后，比较 host/vpn、两个 DNS、两条同供应商 HTTP、跨境 RTT/承载重传、接口和 CPU/PSI。只写证据支持的结论，暂不修改健康规则。
5. [汇总器](../../../scripts/summarize_observation.py)只保留前 100 个失败示例，计数完整；全量序列仍在服务器。若出现新事件，按时间段读取原始脱敏序列，不仅依赖摘要前 100 个示例。日志尾部未完成的一行可暂跳过；其他 JSON 损坏显式失败。资源指标单位见字段名，HTTP 分阶段耗时在汇总中转换为毫秒。
6. 到期确认两端 `reason=deadline`、采样起止/间隔/缺口、失败率与状态轮数，关联完整 monitor 日志，更新本报告、compatibility.yaml、implementation-status 和 01 票。没有复现只写“该窗口未复现”，真实超时根因未确定则保持未解决。暂停 `wayvia-24`，并保留有限的远端证据供后续复查。

不会运行手机验收、故障/重启测试或持续测速压测；这些不在本窗口中。

## 软件和运行验证

- `make check`：退出 0，369 项通过、3 项既有警告，格式/lint/build/CLI 通过；新增 12 项保护脱敏、TLS/身份/期限失败结果、文件权限/排他写入/容量和摘要完整性。最后错误码白名单收紧后专项 12 项及格式/lint 再次通过。
- `make demo`：退出 0，simulated。
- `make test-configs`：退出 0，10 项锁定组件真实配置检查，见 [结果](config-result.json)。
- `make test-lab`：用户明确暂缓故障与重启，本轮不执行、不计通过；不是环境缺失被忽略。
- 两台真实主机仅新增正常诊断采样，75 秒自动退出已确认；24 小时实网稳定性仍进行中，不能写成通过。

[门禁](gates.json)及相邻日志保留完整结果。运行时 Python 3.12.3，生产 Wayvia 0.1.0 wheel SHA256 仍为 `6289bb94661a4e008eac3f85488bce71e0d7e95811918ac3f9e83142f4f09b31`；软件版本及组件版本继承[上一阶段记录](../server-followup-2026-10-20/source-and-versions.json)，生产包未重新部署。
