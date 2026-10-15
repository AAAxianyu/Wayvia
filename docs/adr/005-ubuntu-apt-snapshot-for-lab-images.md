# ADR 005：实验镜像使用固定 Ubuntu APT 快照

状态：采用。日期：2026-10-15。

## 背景

`deploy/lab/Dockerfile` 固定 OpenVPN 版本，`deploy/lab/Dockerfile.systemd` 固定 systemd 版本，但两者从可变的 Ubuntu 在线软件源解析依赖。2026-10-15 的干净 CI 构建中，systemd 固定为 `255.4-1ubuntu8.17`，APT 却选择 `libsystemd-shared 255.4-1ubuntu8.18`，导致 systemd 镜像无法构建。复用本地已有镜像通过实验不能覆盖这个干净构建失败。

## 决策

- Ubuntu 24.04 实验镜像固定使用 Ubuntu 官方 APT Snapshot Service 的快照 `20261014T064100Z`。
- 先从 Ubuntu 官方签名的普通 HTTP 仓库安装 `ca-certificates`，再由 `deploy/lab/Dockerfile` 写入 `/etc/apt/apt.conf.d/50snapshot`。快照服务使用 HTTPS；CA 证书先行安装，使 APT 能验证快照服务的 TLS 证书。
- 固定 APT 快照配置随基础镜像传递，让 systemd 派生镜像和事务派生镜像也从同一快照解析包。
- 继续保留关键包的精确版本约束。快照固定依赖集合，包版本约束表达实验要求；两者共同避免“固定顶层包但依赖来自另一时点”的不一致。
- 更新快照时，先确认锁定包在新快照中可用，再在干净 Linux 环境完整运行 `make test-lab`。安全更新通过有审查的快照更新进入构建，而不是隐式跟随实时仓库。

## 后果

除用于引导 TLS 验证的 `ca-certificates` 外，实验镜像包解析使用同一固定快照；该 CA 引导包仍从实时 Ubuntu 仓库安装。快照不会自动吸收后续安全更新；维护者需要定期更新快照并执行完整门禁。Dockerfile 默认值基于 2026-10-14 最近一次成功的 CI 实验构建时间选择。2026-10-15 的干净基础镜像和派生 systemd 镜像已成功构建；完整实验从 `/tmp` 启动时退出码为 0，证据见[验证摘要](../validation/apt-snapshot-lock-2026-10-15/summary.md)。WSL 进程以 `/mnt/d` 为当前目录时，实验容器写入 D 盘 bind mount 会使其 `getcwd()` 失效；从 Linux 文件系统目录（如 `/tmp`）启动完整脚本可通过实验。

官方依据：[Ubuntu Snapshot Service](https://snapshot.ubuntu.com/) 和 [Ubuntu Server 快照服务使用文档](https://ubuntu.com/server/docs/how-to/software/snapshot-service/)。Ubuntu 24.04 的 APT 支持 `APT::Snapshot` 配置以固定支持快照的仓库时间点。
