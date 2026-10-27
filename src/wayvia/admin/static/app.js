const $ = (id) => document.getElementById(id);
let csrf = "",
  page = "overview",
  selected = null,
  activeFilters = {},
  nextCursor = null;
const pendingActions = new Map();
let renderVersion = 0;
let refreshing = false,
  refreshView = null,
  filtersPinned = false;
const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const names = {
  overview: ["总览", "业务状态与观测完整性分开查看。"],
  devices: [
    "设备与订阅",
    "按设备凭据统计。共用同一凭据的客户端无法进一步区分。",
  ],
  requests: [
    "订阅请求",
    "查询入口请求、来源和限流原因。认证前拦截的请求归属未知，时间按浏览器时区显示。",
  ],
  events: [
    "服务器事件",
    "两端服务、SSH 认证和主机指标；时间接近不代表因果关系。",
  ],
  audit: ["操作记录", "提交、待生效与实际结果分别记录。"],
  settings: [
    "日志设置",
    "被动采集独立于已有诊断任务，不会自动恢复已停止的诊断。",
  ],
};
const labels = {
  unpublished: "尚未发布",
  published: "已发布",
  simulated: "模拟状态",
  sampled: "已采样",
  unobserved: "未观测",
  expired: "超出保留期",
  observed: "近期有记录",
  unavailable: "来源不可用",
  stale: "记录已过期",
  disabled: "未启用",
  unknown: "未知",
  degraded: "采集降级",
  active: "生效",
  pending: "待生效",
  revoked: "已撤销",
  closed: "已关闭",
  rate_limit: "速率限制",
  concurrency_limit: "并发限制",
  unknown_429: "429，原因未知",
  denied: "访问被拒绝",
  none: "无",
  upstream_error: "上游错误",
  client_closed: "客户端中止",
  credential_only: "设备凭据",
  success: "成功",
  submitted: "已提交",
  applied: "已应用",
  failed: "失败",
  reconnected: "重连缺口",
  journal_gap: "日志缺口",
  source_unavailable: "来源不可用",
  missing_start: "缺少起点",
  counter_reset: "计数重置",
  source_error: "采集异常",
  heartbeat: "心跳",
  host_metrics: "主机指标",
  ssh_accepted: "SSH 登录成功",
  ssh_failed: "SSH 登录失败",
  service_started: "服务启动",
  service_stopped: "服务停止",
  service_failed: "服务失败",
  management: "管理操作",
  gap: "观测缺口",
  connection: "代理连接",
  request: "入口请求",
  api: "API 身份补充",
  nginx: "订阅入口",
  connections: "代理连接",
  system: "系统事件",
  audit: "控制操作",
  transfer: "日志传输",
  ingress: "入口",
  exit: "出口",
  subscription_download: "订阅下载",
  subscription_info: "订阅信息",
  unknown_path: "未识别路径",
  management_denied: "管理路径请求",
  health: "健康检查",
  issued: "已签发",
  "logs.query": "查询日志",
  "device.add": "新增设备",
  "device.rotate": "轮换设备凭据",
  "device.revoke": "撤销设备",
  "subscription.issue": "签发订阅",
  "subscription.revoke": "撤销订阅",
  "session.login": "登录",
  "session.logout": "退出登录",
  "policy.update": "修改日志策略",
};
const label = (v) => labels[v] || v || "未知";
const when = (v) =>
  v ? new Date(v * 1000).toLocaleString("zh-CN", { hour12: false }) : "未知";
const bytes = (v) =>
  v == null
    ? "未知"
    : v < 1024
      ? `${v} B`
      : v < 1048576
        ? `${(v / 1024).toFixed(1)} KB`
        : v < 1073741824
          ? `${(v / 1048576).toFixed(1)} MB`
          : `${(v / 1073741824).toFixed(2)} GB`;
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = false;
}
function clearNotice() {
  $("notice").hidden = true;
}
function button(text, fn, cls) {
  const b = el("button", text, cls);
  b.type = "button";
  b.onclick = async () => {
    b.disabled = true;
    try {
      await fn();
    } catch (e) {
      notice(e.message);
    } finally {
      b.disabled = false;
    }
  };
  return b;
}
async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const r = await fetch("/admin" + path, {
    method,
    credentials: "same-origin",
    headers,
    body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
  });
  let data;
  try {
    data = await r.json();
  } catch {
    throw new Error("服务未返回有效数据，请稍后重试。");
  }
  if (!r.ok) {
    if (r.status === 401) {
      endSession();
    }
    const messages = {
      ADMIN_LOGIN_INVALID: "登录码无效或已过期，请重新生成。",
      ADMIN_LOGIN_LIMIT: "尝试过于频繁，请 1 分钟后重试。",
      ADMIN_SESSION_EXPIRED: "登录已过期，请重新登录。",
      LOG_DISABLED: "日志采集尚未启用。",
      ADMIN_CSRF: "页面会话校验失败，请刷新后重试。",
      LOG_BROKER_UNAVAILABLE: "日志服务不可用，订阅服务不受此页面影响。",
      ADMIN_CONTROL_UNAVAILABLE: "控制服务不可用，请检查服务状态。",
      ADMIN_ACTION_CONFLICT: "操作编号已用于另一项变更，请刷新后重试。",
    };
    throw new Error(
      messages[data.code] ||
        `操作未完成（${data.code || r.status}），请检查来源状态后重试。`,
    );
  }
  return data;
}
function badge(value) {
  return el(
    "span",
    label(value),
    "status " +
      (["stale", "degraded", "failed", "unavailable", "unknown"].includes(value)
        ? "warn"
        : ["active", "success", "applied"].includes(value)
          ? "good"
          : ""),
  );
}
function table(parent, columns, rows) {
  if (!rows.length) {
    parent.append(
      el(
        "p",
        "当前筛选没有记录。请同时检查来源覆盖和保留期限；没有记录不代表没有活动。",
        "empty",
      ),
    );
    return;
  }
  const wrap = el("div", null, "table-wrap"),
    t = el("table"),
    head = el("thead"),
    tr = el("tr");
  for (const [title] of columns) {
    const th = el("th", title);
    th.scope = "col";
    tr.append(th);
  }
  head.append(tr);
  t.append(head);
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const [, get] of columns) {
      const td = el("td"),
        v = get(row);
      if (v instanceof Node) td.append(v);
      else td.textContent = v ?? "未知";
      tr.append(td);
    }
    body.append(tr);
  }
  t.append(body);
  wrap.tabIndex = 0;
  wrap.setAttribute("aria-label", "数据表格，可横向滚动");
  wrap.append(t);
  parent.append(wrap);
}
function detail(parent, title, value) {
  parent.append(el("h2", title));
  const dl = el("dl");
  for (const [k, v] of Object.entries(value)) {
    dl.append(el("dt", k), el("dd", v));
  }
  parent.append(dl);
}
function raw(parent, title, data) {
  const d = el("details");
  d.append(el("summary", title), el("pre", JSON.stringify(data, null, 2)));
  parent.append(d);
}
async function confirm(title, message) {
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  const d = $("confirm");
  d.returnValue = "cancel";
  d.showModal();
  return new Promise((resolve) =>
    d.addEventListener("close", () => resolve(d.returnValue === "confirm"), {
      once: true,
    }),
  );
}
async function show(which = page) {
  const version = ++renderVersion;
  refreshView = null;
  page = which;
  clearNotice();
  selected = which === "devices" ? selected : null;
  document
    .querySelectorAll("nav button")
    .forEach((b) =>
      b.setAttribute(
        "aria-current",
        b.dataset.page === which ? "page" : "false",
      ),
    );
  $("page-title").textContent = names[which][0];
  $("page-intro").textContent =
    `${names[which][1]} 时间：${timeZone}；最近读取 ${new Date().toLocaleTimeString("zh-CN")}。`;
  const view = $("view");
  view.replaceChildren(el("p", "正在读取…", "muted"));
  try {
    const fragment = document.createDocumentFragment();
    await {
      overview,
      devices,
      requests: () => logPage(fragment, "request"),
      events: () => logPage(fragment),
      audit: () => auditPage(fragment),
      settings,
    }[which](fragment);
    if (version === renderVersion) view.replaceChildren(fragment);
  } catch (e) {
    if (version !== renderVersion) return;
    view.replaceChildren(el("p", e.message, "empty"));
    notice(e.message);
  }
}
async function overview(parent) {
  const data = await api("/api/overview");
  detail(parent, "连接状态", {
    业务健康:
      (data.control.business_health || [])
        .map((r) => `${label(r.role)}：${label(r.status)}`)
        .join("；") || "未知",
    配置发布: label(data.control.publication?.status),
    设备数量: data.control.devices?.length ?? "未知",
    查询时间: when(data.as_of),
  });
  raw(
    parent,
    "查看原有链路状态与证据",
    data.control.dataplane ?? { status: "unknown" },
  );
  parent.append(el("h2", "日志覆盖"));
  sourceTable(parent, data.sources);
  parent.append(el("p", data.visibility, "coverage"));
  parent.append(el("h2", "最近 7 天入口请求"));
  table(
    parent,
    [
      [
        "UTC 日期",
        (r) => new Date(r.day * 86400000).toISOString().slice(0, 10),
      ],
      ["状态码", (r) => r.status],
      ["原因", (r) => label(r.reason)],
      ["已记录次数", (r) => r.count],
    ],
    data.request_summary?.items || [],
  );
  parent.append(
    el(
      "p",
      "按完整 UTC 日期汇总已记录请求。日志缺口期间的请求量未知。",
      "coverage",
    ),
  );
  parent.append(el("h2", "待处理事项"));
  if (!data.alerts.length)
    parent.append(
      el(
        "p",
        data.policy?.desired_enabled
          ? "当前没有待处理的来源告警。"
          : "采集未启用；当前没有日志覆盖。",
        "empty",
      ),
    );
  for (const item of data.alerts) {
    const section = el("div", null, "alert");
    section.append(
      el(
        "strong",
        `${item.host} · ${label(item.source)} · ${label(item.reason)}`,
      ),
      el("p", item.next_step),
    );
    section.append(
      item.acknowledged_at
        ? el("p", `已知晓 ${when(item.acknowledged_at)}。这不表示业务已恢复。`)
        : button("标记已知晓", async () => {
            await api(`/api/alerts/${item.id}/ack`, { method: "POST" });
            await show();
          }),
    );
    parent.append(section);
  }
}
function sourceTable(parent, sources) {
  table(
    parent,
    [
      ["服务器", (r) => `${r.host} · ${label(r.role)}`],
      ["来源", (r) => label(r.source)],
      ["状态", (r) => badge(r.state)],
      ["最后记录", (r) => when(r.last_at)],
      ["最后接收", (r) => when(r.received)],
      ["说明", (r) => label(r.code)],
    ],
    sources,
  );
}
function filterForm(parent, run, { kind, device } = {}) {
  const form = el("form", null, "filters");
  const inputs = {};
  const now = new Date();
  const local = (d) =>
    new Date(d.getTime() - d.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 19);
  for (const [name, title, type, value] of [
    ["since", "开始时间", "datetime-local", local(new Date(now - 3600000))],
    ["until", "结束时间", "datetime-local", local(now)],
    ["source_ip", "来源 IP 或网段", "text", ""],
    ["target", "目标域名或 IP", "text", ""],
  ]) {
    if (kind === "audit" && type === "text") continue;
    if (name === "target" && kind !== "connection") continue;
    const l = el("label", title),
      i = el("input");
    i.type = type;
    if (type === "datetime-local") i.step = "1";
    i.name = name;
    i.value = value;
    if (filtersPinned && activeFilters[name] != null)
      i.value =
        type === "datetime-local"
          ? local(new Date(activeFilters[name] * 1000))
          : activeFilters[name];
    if (type === "text") i.maxLength = 253;
    l.append(i);
    inputs[name] = i;
    form.append(l);
  }
  const extra = {};
  for (const [key, title, options] of [
    [
      "role",
      "服务器角色",
      [
        ["", "全部"],
        ["ingress", "入口"],
        ["exit", "出口"],
      ],
    ],
    [
      "status",
      "HTTP 状态",
      [
        ["", "全部"],
        ["200", "200"],
        ["401", "401"],
        ["403", "403"],
        ["404", "404"],
        ["429", "429"],
        ["500", "500"],
        ["502", "502"],
        ["503", "503"],
      ],
    ],
  ]) {
    if (kind === "audit" || (key === "status" && kind !== "request")) continue;
    const l = el("label", title),
      i = el("select");
    for (const [v, t] of options) {
      const o = el("option", t);
      o.value = v;
      i.append(o);
    }
    extra[key] = i;
    if (filtersPinned && activeFilters[key] != null)
      i.value = activeFilters[key];
    l.append(i);
    form.append(l);
  }
  const reason = el("label", "结果"),
    select = el("select");
  const reasons =
    kind === "request"
      ? [
          ["", "全部"],
          ["rate_limit", "速率限制"],
          ["concurrency_limit", "并发限制"],
          ["unknown_429", "原因未知的 429"],
          ["denied", "访问被拒绝"],
        ]
      : kind === "connection"
        ? [
            ["", "全部"],
            ["missing_start", "缺少起点"],
            ["counter_reset", "计数重置"],
          ]
        : [
            ["", "全部"],
            ["source_unavailable", "来源不可用"],
            ["journal_gap", "日志缺口"],
            ["sequence_gap", "传输缺口"],
            ["storage_full", "容量不足"],
          ];
  for (const [v, t] of reasons) {
    const o = el("option", t);
    o.value = v;
    select.append(o);
  }
  reason.append(select);
  if (filtersPinned && activeFilters.kind === kind && activeFilters.code)
    select.value = activeFilters.code;
  if (kind !== "audit") form.append(reason);
  const submit = el("button", "查询");
  submit.type = "submit";
  form.append(submit);
  function values() {
    const filters = {};
    for (const [name, i] of Object.entries(inputs))
      if (i.value)
        filters[name] =
          i.type === "datetime-local"
            ? new Date(i.value).getTime() / 1000
            : i.value.trim();
    for (const [k, i] of Object.entries(extra))
      if (i.value) filters[k] = k === "status" ? Number(i.value) : i.value;
    if (select.value) filters.code = select.value;
    if (kind && kind !== "audit") filters.kind = kind;
    if (device) filters.device_id = device;
    if (
      !Number.isFinite(filters.since) ||
      !Number.isFinite(filters.until) ||
      filters.until <= filters.since ||
      filters.until - filters.since > 86400
    )
      throw new Error("请选择不超过 24 小时的有效时间范围。");
    return filters;
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try {
      filtersPinned = true;
      await run(values());
    } catch (e) {
      notice(e.message);
    } finally {
      submit.disabled = false;
    }
  };
  parent.append(form);
  return values;
}
async function logPage(parent, kind, device) {
  const results = el("div");
  const path =
    device && kind === "connection"
      ? `/api/devices/${device}/connections`
      : kind === "request"
        ? "/api/subscription-requests"
        : "/api/events";
  const get = filterForm(parent, (filters) => load(filters), { kind, device });
  parent.append(results);
  async function load(filters, cursor = null) {
    if (!kind) filters.category = "system";
    const data = await api(path, {
      method: "POST",
      body: { filters, cursor, limit: 100 },
    });
    activeFilters = filters;
    nextCursor = data.next_cursor;
    results.replaceChildren();
    const summary = el("div", null, "summary-line");
    summary.append(el("span", `已记录 ${data.total} 条`));
    if (kind === "request")
      summary.append(el("strong", `限流 ${data.limited} 次`));
    summary.append(el("span", `截至 ${when(data.as_of)}`));
    results.append(
      summary,
      el(
        "p",
        "计数仅覆盖已记录事件；连接字节是明细口径，不替代设备历史累计量。",
        "coverage",
      ),
    );
    const columns =
      kind === "connection"
        ? [
            ["时间", (r) => when(r.observed_at)],
            [
              "目标",
              (r) =>
                `${r.target_host || r.target_ip || "未知"}${r.target_port ? ":" + r.target_port : ""}`,
            ],
            ["来源", (r) => r.source_ip],
            ["协议", (r) => r.transport],
            [
              "上传 / 下载",
              (r) => `${bytes(r.upload_bytes)} / ${bytes(r.download_bytes)}`,
            ],
            ["状态", (r) => badge(r.lifecycle)],
            ["起止时间", (r) => `${when(r.created_at)} — ${when(r.closed_at)}`],
            ["完整性", (r) => label(r.code)],
            [
              "详情",
              (r) => {
                const d = el("details");
                d.append(
                  el("summary", "查看证据"),
                  el("pre", JSON.stringify(r, null, 2)),
                );
                d.append(
                  button("查看同时段事件", async () => {
                    const end = Math.min(
                      Date.now() / 1000,
                      Math.max(r.closed_at || 0, r.observed_at) + 300,
                    );
                    activeFilters = {
                      since: Math.max(
                        end - 86400,
                        (r.created_at || r.observed_at) - 300,
                      ),
                      until: end,
                    };
                    filtersPinned = true;
                    await show("events");
                  }),
                );
                return d;
              },
            ],
          ]
        : kind === "request"
          ? [
              ["时间", (r) => when(r.observed_at)],
              ["来源 IP", (r) => r.source_ip],
              ["请求", (r) => `${r.method || ""} ${label(r.route)}`],
              ["状态码", (r) => r.status],
              ["结果", (r) => label(r.code)],
              ["设备归属", (r) => r.device_id || "未知（未关联）"],
              [
                "耗时",
                (r) =>
                  r.duration_ms == null
                    ? "未知"
                    : r.duration_ms.toFixed(1) + " ms",
              ],
            ]
          : [
              ["时间", (r) => when(r.observed_at)],
              ["服务器", (r) => r.host_id],
              ["事件", (r) => label(r.kind)],
              ["服务 / 对象", (r) => r.unit || r.account || r.device_id || "—"],
              ["结果", (r) => label(r.code)],
              [
                "指标",
                (r) =>
                  r.kind === "host_metrics"
                    ? `CPU ${r.cpu_percent?.toFixed(1) ?? "未知"}% · 内存余量 ${bytes(r.memory_available)} · 磁盘余量 ${bytes(r.disk_free)}`
                    : r.source_ip || "—",
              ],
            ];
    table(results, columns, data.items);
    const actions = el("div", null, "actions");
    if (nextCursor)
      actions.append(
        button("下一页", () => {
          filtersPinned = true;
          return load(filters, data.next_cursor);
        }),
      );
    actions.append(
      button("导出共享脱敏包", () => exportLogs(filters, "shared")),
      button("导出内部明细", () => exportLogs(filters, "internal")),
    );
    results.append(actions);
    const coverage = el("details");
    coverage.append(el("summary", "查看来源覆盖与缺口"));
    sourceTable(coverage, data.coverage.sources);
    results.append(coverage);
  }
  await load(get());
  refreshView = async () => {
    const now = Date.now() / 1000;
    await load({ ...get(), since: now - 3600, until: now });
  };
}
async function exportLogs(filters, mode) {
  if (
    mode === "internal" &&
    !(await confirm(
      "导出内部明细",
      "内部包包含来源 IP、目标信息和设备归属。确认仅在管理员受控环境保存？",
    ))
  )
    return;
  const data = await api("/api/exports", {
    method: "POST",
    body: { filters, mode, confirmed: mode === "internal" },
  });
  const a = el(
    "a",
    `下载${mode === "shared" ? "脱敏" : "内部"}日志（24 小时有效${data.truncated ? "，已截断至 10,000 条" : ""}）`,
  );
  a.href = `/admin/api/exports/${data.id}/download`;
  a.download = "wayvia-logs.json";
  const panel = el("div", null, "actions");
  panel.append(
    a,
    button("删除导出包", async () => {
      await api(`/api/exports/${data.id}`, { method: "DELETE" });
      panel.remove();
      notice("导出包已删除。");
    }),
  );
  $("view").prepend(panel);
  a.focus();
}
async function devices(parent) {
  const data = await api("/api/devices");
  const actions = el("div", null, "actions");
  const name = el("input");
  name.placeholder = "新设备名称";
  name.setAttribute("aria-label", "新设备名称");
  name.maxLength = 100;
  const platform = el("select");
  platform.setAttribute("aria-label", "设备平台");
  for (const p of ["android", "ios", "windows", "macos", "linux"]) {
    const o = el("option", p);
    o.value = p;
    platform.append(o);
  }
  actions.append(
    name,
    platform,
    button("添加设备", async () => {
      if (!name.value.trim()) throw new Error("请填写设备名称。");
      await perform(
        "device.add",
        null,
        { name: name.value.trim(), platform: platform.value },
        `添加设备 ${name.value.trim()}？`,
      );
      await show();
    }),
  );
  parent.append(actions);
  table(
    parent,
    [
      [
        "设备",
        (r) =>
          button(r.name, async () => {
            selected = r.id;
            await show();
          }),
      ],
      ["状态", (r) => badge(r.status)],
      ["平台", (r) => r.platform],
      [
        "现有用量",
        (r) =>
          `${bytes(r.usage?.upload_bytes)} / ${bytes(r.usage?.download_bytes)}`,
      ],
      ["最近更新", (r) => when(r.updated_at)],
    ],
    data.devices,
  );
  if (!selected) return;
  const value = await api(`/api/devices/${selected}`);
  parent.append(el("h2", value.device.name));
  detail(parent, "设备信息", {
    "设备 ID": selected,
    归属范围: "同一凭据的所有客户端",
    状态: label(value.device.status),
  });
  const controls = el("div", null, "actions");
  controls.append(
    button("撤销设备", async () => {
      await perform(
        "device.revoke",
        selected,
        {},
        "撤销此设备后，其现有连接权限会按原控制流程被移除。继续？",
      );
      await show();
    }),
    button("轮换凭据", async () => {
      await perform(
        "device.rotate",
        selected,
        {},
        "轮换会改变代理凭据，已有客户端需要更新配置。继续？",
      );
      await show();
    }),
    button("签发新订阅", async () => {
      const result = await perform(
        "subscription.issue",
        null,
        { device_id: selected, format: "mihomo", lifetime: 2592000 },
        "为此设备新增一条 30 天有效的 Mihomo 订阅？原链接保持不变。",
      );
      if (result) {
        raw(
          parent,
          "新订阅（请立即保存；历史链接不会在平台重新展示）",
          result.result,
        );
      }
    }),
  );
  parent.append(controls);
  table(
    parent,
    [
      ["订阅 ID", (r) => r.id],
      ["格式", (r) => r.format],
      ["有效期", (r) => when(r.expires_at)],
      [
        "状态",
        (r) =>
          r.revoked_at
            ? "已撤销"
            : r.expires_at < Date.now() / 1000
              ? "已过期"
              : "有效",
      ],
      [
        "管理",
        (r) =>
          r.revoked_at
            ? "—"
            : button("撤销", async () => {
                await perform(
                  "subscription.revoke",
                  r.id,
                  {},
                  "撤销这条订阅链接？此操作不会更换其他链接。",
                );
                await show();
              }),
      ],
    ],
    value.subscriptions,
  );
  const tabs = el("div", null, "tabs"),
    content = el("div");
  let current = "connection";
  const choices = [
    ["connection", "连接目标"],
    ["history", "流量历史"],
    ["requests", "订阅记录"],
    ["events", "同时段事件"],
  ];
  async function tab(which) {
    current = which;
    for (const b of tabs.children)
      b.setAttribute("aria-selected", String(b.dataset.tab === current));
    content.replaceChildren();
    if (which === "history") {
      const form = el("div", null, "actions"),
        select = el("select");
      select.setAttribute("aria-label", "流量历史范围");
      for (const [v, t] of [
        ["3600", "最近 1 小时（分钟）"],
        ["86400", "最近 24 小时（小时）"],
        ["604800", "最近 7 天（小时）"],
        ["2592000", "最近 30 天（小时）"],
        ["31536000", "最近 365 天（小时）"],
      ]) {
        const o = el("option", t);
        o.value = v;
        select.append(o);
      }
      const output = el("div");
      async function historyLoad() {
        const until = Date.now() / 1000,
          span = Number(select.value),
          grain = span === 3600 ? "minute" : "hour";
        const history = await api(
          `/api/devices/${selected}/history?since=${until - span}&until=${until}&grain=${grain}`,
        );
        output.replaceChildren();
        table(
          output,
          [
            ["时间", (r) => when(r.bucket)],
            ["上传", (r) => bytes(r.upload_bytes)],
            ["下载", (r) => bytes(r.download_bytes)],
            ["采样状态", (r) => label(r.status)],
            ["覆盖", (r) => `${Math.round((r.coverage_fraction || 0) * 100)}%`],
          ],
          history.buckets || [],
        );
        raw(output, "查看统计口径和覆盖", history);
      }
      form.append(select, button("查询流量历史", historyLoad));
      content.append(form, output);
      await historyLoad();
    } else if (which === "connection")
      await logPage(content, "connection", selected);
    else if (which === "requests") await logPage(content, "request", selected);
    else await logPage(content);
  }
  for (const [key, text] of choices) {
    const b = button(text, () => tab(key));
    b.dataset.tab = key;
    b.setAttribute("role", "tab");
    tabs.append(b);
  }
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "设备记录");
  parent.append(tabs, content);
  await tab(current);
}
async function perform(action, object_id, payload, message) {
  if (!(await confirm("确认管理操作", message))) return null;
  const fingerprint = JSON.stringify([action, object_id, payload]);
  const key =
    pendingActions.get(fingerprint) || crypto.randomUUID().replaceAll("-", "");
  pendingActions.set(fingerprint, key);
  const result = await api("/api/actions", {
    method: "POST",
    body: { action, object_id, payload, key, confirmed: true },
  });
  pendingActions.delete(fingerprint);
  notice(`操作已提交：${label(result.audit_result)}。请核对设备最终状态。`);
  return result;
}
async function auditPage(parent) {
  const result = el("div");
  const get = filterForm(parent, load, { kind: "audit" });
  parent.append(result);
  async function load(filters) {
    const data = await api("/api/audit", {
      method: "POST",
      body: { filters, limit: 100 },
    });
    result.replaceChildren();
    result.append(el("h2", "平台操作"));
    table(
      result,
      [
        ["时间", (r) => when(r.at)],
        ["主体", () => "管理员"],
        ["动作", (r) => label(r.action)],
        ["对象", (r) => r.object || "—"],
        ["结果", (r) => badge(r.result)],
      ],
      data.admin.items,
    );
    result.append(el("h2", "原有控制记录"));
    table(
      result,
      [
        ["时间", (r) => when(r.observed_at)],
        ["动作", (r) => label(r.action)],
        ["对象", (r) => r.object_id],
        ["结果", (r) => badge(r.result)],
      ],
      data.control.items,
    );
    result.append(
      el("p", "每组最多显示 100 条；请缩小时间范围查看更早记录。", "coverage"),
    );
  }
  await load(get());
}
async function settings(parent) {
  const data = await api("/api/sources");
  sourceTable(parent, data.sources);
  const p = data.policy;
  detail(parent, "当前策略", {
    采集状态: p.desired_enabled ? "已启用" : "未启用",
    连接明细: `${p.connection_days} 天`,
    "入口请求 / SSH": `${p.request_days} 天`,
    系统事件: `${p.system_days} 天`,
    操作审计: `${p.audit_days} 天`,
    数据文件上限: bytes(p.max_data_bytes),
    磁盘余量底线: bytes(p.min_free_bytes),
    策略修订: p.revision,
  });
  parent.append(
    el(
      "p",
      "过期明细不会因为延长保留期而恢复。缺口与连接目标仅代表服务器实际观测范围；国内直连流量不可见。",
      "coverage",
    ),
  );
  const form = el("form", null, "filters");
  const inputs = {};
  for (const [key, title, max] of [
    ["connection_days", "连接明细天数", 30],
    ["request_days", "请求记录天数", 30],
    ["system_days", "系统事件天数", 90],
    ["audit_days", "审计天数", 365],
  ]) {
    const l = el("label", title),
      i = el("input");
    i.type = "number";
    i.min = 1;
    i.max = max;
    i.value = p[key];
    l.append(i);
    form.append(l);
    inputs[key] = i;
  }
  const submit = el("button", "保存保留期限");
  submit.type = "submit";
  form.append(submit);
  async function update(changes, message) {
    if (!(await confirm("修改日志策略", message))) return;
    await api("/api/policy", {
      method: "POST",
      body: {
        changes,
        expected_revision: p.revision,
        key: crypto.randomUUID().replaceAll("-", ""),
        confirmed: true,
      },
    });
    await show();
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await update(
        Object.fromEntries(
          Object.entries(inputs).map(([k, v]) => [k, Number(v.value)]),
        ),
        "缩短期限会让旧记录立即不可查询，并在下一次清理时删除。继续？",
      );
    } catch (e) {
      notice(e.message);
    }
  };
  parent.append(
    form,
    button(p.desired_enabled ? "停止被动采集" : "启用被动采集", () =>
      update(
        { desired_enabled: !p.desired_enabled },
        p.desired_enabled
          ? "停止后仍可查询保留期内的记录。不会停止代理服务。"
          : "启用本机已配置的被动来源。请确认已向使用者说明连接元数据记录范围。",
      ),
    ),
  );
}
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const b = e.currentTarget.querySelector("button");
  b.disabled = true;
  try {
    const data = await api("/session", {
      method: "POST",
      body: { code: $("login-code").value },
    });
    $("login-code").value = "";
    csrf = data.csrf;
    await ready();
  } catch (e) {
    notice(e.message);
  } finally {
    b.disabled = false;
  }
};
async function ready() {
  $("login").hidden = true;
  $("workspace").hidden = false;
  $("logout").hidden = false;
  $("session-state").textContent = "管理员 · 私有会话";
  await show();
}
function endSession() {
  csrf = "";
  renderVersion++;
  refreshView = null;
  filtersPinned = false;
  activeFilters = {};
  selected = null;
  page = "overview";
  pendingActions.clear();
  $("workspace").hidden = true;
  $("login").hidden = false;
  $("logout").hidden = true;
  $("session-state").textContent = "私有管理入口 · 未登录";
  $("view").replaceChildren();
  $("login-code").focus();
}
$("logout").onclick = async () => {
  try {
    await api("/session", { method: "DELETE" });
  } catch (e) {
    notice(`未能确认会话撤销：${e.message}`);
    return;
  }
  endSession();
  clearNotice();
};
$("refresh").onclick = () => {
  filtersPinned = false;
  activeFilters = {};
  return show();
};
async function autoRefresh() {
  if (
    !csrf ||
    document.hidden ||
    refreshing ||
    filtersPinned ||
    $("confirm").open ||
    document.querySelector("details[open]") ||
    document.activeElement?.closest("form, .tabs, .table-wrap") ||
    !["overview", "requests", "events"].includes(page)
  )
    return;
  refreshing = true;
  const position = window.scrollY;
  try {
    if (refreshView) await refreshView();
    else await show();
    window.scrollTo({ top: position });
  } catch (e) {
    notice(e.message);
  } finally {
    refreshing = false;
  }
}
setInterval(autoRefresh, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) autoRefresh();
});
document
  .querySelectorAll("nav button")
  .forEach((b) => (b.onclick = () => show(b.dataset.page)));
try {
  const data = await api("/session");
  csrf = data.csrf;
  await ready();
} catch {
  $("login-code").focus();
}
