# LTY 原订阅更新与重启后检查

Date: 2026-10-26 America/Los_Angeles
Branch: codex/probe-observer-evidence
Status: subscription API updated; HTTPS and native core verified

用户要求更新原有订阅并交付 LTY 手机 Clash/Mihomo 链接。API 专用发布已完成，原手机及 JXY 的账号、URL、密码、有效期保留，线上数据库仍为 schema 3。手机标题为 LTY，电脑为 Wayvia · JXY；节点为 Wayvia · 主线路，24 小时更新，无默认周期测速。私人链接和配置正文不入 Git。

## 验证结果

- make check：530 passed，3 项既有弃用警告；format/lint/build/CLI 通过。
- make demo：模拟通过；make test-configs：11 项真实组件/接口检查通过。
- 原安装源码加四文件 API overlay 的 schema 3 回归在本地通过；少量合成规则夹具，CPU 上限 30 秒，外部请求 0。
- 手机/JXY 原 HTTPS URL 均下载 200，正常验证 TLS；信息页 200、保留非默认端口；公开管理路由 404。
- 两份实际下载配置与更新前比较：凭据/地址/TLS、分流、DNS、TUN、IPv6 和其他策略相同，只有既定展示/选择/手动延迟字段变化。
- 两份实际下载配置均通过 Mihomo 1.19.32 parser，运行在禁外网 disposable 容器，仅配置检查，不启动 TUN 或测速。
- 发布前后网络服务 PID/启动时刻、网络配置、原包代码、设备/订阅身份、发布 revision 和 schema 校验通过；仅切换 API。
- API MemoryHigh 384 MiB、MemoryMax 512 MiB；两次实际下载后峰值 261865472 字节，OOM/重启计数 0。
- 完整 make test-lab、实网测速/采样、故障实验继续按用户要求暂缓，退出码 null。手机 GUI/权限与实际接管另行验收。

结构化结果、产物摘要与接受度边界见 [results.json](results.json)。原始软件门禁使用 Python 3.12.12、uv 0.9.7、Ruff 0.14.2、pytest 8.4.2；真实核心 Mihomo 1.19.32、sing-box 1.14.3。API release 清单摘要 cfb2e2e3ecf7bacc9bc32aa9ed36de26aab8951f6085d9dd58086ab69be53bd8，只覆盖 client/__init__.py、client/names.py、client/legacy_api.py、render/__init__.py；ServerRenderer AST 与原安装包保持不变。

## 本次资源事故与恢复

候选包于 08:03 UTC 上传并运行完整服务器端软件回归后，SSH/HTTPS 访问失联。上一 boot 的内核于 08:51:38 UTC 记录全局 OOM，杀掉 root Python PID 2450465，匿名 RSS 520148 KiB，属于 07:53:30 UTC 开始的本次 SSH session 18781；该机可用物理内存约 1.58 GiB，无 swap。结合会话、运行时间和负载，指向本次完整回归造成不适当的生产资源压力；这是执行位置选择不当，不能把本地可通过当作生产机有足够余量。内核证据没有完整进程 argv，具体归因属于相关性推断，不据此解释此前 JXY 的网络超时。

直连、经日本 SSH 管理中转、VNC 登录及现有云助手均未能取得进一步实例内结果；云助手只读命令发送失败。用户先要求不重启，随后自行于 09:37:10 UTC 重启入口，SSH 恢复，临时测试进程已不存在。代理/VPN/agent/usage/Nginx 随既有配置启动，数据库 quick_check 为 ok。Agent 未提交整机重启。此手动排障恢复不记为完整故障/重启测试通过。脱敏原始证据见 [oom-evidence.json](oom-evidence.json)。

完整回归后续只在本地/隔离环境运行；服务器上仅保留有界、无配置导出和无网络请求的服务账号模块加载检查（10 秒 CPU / 512 MiB 地址空间 / 15 秒 wall timeout）。旧重型检查脚本已换成小型夹具，不复制进 API runtime。

## 发布回退过程

第一次切换暴露私有 umask 下发布父目录不可遍历，模块加载失败；旧 API 已恢复，补上父目录显式权限、服务账号加载预检、权限回归和 StartLimit 回退恢复。第二次状态保护比较拒绝后自动回退，随后未发现持久差异，保留备份不伪造成功。第三次核对前后保护状态通过并提交，最终真实下载后复核仍一致。

当前备份在服务器 root-only 目录保存当前数据库、原 API drop-in、安装清单与快照。失败仅回退名称键/API 配置，从未恢复可能重新激活旧凭据的历史数据库。日本仅用于只读 SSH 管理中转，服务/配置未修改；旧观察和 heartbeat 未恢复。范围和发布方法见 [ADR 012](../../adr/012-subscription-title-and-api-overlay.md)。
