# 日志系统与管理员平台方案验证

日期：2026-10-27，America/Los_Angeles。范围：开发方案与既有软件基线检查。

用户要求先提供完整开发方案，不修改实现代码。主文档为[日志系统与管理员平台开发方案](../../../.scratch/logging-admin/spec.md)，覆盖采集范围、数据模型、生命周期、页面、权限、资源预算、8 阶段开发顺序、验收与保留数据的回退。方案中的能力和性能门槛是拟实现要求，不是已有功能或实测结果。

## 基线

- 分支：`codex/probe-observer-evidence`；HEAD：`c98141f83668182733e0989a7b0d54a461adff39`。
- 保留本轮前已有工作区改动，包括订阅限流实现和产品范围文档；没有提交、推送或部署。
- 环境：Darwin arm64、Python 3.12.12、uv 0.9.7、SQLite 3.50.4、pytest 8.4.2。
- 本轮对 `src/`、`tests/`、`scripts/`、`deploy/` 下 135 个非缓存文件进行内容指纹核对，用于确认没有修改现有实现或新增实现文件。

## 检查结果

| 项目 | 结果与证据 |
| --- | --- |
| 主方案本地链接、代码围栏、空白 | 检查通过；最终统计见 [results.json](results.json) |
| `make check` | 退出 0；544 项通过、3 项已有弃用警告，格式、lint、构建和 CLI 通过；见 [check.log](check.log) |
| `make demo` | 退出 0；临时数据库和模拟数据；见 [demo.log](demo.log) |
| `make test-configs` | 退出 0；11 项既有配置/核心检查；见 [test-configs.log](test-configs.log)及[结构化结果](config-result.json) |
| `make test-lab` | 按用户此前继续暂缓故障测试的要求未运行；退出码 null，不能记为通过 |
| 新日志与后台功能测试 | 尚无实现，未运行；验收要求已写入方案 |
| 生产检查、部署、流量采样或故障注入 | 本轮均未执行 |

执行软件门禁时显式指定 `UV=/private/tmp/wayvia-subscription-limit-tools/uv`。`make test-configs` 使用项目既有的一次性 Linux 容器流程，包含命名空间能力预检及禁外网配置验证，没有修改开发机路由。运行版本包括 sing-box 1.14.3、Mihomo 1.19.32、v2rayA/core 2.5.10、OpenVPN 2.6.19。检查产生的新配置报告保存在本目录，`latest-configs/config-result.json` 恢复为执行前内容，避免覆盖旧验收记录。

本次检查只说明当前软件基线通过。它不能证明尚未实现的日志和管理平台已可用、已满足资源预算或已对生产用户无影响。完整新功能验收应按方案第 13 节执行，并区分软件、隔离环境和真实部署结果。
