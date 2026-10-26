# 14/16 持续双端诊断与故障证据软件验证

日期：2026-10-25 America/Los_Angeles（执行日志 2026-10-26 UTC）。分支 `codex/probe-observer-evidence`，基线 `a27fba6`。

本轮用户授权仓库开发和软件验证。实现滚动被动采集、双端事件窗口、时间线查询及脱敏导出，沿用既有证据字段和 ObservationRun 生命周期。生产部署、实网采样、完整隔离故障与重启测试继续暂缓，未恢复旧观察或 heartbeat。

## 软件结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `make check` | 退出 0；456 项测试通过、3 项既有弃用警告，格式/静态检查/构建/CLI 通过 | [check.log](check.log) |
| `make demo` | 退出 0，simulated 控制面闭环 | [demo.log](demo.log) |
| `make test-configs` | 退出 0，10 项真实组件配置检查通过 | [config-result.json](config-result.json)、[日志](test-configs.log) |
| 离线双端例子 | 退出 0；两个临时私有 SQLite、合成故障/恢复、滚动轮转后双端窗口仍完整，ZIP 校验通过 | [结果](example-private/result.json)、[合成证据包](example-private/synthetic-evidence.zip)、[日志](offline-demo.log) |
| `make test-lab` | **未执行：用户继续暂缓故障/恢复测试**，退出码 null | 不记为通过或环境缺失 |

归档日志仅规范化行尾空白及末尾空行，退出状态与内容保留。

版本与源文件摘要见 [summary.json](summary.json)。本机 Python 3.12.12、uv 0.9.7、Ruff 0.14.2、pytest 8.4.2。uv 使用已有 `/Users/dev2/Documents/project/Wayvia/.cache/progress-check-tools/bin/uv`，make 命令通过 `UV=` 指定。配置环境是明确 disposable、无外部网络的 Linux 容器，真实核心版本保持锁定。

配置命令附带原有容器 namespace/guard 前置预检；未运行完整链路故障/恢复实验。OpenVPN 配置结果 `tunnel_established=false` 不改写为隧道验收通过。生产实际版本未读取或修改。

## 有意义的回归

- `tests/unit/test_telemetry.py` 使用合成记录验证仅入口失败时仍固定两端前后窗口、延迟请求起点前移、恢复后窗延长回传、重复/过期/冲突触发、单事件容量、滚动清理与数据库页数上限；达到长窗口上限拆事件，旧证据不缩短。
- 真实 SQLite mode=ro 查询、导出前后源文件摘要一致；篡改 ZIP、非法路径、权限/链接、缺对端、空洞及未完成后窗分别返回明确结果。字段白名单覆盖坏容器类型、秘密正文/地址/计划夹具和未授权终端事件。
- 有界命令读取使用本地 Python 输出夹具测试容量/期限，不发网络请求。TCP/接口解析与版本读取采用固定命令输出，验证队列、重传、窗口、计数器重置、正确 peer 端口、安装版本范围及原文脱敏。
- `tests/integration/test_diagnostic_evidence.py` 覆盖 CLI 查询/导出/校验、筛选边界、显式终端授权、只读既有设备/订阅审计及控制事实输出失败隔离。非法/过深的私有输入返回固定错误，避免携带原文的异常回溯。原有事务、凭据、保护和发布回归纳入全量检查。

## 离线例子与范围

可用 `uv run --locked python scripts/demo_diagnostics.py --output NEW_PRIVATE_DIRECTORY` 重现。它完全不调用 sampler、journalctl、SSH、主动探针或服务控制；输出标为 simulated。示例指标不是实测开销或线路性能，clock_alignment 保持 unknown。仓库样例是公开合成数据；Git 不保存运行时 0600/0700 权限，重新生成命令会创建相应私有目录/文件。

公共字段与实现取舍见 [ADR 009](../../adr/009-rolling-diagnostics-and-incident-export.md)，命令见[操作说明](../../diagnostic-evidence.md)。导出校验和不是远端签名认证；实际组件版本字段区分已安装二进制与未知的运行进程版本。

软件通过、配置/隔离路径通过、实网接受度是三个独立结论。本次没有证明常态资源开销、真实维护恢复、双端时钟精度、管理文件通道的实际可靠性或 JXY 最初超时根因。新能力没有启用；后续生产输入/部署与自然事件验收仍需另行安排。
