# Native TUN 启动竞态定位与隔离验证

本次修复的是实验脚本的就绪条件。没有修改 sing-box，也不把 Linux 容器的结果当作手机、Windows 或实网验收。

## 根因与原始证据

旧实验先等本地 SOCKS 业务连接成功，随后立即创建原生 TCP socket。sing-box 的 SOCKS 入站可在 TUN 接口及策略路由完成初始化前工作，因此这个条件不足以证明原生流量可以进入 TUN。

- `original-failure.json` 保留审计时的首次失败：SOCKS 完整双层链路与国内直连成功，首次原生 TCP 超时。
- `source-failure.json` 是后续独立冷启动捕获。失败 socket 已选择物理源地址 `10.72.0.2`；超时后查询路由已指向 `wayvia-tun` 的 `172.28.0.1`，抓包没有目标 SYN。
- `netlink-failure.json` 给出时间闭环（日志为 UTC）：原生 TCP 在 `04:01:17.683181` 开始连接并选中 `10.72.0.2`。TUN 在 `04:01:18.005490` 才变为 UP，随后安装表 2022 和规则，晚约 322 ms。接口创建时仍为 DOWN。已创建 socket 的物理源地址不因后续规则安装而自动变为 TUN 源地址，终端保护规则把这些包丢弃，所以此次失败没有发生物理出口泄露。

这些文件仅保留相关阶段、终端网络状态与无秘密的终端/netlink/tcpdump 日志。临时 CA、私钥、密码和完整服务器配置未纳入版本库。

## 修复与确定性反例

`deploy/lab/run.py` 在原生探针前加入 `terminal_tun_kernel_ready`。它要求接口同时具有 UP、LOWER_UP 和预期地址，无指定源地址以及指定 TUN 源地址的 TCP 路由查询均选择 TUN，并通过仅执行 UDP connect（不发送数据）确认内核选择的源地址。等待有明确超时，进程退出立即失败。业务 TCP/UDP 探针仍各执行一次；没有给失败业务请求加重试。原生 TCP 的实际选源也单独记录并断言。

`tests/lab/tun_ready_probe.py` 使用真实隔离 netns、TUN 文件描述符、路由和规则，按阶段扣住安装进度。`readiness-stages.json` 显示以下阶段都没有放行：

1. 接口有地址但仍 DOWN。
2. 接口 UP 且表 2022 已有默认路由，但未安装策略规则。
3. 只安装 `from 0.0.0.0/32` 规则：首次查询已指向 TUN，源地址也选中 TUN，但以该源地址再次查询仍走物理接口。

补齐 TUN 源网段规则后门禁才成功；再次删除该规则后门禁超时并明确失败。这个反例证明只看接口存在、只看第一次 route get 或只做一次源地址查询仍不足够。

## 修复后结果与限制

`cold-starts-after.json` 保存 12 个独立容器冷启动的就绪检查、原生 TCP 和 UDP 结果；每次 TCP 实际源地址均为 `172.28.0.1`，目标见到日本 NAT 地址。冷启动采样在原生 UDP 成功后终止，不能代替完整故障注入。`full-lab-after.json` 为最终完整实验输出，23 项全部通过，包含后续 VPN/承载故障、重连、撤销和真实 OpenVPN 适配验证。该次启动实际经历 8 次内核就绪观测，等待 488 ms 后才允许创建原生 TCP socket；随后单次原生 TCP/UDP 均通过。

采样成功没有证明所有调度时序都不会出问题；确定性分阶段测试负责验证等待条件，完整实验负责验证条件满足后的实际链路。当前修复针对锁定 sing-box、Linux 和本实验拓扑；其他平台仍需独立验收。

复现确定性反例（从仓库根目录，镜像由 `make test-lab` 构建）：

```sh
mkdir -p .cache/tun-readiness-evidence
docker run --rm --label org.wayvia.disposable=tun-readiness \
  --network none --privileged \
  -v "$PWD/deploy/lab:/opt/wayvia-lab:ro" \
  -v "$PWD/tests/lab:/workspace/tests/lab:ro" \
  -v "$PWD/.cache/tun-readiness-evidence:/evidence" \
  -e WAYVIA_LAB_DISPOSABLE=1 --entrypoint python3 \
  wayvia-lab:locked /workspace/tests/lab/tun_ready_probe.py
```

完整回归运行 `make test-lab`。以上命令只操作显式标记的临时 Linux 容器，不操作开发机网络。
