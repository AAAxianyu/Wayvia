# 原有订阅更新与 LTY 手机交付

Status: done
Date: 2026-10-26 America/Los_Angeles
Branch: codex/probe-observer-evidence

用户要求更新原有订阅，提供可导入手机 Clash 的真实订阅链接，名称 LTY。自主处理常规问题；此次授权限于必要的订阅 API 生产更新和 HTTPS 下载校验。保持现有手机和 JXY 独立账号、token/URL/凭据；不更新数据面或 schema 4、不重启网络代理/VPN、不改变路由/防火墙、不启动采样/测速/故障测试/观察/heartbeat，不触碰日本服务器。

## 顺序与验收

1. 核对线上 schema、API 安装基线、有效账号/订阅与 HTTPS；从当前服务端私有交付资料恢复 URL，重新核验数据库 token hash、状态和有效期。
2. 实现独立名称元数据、CLI 和兼容 schema 3 的 API release，保留原始代码与统计服务；执行 make check/demo/test-configs 和实际旧基线的软件回归。
3. 备份当前数据库/API 配置，仅切换 API release 并设置 LTY；验证其他服务、网络配置、设备身份和原代码不变。
4. 真实 HTTPS 下载、策略/凭据比较、禁外网真实核心 parser，交付私人链接并记录脱敏证据。

完整 test-lab 继续按用户要求暂缓，退出码 null。URL/token、密码和配置正文不入 Git。手机实际导入、系统接管、实网性能及完整故障接受度另行验收。

## 完成结果

用户手动重启后恢复 SSH，API 专用发布成功；原手机订阅显示 LTY，原 URL/凭据与 schema 3 不变。软件、实际下载与真实核心校验均通过。OOM 与发布回退过程见[验证记录](../../docs/validation/subscription-lty-2026-10-26/README.md)，手机 GUI/接管与完整故障接受度继续分开验收。
