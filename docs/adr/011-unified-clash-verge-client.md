# ADR 011 — 统一 Clash Verge Rev / Mihomo 客户端契约

Status: accepted (repository/software scope)
Date: 2026-10-25 America/Los_Angeles; validation continued on 2026-10-26
Base: f5957a0

## 范围

用户要求按订阅名称、稳定节点名称、测速、更新与选择保留的顺序完成统一适配，再完成分流/覆写检查、流量和有效期、诊断、日常使用及兼容验证；普通问题自主决策。用户不填写操作系统或客户端版本，UA 不决定安全策略。保留 ADR 001/004/007/010 的国内直连、服务器 VPN-over-proxy、终端凭据隔离、发布快照和 fail-closed。没有新增配置 schema 字段或改变旧发布 revision 的计算。

本轮不启用生产部署、实网采样/测速、故障/切网/服务器重启实验，不接管开发机网络或修改既有 VPN。桌面 GUI 和真实环境接受度独立于软件完成。

## 导入、名称与更新

Mihomo 与 sing-box 下载均设置带 ASCII fallback 和 UTF-8 filename* 的 Content-Disposition。标题由设备别名生成，过滤控制符/路径字符并限长；不含 token、地址或设备 UUID。Mihomo 标准更新周期为 24 小时，所有响应继续 no-store/private/no-cache，新增 no-referrer。统一 Mihomo 订阅忽略客户端 OS/UA 分支，Verge UA 只保存家族及数字版本的未认证声明。

`subscription import-link` 对既有 HTTPS Mihomo URL 做一次完整 URI 编码，不请求该地址。`subscription issue --format mihomo --base-url HTTPS_ORIGIN` 可同时返回完整链接和一键导入链接；在签发前验证 origin/格式。base-url 是服务地址，不是平台设置。令牌仍属于设备凭据，输出仅供持有人使用，不入日志。

真实可选路径仍只有一条。策略组保持 `Wayvia`，节点显示为固定 `Wayvia · 主线路`，不随设备密码、版本、入口地址或配置 revision 改名，不将服务器承载/VPN 制造成独立节点。旧 Wayvia-entry 缓存失配时回到受保护首节点；离线 staging 提供同义名迁移说明。组没有 DIRECT/COMPATIBLE 候选，空组 fallback 明确 REJECT；profile.store-selected 开启。只有将来存在独立实际路径时才增加优选或故障切换。

Verge 自己负责远程订阅下载/激活。服务端非 2xx 不返回伪装成配置的错误正文；下载权限撤销与设备撤销继续区分。离线 stage 先验证候选，拒绝时保留原文件，再原子写 owner-private 文件；不重载或控制正在运行的客户端。磁盘提交异常报告未确认，不谎称原文件一定未变化。GUI 自身的选择/更新行为以真实核心接口检查和未来桌面矩阵分别记录。

## 测速与接管边界

组配置标准 HTTPS 204 目标、5 秒 timeout、expected-status 204、interval=0、lazy=true，默认没有周期检查。Mihomo unified-delay 展示 RTT；其内核处理默认目标时可让界面沿用自身测速目标设置，不能保证任意全局覆写仍使用订阅默认值。原生节点延迟调用选择指定代理；仅可用性/毫秒结果不能当作带宽、完整业务成功或所有服务器层的路径证明。

`profile measure` 是另一项显式单次工具，默认拒绝网络操作。授权时只通过 127.0.0.1 指定 mixed-port 发 HTTPS，请求目标固定，验证 TLS、禁环境代理/重定向，没有直连重试。下载上限 8 MiB 应用 payload、总期限最多 10 秒，子进程到期被终止并回收，覆盖 DNS/握手/慢流。底层缓冲及 TLS/协议开销不等于 payload 上限。指标分别报告毫秒、字节和 payload/整个请求时间得到的 Mbps；上传未测，失败不给 0 Mbps，代理/目标问题不直接命名为业务断连。实际路由由本地客户端规则决定，结果始终注明出口身份/服务器完整路径未核实。本轮只用 MockTransport，未访问测速站点。

订阅继续请求 TUN、strict-route、DNS 劫持及接口自动识别。系统代理只覆盖遵循代理设置的应用；TUN 需要客户端服务/系统权限，订阅不能替用户授予权限。用户退出客户端后的系统级阻断不在承诺内。Windows/macOS/Linux 共用一份配置，权限、休眠、切网及 IDE/终端接受度列入独立矩阵。

## 真实统计、有效期与诊断

HTTPS 下载响应的 profile-web-page-url 指向同一请求 origin 下的本设备 `/info`，无管理员能力；HTTP 请求不生成该 HTTPS 首页入口。该 endpoint 是令牌保护的 JSON 信息接口，返回真实累计代理 payload、最近采样/缺口、账户状态和下载链接有效期，查看信息不算订阅下载。过期/撤销/非 active 设备拒绝信息与下载。所有信息不带 token/hash、设备密码、secret_ref、服务器承载地址或原始计划。

未知总配额、设备资格到期均为 null；下载链接到期不等于已导入设备凭据到期。Verge 原生流量卡需要 total/expire 语义，本项目没有可靠的套餐额度/期限，故不发送误导的 subscription-userinfo（例如 total=0 或将 token 到期冒充套餐到期）。流量和下载期限通过 `/info` 或私有 CLI 查看，不承诺客户端原生套餐进度条。

`profile inspect` 检查用户明确提供的有效配置；可与新鲜可信订阅文件对照路由、DNS、代理/TLS/凭据、MTU，检测监听器/provider 扩展、TUN 排除范围、开放 controller、DIRECT 候选及接管缺口。缺参考不能证明国内/DNS 等价，缺运行信息不能证明实际接管或出口。报告只有固定错误码和可操作说明，原文/域名/密码/地址不导出。stage 仅是离线保存，不代替真实核心 parser 或 GUI 激活。

`profile diagnose` 只读用户提供的私有状态与告警文件，沿用 90 秒证据/monitor 时效，按角色/revision/generation 对应告警和 incident 引用。重复 current 角色、模拟/陈旧/缺失证据保持 unknown；本地配置错误、服务端观测与终端业务影响分别报告，不依据失联自动恢复、改路由或重开任务。

## 验证契约

真实核心检查在明确 disposable 的 `--network none` Linux 容器运行；新增只读源代码 mount 使辅助检查和当前源码一致。额外本地 listener、CA、HTTP 目标及第二节点别名仅为接口/缓存夹具，不进入发布配置。关闭 TUN，不操作宿主路由。验证默认不探测、TLS Trojan 手动延迟、payload、活动连接链、正常 reload/关闭重开后的选择缓存和无效 YAML 保留当前选择。缓存写入采用正常关闭的等待，不宣称崩溃/断电持久性；不执行生产或完整服务器故障实验。

软件结果、版本、源码摘要和剩余接受度见[验证记录](../validation/clash-verge-2026-10-25/README.md)。

## 依据

- [Verge 官方 URL scheme 与响应头](https://www.clashverge.dev/guide/url_schemes.html)：文件名、更新周期、使用信息与主页入口。
- [Mihomo 通用组字段](https://wiki.metacubex.one/en/config/proxy-groups/)及[通用配置](https://wiki.metacubex.one/en/config/general/)：手动/周期检查、fallback、选择缓存与统一延迟。
- [Verge 订阅解析](https://github.com/clash-verge-rev/clash-verge-rev/blob/main/src-tauri/src/config/prfitem.rs)及[流量卡](https://github.com/clash-verge-rev/clash-verge-rev/blob/main/src/components/profile/profile-item.tsx)：确认卡片需要的额度/期限口径，未知数据不捏造。
