# 13 一次性观察器与系统维护的协调

Status: open
Priority: P1

## 已确定的问题

2026-10-21 22:22 UTC，阿里云 unattended-upgrades 的 needrestart 立即重启命令包含观察单元。原进程收到信号后保存结果并退出；新调用因同名证据目录已存在而 FileExistsError。24 小时窗口仅完成约 80.09%，后续业务故障缺少入口独立采样。系统自动维护还重启了 agent/API/usage/VPN；本次观察自身未发出这些变更。

## 后续工作

1. 设计在预定期限内区分首次启动、意外恢复与已结束任务的生命周期；证据追加须验证原 run、配置/代次、权限和截止时间，避免覆盖既有证据或隐式延长观察。
2. 评估与系统自动维护的协调及生产服务重启通知，不默认关闭安全更新，不擅自修改现有 needrestart 或生产配置。
3. 覆盖目录已存在、终止信号、到期、半行、文件权限、重复进程等本地测试；涉及故障/重启实验时等待用户另行安排。
4. 新的正常运行观察需另行授权，不能自动重启或延长本次已结束窗口。

## 证据

[收尾报告](../../../docs/validation/stability-observation-2026-10-20/final-report.md)、[needrestart 命令分类](../../../docs/validation/stability-observation-2026-10-20/observer-stop-needrestart-ali.json)、[原进程和后续调用](../../../docs/validation/stability-observation-2026-10-20/observer-stop-ali.json)。本项不是业务最初超时根因，尚未实施修复。
