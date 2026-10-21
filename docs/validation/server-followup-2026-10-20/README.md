# 无手机服务器续查与修复

日期：2026-10-20 America/Los_Angeles；服务器事件统一使用 UTC（本次操作跨到 2026-10-21）。基线 `07ba4ff303334170ddfd96f2774df47754fffece`，开发分支 `codex/server-followup-20261020`。范围是用户指定的稳定性排查、订阅生成速度和安装权限；没有操作手机、主动制造链路故障或执行整机重启测试。

订阅优化和安装权限修复已完成验证。稳定性排查修复了一个可复现的探针误判，并已部署；真实网络超时的底层原因及修复后的长周期稳定性仍未验收。

## 稳定性：历史记录与因果边界

只读收集当前代次从 2026-10-20 10:06 UTC 到 2026-10-21 02:27 UTC 的约 16 小时 21 分钟日志：

| 主机角色 | monitor 轮数 | ACTIVE 记录 | DEGRADED 记录 | 降级事件 |
| --- | ---: | ---: | ---: | ---: |
| 阿里云 ingress | 3708 | 3699 | 9 | 9 |
| 日本 exit | 3728 | 3725 | 3 | 2 |

这些是周期采样状态，不是业务请求成功率或 SLA。日本有一轮恢复期限耗尽而保持 DEGRADED，不能算新的独立网络故障。原始脱敏摘要、按小时统计和各层耗时见 [阿里云](stability-ali.json)、[日本](stability-jp.json)。

- **13:59:54 UTC**：日本 egress 的两个身份端点均超时；随后 DNS 也超时。14:00:18 日本进入 DEGRADED，14:00:19 才发生 tun0 重建，14:00:20 阿里云 VPN 连接重置，阿里云 14:00:30 降级、14:00:49 恢复。另一次日本超时起于 14:00:56。初始请求失败发生在保护/恢复动作之前，恢复动作进一步影响入口。
- **20:26–20:28 UTC**：阿里云出现 namespace egress 超时、TLS 建连失败及承载检查失败，而日本 monitor 没有进入 DEGRADED。证据不足以区分国际链路、隧道瞬时异常或目标服务故障。
- **另外 5 次入口 access 单层失败**：两个尝试都在约 0.2 秒出现 `HTTP_TRANSPORT_FAILED`，其它层通过；分别发生在 15:31、20:28、22:10、22:59、23:23。它们与下述误判机制相符，但历史记录未保留 curl 完成结果，不能追溯认定每次都属误判。

[系统事件](incident-system-ali.json)及[日本接口事件](incident-jp-interfaces.json)没有提供物理接口断链证据；日志里的上下线全部是 tun0。两端没有本窗口内 OOM、conntrack 满或 soft lockup 的匹配日志。阿里云 [CPU 历史采样](incident-ali-cpu.json)为约 10 分钟平均，相关窗口 idle 86.47–92.33%，最大 1 分钟负载 0.34；不能排除秒级尖峰。日本没有可用的历史 CPU 采样。大量日本 VPN TLS/reset 日志包含认证探针的短连接，不可当成同等次数的用户断连。

### 已证明并修复的误判

[旧实现诊断](probe-diagnosis.json)在第 37/100 次只读新鲜业务请求中捕获：curl exit 0 → HTTP 200 → 日本出口身份匹配 → relay 退出时 `ConnectionResetError(104)` → 旧实现误判传输失败并调用备用端点。关闭连接的异常覆盖了已经完整收到的响应。

修复仅在已有完整 curl 结果时保留结果，再执行原有 TLS、状态码、出口身份及总期限检查。响应前断开、不完整响应、TLS 异常、错误身份、重定向和期限超限仍失败；没有宽限期、缓存成功或直连回退。新增证据字段 `transport_closed_after_response`。见 [ADR 006](../../adr/006-bounded-egress-probe-redundancy.md)。

[只在诊断进程加载候选代码](probe-diagnosis-fixed.json)的 100 次全部通过，其中一次准确记录关闭异常而保留正确响应；[正式部署后](probe-diagnosis-deployed.json)再测 100 次全部通过。诊断请求没有修改 monitor、制造丢包或停止服务。部署后的正常 monitor 窗口为 ali 19 轮、4.7 分钟；jp 19 轮、4.8 分钟，均为 ACTIVE，未见新降级。详见 [入口](post-deploy-ali.json)、[出口](post-deploy-jp.json)；该短窗口不能替代长期验收。

两个探针 IP 仍同属 Cloudflare。当前方案所有端点共享同一响应格式，增加独立供应商前还需确定出口身份语义、TLS、DNS 路径和隐私边界；本轮没有冒用另一种响应作为成功，也没有放宽故障保护。

## 订阅生成：前后对照

完整示例规则包含 110188 条 DNS 策略、117712 条路由规则。分段计时见 [render-baseline.json](render-baseline.json)：结构生成约 0.151 秒，Python 安全 YAML 序列化 4.330 秒，C 安全序列化 1.478 秒；本地输出逐字节一致。采用同一锁定 PyYAML 6.0.3 的 `CSafeDumper`，缺少扩展时回退 `SafeDumper`。

在阿里云使用**相同既有私人订阅、相同发布版本、相同 gzip 设置**，优化前后每种路径各取 3 个样本，报告中位数：

| 测量路径（均非手机） | 首字节：前 → 后 | 完整下载：前 → 后 |
| --- | --- | --- |
| 同机 HTTPS 9443，解析到 loopback，仍校验证书 | 12.971 → 4.218 秒 | 13.178 → 4.417 秒 |
| 阿里云自身访问公网 IP 的 HTTPS 9443 | 13.494 → 4.334 秒 | 15.145 → 6.394 秒 |

同机首字节减少约 67.5%。公网地址测量仍由服务器自身发起，不能当成外部网络或手机的下载速度。证据：[同机前](subscription-before.json)、[同机后](subscription-after.json)、[公网地址前](subscription-public-before.json)、[公网地址后](subscription-public-after.json)。12 次响应解压后的 SHA256 全部相同，HTTP 200、证书验证通过、gzip 及 `Cache-Control: no-store, private` 均保持。

没有最终响应缓存或凭据中间缓存。软件回归覆盖 Mihomo 和 sing-box 的新增、轮换、撤销，现有期限、格式与响应头检查继续通过；安全序列化回退还覆盖带换行、YAML 标记和 Unicode 的密码。10 项锁定组件配置校验通过。没有为测试轮换或撤销手机现有凭据，也未重新签发订阅。

## 安装权限：责任、修复和验证

原正式安装器的公开父目录创建、uv sync 和 uv pip install 都可能继承调用方的 077；外部部署辅助脚本用同样方式重装也有相同风险。原版本仅以 root 执行 `--version`，无法发现服务账户不可读；同版本 wheel 还可能被 uv 跳过。

[基线复现](install-permission-baseline.json)在明确一次性、无外部网络的 Ubuntu 24.04 容器里，077 使 `/opt/wayvia`、venv 和包目录成为 0700；非 root 账户执行失败。修复明确设置公开父目录 0755，仅两个安装依赖的子进程使用 022，并添加 `--reinstall-package wayvia` 和真实服务账户执行检查。调用方 umask 及秘密路径没有放宽。

[修复验证](install-permission-fixed.json)覆盖严格 077 下新安装、相同版本但内容不同的重装、服务账户 CLI、服务账户新启动 API 的 `/healthz` 200。公开运行目录 0755，密钥和控制目录仍 0700，环境/私密文件 0600；使用 uv 0.9.7、Python 3.12.3。API 是隔离容器中的正常首次启动，随后清理，不是故障/重启恢复实验。

补充 API 首次启动的第一版实验脚本遗漏正式安装器已有的父目录组拥有权步骤，初始化返回 5；补齐相同的 init/chown 步骤后通过。最初部署预检查也因误要求日本未启用的 nginx 必须 active 而拒绝，未修改生产文件；随后改为保留其原状态。两次都是验证脚本错误，没有隐去后把它们算作产品通过。

[阿里云权限审查](permissions-ali.json)和[日本权限审查](permissions-jp.json)确认：公开运行目录可由服务账户执行，服务器私钥由 root 持有 0600 且 API 无法读取，控制面秘密为服务 UID 的 0600 且 nobody 不可读。旧的错误权限 venv 不做递归放权；安装器会明确报 `runtime_unreadable`，需在维护范围内审核/重建。没有执行真实服务器重装、卸载、失败恢复或整机重启。

## 部署、可复现材料与门禁

正式 wheel SHA256：`6289bb94661a4e008eac3f85488bce71e0d7e95811918ac3f9e83142f4f09b31`，软件版本仍为 0.1.0，不能只靠版本号识别本次补丁。

这是针对已知旧 wheel 的一次审核后模块维护，不是通用升级流程：旧/新 wheel 只改变两个互不依赖的模块和 RECORD。预检查验证全部旧包、安装源文件和安装清单；在仅 root 可读的路径保留前态，原子替换两个模块、对应源文件、wheel、RECORD 和清单摘要。探针 helper 每次是新进程，API 为加载新 renderer 单独重载。两端数据面、agent、monitor timer、nginx 的 PID/激活时间和宿主默认路由前后不变，事务代次和发布逻辑配置没有变更。见 [日本部署](deployment-jp.json)、[阿里云部署](deployment-ali.json)和[已执行维护脚本](runtime-patch.py)。正式安装器修复用于之后的正常安装，未在真实主机上跑全量重装。

| 检查 | 本轮结果 |
| --- | --- |
| `make check` | 退出 0，357 项通过、3 项既有弃用警告；格式/lint、wheel/sdist、CLI 通过 |
| `make demo` | 退出 0，明确 simulated 演示 |
| `make test-configs` | 退出 0，10 项锁定真实组件检查 |
| 一次性 Linux 正常安装权限专项 | 基线失败复现；修复后新装/同版本重装/非 root API 启动通过，容器已移除 |
| `make test-lab` | 用户明确暂缓，因包含故障注入及恢复实验，本轮未执行、未计通过 |
| 真实故障与整机重启、手机矩阵 | 用户暂缓/未接入，本轮未执行 |

命令和日志见 [gates.json](gates.json)，源码/运行时/镜像摘要见 [source-and-versions.json](source-and-versions.json)。

只读采集可使用 `collect-stability.py ingress|exit [UTC起点]`、`audit-permissions.py`、`diagnose-probe.py`；后者只在已授权入口服务器运行，在内存中读取当前探针凭据，输出不含凭据、URL 或响应体。本地分段基准为 `benchmark-render.py`。

权限专项脚本只允许在一次性容器中执行。`reproduce-install-baseline.py` 的 `/repo` 必须对应基线提交 `07ba4ff`；`verify-install-fixed.py` 使用本次源码和已构建 wheel。挂载 `/repo:ro`、`/evidence`，使用记录的 transaction 镜像及已核验 `/components`，`--network none --rm --entrypoint python3.12 -e UV_OFFLINE=true --label org.wayvia.disposable=followup-permissions`。没有启用特权容器或 host 网络，也没有修改开发机网络。

## 尚未关闭

待办 08/09 已完成本轮范围；01 保持 open：需要更长的修复后正常观察，以及对真正的 HTTP/DNS 同时超时取得独立路径/提供商证据。5 次历史 transport-only 事件只能列为疑似误判；不能宣称自然断连已全部消除。故障和重启相关的 05/06 继续 deferred，不创建定时任务。手机、带宽额度、独立供应商探针及完整验收仍按[待办索引](../../../.scratch/wayvia-deployment-followup/spec.md)继续。
