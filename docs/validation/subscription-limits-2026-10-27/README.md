# 订阅入口限流：验证和部署

日期：2026-10-27 America/Los_Angeles。用户明确要求按讨论方案先验证正常使用，再部署至入口。仅修改指定主机的 Wayvia Nginx 订阅虚拟主机配置；未改设备、链接、代理/VPN、主机路由或防火墙，没有恢复观察、通知或生产故障实验。

## 配置

- 每来源 `5r/m`、`burst=5 nodelay`；初始可立即接受 6 个请求，接着约每 12 秒恢复一个突发额度。来源取 Nginx 实际连接地址；生产 Nginx 配置未发现 `real_ip_header` 或 `set_real_ip_from`。共享 NAT 的设备共同使用来源额度。
- 所有 `/subscriptions/` 请求共用全局并发 2，包括配置下载与信息页；超速或超并发返回 429。健康探针及其他站点不在此 location 内。限流不主动断开已建立的代理连接。
- 原配置 SHA256 `856226fefad4be91e63586242314a245f8b8afa9061c52bfe60ec75be21dfb40`；部署后 `ce9320f8bdecf2b7da18daeb268abf0acc0db78b4c4d6bb14513791604f23d04`。原配置备份：`/var/backups/wayvia/subscription-limits-dzdx2uyv`，由 [部署工具](../../../deploy/subscription_limits.py)创建并检查指纹。[操作说明](../../operations.md)列出限制与回退边界。

## 隔离验证

生产为 Ubuntu Nginx 1.24.0；测试使用相同 Nginx 版本的官方 Docker 镜像 `nginx@sha256:f6daac2445b0ce70e64d77442ccf62839f3f1b4c24bf6746a857eff014e798c8`。发行版构建不同，生产实际配置另经 `nginx -t` 校验。容器使用 `--network none`；仅把实际虚拟主机候选中的证书/私钥路径替换为临时合成证书，代理后端、设备与令牌为合成数据，没有真实订阅或密码进入容器。

- 两个模拟设备顺序下载约 9,207,741 字节配置并访问信息页，成功；默认拒绝管理路径和不允许的方法，健康探针不受影响。
- 同一来源的无效请求前 6 次得到认证拒绝，第 7 次得到 429；等待后可再次请求。伪造的 `X-Forwarded-For` 未绕过来源限制，429 正文不含合成令牌。
- 两个长请求占满配额时，第三个来源的信息页得到 429；请求结束后恢复为 200。此测试验证 Nginx 并发行为，不等于生产真实 API 在两个完整配置同时渲染时的内存验收。
- [部署工具](../../../deploy/subscription_limits.py)的软件测试覆盖配置漂移拒绝、重载失败时恢复、拒绝覆盖后续改动及校验后的恢复；未在生产注入重载故障或实际演练回退。

## 部署与正常使用核对

实施前先通过合法 HTTPS 请求分别取得手机和 JXY 当前配置与信息页，保留脱敏摘要；实施后再次请求，两个配置下载及两个信息页均为 200，TLS 校验开启，配置字节数和 SHA256 与部署前一致，24 小时更新及手机 LTY 名称保持。没有把令牌或配置正文写入此记录。线上 Nginx 配置检查和重载成功，Nginx、API、入口、agent 和用量服务均 active；Nginx 主进程与 Wayvia API/入口进程未重启。

| 门禁 | 结果 |
| --- | --- |
| `make check` | 544 项通过，3 项已有弃用警告；格式、lint、构建和 CLI 通过 |
| `make demo` | 通过，模拟数据 |
| `make test-configs` | 通过，11 项真实组件配置检查 |
| Nginx 专项隔离验证 | 通过；同版本 Nginx、合成数据、无外网 |
| `make test-lab` | 按既有故障测试暂缓要求未运行，退出码 null |
| 生产超限、故障、实机 GUI | 未运行；本次只进行正常请求和就绪检查 |

本地工具版本：Python 3.12.12、uv 0.9.7、pytest 8.4.2；uv macOS wheel 摘要 `0fdbfad5b367e7a3968264af6da5bbfffd4944a90319042f166e8df1a2d9de09` 与官方 PyPI 元数据一致。软件门禁通过后，额外验证脚本及隔离探针经过格式/lint 检查，隔离探针再次通过。当前阈值是经功能验证的初始配置，不是生产压测后的容量保证。

结构化的脱敏摘要见 [results.json](results.json)，本次组件配置检查的完整结果见 [config-result.json](config-result.json)。
