# Clash Verge Rev 统一适配软件验证

分支 `codex/probe-observer-evidence`，基线 `f5957a0`；2026-10-25 America/Los_Angeles 开始，验证延续至 2026-10-26。用户授权按既定顺序完成统一适配、普通问题自主决策；仍只做仓库和软件验证。

## 结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| make check | 退出 0，524 项通过，3 项既有弃用警告；格式/静态检查/打包/CLI 通过 | [check.log](check.log) |
| make demo | 退出 0，simulated 控制面闭环 | [demo.log](demo.log) |
| make test-configs | 退出 0，11 项真实配置/客户端接口检查 | [config-result.json](config-result.json)、[日志](test-configs.log) |
| 离线统一客户端示例 | 退出 0；ASGI/MockTransport，无实网操作 | [result.json](example-private/result.json)、[日志](offline-demo.log) |
| make test-lab | **未执行：用户继续暂缓故障/恢复测试**，退出码 null | 不记通过，也不伪造环境缺失 |
| 最终文档/示例打包 | 退出 0；wheel 源文件逐项比对 | [summary.json](summary.json)、[final-build.log](final-build.log) |

版本、命令、实际退出码和源码摘要见 [summary.json](summary.json)。配置环境是明确 disposable、`--network none` 的 Linux 容器；原有 namespace/guard 预检仅在容器内运行，不改开发机路由。OpenVPN 配置启动不等于真实建隧道，记录仍为 tunnel_established=false。没有访问生产服务器、测速站点或真实客户端控制接口。

## 验证内容

1. 标准 Content-Disposition filename* 的中文名、24 小时周期、一键导入 URI 往返、HTTPS 同 origin 信息入口；控制符/路径字符过滤、下载/元数据鉴权与撤销/到期、缓存/Referrer 边界。UA 只作为声明，不影响配置策略。
2. 固定真实路径节点、组身份和选择缓存；轮换密码改变而名称不变，不出现 DIRECT 代理候选，空组 REJECT。没有为生产制造第二路径。
3. inspect/stage 对照可信参考检查路由、DNS、代理/TLS、TUN 排除、未审阅 listener/provider、controller 暴露等覆写。错误报告不复制目标、凭据、正文；无效候选保留旧文件，私有写入及旧节点同义名迁移通过。
4. 显式测速只有 MockTransport：204、Mbps/字节上限、重定向/错误/压缩拒绝、代理/目标问题、总期限子进程契约和单位区分。失败不伪造 0 Mbps 或业务断连，所有实际出口/服务器全路径保持未核实。
5. info 使用已有真实持久统计与采样新鲜度；未知额度/设备资格到期为 null，下载链接期限单列。原生 subscription-userinfo 不使用虚假 total/expire。诊断只消费私有文件，过期/模拟/角色歧义 unknown，告警引用校验角色/revision/generation，原文/坏容器不进入异常输出。

新增测试在 `tests/unit/test_client_profiles.py`、`tests/integration/test_client_subscription.py`；原有凭据、发布、保护、分流与诊断回归继续纳入全量门禁。

## 真实核心的附加客户端检查

`deploy/lab/client_acceptance.py` 集成进 make test-configs：同一容器内生成 TLS CA/本地 Trojan listener 和 HTTP 目标，关闭 TUN，显式只读挂载本轮 src。使用 Mihomo 1.19.32、sing-box 1.14.3 验证：

- 启动后没有目标请求；手动节点延迟请求确实经过认证 TLS Trojan 到本地目标。
- 409600 字节 payload 完整，Mihomo 活动连接的 chains 包含 Wayvia 组与节点。
- 改凭据的正常 reload 保留选择，无效 YAML 被拒绝后仍保留原选择；正常关闭/重开后缓存保留。等待核心批量写入，不宣称断电/崩溃持久性。
- 第二节点别名只是缓存夹具，与当前真实生产路径数量无关；目标是本地 HTTP，因此不把它当作真实公网 HTTPS 或完整服务器 VPN-over-proxy 验收。

期间修复了只读源码 mount 缺失、API 在组配置就绪前可响应的启动时序，以及夹具的 HEAD 支持；这些问题均在最终门禁中复验。离线示例的 MockTransport 响应流也已修正，示例与最终格式/静态检查复验通过。随后补充空 HTTP 200 payload 不应计为 0 Mbps 的分类与回归，补跑最终全量检查；真实配置/原生接口输入未改。归档为最终日志，仅规范行尾空白/末尾空行。

## 合成示例与剩余范围

[result.json](example-private/result.json)、[诊断](example-private/diagnostics.json)、[测量](example-private/measurement.json) 均标为 simulated。报告不包含随机 token、配置正文或设备密码。生成时目录/文件为 0700/0600；Git 不保留这些模式，重新生成可检查权限。`uv run --locked python scripts/demo_client.py --output NEW_PRIVATE_DIRECTORY` 拒绝覆盖旧目录。

[使用说明和平台矩阵](../../clash-verge.md)、[ADR 011](../../adr/011-unified-clash-verge-client.md)及[逐项任务](../../../.scratch/clash-verge-adaptation/spec.md)给出实现、命令和验收边界。统一订阅的软件范围完成；Windows/macOS/Linux 的 GUI 导入、TUN 授权/IDE/终端接管、休眠/切网、真实测速、生产部署、故障与服务器重启验收继续暂缓，不记为已通过。旧观察和 heartbeat 未恢复，健康/恢复决策没有改变，JXY 原始超时根因没有新结论。
