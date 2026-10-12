# Wayvia 实现与验收状态

更新：2026-10-12（America/Los_Angeles）。开发分支 `codex/m0-m5-delivery`。

## 当前结论

**软件交付：M0–M3、M5 已实现并验证。受控双层链路：M4 已验证（Ubuntu24.04 arm64一次性环境）。真实环境验收：M6 未执行。** 未连接真实阿里云/日本主机，未取得手机导入、切网、目标主机整机重启或实网性能/费用证据。三个结论独立维护。

| 阶段 | 状态 | 实现与证据 | 下一步 |
| --- | --- | --- | --- |
| M0 | 已实现、已验证 | Python3.12.12、uv0.9.7、uv.lock、Typer CLI、错误码、CI、wheel/sdist | `uv sync --locked`、`make check`；`validation/software/` |
| M1 | 已实现、已验证 | 严格Schema、规则解释、DNS/IPv6、完整sing-box/mihomo配置；7511 APNIC CN CIDR和110185固定国内域名后缀 | `make test-configs`10项；`latest-configs/config-result.json`、ADR001 |
| M2 | 已实现、已验证 | SQLite显式迁移、独立secret_ref、设备轮换/撤销、API认证/幂等/no-store、私人订阅 | `tests/integration/`、`make demo`；M4真实入口撤销后新连接拒绝 |
| M3 | 已实现、已验证 | 固定拓扑动作、SO_PEERCRED、真实组件适配、指定启动DNS、证书/资源预检、持久事务、独立回退与恢复 | 故障契约测试、`executor-result.json`、`recovery-result.json`、ADR002 |
| M4 | 已实现、已验证（arm64隔离） | 正式渲染TUN、两角色、VPN over SOCKS/Trojan、DNS、IPv6、UDP、NAT回程、两层断开与重连 | `make test-lab`22项+4个kernel动作回退+2个真实systemd恢复case；`validation/m4-controlled-lab.md` |
| M5 | 已实现、已验证 | 安装stage/system、升级备份、有限卸载、分层doctor、兼容版本与运维资料 | 干净venv安装最终wheel、真实archive安装stage、安装器10项测试；`operations.md` |

最终软件测试计数及命令结果见 `validation/software/software-result.json` 和 `validation/software/make-check.txt`；实验约定命令退出码和用时见 `validation/commands.json`。所有生成配置由锁定真实核心校验。fake只用于明确demo/单元测试，生产拒绝fake；设备配置应用失败保留pending/degraded并请求阻断，unknown/unsupported/未确认探针不能归为healthy。

## 修复与验证边界

已修复v2rayA配套core/geodata、UDP跨namespace回包、IPv6启动竞争、旧凭据复活、旧ACTIVE计划重放、恢复单元排序死锁、损坏凭据保护、越权TUN/脚本/文件配置、证书失效/公钥不匹配、端口和网段冲突、doctor误报健康。受控实验安装保护前实际物理绕行可通并看到阿里云测试出口；正式guard后同请求失败。断VPN或代理时受保护SOCKS/TUN失败，国内及管理继续工作。

systemd证据验证控制进程退出后的独立timer，以及新恢复进程读取持久journal；完整目标云主机重启仍属M6。生产入口重启会关闭全部旧会话，轮换/撤销会短暂影响同入口其它连接。订阅单独撤销不改变已导入密码权限。最新设备revision覆盖恢复/回滚中的旧users，避免已撤销密码复活。

amd64资产已锁定但数据链路未测；代理IPv6与UDP底层VPN未支持，能力不支持时明确拒绝。当前业务TCP/UDP经OpenVPN TCP over SOCKS5 over Trojan TLS。手机核心格式解析通过不表示任意App导入通过。Ubuntu辅助安全库的已测版本已记录，不宣称整套系统字节级可重复。

本轮只操作仓库、本地普通服务和明确的一次性容器。开发机默认路由、全局防火墙、现有VPN与未指定真实服务器没有被更改。临时CA/密码随容器和临时产物清理，验证资料没有私钥或可用订阅密码。

## 剩余实网输入

下一项外部任务是M6，不存在阻塞本次软件交付的必需输入。实网需要阿里云/日本主机明确授权及系统、地域、CPU/内存、接口和带宽；域名与TLS/VPN证书条件；Android/iPhone客户端及版本；校园域名/网段/DNS、UDP应用、Wi-Fi/蜂窝切换、主机重启测试和性能费用目标。OpenWrt型号/固件属于M7。
