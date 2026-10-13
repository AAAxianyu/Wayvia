# M0–M5 软件交付验证与修复回归

更新：2026-10-12（America/Los_Angeles）。首次交付记录完整保留在下方，150 项 pytest、22 项路径以及只完成 stage 安装的结论均属于历史基线。后续审计重新打开了软件验收，见 [15 项问题及重现](review-2026-10-12/README.md)；最终关闭状态以 [实现状态](../implementation-status.md) 和 [本轮修复证据](fixes-2026-10-12/) 为准，不沿用旧记录的 complete 标志。

## 当前交付契约与验证范围

- `make check` 检查格式、静态规则、全部非特权回归、wheel/sdist 与 CLI。构建内容显式限制为项目软件、文档和测试；本轮计数与构建结果以新的完整命令日志为准，不能引用旧 150 项结果替代。
- `make demo` 仍明确使用 simulated 数据面，只证明临时数据库、设备、配置和 API 订阅流程。production 不回退到模拟。首次无入口时的明确 `INGRESS_NOT_DEPLOYED` 保存凭据并保持 pending，不新增阻止初次 apply 的入口阻断；其它同步失败继续尝试 fail-closed。
- 配置渲染、设备 active、网络事务确认与终端配置发布是不同状态。生产导出及订阅使用持久已发布快照；新配置只有两角色当前代次、相同 revision 和新鲜真实证据通过后，才由显式 `config publish` 替换。重启配置服务不丢旧快照；凭据轮换/撤销仍使用当前设备状态。[ADR 004](../adr/004-configuration-publication-and-fresh-evidence.md)说明契约及信任边界。
- 系统 CLI 使用 `sudo -u wayvia /opt/wayvia/.venv/bin/wayvia --system ...`，与安装的 API 共用目录、身份和 agent socket。`render --role ... --output ...`、`plan --role ... --output-file ...` 与操作手册一致。命令返回对象时还须检查 status，pending/degraded 不等于数据面已生效。
- `make test-configs` 继续由锁定真实核心验证生产生成物。`make test-lab` 已扩展为 23 项双层路径、7 项协议正负向、6 项 mihomo DNS、确定性 TUN 就绪反例、真实 executor、12 项真实内核冲突检查、独立 systemd 恢复及 23 项正式安装事务集成。各专项证据和边界见 [M4 记录](m4-controlled-lab.md)。

本轮正式安装事务已取得 [23/23 专项通过证据](fixes-2026-10-12/transaction-result.json)。实验通过安装器生成系统布局，在独立 systemd 容器中运行安装后的 venv、API、agent、动态网络单元和 monitor，覆盖 API/CLI 同库、设备/发布、入口实际 `443→8443→9443→8443→443` 与旧 monitor 交错、出口同配置三代次及回退、VPN/承载故障和认证恢复、独立恢复、同版本软件重装、轮换/撤销及旧计划不复活权限、两端卸载。它没有把独立恢复写作整机重启，也没有把同版本重装写作跨版本升级。真实内核冲突另有 [12/12 专项通过证据](fixes-2026-10-12/preflight-conflicts-result.json)。最终统一门禁的完整运行结果与专项结果分开，见本轮 `fixes-2026-10-12/commands.json`。安装器 stage、历史手工启动路径和非特权模拟回归不能单独证明正式安装闭环。

所有新增记录区分软件实现、隔离系统与链路、实网验收。真实云服务器、手机导入/切网、整机重启、性能与费用仍需 M6；OpenWrt 仍需 M7。当前仅操作授权的一次性 Linux 实验环境，不操作开发机网络或未指定的服务器。

---

以下原文属于首次交付历史。`software/software-result.json`、当时日志和审计重现保留用于比较；其中结论、哈希和计数不自动适用于当前代码。

# 历史基线：M0–M5 软件交付验证

日期：2026-10-12。环境：macOS arm64、Python3.12.12、uv0.9.7。Linux链路证据单独见 `m4-controlled-lab.md`，这不是实网验收记录。

`make check` 执行Ruff格式/静态检查、全部非特权pytest、wheel/sdist构建与CLI帮助。输出为 `software/make-check.txt`，最终计数为 `software/software-result.json`。锁定依赖的三条弃用提示不影响门禁。

`make demo` 用临时SQLite与明确simulated数据面完成设备、完整配置、API订阅/no-store、链接撤销与设备权限独立验证。输出为 `software/make-demo.txt`，不作为真实链路证据。production缺少agent时设备保持pending，不回退到fake。

最终wheel在新 `/tmp/wayvia-clean-env` 中安装：依赖经 `uv export --locked --no-dev --no-emit-project` 导出并以 `uv pip sync --require-hashes` 安装，再用 `uv pip install --no-deps` 安装wheel。已核查 `wayvia --version`=0.1.0，打包规则含7511个CN CIDR与110185个国内域名后缀。

安装器用真实已核验arm64官方archive和项目wheel在 `.cache/install-final` 生成stage：prepared、archive/binary摘要已记录、未启动服务或改网络。系统模式仅接受明确Ubuntu24.04 root目标，本轮未在开发机运行`--system`。安装/备份/权限/冲突/篡改/有限卸载有10项专门测试；canonical wheel名称保留，避免无效`wayvia.whl`安装路径。

`scripts/fetch_domestic_rules.py --source .cache/domestic-domains.conf` 固定原始SHA并忽略disabled注释，域名去冗余后与bundle摘要一致，不继承源DNS地址。来源与许可见ADR001、`docs/licenses/`和JSON资源。

M3测试覆盖逐步骤失败、timer安装失败零变更、超时、确认前重探、持久恢复、回退失败、旧计划/凭据、固定角色矩阵、TUN/脚本/任意路径拒绝、失效证书、Unix权限、组件版本、非零退出及超时。真实kernel执行、独立systemd恢复和业务数据链路另由M4验证。分层doctor将agent连通与实际链路健康分开。

运行方式见 `docs/operations.md`。本次确认软件交付和受控链路；真实服务器、手机、切网、吞吐/延迟/费用及完整主机重启继续按M6验收。
