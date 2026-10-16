# QOKIK 合入 main 与剩余问题验收

日期：2026-10-15，America/Los_Angeles。集成基线为 QOKIK `cb2676bba916c734fa5765cd8a03b2b3b20f946c`；main 合并前为 `a23f593dff32b79601c96b7f10dfe839867dd298`。开发在 `codex/qokik-main-integration-20261015` 独立工作树完成，保留原开发工作区及旧验收工作区。

## 问题、原因和修复

1. **nftables 计数器与未知对象**：QOKIK 已包含结构化基线及未知顶层对象拒绝修复，本次保留并复验。18 项软件回归通过；真实 nft 执行器也确认计数增加不造成误报，而规则变化仍返回 `BASELINE_CHANGED`，完整快照仍能回退。
2. **v2rayA 启动与回退竞态**：systemd 的 simple 服务进入 active 时，HTTP API 可能尚未监听。旧实现只尝试 10 次、间隔 0.2 秒；快速拒绝连接时约 1.8 秒就耗尽，正常但稍慢的重启也会失败。本次用 30 秒单调时钟截止时间等待兼容的只读 `/api/version`；每次 HTTP 各阶段超时不超过 2 秒且按剩余时间收缩。连接／传输不可用可重试，非法响应、API 拒绝、版本／核心不匹配立即失败，截止后到达的成功响应不接受。就绪以后才执行账户及配置操作，配置写入不重试。实际超时仍进入原有错误处理和 fail-closed 恢复路径。
3. **systemd 包依赖与 arm64 冷索引**：QOKIK 已用固定 Ubuntu 快照解决 `.17` systemd 配上实时源 `.18` 依赖的问题，但原修复在 arm64 空索引派生构建时仍失败。锁定基础镜像的 APT 2.8.3 有 archive/security 主机映射，却没有 `ports.ubuntu.com` 映射；基础构建的旧 Release 索引曾掩盖问题，清空后首次更新只拿实时索引，安装却找不到目标快照的 systemd 包。现显式映射 ports 到同一官方快照服务，并在 CA 引导后清空索引再更新。快照仍为 `20261014T064100Z`，关键包版本保持原约束。实际重建确认 systemd、systemd-sysv、systemd-dev、libsystemd-shared、libsystemd0、libudev1 均为 `255.4-1ubuntu8.17`。

官方机制依据：[Ubuntu Snapshot Service](https://snapshot.ubuntu.com/)。配置和维护策略见 [ADR 005](../../adr/005-ubuntu-apt-snapshot-for-lab-images.md)。CA 引导包仍来自实时官方签名源，其版本单独记录，不声称所有基础包均由快照重建。

## 回归证据

- [readiness-before.log](readiness-before.log)：在 QOKIK 原生产代码上，apply 和 rollback 两个 3 秒启动延迟用例均失败；之后才应用生产修复。
- [focused-after.log](focused-after.log)：修复后 9 项启动等待测试和 18 项 nft 基线测试全部通过。覆盖永久不可用、慢读取、截止后的响应、终止错误和错误内容不泄露。
- [first-full-gate-failure/make-test-lab.log](first-full-gate-failure/make-test-lab.log)：本轮第一次完整实验在 arm64 派生 systemd 镜像失败，保留 `Unable to locate package systemd` 原始证据，退出 2。此前核心路径通过不算完整验收通过。
- [apt-cold-mapping-probe.log](apt-cold-mapping-probe.log)：在空索引镜像中只补 ports 映射，APT 成功从固定快照解析 systemd 及匹配依赖，退出 0。
- [make-test-lab.log](make-test-lab.log)：修改后的基础镜像与 systemd 派生镜像重新构建，完整实验退出 0；未使用旧 `.18` 镜像，也未用 `--no-build` 跳过构建。
- [final-latest-lab/transaction-result.json](final-latest-lab/transaction-result.json)：systemd ExecStart 包装器延迟真正的 v2rayA 进程 4 秒。不是用 ExecStartPre 推迟 systemctl 返回，也不是模拟 HTTP 返回。日志单调时间差依次为 `4.009317`、`4.007561`、`4.001168`、`4.002643` 秒。入口依次经过 `443→8443→9443→8443→443`，两次更新与两次回退的认证访问通过，被替换监听端口关闭。
- [installed-source.json](installed-source.json) 与 [source-under-test.json](source-under-test.json)：正式安装容器内的 Linux/v2rayA 适配器摘要与被测源码一致。

## 完整门禁结果

| 命令 | 结果 | 证据 |
| --- | --- | --- |
| `make check` | 退出 0；325 项测试、格式、lint、sdist/wheel、CLI help 通过；3 项已有弃用警告 | [最终日志](make-check-final.log) |
| `make demo` | 退出 0；明确的 simulated 控制面演示 | [日志](make-demo.log) |
| `make test-configs` | 退出 0；新快照镜像上 10 项真实组件配置校验 | [最终日志](make-test-configs-final.log)、[结果](final-latest-configs/config-result.json) |
| `make test-lab` | 退出 0；23 核心、23 正式事务、7 协议、6 DNS、12 冲突、2 独立恢复，另有 TUN 与 executor 回归 | [日志](make-test-lab.log)、[完整结果](final-latest-lab/) |
| `make clean-lab` | 退出 0；本次容器／网络移除；原有三个实验容器 ID、启动时间与运行状态不变 | [清理验证](cleanup-verification.json) |

[summary.json](summary.json) 提供机器可读门禁摘要。[snapshot-runtime-versions.json](snapshot-runtime-versions.json) 记录 Docker 29.5.2、arm64、Linux 6.8.0-117、Python 3.12.3、APT 2.8.3、快照配置、镜像 ID 和实际包版本。开发机软件测试使用 Python 3.12.12。

为避免触碰已有实验，执行时仅临时替换了 Docker 容器、网络和镜像名称；[名称映射](lab-namespace.json) 与 [替换补丁](lab-name-isolation.patch) 可复核。最终提交已恢复标准名称，生产代码、测试断言和版本锁在验收与提交之间不变。以前的 `latest-*` 验证文件也已恢复，本轮结果独立归档。

## 持续验收与边界

隔离 Linux CI 现在同时使用 `ubuntu-24.04` 和 `ubuntu-24.04-arm`，两者均从干净 runner 构建实验镜像并运行完整 `make test-lab`。CA 引导后的空索引构建和真实 4 秒启动延迟场景成为每次 CI 的回归门禁。两种 runner 的官方支持见 [GitHub runner 文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)。

本报告记录提交前本地证据；推送后远端验证须对应同一提交的 [GitHub Actions](https://github.com/AAAxianyu/Wayvia/actions/workflows/check.yml)。main 与 QOKIK 的分支对齐通过 fast-forward 完成，不重写已有提交历史。

这些证据确认软件与一次性 Linux 容器中的网络路径和事务行为。没有操作开发宿主的路由／防火墙／已有 VPN，也没有访问生产服务器。M6 真实服务器、手机切网及重启验收尚未执行，M7 OpenWrt 未实现／未验收。
