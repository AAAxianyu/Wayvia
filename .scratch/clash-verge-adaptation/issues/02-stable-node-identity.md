# 02 稳定节点与策略组

Status: done
Scope: software_only

完成条件见 [spec](../spec.md) 第 2 项。

## Comments

2026-10-25：按用户授权推进；不执行生产、实网采样和故障测试。

## Software result

生产只有固定 Wayvia · 主线路 节点、Wayvia 组和 REJECT 空组回退；轮换/更新名称稳定，旧名离线迁移和真实核心选择缓存已验证。

见 [使用说明](../../../docs/clash-verge.md)、[验证](../../../docs/validation/clash-verge-2026-10-25/README.md)与 [ADR 011](../../../docs/adr/011-unified-clash-verge-client.md)。本轮未部署、未接管桌面、未实网测速或注入故障。
