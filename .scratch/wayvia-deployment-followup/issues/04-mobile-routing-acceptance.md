# 04 补齐实体 Android 分流与协议矩阵

Status: blocked
Priority: P1

## 当前证据

CMFA 2.11.35.Meta 导入、LTE 选定国内 DIRECT 与国外日本出口已有证据。最新 Wi-Fi 国外出口通过；国内镜像 HTTP 403 不计为吞吐，新的路由日志未取得。完整全应用 TUN、DNS/IPv6/UDP 和切网尚未验收。

## 前置条件

实体手机重新连接，用户选择测试时间与 Wi-Fi/蜂窝条件；校园 DNS/网段另需真实输入。

## 下一步

按国内/局域网/校园/国外域名与 IP 建矩阵，核验真实目标、DNS 路径、保护 IPv6、隧道内 UDP及应用接管。比较系统私人 DNS、浏览器加密 DNS 和缓存条件。切网测试单独安排。浏览器或 USB 经本地代理的结果不能代替全应用 TUN。

## 完成标准

每项有客户端版本、网络条件、规则版本、期望路径、实际出口/日志及结果；未支持项显式失败。保留国内直连与代理失败阻断边界，错误页不当作测速。

## 证据入口

- [阶段报告](../../../docs/validation/stability-fix-2026-10-20/README.md)
- [结构化结果](../../../docs/validation/stability-fix-2026-10-20/summary.json)
- [实现与验收状态](../../../docs/implementation-status.md)

## Comments

2026-10-20：阶段收尾时建立，尚未执行本项续查。
