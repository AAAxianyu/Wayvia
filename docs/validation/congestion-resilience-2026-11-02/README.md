# 拥塞感知健康判定与出口整形：仓库实现验证 2026-11-02

范围：按[方案](../../../.scratch/congestion-resilience/spec.md)与 [ADR 014](../../adr/014-congestion-aware-health-and-exit-shaping.md) 在 `codex/congestion-resilience`（基于 `0873d61`）实现的软件部分。只运行本地非特权门禁；没有隔离网络实验、没有连接或改动生产服务器。问题范围的生产只读证据见 [congestion-scope-2026-11-02](../congestion-scope-2026-11-02/README.md)。

环境：Darwin arm64，Python 3.12.12，uv 0.9.7（仓库 `.tools`），SQLite 3.50.4；锁定组件 sing-box 1.14.3、OpenVPN 2.6.19（由 `make test-configs` 在容器中使用）。

## 改动

| 项目 | 内容 | 默认 |
| --- | --- | --- |
| 失败分类 | 传输/期限类为软失败，身份/证书/会话/guard/服务/无错误码为立即保护；VPN 身份探针新增 `VPN_IDENTITY_TIMEOUT`/`REJECTED`/`FAILED` | 生效 |
| 承载进展 | `LinuxExecutor.carrier_counters()` 用 `ss -tin` 读取承载套接字 `bytes_received`/`bytes_acked`；域名 carrier 不猜测 | 生效 |
| 连续确认 | 无进展/未知 3 轮、仍前进 20 轮才保护；期间证据记失败，发布仍被阻止 | 生效 |
| 重建退避 | 1 小时窗口内 0/30/60/120/240/300 秒，`recovery_deferred` 事件，状态 `rebuild_after` | 生效 |
| 日志降噪 | monitor 健康快速轮紧凑输出；`health_healthy` 5 分钟心跳；新事件 `health_congested`（触发诊断事件包） | 生效 |
| 诊断写入 | `DiagnosticStore.append_many`：一轮一个事务、一次清理与 pin，检查点同事务提交；collector 每轮 2 个写事务 | 生效 |
| 日志库清理 | 每批最多 1000 行，显式清理循环分批提交 | 生效 |
| 出口整形 | 固定 `shaping` 事务动作：HTB 总速率 + 承载保证类 + `fq_codel` 叶子，`tcp_notsent_lowat`；可回退、拒绝外来根队列 | 关闭 |
| 承载缓冲 | OpenVPN `tcp-queue-limit`、`sndbuf`/`rcvbuf`，白名单仅单整数 | 关闭 |
| 固定代理域名 | `policy.proxy_domains` 在国内规则前走代理与代理 DNS，sing-box 与 mihomo 均输出 | 空 |
| journald | `deploy/journald/wayvia-limits.conf`，主机级、手动安装 | 不安装 |

未设置的新配置字段不进入 revision；未设置整形的旧计划摘要不变（新增 `shaping` 字段为空时不参与摘要）。出口计划总会包含 `shaping` 动作（为空即确保无 Wayvia 整形），因此出口新计划的动作列表变化。

## 门禁

| 命令 | 结果 |
| --- | --- |
| `make check` | 退出 0；ruff 格式与检查通过；625 项通过（基线 593 + 新增 32），3 项原有警告；sdist/wheel 构建与 `wayvia --help` 正常 |
| `make demo` | 退出 0，模拟模式 |
| `make test-configs` | 退出 0，11 项真实组件配置校验通过 |
| `make test-lab` | 未运行，退出码 null：含故障实验，沿用用户此前"故障/重启测试暂缓"的约束 |

新增测试：`tests/unit/test_congestion_monitor.py`（分类、进展、确认轮数、退避、心跳、紧凑输出、`ss` 解析 15 项）、`tests/unit/test_exit_shaping.py`（配置、渲染、计划、topology、tc/sysctl 命令、回退与外来队列 12 项）、固定代理域名 3 项、批量写入与分批清理各 1 项。`test_legacy_sealed_plan_digest_survives_optional_fields` 改为同时去掉新的可选 `shaping` 字段，以模拟真实旧计划。

## 边界

- 软件门禁不能证明整形在 2 Mbps 供应商限速下有效，也不能证明宽限期的 fail-closed 安全；两者都需要一次性 Linux 实验（ADR 014 的验收门槛）。
- tun 公平队列、OpenVPN 探针日志降噪、日志库 WAL 未实现，原因见方案"实现状态"。
- 未部署、未提交。生产启用需用户另行授权：判定与退避随软件升级生效；整形需先完成实验。
