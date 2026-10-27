# 订阅隔离与防滥用范围

Status: partially_deployed
Scope: subscription_limits_deployed_other_work_planned
Date: 2026-10-26 America/Los_Angeles

目标：限制公开订阅端点及代理凭据遭滥用时对其他设备、入口服务器和共享带宽的影响；提供可操作、隐私友好的撤销和观察能力。管理员统一维护，其他人只导入链接；以[私人使用产品范围](../product-scope/spec.md)为准。任务索引及当前分类见 [TODO.md](TODO.md)。

## 信任与范围

- 订阅链接是 bearer 凭据；持有者能获取该设备配置和对应代理密码。不同设备凭据独立，同设备多个链接共享设备凭据。订阅令牌不是管理令牌，也不是设备到期或流量配额。
- 没有发现凭订阅直接获得服务器管理员权限的证据；这不是完整安全审计或线上攻击验证。私网/元数据是否可达、单设备资源限制可否由当前核心实现，均待验证，不预设结果。
- 当前 API 运行在本机并可访问控制面数据；公开反向代理仅放行订阅路径。进程隔离是纵深防御，不能把代码鉴权等同于资源隔离或证明 API 受入侵后其他设备安全。
- API 局部内存限制、HTTPS、令牌哈希、no-store、禁止公开管理路由、日志关闭及独立凭据是已有措施，不能把它们写成整机 DDoS 防护或完整泄露防护。
- 参考 [ADR 007](../../docs/adr/007-device-usage-observations.md)、[ADR 010](../../docs/adr/010-device-history-and-observation-alerts.md)、[ADR 011](../../docs/adr/011-unified-clash-verge-client.md)。若拟改变仅撤销订阅不影响设备的既有语义，必须显式更新 ADR 和兼容方案；默认应保留现有接口语义，另提供明确的完整处置操作。

## 设计原则

优先限制公开请求及昂贵渲染并发，按设备强制限速和生成缓存暂缓。凭据隔离与资源隔离分开验收；缺少设备限速时不承诺带宽公平。可信代理/IP 来源、NAT、网络切换和正常订阅更新均需考虑。目标访问先以获授权的隔离环境核查，再针对证据决定必要防护；若实施地址限制，需考虑 DNS 解析后地址及明确授权的基础设施。不能将 01 的 IP 限流声称为应对任意大规模分布式攻击的保证。

## 现有证据

- [API 公开路由与响应头](../../src/wayvia/api/__init__.py)、[令牌、设备凭据与撤销](../../src/wayvia/services/control.py)、[渲染与服务器路由](../../src/wayvia/render/__init__.py)。
- [设备凭据隔离和撤销测试](../../tests/integration/test_control.py)、[订阅测试](../../tests/integration/test_client_subscription.py)。2026-10-26 针对性本地测试 5 项通过；此结果只说明已有软件行为。
- [完整升级记录](../../docs/validation/runtime-upgrade-2026-10-26/README.md)：部署版本与生产/隔离验收边界。

## Comments

2026-10-26：按用户要求创建待办，未实现或部署修复。线上仅做只读配置检查，没有运行线上压力、攻击或故障实验。

文档检查：10 个 Markdown 文件的本地链接均可解析。按仓库要求尝试 `make check`、`make demo` 和 `make test-configs`，三项均因当前 shell 找不到 `uv` 而以 2 退出，未执行对应检查；`make test-lab` 含故障实验，按既有暂缓要求未运行。此次仅新增规划文档，未更改代码、组件版本或线上配置。

2026-10-26：用户确认服务少量熟人且由本人统一管理；02、04 暂缓，05–07 收窄为基本观测和边界检查，08 并入每项变更的验收。历史计划不再代表当前排期。

2026-10-27：用户明确授权 01 在验证后部署。实际实施和证据见[限流验证](../../docs/validation/subscription-limits-2026-10-27/README.md)，其他事项未据此授权。
