# 更新原有订阅并交付 LTY

Type: task
Status: resolved

按[规格](../spec.md)实现独立订阅名称、schema 3 API 专用发布，保留原账号/链接，完成 HTTPS 和真实核心检查，交付手机私人链接。软件、订阅服务和手机实际接管的接受度分开记录；故障/测速/采样继续暂缓。

## 完成结果

用户手动重启后恢复 SSH。API 专用发布成功，原手机订阅显示 LTY、JXY 显示名保留，原 URL/凭据与 schema 3 不变。530 项测试、11 项组件检查和两份实际下载配置的禁外网 Mihomo parser 通过。API 内存上限 512 MiB；服务器端完整回归的 OOM 已记录，后续回归只在本地/隔离环境运行。见[验证记录](../../../docs/validation/subscription-lty-2026-10-26/README.md)。
