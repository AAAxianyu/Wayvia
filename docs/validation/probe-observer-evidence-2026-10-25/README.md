# 证据字段、请求阶段与观察器生命周期的软件验证

日期：2026-10-25 America/Los_Angeles（执行日志为 2026-10-26 UTC）。分支：`codex/probe-observer-evidence`，基线：`2926b34bcdd3e84e26147aaa5c67d3a5f555bd4f`。

用户授权本轮统一证据字段、实现待办 15/13、整理待办 12 恢复决策表，限仓库开发与软件验证。主工作区既有未提交改动保持原状；本轮在独立 worktree 开发。没有生产部署、实网采样、联系供应商、恢复 heartbeat 或注入真实服务故障。

## 交付与证据

- [ADR 008](../../adr/008-observation-evidence-and-lifecycle.md)：证据 v1、字段含义、curl/中继阶段、超时与续接的限制和运行方式。
- [恢复决策表](../../recovery-decision-table.md)：保护优先级、最小候选动作及验收门槛，仅设计，未修改保护或整链恢复行为。
- `tests/contract/test_probe_phase_diagnostics.py`：离线命令夹具验证连接/TLS/首字节/响应阶段、已取得的超时输出、证书/身份区分、预算收缩、helper 期限和完整脱敏链路。
- `tests/unit/test_observation_lifecycle.py`：临时目录、受控时钟和真实文件锁验证重复进程、显式续接、用户停止、到期、时钟倒退、半行、权限/链接、容量与终态检查点。这里的中断场景是软件状态模拟，不是生产或隔离网络故障注入。
- `tests/unit/test_stability_observation.py`：多段摘要、缺测、到期不启动新命令、外部命令预算收缩；保留既有身份/脱敏回归。

## 验证命令

归档日志仅规范化行尾空白和末尾空行；内容与退出状态保留，校验摘要针对归档文件。

本机 `uv` 不在 PATH，使用已有工具 `/Users/dev2/Documents/project/Wayvia/.cache/progress-check-tools/bin/uv`（0.9.7），各 make 命令以 `UV=该路径` 指定。项目按锁文件建立独立 `.venv`，Python 3.12.12、Ruff 0.14.2、pytest 8.4.2。

| 命令/检查 | 结果 | 范围 |
| --- | --- | --- |
| `make check` | 退出 0，421 项通过、3 项既有警告；[日志](check.log)、[汇总](summary.json) | 格式、静态检查、全部非特权自动测试、包构建与 CLI |
| `make demo` | 退出 0，[demo.log](demo.log) | simulated SQLite/设备/订阅闭环，不代表数据链路 |
| `make test-configs` | 退出 0，10 项真实组件配置检查通过 | [配置结果](config-result.json)、[完整日志](test-configs.log) |
| curl write-out 离线冒烟 | 通过，[curl-format-smoke.json](curl-format-smoke.json) | 在无网络容器中请求被协议白名单禁止的 file URL；验证真实 curl 输出格式和失败投影，无网络请求 |
| `make test-lab` | **未执行：用户继续暂缓故障/恢复实验** | 非通过、非环境缺失；退出码为 null，不伪造成功或失败执行 |

配置检查由项目原命令使用标记为 disposable 的一次性 Linux 容器、`--network none` 执行；其前置拓扑预检创建容器内 namespace 并验证 guard 阻断物理 fallback。没有执行完整双层故障/恢复实验。OpenVPN 配置检查仅证明真实配置加载和受控进程启动，配置结果中的 `tunnel_established=false` 不得改写为隧道验收通过。

实际配置环境：Ubuntu 24.04.5 LTS / aarch64、内核 6.8.0-117-generic、Python 3.12.3；sing-box 1.14.3、mihomo 1.19.32、v2rayA 与匹配 core 2.5.10、OpenVPN 2.6.19。完整版本和配置警告见 config-result.json；生产版本未读取或改变。

## 验收边界

软件证据不能证明维护自动重启、真实进程硬终止、时钟校准或双端请求里程碑已在生产接受。硬终止前尚未取得的 curl write-out 保持 unknown。新观察任务没有启动，旧任务不会迁移或重新打开。

公共证据字段不等于 16 的完整时间线/导出；分段续接不等于 14 的常驻滚动缓冲；12 的决策表不等于分层恢复已实现。后续仍需分别记录隔离链路与实网验收，不能宣布 JXY 最初超时根因或断连问题已解决。
