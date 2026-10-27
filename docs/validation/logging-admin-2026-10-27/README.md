# 日志系统与管理员平台：仓库验收

2026-10-27，America/Los_Angeles。基于 `codex/probe-observer-evidence` 的 `c98141f83668182733e0989a7b0d54a461adff39`，保留进入任务前的限流、产品定义和其他工作区改动。新增功能尚未提交或推送，也未部署到服务器。

本轮完成[开发方案](../../../.scratch/logging-admin/spec.md)的仓库开发、软件验证及非故障的隔离正常路径。生产部署、真实用户流量采样、完整故障实验和原后台观察任务继续暂缓。所有新增测试和截图使用合成设备、测试凭据、保留地址及本地目标。

## 交付内容

- 独立、默认关闭的持久日志策略；严格事件字段、有界 SQLite/队列/spool、来源新鲜度、缺口、保留期和限额。
- Nginx 安全 JSON 到 Unix syslog，API 补充认证后的设备/订阅归属；请求去重并区分速率、并发和未知 429。原 URL、token、代理密码、配置正文和导入方式不变。
- sing-box 1.14.3/API 4 的独立持续连接流，记录可观测的来源/目标、TCP/UDP、字节与生命周期；不进入原 agent 串行处理，不替换旧累计量。
- 固定白名单的两端服务/SSH/主机指标与旧审计投影；有界批次、序号/epoch/摘要/确认、专用 SSH 强制命令与主机密钥校验。
- 私有管理员六页界面、一次性登录码、会话/CSRF/Host/Origin、固定管理动作及写前审计、会话绑定的共享脱敏/内部导出。
- 独立 systemd 账户、Unix peer credentials、只读 broker 和资源 slice；安装器只暂存单元，不启动采集，不迁移原控制库。

实现入口见 [observability](../../../src/wayvia/observability/commands.py)、[admin](../../../src/wayvia/admin/app.py)，设计约束见 [ADR 013](../../adr/013-private-traffic-metadata-and-admin.md)，操作步骤见[运维说明](../../logging-admin.md)。

## 门禁与环境

最终命令、退出码、版本、测试数、数据摘要及文件摘要见 [results.json](results.json)。完整可保存输出见 [check.txt](check.txt)、[demo.txt](demo.txt)、[test-configs.txt](test-configs.txt)。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| `make check` | 593 项通过，3 项原有弃用警告；格式、lint、wheel/sdist、CLI 均通过 | 仓库软件 |
| `make demo` | 退出 0 | 临时库和模拟数据面，不改网络 |
| `make test-configs` | 11 项通过 | 锁定真实组件及一次性 namespace 能力检查 |
| 新日志/管理测试 | 49 项通过，包含于 593 项 | 投影、存储、权限、生命周期、API 与原订阅兼容 |
| 原生核心专项 | 通过 | 两设备 TCP、UDP、长连接更新、关闭、IP-only、旧统计共存 |
| Nginx 专项 | 通过 | 正常下载/信息页、速率/并发拒绝、恢复、安全输出和性能对照 |
| Linux collector / broker | 通过 | 真 Unix 凭据、私有存储、5 设备 100 条记录、停用持久性 |
| 四进程资源专项 | 通过 | 实际 CLI 进程，合成 RPC/控制后端；10 秒短负载 |
| 桌面与手机网页 | 通过 | 合成任务、键盘取消、筛选、详情、导出与未知状态 |
| `make test-lab` | **未运行，退出码 null** | 按用户既有约束暂缓完整故障实验，不能算通过 |

本机 macOS 26.6.2 arm64，Python 3.12.12、uv 0.9.7、SQLite 3.50.4、pytest 8.4.2。核心 sing-box 1.14.3、Mihomo 1.19.32、v2rayA 2.5.10、v2ray 5.53.0；依赖来自现有锁文件，没有更新生产组件版本。Nginx 容器固定 1.24.0 digest，见 results.json。Linux 依赖按 `uv.lock` 导出并带哈希安装，避免使用 macOS ABI。

原 `docs/validation/latest-configs/config-result.json` 在运行后恢复，新的 11 项结果单独保存为 [config-result.json](config-result.json)，不覆盖既有历史证据。

## 可复现命令

在仓库根目录运行，`UV` 指向可用 uv；本轮使用 `/private/tmp/wayvia-subscription-limit-tools/uv`。

```sh
make check UV="$UV"
make demo UV="$UV"
make test-configs UV="$UV"
"$UV" export --frozen --no-dev --no-emit-project --format requirements-txt --output-file .cache/logging-linux-requirements.txt
"$UV" pip install --target .cache/logging-linux --python-platform aarch64-manylinux_2_28 --python-version 3.12 --only-binary :all: --require-hashes -r .cache/logging-linux-requirements.txt
"$UV" run python scripts/test_logging_isolated.py --original .cache/subscription-limit-original.conf --dependencies .cache/logging-linux --output docs/validation/logging-admin-2026-10-27/isolated.json
```

`.cache/subscription-limit-original.conf` 是此前保存的原站点基线，不在仓库提交。缺失时应提供经过核实的本地候选文件；程序拒绝不认识的布局，不会读取服务器或自动修改 Nginx。测试把证书/订阅替换为合成值，禁止外网，Linux socket 位于专用 Docker volume，结束即删除。

核心增强、collector 与资源测试分别运行以下命令，把末尾脚本替换为 `logging_runtime_probe.py`、`logging_process_budget_probe.py`。资源专项容器限制可用 `512m`；程序自行检查新增进程合计 256 MiB 和单核 25% 预算。

```sh
docker run --rm --network none --label org.wayvia.disposable=logging \
  --memory 384m --cpus 1 --pids-limit 128 \
  -v "$PWD/src:/repo/src:ro" -v "$PWD/tests:/repo/tests:ro" \
  -v "$PWD/.cache/logging-linux:/python:ro" -e PYTHONPATH=/repo/src:/python \
  --entrypoint python3 wayvia-lab:locked /repo/tests/lab/logging_core_probe.py
```

这些测试不连接真实阿里云/日本服务器，不使用 host network，不修改开发机路由、VPN 或全局防火墙。完整 test-lab 没有运行。

## 正常路径与性能证据

[Nginx 对照](isolated.json)：约 9.2 MB 合成订阅正文摘要逐字节一致，未启用/启用各 3 轮、每轮 10 次。使用每组最差一轮的 p95：111.597 → 137.682 ms，增量 26.085 ms，小于 100 ms 门槛；三轮有效吞吐中位数未下降超过 5%。51 条安全事件只含允许字段，速率/并发原因均被观测；无原 token、完整路径或合成秘密标记。API 身份补充与乱序合并由软件集成测试另行覆盖，不能把 Nginx 合成后端当作生产 API。

[核心增强结果](core-extended.json)：每组 3 轮、每轮 20 个正常短请求，间隔 0.1 秒；每轮另有 32 MiB 传输。首字节测量包含 SOCKS/Trojan/TLS 建连与响应，是建连延迟的上界代理指标。两组最差轮 p95 为 25.802 / 8.693 ms；持续吞吐三轮中位比值为 1.235，满足不下降超过 5% 的门槛。容器本地回环的波动明显，**不能解读为性能提升或用户网速承诺**。真实网络路径、核心和采集全组合的长期资源表现尚未验收。

[collector 结果](runtime.json)：5 个设备、100 条事件、10 条/秒，全部可见并正确隔离为每设备 20 条，额外等待约 0.347 秒，峰值 RSS 34.4 MiB。真实不同 UID 请求被 broker 拒绝；数据库没有 WAL 无界增长，停用意图重开后仍保留。

[四进程结果](process-budget.json)：实际 collector/source/core/admin 常驻命令，合成 gRPC 与控制 API，100 条连接、10 条/秒并发管理查询，10.319 秒。四进程各峰值 RSS 相加为 234,373,120 字节（223.5 MiB），PSS 合计 174.4 MiB；CPU 0.68 秒，约单核 6.6%。这些是短期软件负载结果，不能替代生产主机剩余内存或长期容量验收。

## 修复与测量限制

- 首轮全量检查发现安装测试夹具没有新增单元，出现 15 项失败；补齐夹具并验证“只暂存、不初始化日志库”后，全量通过。
- 初始 Docker 共享 socket 放在 macOS bind mount，权限操作返回 EINVAL；改为 Docker 专用 volume，未放宽开发机权限。
- 首次订阅性能测试与全量 pytest 同时运行，未过延迟门槛；完善脚本，让门槛失败也保存测量值，再在无并发重负载的环境执行三轮完整对照。首次原始采样没有被旧脚本保存，不补造数字。
- 首次核心测试把不间隔的 64 KiB 请求总耗时换算为吞吐，得到约 0.801 的比值，见[保留记录](core-short-request-trial.txt)。该指标混合握手与短对象吞吐，不满足原定 10 新连接/秒的输入。修订夹具后分别测连接/首字节与 32 MiB 持续传输，所有最终轮次完整保留；没有将失败记录改成成功。
- Linux runtime 夹具从多线程 fork 改为 subprocess 指定 UID，复验通过；四进程夹具改用实际 Typer 入口，因为包没有 `__main__`。
- 界面复核补齐新签发订阅 ID 的最终审计、同时间段跳转、过滤保留与有条件刷新。原生事件/日志只能尽力收集，不能保证看见所有包或真实用户操作。

## 网页验收

[桌面截图](admin-desktop.jpg)与[390px 手机截图](admin-mobile.jpg)均为合成数据。桌面订阅页显示 6 条记录、速率及并发各 1 次；JXY 只显示所属 8 条连接。手机文档宽度 390、scrollWidth 390，表格内部滚动，不把文字压成不可读的竖列。

完成的任务包括：登录、查看 JXY 连接与未知历史桶、速率筛选、无身份请求、来源过期、查看证据并跳到同时间段服务/SSH 事件、标记知晓且继续提示未证明恢复、生成共享导出、轮换确认键盘取消。会话失效/他会话无法下载、恶意 Host/Origin/CSRF、私有接口不在订阅应用中、敏感值不进入共享导出由自动化集成测试覆盖。

最终全量检查后仅修正身份未知文案和退出/会话过期时的界面清理；Node 语法检查、浏览器登录退出复核及重新构建 wheel 通过。退出完成后内容区为空、未登录提示正确，见[退出截图](admin-logged-out.jpg)。wheel 内三个静态资源与源文件逐字节一致。

页面只使用文本 DOM，没有第三方脚本/字体、持久浏览器 token 或遥测。截图不代表线上 JXY/LTY 的真实活动。

## 未运行与未来启用条件

- 未连接生产、未部署新 wheel/单元/Nginx/API 参数、未启用生产被动日志；原订阅链接、用户客户端和原数据库未被本轮访问或修改。
- 未执行采集器/链路故障注入、服务器重启、完整 `make test-lab`，也未恢复旧观察任务、通知或 heartbeat。
- 对端 spool/ACK/重放/固定 SSH 参数通过软件测试，实际日本主机 SSH 账户、sudoers、journal 权限与密钥固定需要未来部署时验收。
- 无 Android/iPhone/Clash 真机 GUI、长时间线上压力和完整双层故障验收；正常容器路径不能替代这些项目。
- 部署前按[运维说明](../../logging-admin.md)备份并核对保护项。必须先保证 Nginx 覆盖可信请求 ID，再将公开流量切到信任该头的候选 API；新日志默认关闭，逐项启用，原业务无对新服务的依赖。
