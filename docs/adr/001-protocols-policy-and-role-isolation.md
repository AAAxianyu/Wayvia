# ADR 001 — 首个可验证协议组合与统一策略

状态：已采用。日期：2026-10-12。

终端接入及服务器承载采用 Trojan over TLS；TLS 身份验证开启。入口、日本代理和
终端采用 sing-box 1.14.3，终端同时生成 mihomo 1.19.32 完整配置。阿里云承载由
v2rayA 2.5.10 与同版本专用 v2raya_core 管理；`carrier.json` 是供该适配器转换的
逻辑 Trojan 清单，同时可由真实 sing-box 校验。普通 v2fly core 5.53.0 用于锁定规则
资产，不作为该版本 v2rayA 的运行核心。OpenVPN 采用 2.6 TCP
客户端，经 SOCKS5 承载。当前组合不接受 OpenVPN UDP 配置；这与支持隧道内部 UDP
业务是两件事。IPv6 国内和本地直连保留，国外 IPv6 明确拒绝。sing-box 对未知域名
AAAA 返回空答案以便尝试 IPv4，mihomo 保留 AAAA 并通过顺序规则拒绝国外 IPv6。
这两份格式均须通过真实核心校验；手机端具体版本的导入和行为仍属于实网验收。

阿里云入口留在宿主，保障入口连接回程。业务送至 `wayvia-egress` 命名空间的
`10.89.0.2:1081` SOCKS bridge，bridge 与其 DNS 均绑定 `tun0`。OpenVPN 同处该
命名空间，底层连接只经过宿主 `10.89.0.1:1080` 的 carrier。carrier 再连接日本
Trojan TLS。阿里云 SOCKS 端口只允许本命名空间访问；日本 carrier 的路由只允许
目标 `127.0.0.1:1194/TCP`，随后拒绝所有其它目标。日本代理不提供通用出口。
命名空间内 nftables 保护以及宿主管理路径由网络事务模块管理，配置渲染不改变网络。

渲染输出按职责选择字段。终端只获得阿里云入口地址、终端自身密码、策略与 DNS；
carrier 密码仅写入服务器角色的对应核心配置；VPN 与 TLS 证书引用只生成受控目录
`/etc/wayvia/secrets` 中的固定文件名。渲染器不接受任意证书路径，也不复制设备字典
的额外字段。配置产物仍包含运行所需代理密码，须按凭据文件处理；清单不包含密码。
证书是否存在、权限、有效期及真实信任链须在组件校验和部署探针中验证。
v2rayA 启动解析适配目前支持数值地址的 UDP/TCP DNS；其 carrier 配置使用域名时，
其它启动解析协议会在渲染阶段返回明确的不支持错误。终端 DNS 可独立采用 TLS/HTTPS。

策略优先级为入口底层连接、本地域名/网段、国内域名/网段、显式修正规则、国外
IPv6/可选 UDP 阻断、默认代理。显式修正可直连或阻断；与国内/本地直连范围重叠
的阻断规则被拒绝，不提供覆盖国内直连的强制代理例外。域名按标签后缀匹配，避免
`evilbaidu.com` 匹配 `baidu.com`。域名和 IP 同时输入时严格按同一规则顺序解释。
未知域名经代理侧 DNS 解析，国内 IP 回退继续直连；已知国内/本地域名选择对应 DNS。

默认模板包含 APNIC 官方分配统计的 CN IPv4/IPv6 快照，序列号 20261012，共
7511 条合并 CIDR。源文件 SHA-256：
`50122adee6fe5f98bb1156c1cea09e06575bc11bdb05dd83824ac28cbf9fdcb9`。
规则 JSON 保存源摘要、源 URL、版本及规范内容摘要，避免无版本的在线隐式更新。
APNIC 表示首次分配国家，不能证明地址当前物理位置；运营者需记录 CDN、误分类和
特殊服务例外。另包含 dnsmasq-china-list 的真实国内域名快照，固定提交
`a66b6ffc158681a3be06c258893d58a4265b76e5`（2026-10-10），规范化并去除重复和
已被父后缀覆盖的记录后共 110185 条后缀。原始源 SHA-256：
`c030567d770eb94e8fd56e75392ebb10ff5d4c3cf6e79d903c546f575a0145e3`；规范规则摘要：
`0fe9acd1afee0fa6a736c06bcefa7f417f95d464ffcddc28f73f5d2445a4fdd6`。
该独立规则集通过可选 `policy.domestic_domain_rules_ref` 进入同一国内直连策略。
例如 `www.bilibili.com` 选择国内直连与国内 DNS，无须先用日本 DNS 解析。
源列表的 DNS 上游地址不被继承，解析器仍由 Wayvia DNS 配置单独选择。
上游以 `#` 注释停用的记录不被启用，未识别指令或摘要不符均中止导入。
这是一份有版本的社区分类库，仍需运营者维护误分类，不能保证所有域名的实时地域。
许可和作者声明见 [许可记录](../licenses/dnsmasq-china-list.txt)。

`RuleSetImporter.from_apnic` 校验预期源摘要并导入地址统计，`from_domains` 导入
一行一个后缀的域名文件，`from_dnsmasq` 严格导入 dnsmasq `server=/domain/address`
记录，仅提取域名意图。可用以下命令重建快照并复核规范内容摘要：

```sh
uv run python scripts/fetch_domestic_rules.py --output .cache/domestic-domains-reproduced.json
uv run python scripts/fetch_domestic_rules.py --source .cache/domestic-domains.conf \
  --output .cache/domestic-domains-reproduced.json
```

首条从固定提交下载并校验原始摘要；第二条对已保存的相同原始源离线重建。
模板入口域名、实际校园域名/公网网段和内部 DNS 必须按实际环境替换后部署。

参考来源：

- [sing-box Trojan](https://sing-box.sagernet.org/configuration/outbound/trojan/)
- [sing-box DNS 新服务器格式](https://sing-box.sagernet.org/configuration/dns/server/udp/)
- [sing-box DNS 规则动作](https://sing-box.sagernet.org/configuration/dns/rule_action/)
- [sing-box 路由动作](https://sing-box.sagernet.org/configuration/route/rule_action/)
- [sing-box Dial Fields](https://sing-box.sagernet.org/configuration/shared/dial/)
- [mihomo DNS](https://wiki.metacubex.one/en/config/dns/)
- [mihomo 规则与 IP 回退解析](https://wiki.metacubex.one/en/config/rules/)
- [OpenVPN 2.6 手册](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html)
- [APNIC delegated statistics](https://ftp.apnic.net/stats/apnic/delegated-apnic-latest)
- [固定国内域名源](https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/a66b6ffc158681a3be06c258893d58a4265b76e5/accelerated-domains.china.conf)
