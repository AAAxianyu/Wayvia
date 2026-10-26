# 保留原数据的滚动升级

Date: 2026-10-26 America/Los_Angeles
Branch: codex/probe-observer-evidence
Runtime code commit: 1fea59a7978ac269abff2a3177432a8ee4a5cf2a
Status: pushed, pulled on both hosts, deployed and verified

用户要求把本地修改推送远程，再保留原有数据部署最新版本。已推送当前开发分支，日本出口与阿里云入口各自从 GitHub pull --ff-only，按 exit → ingress 完成升级；两端当前角色均为 ACTIVE，相关服务 active。没有合并或覆盖 main，也未改动用户主工作区的其他未提交文件。

## 原数据与恢复边界

升级前核对精确 Git commit、安装清单所有文件、锁定组件 archive SHA256、wheel 全部包文件与 source 的一致性，以及官方 uv 0.9.7 wheel/native 摘要。缺少 uv 的主机仅补充专用工具路径，不改系统 Python。日本缺少 ensurepip 时从已验证官方 wheel 提取 uv 原生程序，不进行全机依赖修复。

停止既有 Wayvia 单元后保存一致状态：数据库、配置、控制面/服务器密钥、agent journal、既有观测、原 venv/软件和 systemd/API 配置。备份保留数字 UID/GID、权限与 ACL/xattr，不采用旧凭据撤销前的历史快照。备份位于各自主机 /var/backups/wayvia/upgrade-1fea59a7978a-exit 或 upgrade-1fea59a7978a-ingress，root-only，另存 archive SHA256 与前后身份快照。

数据库迁移日本 2 → 4、入口 3 → 4，均通过 quick_check。入口仍为 3 个设备记录、5 个订阅记录，包含既有撤销记录；两份有效手机/JXY URL 和密码、期限、LTY 名称、发布快照、配置、静态 TLS 私钥与累计计数通过保留校验。日本原无终端账号，计数仍为 0。旧统计不被伪造成历史时间桶，新历史从新版采集开始；累计值允许正常增长，禁止降低或丢失。

升级器受 MemoryHigh 640 MiB / MemoryMax 768 MiB、单 CPU 和 600 秒期限保护。出口约 1 分钟、内存峰值 578.2 MiB；入口约 3 分 28 秒、峰值 640.5 MiB，均退出 0。维护时段分别为 10:30:09–10:31:04 UTC 和 10:31:43–10:35:09 UTC，应从自然故障/性能归因中排除。启动时复用既有恢复/监控与原计划，没有新建网络计划或重新签发终端凭据。

## 当前运行与订阅

安装的全部 wheel 文件逐项核对一致。中间 schema 3 API overlay 已退出使用，API 改为完整新版 CLI，保留 384/512 MiB 内存限制；线上今后使用新 CLI 和 schema 4。入口 Nginx 的 Wayvia 配置仅将 Host 透传改为 $http_host，保留 HTTPS 端口；管理路由仍不公开。

手机 LTY 和电脑 Wayvia · JXY 的原 HTTPS URL 均为 200，TLS 验证开启，信息页 200、24 小时更新，公开管理路由 404。实际下载正文与此前已通过 Mihomo 1.19.32 禁外网 parser 的两份文件逐字节相同；分流、DNS、TUN、IPv6、入口与凭据保持原配置。不是物理手机 GUI 或权限/接管验收。

make check 537 项通过（3 项既有警告），make demo 通过，make test-configs 11 项真实组件/接口检查通过。本地完整 make test-lab 仍按用户要求未手动执行，退出码 null。既有 GitHub push 工作流自动执行了 amd64/arm64 的隔离 Linux make test-lab，均成功，见[CI 运行](https://github.com/AAAxianyu/Wayvia/actions/runs/40307314654)。生产故障/切网/服务器重启实验和新实网测速/采样仍暂缓；没有以本次正常升级启动就绪代替完整 M6 或长期稳定性验收。旧观察/heartbeat、独立持续诊断、告警投递均未自动启用。国内直连、VPN-over-proxy、服务器秘密和 fail-closed 约束保留。

结构化证据见 [results.json](results.json)，部署工具为 [deploy/upgrade.py](../../../deploy/upgrade.py)。失败路径会保留新状态并恢复本次维护前的快照；本次两端均未触发回退，不将未运行的回退/故障实验记为通过。此前 API 限范围发布和 OOM 事故记录见 [LTY 记录](../subscription-lty-2026-10-26/README.md)，本次全量升级在用户进一步授权后执行，两个阶段分开记录。
