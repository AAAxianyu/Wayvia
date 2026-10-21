# 10 补充 NekoBox 与 iPhone 客户端适配

Status: blocked
Priority: P3

## 当前证据

NekoBox 仅有节点协议可行性判断，完整分流配置未适配验收；iPhone 客户端未选定。OpenWrt M7 仍属后续独立扩展。

## 前置条件

需要确定具体客户端和版本；当前只验证实体 Android CMFA 2.11.35.Meta。

## 下一步

选定版本后验证完整配置格式、导入入口、规则/DNS/IPv6/UDP 和权限行为，保持终端只接入阿里云且不携带服务器承载秘密。OpenWrt 另开独立工作范围，先确认设备/固件与校园认证条件。

## 完成标准

更新兼容矩阵，分清节点能连接、完整配置能解析与真实设备验收；没有实机证据的能力保持待验收。

## 证据入口

- [阶段报告](../../../docs/validation/stability-fix-2026-10-20/README.md)
- [结构化结果](../../../docs/validation/stability-fix-2026-10-20/summary.json)
- [实现与验收状态](../../../docs/implementation-status.md)

## Comments

2026-10-20：阶段收尾时建立，尚未执行本项续查。
