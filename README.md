# Wayvia 途序

智能分流代理网关。国内服务直连，海外服务按需代理，手机、电脑与支持的路由器共用自有出口。

## 文档

| 文档 | 内容 |
| --- | --- |
| [产品介绍](docs/product-overview.md) | 产品功能、使用方式与适用场景 |
| [设计方案](docs/design.md) | 系统架构、分流策略、组件职责与验收标准 |
| [产品开发文档](docs/development.md) | 工程结构、模块契约、里程碑与完成标准 |
| [开发 Goal 指令](docs/development-goal.md) | 可复制到 Codex 的持续开发目标 |

## 连接路径

- 国内与本地资源：通过终端当前网络直连。
- 海外服务：通过阿里云入口接入，经代理承载的 OpenVPN 链路连接自有日本出口。

日本服务器同时运行代理服务端与 OpenVPN 服务端，终端使用兼容客户端或支持的路由器接入。

## 软件运行

Python 3.12，使用 `uv==0.9.7` 安装锁定依赖：

```sh
uv sync --locked
uv run wayvia --help
uv run wayvia init
uv run wayvia validate .wayvia/config.yaml
uv run wayvia policy explain baidu.com
make check
make demo
make test-configs
make test-lab
```

`make demo` 在临时 SQLite 中运行明确标记为 simulated 的控制面闭环；`make demo-live` 启动可交互的本地演示 API。生产模式默认通过受限 Unix socket 请求真实入口变更，缺少 agent 时设备保持 pending，不签发可用订阅。

真实组件校验与链路实验使用专属 Docker 容器，不接管开发机网络。安装、证书、代理与 VPN 运维见 [操作手册](docs/operations.md)，[实现状态](docs/implementation-status.md) 和 [兼容清单](docs/compatibility.md) 分别记录软件、隔离链路与实网验收。
