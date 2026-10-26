# LTY 原订阅更新验证

Date: 2026-10-26 America/Los_Angeles
Branch: codex/probe-observer-evidence
Base: dcee617
Status: software verified; production update blocked by management timeout

用户授权更新原有订阅并提供名为 LTY 的手机 Clash/Mihomo 链接。只进行必要的订阅 API 更新与 HTTPS 下载验证，保持国内直连、VPN-over-proxy、服务器秘密与 fail-closed；不部署其他观测/恢复功能、不运行实网测速/采样或故障实验，不恢复旧观察/heartbeat。

## 软件与基线

- 线上 schema 3；原手机、JXY 电脑各自为 active，旧共享账号及三个旧链接仍已撤销。当前有效手机/电脑链接从服务端私有交付资料读取，并重新核对当前数据库 token hash、设备状态与期限；不恢复旧数据库，不签发或轮换凭据。
- 现有 API/control/storage/renderer 与已审阅的统计基线摘要一致；线上既有 CLI/agent 补丁原样复制，清单保存全部当前代码摘要。独立 release 只更改四个白名单文件，ServerRenderer AST 不变，schema 仍为 3。原运行包不覆盖，schema 4 历史表与新后台功能不部署。
- `subscription name` 在现有 settings 表写名称并审计，不修改 devices/subscriptions 身份、不调用 dataplane。LTY 同时用于 ASCII 与 UTF-8 文件名；无自定义名称的 JXY 仍按原别名显示。
- API 兼容层保留原本机管理鉴权、订阅/设备拒绝条件、发布快照与 fetch 计数；信息页不算下载。明确 HTTPS origin 保留非默认端口，避免改 Nginx；未知配额/设备资格到期继续为 null。

## 检查

| 命令/证据 | 结果与范围 |
| --- | --- |
| make check | 527 passed，3 项既有弃用警告；format/lint/build/CLI 通过 |
| make demo | 模拟通过，无网络操作 |
| make test-configs | 11 项真实组件/接口检查通过，禁外网 disposable 容器 |
| PYTHONPATH=API_RELEASE python scripts/verify_subscription_overlay.py | 原安装源码 + 四文件 overlay，真实 SQLite schema 3 和 ASGI 回归通过；外部请求 0、设置完成后的 dataplane 调用 0 |
| 更新前原手机与 JXY HTTPS 下载 | 均 200、证书校验开启、禁环境代理与重定向 |
| make test-lab | 用户暂缓，未执行、退出码 null |

最终软件命令使用 Python 3.12.12、uv 0.9.7、Ruff 0.14.2、pytest 8.4.2；真实核心为 Mihomo 1.19.32、sing-box 1.14.3。结构化结果见 [results.json](results.json)。原配置、token/URL、密码和备份只保存在 Git 外私有目录，不进入此报告。

## 发布边界

新发布脚本先逐文件核对基线，备份当前数据库/API drop-in/安装清单，只重启 API；检查原包、代理/VPN/agent/usage/Nginx PID 与启动时刻、网络配置、设备/订阅/凭据身份、发布 revision 和 schema 不变。失败只回退 API drop-in 与名称键，不恢复旧数据库。候选包已上传；服务器端软件检查未返回结果后，直连 SSH、经已知日本服务器 SSH 管理中转及 HTTPS 均超时。未执行 `--apply`，API 原配置与 schema 未经本次切换，不记为生产完成。已请求停止本次临时软件检查，但该请求是否执行也未确认；失联原因尚不能归因。日本仅用于 SSH 管理中转，没有配置或服务改动。

恢复访问后先只读检查本次临时检查进程、资源/OOM 与既有日志，清理临时进程并重新核对当前基线；不启动故障或性能实验。兼容回归后续只在本地/隔离环境运行，已改用明确标注的少量合成规则夹具并加 30 秒 CPU 上限，本地通过。生产策略的完整规则仍由 make test-configs 验证，不能把合成夹具称为真实链路验收。待基线确认后才允许执行可回退 API 更新、LTY 设置和真实下载检查。

手机 GUI 导入、TUN 权限、实际出口/性能、完整故障/重启接受度仍需独立证据，核心 parser 与 HTTPS 下载不代替这些结论。
