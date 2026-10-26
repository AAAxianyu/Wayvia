# 03 手动延迟与有界吞吐工具

Status: done
Scope: software_only

完成条件见 [spec](../spec.md) 第 3 项。

## Comments

2026-10-25：按用户授权推进；不执行生产、实网采样和故障测试。

## Software result

手动原生节点延迟配置、显式 loopback 代理测速工具、10 秒子进程期限/8 MiB payload 上限、单位与未知路径说明已实现；本轮测速仅 MockTransport。

见 [使用说明](../../../docs/clash-verge.md)、[验证](../../../docs/validation/clash-verge-2026-10-25/README.md)与 [ADR 011](../../../docs/adr/011-unified-clash-verge-client.md)。本轮未部署、未接管桌面、未实网测速或注入故障。
