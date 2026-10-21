# Clash Verge v1.7.2 / Mihomo v1.19.13 订阅交付

用户指定 Clash Verge v1.7.2、Mihomo v1.19.13。本轮为当前已启用设备签发额外的 Mihomo 完整订阅，沿用该设备数据面凭据；没有创建新设备、轮换凭据、重启或修改生产数据面。订阅读取期限至 2026-11-20T03:33:09.191017+00:00，URL/token/完整 YAML 仅保存在忽略的 private 交付目录并发给用户，不进入 Git。

用 Clash Verge v1.7.2 默认 User-Agent，从当前外部网络直接下载新链接：HTTPS 验证通过、HTTP 200、gzip 1,152,606 字节（解压 9,207,514 字节），首字节 4.082 秒、总计 5.977 秒，no-store/private 保留。见 [下载验证](download.json)。这是当前外部网络样本，不代表用户桌面下载耗时。

从 [Mihomo 官方 v1.19.13 发布](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.13)下载 Linux arm64 核心，并核对 GitHub release asset 的 SHA256：`7aa8651738202741467d83b9ce51638ab7f01eef352472d2fba17e71dd5cce9a`；解压核心 SHA256 `9b4fa20a9526df83370b960d201896fd93c4c3b135cb9e86d60db1989e5f41f3`。版本输出为 `Mihomo Meta v1.19.13 linux arm64 with go1.25.0`。见 [资产来源](release-asset.json)。

在任务专用一次性 Linux 容器内运行 `mihomo -t -d /tmp/mihomo -f /input/profile.yaml`，输入为未经修改的完整订阅下载文件。配置检查退出 0，明确报告成功；117712 条路由规则、110187 条 DNS policy、1 个 Trojan 节点、rule 模式、节点 TLS 验证开启、默认 MATCH 到 Wayvia。见 [核心校验](validation.json)。容器 `--network none --read-only --cap-drop=ALL --security-opt=no-new-privileges`，非 root 身份，仅私有文件只读挂载和 /tmp 临时目录，`--rm` 完成即移除；没有启动核心代理、TUN 或改开发机路由。

[Clash Verge v1.7.2 的订阅页面源码](https://github.com/clash-verge-rev/clash-verge-rev/blob/v1.7.2/src/pages/profiles.tsx)支持 URL 导入；[该版本下载代码](https://github.com/clash-verge-rev/clash-verge-rev/blob/v1.7.2/src-tauri/src/config/prfitem.rs)使用 HTTPS 证书验证、默认直连获取 YAML。用户可复制链接至订阅页面输入框后点击导入，选中该配置并保持规则模式。尚未在用户桌面实际运行 Clash Verge GUI，不将上述配置解析结果当作桌面 TUN、UDP、DNS/IPv6 或全应用分流验收。

签发发生于 2026-10-21 03:33 UTC，随后发生一次完整订阅下载，这些是当前 24 小时观察期间的正常控制面活动。复核两端事务代次未变、状态均 ACTIVE，见 [生产状态](production-state.json)。没有改变已启动采样；故障与重启测试继续暂缓。生产源码未改，沿用当前源码已完成的 369 项软件回归、demo 和 10 项配置门禁；本次额外完成指定旧核心的独立真实解析验证，不重复执行故障/重启实验。
