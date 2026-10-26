# Clash Verge Rev 统一使用与适配

Wayvia 生成一份统一 Mihomo 订阅，供 Clash Verge Rev 的 Windows、macOS、Linux 客户端使用，不要求填写系统或版本。当前生成一个 `Wayvia · 主线路` 节点和 `Wayvia` 选择组，线路内部仍由服务器完成 VPN-over-proxy；国内/校内直连策略保留。

本次交付是仓库软件与真实组件验证，尚未部署到生产。见[验证记录](validation/clash-verge-2026-10-25/README.md)和[ADR 011](adr/011-unified-clash-verge-client.md)。以下真实网络步骤供后续恢复授权后使用；本轮没有实际导入或接管任何桌面网络。

## 导入与日常使用

1. 获取该设备的私人 **Mihomo** 订阅，粘贴到 Verge 订阅页，或使用一键导入链接。
2. 导入后名称为 `Wayvia · 设备别名`，中文由标准响应头编码。点击订阅激活，再确认选择组为 Wayvia；目前一条真实线路显示一个节点。
3. 需要整个设备接管时，在 Verge 授予所需服务/TUN 权限；只使用系统代理时，终端、IDE、游戏等是否遵循代理需分别确认。订阅中的 TUN 请求不代表系统已经允许接管。
4. 默认规则模式保留国内/本地直连，国外与未知流量进 Wayvia；受保护 IPv6 按既有策略拒绝。节点不可用时没有 DIRECT 回退。
5. 点击节点延迟测试得到毫秒级 HTTPS 可用性/RTT。默认没有周期测试；延迟不代表 Mbps 下载能力，也不能证明任意业务可用。
6. 标准更新周期 24 小时，支持手动更新；名称在凭据轮换和配置更新后保持稳定，保留选择。旧 `Wayvia-entry` 首次更新失配时落到新的受保护首节点。

规则、DNS、TUN 可能受 Verge 全局/订阅 Merge 或 Script 改写。查看最终运行配置并与可信新订阅对照；保护结论只针对客户端正在接管的流量。主动停止客户端后的行为取决于操作系统，不承诺始终阻断全机流量。

## 名称、流量与期限

订阅使用标准 Content-Disposition filename* 和 profile-update-interval；一键导入采用官方 clash://install-config。HTTPS 订阅卡的“首页”是本设备受令牌保护的只读 `/info` JSON 信息接口，提供实际代理 payload、采样时间/缺口和下载链接期限；它没有管理员权限。链接需按凭据保密，响应 no-store、no-referrer。

流量是凭据对应的尽力累计代理 payload，不含国内直连或承载开销，不用于计费。没有真实套餐总量和设备资格到期，所以不会伪造原生套餐进度条或订阅额度；相关信息返回 null。下载链接过期只影响重新下载，已经导入的设备凭据由设备轮换/撤销管理。删除/撤销订阅与撤销设备权限是两项不同操作。

## 管理侧生成链接

```sh
wayvia --system subscription issue DEVICE_ID --format mihomo --base-url https://SUBSCRIPTION_ORIGIN
wayvia subscription import-link 'EXISTING_PRIVATE_HTTPS_MIHOMO_URL'
wayvia --system subscription name SUBSCRIPTION_ID LTY
```

base-url 是已经部署的订阅服务 HTTPS origin，不是平台选项。已有反向代理前缀的完整 URL 可以直接交给 import-link。不要将输出、完整 URL 或 token 放入日志、截图和公开工单。命令只编码或签发，不在本机导入、不请求该 URL。

`subscription name` 适用于含此功能的新版本 CLI；API 专用发布不会升级线上旧 CLI。`subscription name` 只设置下载显示名称，不改设备别名、token、凭据或网络 revision，也不重载代理。未设置时仍使用设备别名生成名称；设置 `LTY` 后导入名为 `LTY`。已有手机账号沿用同一 Mihomo 格式；实际手机 GUI/接管另行验收。线上 schema 3 的 API 专用发布方法见 [ADR 012](adr/012-subscription-title-and-api-overlay.md)，不能直接用本分支完整升级旧运行时。

私有 token 文件查询本地持久统计（不下载配置、无外部请求）：

```sh
wayvia --system subscription info /PRIVATE/token.txt
```

token.txt 须为 owner-private 0600。公开接口 `GET /subscriptions/TOKEN/info` 同样要求有效的该设备令牌；过期、撤销或非 active 设备拒绝。管理 API 仍要求管理员 Bearer；subscriptions-only 不提供管理路由。

## 只读检查与脱敏诊断

将可信订阅和 Verge 导出的最终配置保存成私有 0600 文件：

```sh
wayvia profile capabilities
wayvia profile inspect /PRIVATE/effective.yaml --reference /PRIVATE/trusted.yaml --output /PRIVATE_NEW_EXPORT/profile-check.json
wayvia profile diagnose /PRIVATE/effective.yaml --reference /PRIVATE/trusted.yaml --server-status /PRIVATE/status.json --alerts /PRIVATE/alerts.json --output /PRIVATE_NEW_REPORT/diagnostics.json
```

inspect 返回不含地址、域名、凭据和配置正文的固定错误码；检测路由/DNS/代理差异、TLS 校验、controller/LAN 暴露、额外 listener/provider、TUN 路由排除及接管范围。不提供 reference 时不能证明国内分流与 DNS 等价；离线检查不代替原生核心校验或实际接管验证。

diagnose 的 status.json 为明确提供的 Wayvia status JSON 或 agent transactions 数组；alerts.json 为 18 的告警导出。只读文件，不主动连接服务器。新鲜、匹配的角色/revision/generation 才能关联，缺失、过期、模拟或角色歧义保持 unknown。报告里的服务端观测与客户端实际连通性分别显示，业务影响需要终端证据。

| 常见结果 | 操作 |
| --- | --- |
| `CLIENT_ROUTE_OVERRIDE` / `CLIENT_DNS_OVERRIDE` | 对照可信订阅核查 Verge 全局/订阅覆写和最终配置 |
| `CLIENT_GROUP_POLICY` / `CLIENT_PROXY_OVERRIDE` | 核对节点、TLS/凭据与 DIRECT 候选，重新取得该设备订阅 |
| `CLIENT_TUN_ROUTE_OVERRIDE` / `CLIENT_TUN_GUARD` | 检查接管排除范围、strict-route 和 DNS 劫持 |
| `CLIENT_SYSTEM_PROXY_PARTIAL_COVERAGE` | 系统代理仅覆盖遵循代理的应用，单独检查终端/IDE 或在后续授权时使用 TUN |
| 订阅 HTTP 401 | 获取有效订阅；可能过期、已撤销或凭据无效，不用关闭证书验证绕过 |
| 服务端 failed | 按匹配 incident/generation 查看诊断时间线，核对保护和恢复事实 |
| 服务端 unknown | 取得获授权的新鲜状态；本地配置错误或测速失败本身不足以判定服务端故障 |

离线 stage 可保存验证后的候选，拒绝时保留旧文件，不重载任何客户端：

```sh
wayvia profile stage /PRIVATE/candidate.yaml /PRIVATE/trusted.yaml /PRIVATE_EXPORT/profile.yaml --selected 'Wayvia · 主线路'
```

输出目录必须 0700，文件 0600；拒绝链接和非自有路径。stage 不是远程下载器，也不代替原生 parser、Verge 激活或崩溃恢复；磁盘持久化异常会明确报告未确认。

## 有界手动测速工具

默认拒绝网络请求。以下命令本轮**未执行**；仅在后续获得实网测速授权后使用：

```sh
wayvia profile measure --kind latency --proxy-port 7890 --authorize-network
wayvia profile measure --kind throughput --proxy-port 7890 --seconds 10 --max-bytes 8388608 --authorize-network
```

只使用 127.0.0.1 的 HTTP mixed-port，固定 HTTPS 测试目标，禁环境代理/重定向，不会失败后直连，也不改规则/选择或启动后台任务。最大期限 10 秒包括 helper 启动和网络阶段，最多计 8 MiB 下载 payload；协议/TLS 与 socket 缓冲不计入 payload。子进程超时终止并回收。结果毫秒、字节、Mbps 分列，失败保留 unknown，不给虚假 0 Mbps；没有上传测速。

Verge 原生节点延迟针对指定代理；此 CLI 的实际路径由本地规则决定，因此始终注明出口身份和服务器全链路未核实。不能把本地代理端口有响应当成已证明经过 VPN-over-proxy。真实完整路径与性能验收继续暂缓。

## 兼容验收矩阵

所有平台使用同一订阅，不需要用户选择配置分支。真实配置和 API 检查锁定 Mihomo 1.19.32、sing-box 1.14.3；软件测试不依赖 UA 自报的版本。

| 场景 | 本轮软件依据 | 实际桌面验收 |
| --- | --- | --- |
| 中文名、一键导入、更新周期、信息入口 | ASGI 响应头与 URI 往返；官方公开协议 | Windows/macOS/Linux GUI 待验收 |
| 节点/选择/更新 | 真实核心接口、TLS Trojan 夹具、正常 reload/重开缓存、无效配置保留选择 | 各客户端 GUI/覆写组合待验收 |
| DNS/IPv6/国内分流 | 既有策略回归、配置真实 parser、最终配置对照 | 系统 DNS/DoH、实际出口待验收 |
| 系统代理、TUN、IDE/终端 | 统一配置、权限和 partial-coverage 诊断 | Windows 服务权限/macOS 授权/Linux 服务及应用矩阵待验收 |
| 休眠唤醒、Wi-Fi/有线切换 | 无固化网卡名，沿用 auto-detect-interface | 持续会话与恢复时间待验收 |
| 设备撤销/生产故障/服务器重启 | 既有软件回归，本轮未重放故障实验 | 继续暂缓 |

原生客户端接口例子只在 `--network none` disposable 容器里使用本地目标和第二节点别名夹具，关闭 TUN；它不证明生产完整 VPN 链路、实网性能或跨系统接管。重现实例：`uv run --locked python scripts/demo_client.py --output NEW_PRIVATE_DIRECTORY`，全部 HTTP 为 ASGI/MockTransport，实际网络测速为 0。
