# 04 更新与选择保留

Status: done
Scope: software_only

完成条件见 [spec](../spec.md) 第 4 项。

## Comments

2026-10-25：按用户授权推进；不执行生产、实网采样和故障测试。

## Software result

选择持久化、标准更新周期、非 2xx 下载/撤销边界与原子私有 staging 已实现；原生正常 reload/重开缓存及无效 YAML 保留已验证。

见 [使用说明](../../../docs/clash-verge.md)、[验证](../../../docs/validation/clash-verge-2026-10-25/README.md)与 [ADR 011](../../../docs/adr/011-unified-clash-verge-client.md)。本轮未部署、未接管桌面、未实网测速或注入故障。
