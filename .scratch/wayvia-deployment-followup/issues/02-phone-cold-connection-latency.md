# 02 拆分手机首次建连延迟

Status: blocked
Priority: P1

## 当前证据

手机本轮 Wi-Fi 新建 HTTPS 中位数 492.984 ms，累计到目标 TLS 完成 423.930 ms；复用首字节 70.385 ms。前轮 LTE 新建 604.615 ms，不能跨网络归因。服务器完整链路新建 259.228→170.737 ms。进一步 CONNECT/TLS 测试因手动停用而未完成。

## 前置条件

手机重新连接并获 USB 调试授权；用户重新启用 Clash 后开始。当前用户已停止并移除手机。

## 下一步

固定同一手机网络、客户端版本、目标和负载，分别测本地代理连接、HTTP CONNECT/代理准备、DNS、目标 TCP/TLS、首字节和连接复用；每种条件保留多次样本。curl 的 time_appconnect 是累计计时，不能全部算 TLS 运算。只有分段证据明确后才评估兼容的连接复用；保持 VPN-over-proxy。

## 完成标准

给出相同条件下新建/复用的分段统计及测量边界；有参数改动须做受控前后对照并保留 fail-closed。所有临时 ADB 转发清理，未经用户指示不切换手机网络或改变当前客户端状态。

## 证据入口

- [阶段报告](../../../docs/validation/stability-fix-2026-10-20/README.md)
- [结构化结果](../../../docs/validation/stability-fix-2026-10-20/summary.json)
- [实现与验收状态](../../../docs/implementation-status.md)

## Comments

2026-10-20：阶段收尾时建立，尚未执行本项续查。
