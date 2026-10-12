# M0–M5 软件交付验证

日期：2026-10-12。环境：macOS arm64、Python3.12.12、uv0.9.7。Linux链路证据单独见 `m4-controlled-lab.md`，这不是实网验收记录。

`make check` 执行Ruff格式/静态检查、全部非特权pytest、wheel/sdist构建与CLI帮助。输出为 `software/make-check.txt`，最终计数为 `software/software-result.json`。锁定依赖的三条弃用提示不影响门禁。

`make demo` 用临时SQLite与明确simulated数据面完成设备、完整配置、API订阅/no-store、链接撤销与设备权限独立验证。输出为 `software/make-demo.txt`，不作为真实链路证据。production缺少agent时设备保持pending，不回退到fake。

最终wheel在新 `/tmp/wayvia-clean-env` 中安装：依赖经 `uv export --locked --no-dev --no-emit-project` 导出并以 `uv pip sync --require-hashes` 安装，再用 `uv pip install --no-deps` 安装wheel。已核查 `wayvia --version`=0.1.0，打包规则含7511个CN CIDR与110185个国内域名后缀。

安装器用真实已核验arm64官方archive和项目wheel在 `.cache/install-final` 生成stage：prepared、archive/binary摘要已记录、未启动服务或改网络。系统模式仅接受明确Ubuntu24.04 root目标，本轮未在开发机运行`--system`。安装/备份/权限/冲突/篡改/有限卸载有10项专门测试；canonical wheel名称保留，避免无效`wayvia.whl`安装路径。

`scripts/fetch_domestic_rules.py --source .cache/domestic-domains.conf` 固定原始SHA并忽略disabled注释，域名去冗余后与bundle摘要一致，不继承源DNS地址。来源与许可见ADR001、`docs/licenses/`和JSON资源。

M3测试覆盖逐步骤失败、timer安装失败零变更、超时、确认前重探、持久恢复、回退失败、旧计划/凭据、固定角色矩阵、TUN/脚本/任意路径拒绝、失效证书、Unix权限、组件版本、非零退出及超时。真实kernel执行、独立systemd恢复和业务数据链路另由M4验证。分层doctor将agent连通与实际链路健康分开。

运行方式见 `docs/operations.md`。本次确认软件交付和受控链路；真实服务器、手机、切网、吞吐/延迟/费用及完整主机重启继续按M6验收。
