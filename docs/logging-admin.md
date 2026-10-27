# 日志与管理员平台

本功能供单一管理员使用。家人朋友继续导入现有链接。部署前不需要修改或重新签发订阅；新增平台不会自行采集生产数据。

## 能看什么

- 订阅请求：时间、来源 IP、下载/信息/未知路径类别、状态、耗时，区分速率限制、并发限制、未知原因 429；认证成功后补设备和订阅 ID。
- 设备：已有累计量和分钟/小时历史、按凭据归属的 TCP/可观测 UDP 连接目标、端口、起止和明细字节。只有 IP 就只显示 IP，不反推域名。
- 两端：白名单 Wayvia 服务、SSH 成功/失败、CPU/内存/磁盘、已有结构化恢复/保护事件，传输水位和缺口。
- 管理：登录、敏感查询、设备与订阅操作、策略、告警确认和导出。提交成功与数据面已生效分别显示。

不保存完整 URI、参数、Cookie、Authorization、请求/响应正文、代理密码或订阅 token。HTTPS 正文、国内直连流量、用户真正点了哪个视频或发了什么消息不可见。凭据被多人共用时，只能看到这个凭据的活动。

## 运行结构与权限

| 进程 | 身份与输入 | 输出 / 权限 |
| --- | --- | --- |
| `wayvia-log.service` | `wayvia-observer`；固定本机 producer UID | 私有日志库、队列、Unix query broker；无控制 bearer 或核心秘密 |
| `wayvia-log-source.service` | `wayvia-observer` + `systemd-journal`，固定单位白名单 | 先投影、再写 source spool；不保存原 MESSAGE |
| `wayvia-log-core.service` | root，只读既有核心 API secret 与控制库固定列 | 原生持续流与旧审计投影到本地 socket；不调用核心写 RPC |
| `wayvia-admin.service` | 独立 `wayvia-admin`，systemd `LoadCredential` 传入控制 bearer | 回环 8790、私有会话/审计库；经 broker 查日志，不读取 root 核心秘密 |
| 日本批次导出 | 独立受限 SSH 身份 → 固定 sudo 命令以 observer 读取投影 spool | 只允许 `wayvia-log-relay-v1`，不能选路径、账户、URL、SQL 或 shell |

Unix socket 可达不代表授权：collector 验证 `SCM_CREDENTIALS`，broker 验证 `SO_PEERCRED`。Linux 不支持这些能力会明确失败。根账号对 broker 仅有策略读权限；管理员 UID 才能查询/修改策略。日志目录和数据库是 0700/0600，socket 目录 0755、socket 0666 以便指定生产者到达，实际授权由内核凭据和来源绑定执行。

## 本地软件检查

```sh
make check UV=/path/to/uv
make demo UV=/path/to/uv
make test-configs UV=/path/to/uv
uv run python scripts/admin_demo.py --directory .cache/admin-demo
```

演示只使用合成 JXY/LTY 和保留测试地址，监听 `127.0.0.1:18790`；终端打印 60 秒登录码。过期后可用 `wayvia admin login-code .cache/admin-demo/admin` 生成新码。不要将演示服务公开。

专项容器验证命令见[本次验收](validation/logging-admin-2026-10-27/README.md)。Linux 依赖从 `uv.lock` 导出，使用哈希校验安装到独立缓存，不复用开发机 ABI。完整 `make test-lab` 包含故障实验，当前仍暂缓；环境缺失应报告失败，不宣称跳过即通过。

## 未来部署步骤（本次未执行）

部署前确认授权、备份和资源。原控制库保持 schema 4；用 SQLite backup API 保留一致副本。原订阅的哈希、凭据引用、撤销、发布 revision、累计量与网络配置作为保护项，比较结果不输出秘密。现有安装器会暂存新 wheel/source/单元，**不启动服务、不创建启用策略**。不要因为添加单元就恢复任何旧诊断或定时任务。

1. 为新进程准备独立的 `wayvia-observer`、`wayvia-admin` 系统账户与 0700 数据目录 `/var/lib/wayvia-observer`、`/var/lib/wayvia-admin`；账户不加入原 `wayvia` 组。observer 仅获得读取 journal 所需组权限。先确认没有同名外部账户，不能接管。
2. 以 observer 运行 `wayvia logs init /var/lib/wayvia-observer`，以 admin 运行 `wayvia admin init /var/lib/wayvia-admin`。策略默认 `desired_enabled=false`。将[示例配置](../deploy/examples/log-collector.json)保存为 observer 拥有的 0600 `/var/lib/wayvia-observer/collector.json`，按实际 Nginx worker 账户核对 `nginx_user`，不要猜 UID。
3. `/etc/wayvia/log-source.env` 使用本机角色固定值：入口 `WAYVIA_LOG_HOST=ali`、`WAYVIA_LOG_ROLE=ingress`；日本为 `jp`/`exit`。环境文件由 root 控制，不存秘密。确认各 unit 的 systemd 用户、credential 支持、访问权限与 slice 预算。可先启动日志 broker 和管理员服务检查“未启用”。
4. 管理端使用 SSH 本地转发：`ssh -N -L 8790:127.0.0.1:8790 <管理入口>`。本机打开 `http://127.0.0.1:8790/admin`。在服务器以 admin 身份执行 `wayvia admin login-code /var/lib/wayvia-admin`，手动输入登录码；不放 URL、聊天记录或浏览器持久存储。
5. 单独启用连接和本机系统来源，再在私有平台启用被动策略。核心 API 必须已经存在且锁定 1.14.3/API 4；不存在时停止该阶段，不为日志擅自重启核心。
6. 先在未接公网流量的候选端口准备订阅 API，显式加 `--log-socket /run/wayvia-log/events.sock --trusted-log-proxy --log-host-id ali`。保留原 loopback、数据路径、秘密、订阅路由与参数；只验证候选，不在这一步切换公开流量。不开启 uvicorn access log。
7. 用 `wayvia logs render-nginx <原站点文件> <候选文件>` 生成增量。只支持预期的单一 Wayvia server、明确代理头和原 `access_log off`/`error_log /dev/null` 基线，不匹配就拒绝。核对变更只有固定安全格式、Unix syslog 和覆盖入站的可信请求 ID；原 5r/m、burst 5、全局并发 2、TLS/压缩/缓存和公开管理路径拒绝不变。`nginx -t` 成功后才平滑重载，保留原站点完整备份。必须先确认 Nginx 已覆盖客户端传入的 X-Wayvia-Request-ID，再把公开订阅流量切到信任该头的候选 API；不能让旧代理与信任新请求头的 API 组合对外运行。候选就绪、平滑切换和请求排空沿用原流程。
8. 日本部署被动 source 与独立策略，配置下述受限传输。最后只进行另行授权的少量正常订阅检查，记录内容摘要、配置保护项和当前链路状态。生产压测、超限测试、故障注入和真实客户端 GUI 不由软件测试替代。

### 日本受限传输

采集文件属于 observer。单独建立锁定密码的 `wayvia-transfer` 账户，只用于 SSH 强制命令，登录 shell 需能执行该固定命令；不要给它通用 sudo。受管公钥放在不可由该账户改写的 authorized_keys，前缀：

```text
restrict,command="/opt/wayvia/.venv/bin/wayvia logs relay-ssh" ssh-ed25519 <专用公钥>
```

Wayvia 专用 sudoers 条目只允许一条完整命令：

```text
wayvia-transfer ALL=(wayvia-observer) NOPASSWD: /opt/wayvia/.venv/bin/wayvia logs relay /var/lib/wayvia-observer jp exit
```

用 `visudo -cf` 验证该独立条目；不更改全局 SSH/防火墙策略。原始 SSH 命令必须精确为 `wayvia-log-relay-v1`，否则 helper 拒绝。sudo 后仍只有 observer 权限，stdin 只接受有界 ACK。入口的专用私钥与 known_hosts 文件置于 observer 的 0700 目录，文件 0600；显式固定服务器主机密钥，不能接受未核实新密钥。

collector 配置可增加 `peer`：

```json
{"host_id":"jp","ssh":{"host":"<已核实的主机>","port":22,"user":"wayvia-transfer","identity":"/var/lib/wayvia-observer/ssh/identity","known_hosts":"/var/lib/wayvia-observer/ssh/known_hosts"}}
```

这是配置片段，替换尖括号内容后合并到 `collector.json`。不填写或不可用时，页面明确显示日本来源不可用。拉取工作在独立线程，一轮最多 12 批；批次最多 200 条/900 KB，有摘要、epoch、序号和确认，提交后才 ACK；重放不重复计数。对端禁用时不输出批次。不得用原 root 密码或任意远程命令替代。

## 保留、容量和缺测

默认连接 7 天、订阅/SSH 14 天、系统 30 天、审计 90 天。请求无目标日汇总保留 90 天；旧流量历史仍使用原分钟 30 天/小时 365 天。缩短期限立即影响查询，后台清理过期内容；SQLite DELETE journal + secure_delete，物理文件可保持已分配大小并复用，不能将其误判为未清理。

单事件 ≤4 KiB，队列 ≤2048 条/约 8 MiB；日志数据 ≤256 MiB，预留同等 rollback journal；本机 spool 48 MiB、source spool 16 MiB，分别最多 512 批、最长 24 小时，超限丢最旧并报告缺口。管理库 ≤32 MiB及同等 journal，导出总计 ≤128 MiB。入口新增磁盘预算 1 GiB。日本只运行 source/spool 时占用受 16 MiB spool 和日志默认空库约束，预留 128 MiB；不启用入口 collector 或管理员服务。

磁盘空闲须至少达到 2 GiB 和分区 10% 的较大值，否则停止新增日志并报告降级。管理员可以缩短期限；不默认清空原统计。查询最多 2 并发、约 2 秒 SQL 时间、100 条默认/500 条上限，明细时间窗 ≤24 小时，游标绑定筛选。共享导出最多 10,000 条/32 MiB，超过会明确截断，24 小时到期或会话撤销后不可下载。

长连接跨明细保留窗口后只显示新窗口内观测差值，旧起点清空；超过 20 秒没有新连接证据，原活动记录展示“状态未知/缺少终点”，不假称仍在线。重新连接、核心 epoch 变化、journal 缺口、来源过期和传输序号缺口不补造数据。Nginx syslog 自身无法证明完全无丢失，界面计数始终标注为“已记录”。

## 日常操作与排障

- 日志策略启停：平台“日志设置”，或以 observer 执行 `wayvia logs policy /var/lib/wayvia-observer --enabled` / `--no-enabled`。这是持久意图；停止新采集仍可查保留期内旧记录，不停止代理。
- 查 JXY：设备与订阅 → JXY → 连接目标/流量历史/订阅记录。共享凭据不是物理设备标识，连接明细不代表正文。
- 查限流：订阅请求 → 时间范围/来源/HTTP 429/速率或并发。认证前未知归属正常，不从来源 IP 推定某个人。
- 查故障：总览先区分新鲜链路证据与观测状态，再用同时间范围查看服务器事件、设备连接和原证据。告警“已知晓”不代表业务恢复。
- 导出：共享包每次换新代号；内部明细需确认。只会展示新签发链接，历史 hash-only token 不被恢复或拼造。
- 服务状态异常：检查自己的 `wayvia-log*`/`wayvia-admin` unit 和 `collector-state.json` 的固定计数/代码；不能临时打开 Nginx 原 URI access/error log。管理员服务不会展示原始堆栈或秘密。
- 存储不足：停止继续增加日志，缩短期限或以对应账户运行 `wayvia logs prune <日志目录>`；不要删除控制库、秘密或发布快照。源不可用和过期数据不能用零补齐。

## 停用与回退

先持久关闭策略，再停用新增日志/管理员单元和单独的日本传输。恢复当次备份的 Nginx 站点并验证/平滑重载；需要时切回原 API 进程参数和已验证旧 release，排空现有下载。原代理/VPN没有被日志功能修改，不因停用日志而重启它们。

保留或隔离新日志/会话库；原控制库不被新功能迁移，也不要用旧整库覆盖部署后的新撤销/轮换/累计量。回退后核对原链接、最新撤销、配置和累计量，不让旧权限复活。新单元没有要求业务单元依赖它们，日志重启不触发业务重启。
