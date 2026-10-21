# Android 订阅导入排查与首次实机验证

2026-10-20 UTC。用户报告手机从 URL 导入后一直显示“下载配置文件”，随后连接手机并授权检查。通过官方 ADB 37.0.1 识别 HUAWEI HBN-AL10，Android 12 / SDK 31。公开记录不含手机序列号、订阅 token、设备密码或浏览历史。

## 发现

手机原应用为 `com.github.kr328.clash`，版本 `2.5.12.premium`，不是交付配置面向的 Mihomo/Clash Meta 客户端。确实观察到其停在下载页面，但没有取得可定位下载失败的错误日志，不能把版本差异单独当成无限等待的已证明原因。

服务端实测也有性能问题：原 YAML 为 9,207,514 字节，每次按当前发布快照生成，服务器本机约 14.8 秒才返回首字节；外部请求首字节约 14.0 秒，总耗时 34.6 秒。请求返回 200，服务端没有相应 API 错误日志。原专属订阅位置未对 YAML 启用压缩。详见 [download-before.json](download-before.json)。

## 处理

仅在 Wayvia 的 Nginx `/subscriptions/` 位置启用 gzip，类型限定 YAML/JSON，压缩等级 5；继续关闭访问日志与响应缓存，不改认证、撤销检查或数据面。修改前保存专属配置备份，`nginx -t` 成功后平滑 reload；四个既有站点文件摘要全部一致。脚本及结果见 [subscription-gzip.sh](subscription-gzip.sh)、[nginx-check.txt](nginx-check.txt)。

压缩后传输 1,152,634 字节，减少约 87.5%；外部完整下载约 15.5 秒。解压内容与此前通过 Mihomo 1.19.32 校验的 YAML 逐字节相同，TLS 验证开启，响应继续为 `no-store, private`。首字节仍约 13.7 秒，生成时间尚未优化，不能宣称全部性能问题解决。详见 [download-after-gzip.json](download-after-gzip.json)。

从 [官方 v2.11.35 发布页](https://github.com/MetaCubeX/ClashMetaForAndroid/releases/tag/v2.11.35) 下载 ARM64 APK，并核对 GitHub 发布资产的 SHA256：

`af7f05d8801798a8e75de682e5f67070138becb8623b7a341c3e5cd042a3753d`

首次安装被手机系统拒绝，返回 `INSTALL_FAILED_ABORTED: User rejected permissions`。说明原因并取得用户继续操作回复后重试成功。安装结果为独立包 `com.github.metacubex.clash.meta`、`2.11.35.Meta`，旧应用仍安装。精确发布资产与 ADB 来源见 [client-release.json](client-release.json)、[adb-version.json](adb-version.json)。没有修改全局系统代理、切换手机网络或执行服务器故障/重启测试。

## 手机实际结果

- 当前为移动网络 LTE。同一私人订阅通过官方导入 intent 进入表单，名称为 `Wayvia Android`；保存后首次观察下载状态在 5.4 秒，21.6 秒时已经返回主页，即本次观察中约 22 秒内完成导入。见 [phone-import.json](phone-import.json)。
- 随后观察主页为“运行中”“规则模式”“Wayvia Android 已激活”，已有转发流量。系统存在归属该应用的 VPN 网络与 `TunService`。
- 手机浏览器访问自有 HTTPS 探针，页面返回日本出口 IP；没有跳过证书校验。见 [phone-probe.json](phone-probe.json)。

这证明该实体 Android、该客户端版本和当前蜂窝网络下，URL 导入与基本代理 HTTPS 路径已通过。旧版转圈的唯一触发原因没有做单变量复现；更换兼容客户端并启用订阅压缩后，用户报告的问题不再复现。

手机国内分流、DNS/IPv6 完整矩阵、Wi-Fi 与蜂窝切换、校园规则、性能及长期运行仍需继续验收。之前 Linux 客户端的 DNS/IPv6 和国内直连结果不能替代这些手机项目。真实服务器故障注入与重启继续按用户要求另行安排。

本次仅修改服务器的订阅传输配置和验证记录，没有修改 Wayvia 生产源码；既有四项软件/隔离门禁结果沿用 [此前完整复验](../deployment-review-2026-10-20/README.md)。本次新增的检查为 Nginx 配置检查、完整响应等价检查、官方 APK 摘要检查和实际手机导入/HTTPS 路径验证。
