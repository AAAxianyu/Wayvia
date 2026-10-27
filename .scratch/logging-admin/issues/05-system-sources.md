# 两端系统与传输

Status: done
Scope: repository_and_software_validation_only

被动来源、受限文件批次、游标与长期启停规则。

以[开发方案](../spec.md)为准。生产部署与真实用户流量采样未授权；不更换订阅 URL 或凭据。

## 验收

实现可运行、验证有证据，不能以占位或演示数据冒充生产能力。

## 完成记录

白名单 journal/SSH、主机指标与原审计投影、有限 spool、epoch/序号/摘要/ACK、受限 SSH helper 和本机 UID 校验已实现。对端传输软件回归通过；真实 SSH 权限配置未部署。

证据：[验收记录](../../../docs/validation/logging-admin-2026-10-27/README.md)。本状态只表示本期仓库开发和软件范围完成；生产部署、实网采样、完整故障实验及长期稳定性未验收。
