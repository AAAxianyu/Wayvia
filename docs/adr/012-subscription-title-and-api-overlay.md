# ADR 012 — 独立订阅名称与 schema 3 API 兼容发布

Status: accepted
Date: 2026-10-26 America/Los_Angeles

用户在统一 Clash/Mihomo 软件适配后要求更新原有订阅，并交付名称为 LTY 的手机订阅。本次授权覆盖必要的订阅 API 更新与下载验证；不扩展到 13–18 的生产部署、网络代理/VPN 重载、实网测速/采样或故障实验，日本端和已停止的观察/heartbeat 不变。

订阅名称是下载展示元数据，与设备别名、设备凭据、网络配置 revision 和订阅 token 分离。`subscription name SUBSCRIPTION_ID NAME` 在现有 settings 表记录 `subscription_title:ID`，写审计，不触发 agent/sync。未设置时继续使用 `Wayvia · 设备别名`；设置后使用确切的清理后名称，例如 `LTY`，两种文件名响应头一致。控制符、路径字符与长度沿用安全清理；空名称拒绝。名称不会改变节点和策略组身份。

线上仍为 schema 3，当前主开发分支为 schema 4。直接升级整个运行时会越过本次订阅范围，并使旧 CLI/采集器无法打开新数据库。因此从当前安装的代码构建独立 API release，只覆盖 `client/__init__.py`、`client/names.py`、`client/legacy_api.py` 和 `render/__init__.py`。构建器固定校验原 API/control/storage/renderer 摘要和 ServerRenderer AST；清单逐文件保存当前基线及新产物摘要，保留线上已存在的其他补丁。API release 不替换原安装包。

兼容入口拒绝非 schema 3 runtime 与不存在/其他版本数据库；复用原 ControlService、设备导出、发布快照、统计和管理鉴权。只替换订阅读取路由并增加受同一 token/设备权限保护的 `/info`。保留旧 fetch 计数，不部署新历史表/采集器。主页地址使用明确的 HTTPS origin，保留非默认端口，不改反向代理。Mihomo 客户端格式统一，未据此宣称所有手机 GUI 已验收。

发布脚本先核对安装基线和候选清单，检查现有有效订阅及服务身份。应用前备份当前数据库、API drop-in 和安装清单；仅添加 API 专用 drop-in，重启 `wayvia-api.service`。检查其他服务 PID/启动时刻、网络配置、原包源码、设备/凭据/订阅身份、发布 revision 与 schema 不变。失败只回退名称键及 API drop-in，绝不恢复可能重新激活旧凭据的历史数据库。

下载验证使用既有 HTTPS 地址、正常证书校验和禁重定向，私人配置保存在 Git 外 0600 文件。真实核心 parser 在禁外网的一次性容器中检查，不接管宿主网络，不执行节点测速。原手机地址和凭据保留，JXY 展示名继续从原设备别名生成。软件、订阅服务交付和真实手机接管/全链路接受度分开记录。
