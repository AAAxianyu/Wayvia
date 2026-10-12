# Wayvia 一次性双层链路实验

该实验使用 Ubuntu 24.04 容器里的六个 Linux network namespace 模拟终端、国内目标、阿里云入口、阿里云出口、日本与国外目标。容器拥有专用 network namespace；`--network none` 防止访问外部网络。privileged 权限只交给明确命名的一次性容器，没有 host network、宿主 `/proc`、`/sys` 或 Docker socket 挂载。

在仓库根目录执行：

```sh
uv sync --locked
make test-configs
make test-lab
make clean-lab
```

直接调用：

```sh
uv run python scripts/lab.py --preflight
uv run python scripts/test_configs.py
uv run python scripts/lab.py
uv run python scripts/lab.py --clean
```

默认运行重新核验下载文件 SHA256 并构建镜像。`--no-build` 只供已构建镜像的开发迭代。Docker 不可用、下载失败、缺少 root/capability、真实核心拒绝配置或任一数据路径断言失败都会返回非零。只清理 `org.wayvia.disposable=m4` 的 `wayvia-m4-disposable` 和 `org.wayvia.disposable=recovery` 的 `wayvia-recovery-disposable` 容器，不接触其他容器或网络。

`components.lock.json` 按 arm64、amd64 锁定官方发布资产；`python-runtime.lock.json` 固定从 uv.lock 选出的 Linux 测试依赖版本与 PyPI wheel 摘要。本次实测为 arm64；amd64 下载锁不等于该架构验收。

## 生成与运行边界

`scripts/lab_config.py` 使用正式的 WayviaConfig、ProfileRenderer、ServerRenderer 生成配置。实验的规则来源明确为受控地址分类；它没有假定 RFC1918 地址的地理归属。终端保留完整生成的 TUN、DNS、TLS 与分流规则，只额外添加本地 SOCKS 入站，供 SOCKS 协议探针使用。原生 TCP/UDP socket 探针验证真实 TUN 接管。

入口、业务桥接、日本代理与两个 OpenVPN 文件直接使用 ServerRenderer 产物。v2rayA 通过生产 V2rayAAdapter.configure_carrier 的正式 HTTP API 管理同版本 core。实验 CA、各服务证书、设备凭据与管理员密码均为每次随机生成，测试结束时随容器及临时目录销毁。仓库只保留脱敏的版本、路径、目标观察与执行日志。

入口到业务桥接使用 10.89.0.0/30。OpenVPN 客户端在 `wayvia-egress` 内先通过 10.89.0.1:1080 SOCKS 连接；阿里云 core 再通过 Trojan TLS 连接日本代理。日本代理仅允许 127.0.0.1:1194 TCP，OpenVPN 服务仅绑定该回环地址。外国业务从日本 TUN 解封装，经 NAT 到目标；国内业务从终端直接到国内目标。

实验先让隔离阿里云具有可用物理转发路径，测得未保护时国外目标看到阿里云出口；随后在 OpenVPN 启动前安装正式出口 drop 保护，确认同一物理请求被拒绝。分别终止 VPN 与日本代理后，SOCKS 与原生 TUN 请求必须失败，国内访问与管理物理路径继续成功；重启组件后必须恢复。设备删除通过真实入口重启生效，旧会话被终止，新连接被拒绝。

## 证据与限制

`docs/validation/latest-configs/config-result.json` 保存正式生成产物的真实解析器结果，以及恶意/不支持协议被拒绝的负向结果。OpenVPN 配置门禁记录真实配置加载和隔离进程启动；握手与业务验证由链路实验完成。

`docs/validation/latest-lab/lab-result.json` 保存各项断言、健康时 socket/route/interface 快照、OpenVPN TLS 验证记录、日本与国内目标源地址、故障与重连结果。`executor-result.json` 保存生产 LinuxExecutor 真实内核资源创建及逆序回退；容器默认路由前后比较相同。`recovery-result.json` 与 `recovery-environment.json` 保存真实systemd计时器、独立恢复进程与私有cgroup证据。

本实验日本节点使用carrier.lab，并由生产v2rayA适配器通过显式受控DNS解析；导入固定IP并保留TLS server_name。终端入口为数值IP。真实部署中的启动 DNS、浏览器/系统私人 DNS、Android/iPhone 导入与 Wi-Fi/蜂窝切换属于 M6；该实验不会把它们写成已验收。生产 OpenVPNAdapter 的真实隔离握手另有一项断言。数据链路容器未运行systemd；命令随后创建仅挂载容器私有cgroup2的systemd PID 1容器，验证控制进程退出后真实timer独立回退，以及新的恢复进程读取未确认journal回退。目标云主机整体重启仍需M6验收。TCP over TCP 的实时业务性能、吞吐与生产 SLA 需要真实网络测量。
