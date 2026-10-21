# 05 补跑当前补丁完整隔离实验

Status: deferred
Priority: P1

## 当前证据

本补丁 make check 347 项、make demo 和 make test-configs 10 项通过。完整 make test-lab 暂缓；此前 23 项正式事务全通过属于旧基线，不能算当前补丁验收。

## 前置条件

用户已要求故障和重启测试暂缓；下次先明确是否恢复包含故障/恢复的完整实验。

## 下一步

获准后仅在明确一次性 Linux 容器/VM中运行完整 make test-lab，记录架构、锁定组件、源码摘要、所有模块退出码，并清理本轮资源。环境缺失或下载失败必须非零并保存证据。不要用生产主机补做隔离故障实验。

## 完成标准

当前提交有完整隔离门禁结果，含新探针在真实组件路径下的语义和原安全/恢复回归；失败原因和清理结果独立记录。

## 证据入口

- [阶段报告](../../../docs/validation/stability-fix-2026-10-20/README.md)
- [结构化结果](../../../docs/validation/stability-fix-2026-10-20/summary.json)
- [实现与验收状态](../../../docs/implementation-status.md)

## Comments

2026-10-20：阶段收尾时建立，尚未执行本项续查。
