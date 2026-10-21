# 11 Windows TUN 中国外 IPv4 自动使用与回退

Status: open
Priority: P1

## 已确认

Clash Verge 1.7.2 / Mihomo 1.19.31，用户开启 TUN 后，Google 默认 curl 请求三次选择 IPv6 并命中 `::/0 REJECT`；强制 IPv6 三次同样被拒绝，强制 IPv4 三次成功，HTTP 代理两次成功。国外 IPv6 本就未支持，拒绝正确；缺口是自动选址没有顺畅使用可用 IPv4。当前未修改用户配置。

## 下一步与完成标准

核对 Mihomo 1.19.31 的 DNS/AAAA 与地址选择能力及 Verge 合并覆盖行为。在保持国内直连、国外 IPv6 阻断和服务器承载秘密隔离的前提下，让不支持 IPv6 的国外目标使用 IPv4；不要通过删除 REJECT 规则放宽外泄，也不要把新增 IPv6 转发当作本票前提。先在一次性环境校验，再按用户目标验证默认 curl、HTTP 代理和实际浏览器；国内 IPv4/IPv6 行为单独记录。WLAN 本次无全局 IPv6，物理 IPv6 不能算通过。

此票不涵盖服务器自然降级，后者继续由 01 跟踪。客户端配置修改与生产发布不是本次只读测试的已完成动作。

## 证据

[Windows 校园网诊断](../../../docs/validation/windows-campus-2026-10-20/README.md)
