const view = {
  form: document.querySelector("#plan-form"),
  prompt: document.querySelector("#prompt-input"),
  promptCount: document.querySelector("#prompt-count"),
  samples: document.querySelector("#sample-prompts"),
  user: document.querySelector("#user-select"),
  date: document.querySelector("#date-input"),
  days: document.querySelector("#days-select"),
  budget: document.querySelector("#budget-input"),
  pace: document.querySelector("#pace-control"),
  interests: document.querySelector("#interest-grid"),
  rain: document.querySelector("#rain-checkbox"),
  remember: document.querySelector("#remember-checkbox"),
  failure: document.querySelector("#failure-checkbox"),
  submit: document.querySelector("#submit-button"),
  run: document.querySelector("#run-button"),
  runLabel: document.querySelector(".run-label"),
  export: document.querySelector("#export-button"),
  validation: document.querySelector("#validation-button"),
  scope: document.querySelector("#form-user-scope"),
  sidebarUser: document.querySelector("#sidebar-user"),
  sidebarRole: document.querySelector("#sidebar-role"),
  sidebarAvatar: document.querySelector("#sidebar-avatar"),
  sidebarBudget: document.querySelector("#sidebar-budget"),
  sidebarEvidence: document.querySelector("#sidebar-evidence"),
  navDayCount: document.querySelector("#nav-day-count"),
  navEvidenceCount: document.querySelector("#nav-evidence-count"),
  navCheckCount: document.querySelector("#nav-check-count"),
  heroProgress: document.querySelector("#hero-progress"),
  heroProgressText: document.querySelector("#hero-progress-text"),
  agentStatus: document.querySelector("#agent-status"),
  agentTrace: document.querySelector("#agent-trace"),
  agentError: document.querySelector("#agent-error"),
  metricTools: document.querySelector("#metric-tools"),
  metricToolBudget: document.querySelector("#metric-tool-budget"),
  metricElapsed: document.querySelector("#metric-elapsed"),
  itineraryTitle: document.querySelector("#itinerary-title"),
  itinerarySubtitle: document.querySelector("#itinerary-subtitle"),
  itineraryBadge: document.querySelector("#itinerary-badge"),
  summarySpend: document.querySelector("#summary-spend"),
  summaryBudget: document.querySelector("#summary-budget"),
  summaryHotel: document.querySelector("#summary-hotel"),
  summaryEvidence: document.querySelector("#summary-evidence"),
  summaryBlocked: document.querySelector("#summary-blocked"),
  summaryDays: document.querySelector("#summary-days"),
  summaryPace: document.querySelector("#summary-pace"),
  assistantNote: document.querySelector("#assistant-note"),
  itineraryList: document.querySelector("#itinerary-list"),
  warningList: document.querySelector("#warning-list"),
  evidenceCount: document.querySelector("#evidence-count"),
  citationList: document.querySelector("#citation-list"),
  retrievalAudit: document.querySelector("#retrieval-audit"),
  citationEmpty: document.querySelector("#citation-empty"),
  blockedCount: document.querySelector("#blocked-count"),
  blockedList: document.querySelector("#blocked-list"),
  checksSummary: document.querySelector("#checks-summary"),
  checksGrid: document.querySelector("#checks-grid"),
  scenarioCount: document.querySelector("#scenario-count"),
  scenarioList: document.querySelector("#scenario-list"),
  footerState: document.querySelector("#footer-state"),
  toast: document.querySelector("#toast"),
};

const state = {
  bootstrap: null,
  plan: null,
  validation: null,
  busy: false,
  toastTimer: null,
};

const checkLabels = {
  alice_sees_private_budget: "Alice 能看到私有预算",
  bob_cannot_see_private_budget: "Bob 看不到私有预算",
  bob_gets_public_budget: "Bob 能获得公开预算",
  cache_hit_for_same_scope: "相同用户范围命中缓存",
  bob_cannot_read_planner_doc: "Bob 无法读取规划者资料",
  route_v2_visible: "路线 v2 对 Alice 可见",
  old_route_not_reused: "旧路线不会被复用",
  preference_uses_new_value: "偏好读取最新值",
  preference_keeps_history: "偏好更新保留历史",
  preference_answer_visible: "偏好进入最终答案",
  request_valid: "请求参数有效",
  weather_checked: "逐日天气已查询",
  acl_filtered_before_answer: "ACL 在生成前过滤",
  itinerary_complete: "逐日行程完整",
  budget_calculated: "预算已计算",
  trace_recorded: "Agent 轨迹已记录",
  memory_policy_visible: "记忆策略可观察",
  tool_failure_recovered: "工具失败后成功恢复",
};

const checkNotes = {
  alice_sees_private_budget: "所有者可读取本行程私有资料",
  bob_cannot_see_private_budget: "成员身份无法读取他人预算",
  bob_gets_public_budget: "公开资料仍可正常检索",
  cache_hit_for_same_scope: "仅同一用户和行程复用答案",
  bob_cannot_read_planner_doc: "规划者文档不会进入回答",
  route_v2_visible: "最新文档版本参与检索",
  old_route_not_reused: "索引修订后旧内容失效",
  preference_uses_new_value: "长期偏好读取当前有效值",
  preference_keeps_history: "历史值保留用于审计",
  preference_answer_visible: "偏好进入规划上下文",
  request_valid: "日期、天数、预算和兴趣通过校验",
  weather_checked: "这轮规划调用过天气工具",
  acl_filtered_before_answer: "无权文档没有进入证据列表",
  itinerary_complete: "每个旅行日都有行程",
  budget_calculated: "住宿、餐饮和门票完成估算",
  trace_recorded: "工具调用和 Agent 步骤可回放",
  memory_policy_visible: "偏好更新或跳过均有记录",
  tool_failure_recovered: "超时被标记并切换到本地降级结果",
};

const blockedReasons = {
  user_not_allowed: "当前用户不在允许名单",
  role_not_allowed: "当前角色没有访问权限",
  different_trip: "资料属于其他行程",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function money(value) {
  return `¥${new Intl.NumberFormat("zh-CN").format(Number(value || 0))}`;
}

function compactJson(value) {
  if (value == null) return "{}";
  if (typeof value === "string") return value;
  const text = JSON.stringify(value);
  return text.length > 64 ? `${text.slice(0, 61)}...` : text;
}

function showToast(message, tone = "success") {
  clearTimeout(state.toastTimer);
  view.toast.textContent = message;
  view.toast.className = `toast show${tone === "error" ? " is-error" : ""}`;
  state.toastTimer = setTimeout(() => {
    view.toast.className = "toast";
  }, 3200);
}

function currentUser() {
  return state.bootstrap?.users.find((user) => user.id === view.user.value) || {
    name: "Alice",
    label: "行程所有者",
    avatar: "A",
  };
}

function updateUserScope() {
  const user = currentUser();
  view.scope.textContent = `${user.name} · ${user.label}`;
  view.sidebarUser.textContent = user.name;
  view.sidebarRole.textContent = user.label;
  view.sidebarAvatar.textContent = user.avatar;
}

function setProgress(percent, text) {
  const value = Math.max(0, Math.min(100, percent));
  view.heroProgress.style.setProperty("--progress", value);
  view.heroProgress.querySelector("span").textContent = `${value}%`;
  view.heroProgressText.textContent = text;
}

function renderBootstrap() {
  const { paces, interests, defaults, sample_prompts: prompts, preview } = state.bootstrap;
  view.samples.innerHTML = prompts
    .map((prompt, index) => `
      <button class="sample-prompt" type="button" data-prompt-index="${index}">
        ${escapeHtml(prompt)}
      </button>
    `)
    .join("");

  view.pace.innerHTML = Object.entries(paces)
    .map(([key, setting]) => `
      <button class="segment${key === defaults.pace ? " active" : ""}" type="button" data-pace="${escapeHtml(key)}">
        ${escapeHtml(setting.label)}
      </button>
    `)
    .join("");

  view.interests.innerHTML = interests
    .map((interest) => `
      <label class="interest-option">
        <input type="checkbox" value="${escapeHtml(interest.id)}" ${defaults.interests.includes(interest.id) ? "checked" : ""} />
        <span>${escapeHtml(interest.label)}</span>
      </label>
    `)
    .join("");

  view.user.value = defaults.user_id;
  view.date.value = defaults.start_date;
  view.date.min = new Date().toISOString().slice(0, 10);
  view.days.value = String(defaults.days);
  view.budget.value = String(defaults.budget);
  view.rain.checked = Boolean(defaults.rain_mode);
  view.remember.checked = Boolean(defaults.remember);
  view.failure.checked = Boolean(defaults.simulate_failure);
  updateUserScope();

  state.plan = preview;
  renderPlan(preview, { animate: false });
}

function selectedPace() {
  return view.pace.querySelector(".segment.active")?.dataset.pace || "轻松";
}

function setPace(pace) {
  view.pace.querySelectorAll("[data-pace]").forEach((button) => {
    button.classList.toggle("active", button.dataset.pace === pace);
  });
}

function selectedInterests() {
  return [...view.interests.querySelectorAll("input:checked")].map((input) => input.value);
}

function limitInterests() {
  const checked = view.interests.querySelectorAll("input:checked");
  view.interests.querySelectorAll("input").forEach((input) => {
    input.disabled = !input.checked && checked.length >= 4;
  });
}

function collectRequest() {
  return {
    user_id: view.user.value,
    prompt: view.prompt.value.trim(),
    city: state.bootstrap?.city.name || "杭州",
    start_date: view.date.value,
    days: Number(view.days.value),
    budget: Number(view.budget.value),
    pace: selectedPace(),
    interests: selectedInterests(),
    rain_mode: view.rain.checked,
    remember: view.remember.checked,
    simulate_failure: view.failure.checked,
  };
}

function setPlanBusy(busy) {
  state.busy = busy;
  view.submit.disabled = busy;
  view.run.disabled = busy;
  view.submit.classList.toggle("loading", busy);
  view.run.classList.toggle("loading", busy);
  view.runLabel.textContent = busy ? "Agent 正在规划..." : "开始规划";
  view.form.setAttribute("aria-busy", String(busy));
  if (busy) {
    view.agentStatus.textContent = "执行中";
    view.agentStatus.className = "status-chip warning";
    view.agentError.classList.add("hidden");
    setProgress(24, "Agent 正在理解需求");
  }
}

function renderTrace(steps) {
  view.agentTrace.innerHTML = steps
    .map((step) => `
      <li class="agent-step${step.status === "fallback" ? " is-fallback" : ""}">
        <span class="trace-dot">${String(step.index).padStart(2, "0")}</span>
        <div class="agent-step-copy">
          <strong>${escapeHtml(step.title)}</strong>
          <p>${escapeHtml(step.detail)}</p>
        </div>
        <span class="agent-step-meta">
          ${step.tool ? `<code>${escapeHtml(step.tool)}</code>` : "<code>agent</code>"}
          <span>${step.status === "fallback" ? "已降级" : step.duration_ms ? `${step.duration_ms}ms` : "ok"}</span>
        </span>
      </li>
    `)
    .join("");
}

async function revealTrace(steps) {
  view.agentTrace.innerHTML = "";
  for (const step of steps) {
    view.agentTrace.insertAdjacentHTML(
      "beforeend",
      `
        <li class="agent-step pending${step.status === "fallback" ? " is-fallback" : ""}">
          <span class="trace-dot">${String(step.index).padStart(2, "0")}</span>
          <div class="agent-step-copy">
            <strong>${escapeHtml(step.title)}</strong>
            <p>${escapeHtml(step.detail)}</p>
          </div>
          <span class="agent-step-meta"><code>${escapeHtml(step.tool || "agent")}</code><span>${step.status === "fallback" ? "已降级" : "ok"}</span></span>
        </li>
      `,
    );
    await new Promise((resolve) => setTimeout(resolve, 55));
  }
  view.agentTrace.querySelectorAll(".agent-step").forEach((item) => item.classList.remove("pending"));
}

function renderSummary(plan) {
  const { summary, request } = plan;
  view.itineraryTitle.textContent = summary.title;
  view.itinerarySubtitle.textContent = `${summary.subtitle} · ${request.start_date} 出发`;
  view.itineraryBadge.textContent = summary.remaining >= 0 ? "方案可执行" : "预算需调整";
  view.itineraryBadge.className = `status-chip ${summary.remaining >= 0 ? "success" : "warning"}`;
  view.summarySpend.textContent = money(summary.planned_total);
  view.summaryBudget.textContent = `预算 ${money(summary.budget)} · ${summary.remaining >= 0 ? "结余" : "超出"} ${money(Math.abs(summary.remaining))}`;
  view.summaryHotel.textContent = summary.hotel_per_night ? `${money(summary.hotel_per_night)}/晚` : "未住店";
  view.summaryEvidence.textContent = `${summary.evidence_count} 条`;
  view.summaryBlocked.textContent = `拦截 ${summary.blocked_count} 份无权资料`;
  view.summaryDays.textContent = `${request.days} 天`;
  view.summaryPace.textContent = `节奏 ${request.pace}`;
  view.summarySpend.closest(".summary-card").classList.toggle("is-warning", summary.remaining < 0);
  view.assistantNote.innerHTML = `
    <span class="assistant-avatar" aria-hidden="true">AI</span>
    <div><strong>TripPilot 助手</strong><p>${escapeHtml(plan.assistant_note)}</p></div>
  `;
  view.navDayCount.textContent = String(request.days);
  view.navEvidenceCount.textContent = String(summary.evidence_count);
  view.sidebarBudget.textContent = `${plan.diagnostics.budget_used} / ${plan.diagnostics.budget_total}`;
  view.sidebarEvidence.textContent = `${summary.evidence_count} 条`;
  view.metricTools.textContent = String(summary.tool_call_count);
  view.metricToolBudget.textContent = `${plan.diagnostics.budget_used} / ${plan.diagnostics.budget_total}`;
  view.metricElapsed.textContent = `${summary.elapsed_ms}ms`;
  view.agentStatus.textContent = "已完成";
  view.agentStatus.className = "status-chip success";
  view.footerState.textContent = `${request.user_id} · ${request.days} 天 · ${summary.evidence_count} 条证据`;
}

function renderItinerary(plan) {
  view.itineraryList.innerHTML = plan.itinerary
    .map((day) => {
      const weather = day.weather;
      return `
        <article class="day-card">
          <header class="day-header">
            <div class="day-heading-main">
              <span class="day-number">D${day.day}</span>
              <div>
                <h3>${escapeHtml(day.date_label)}</h3>
                <p>${escapeHtml(day.theme)} · 门票 ${money(day.ticket_total)}</p>
              </div>
            </div>
            <div class="day-weather">
              <span>${weather.indoor_recommended ? "☂" : "☼"}</span>
              <strong>${escapeHtml(weather.condition)}</strong>
              <span>${escapeHtml(weather.temperature)}</span>
            </div>
          </header>
          <div class="day-timeline">
            ${day.items.map((item) => `
              <div class="itinerary-item ${item.name.startsWith("前往 ") ? "is-route" : ""}">
                <span class="item-time">${escapeHtml(item.time)}</span>
                <span class="item-marker" aria-hidden="true"></span>
                <div class="item-copy">
                  <div class="item-title-row">
                    <strong>${escapeHtml(item.name)}</strong>
                    <span class="item-kind">${escapeHtml(item.kind)}</span>
                  </div>
                  <p>${escapeHtml(item.reason)}</p>
                  ${item.interests?.length ? `<div class="item-tags">${item.interests.map((tag) => `<span># ${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
                </div>
                <span class="item-price">${item.ticket ? money(item.ticket) : item.duration ? `${item.duration} 分钟` : "—"}</span>
              </div>
            `).join("")}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderWarnings(plan) {
  view.warningList.innerHTML = plan.warnings
    .map((warning) => `<div class="warning-item"><span>!</span><p>${escapeHtml(warning)}</p></div>`)
    .join("");
}

function renderRetrievalAudit(plan) {
  const traces = plan.diagnostics.acl || [];
  const cacheCounts = traces.reduce((counts, trace) => {
    const status = trace.cache?.status || "unknown";
    counts[status] = (counts[status] || 0) + 1;
    return counts;
  }, {});
  const allowed = traces[0]?.allowed_doc_ids?.length ?? 0;
  const evaluated = traces[0]?.evaluated ?? 0;
  const revision = traces[0]?.index_revision ?? "—";
  const cacheText = Object.entries(cacheCounts)
    .map(([status, count]) => `${status} ${count}`)
    .join(" · ") || "尚未检索";
  const fallbackCount = plan.diagnostics.recovered_failures?.length || 0;
  view.retrievalAudit.innerHTML = `
    <span class="audit-label">检索审计</span>
    <span class="audit-item"><b>ACL</b> ${allowed} / ${evaluated} 份可见</span>
    <span class="audit-item"><b>召回</b> 向量 + 词项交集</span>
    <span class="audit-item"><b>缓存</b> ${escapeHtml(cacheText)}</span>
    <span class="audit-item"><b>索引</b> r${escapeHtml(String(revision))}</span>
    ${fallbackCount ? `<span class="audit-item is-warning"><b>恢复</b> ${fallbackCount} 次超时降级</span>` : ""}
  `;
}

function renderEvidence(plan) {
  renderRetrievalAudit(plan);
  view.evidenceCount.textContent = String(plan.evidence.length);
  view.citationList.innerHTML = plan.evidence
    .map((citation) => `
      <article class="citation-card">
        <span class="citation-index">[${citation.number}]</span>
        <div class="citation-copy">
          <strong>${escapeHtml(citation.title)}</strong>
          <span>${escapeHtml(citation.doc_id)} · 更新于 ${escapeHtml(citation.updated_at)}</span>
          <p>${escapeHtml(citation.text)}</p>
        </div>
        <span class="citation-version">v${citation.version}</span>
      </article>
    `)
    .join("");
  view.citationEmpty.classList.toggle("hidden", plan.evidence.length > 0);
  if (!plan.evidence.length) {
    view.citationEmpty.innerHTML = `
      <span class="empty-icon" aria-hidden="true">∅</span>
      <div><strong>没有可用证据</strong><p>系统没有把无权或无关资料拼进行程。</p></div>
    `;
  }

  view.blockedCount.textContent = `${plan.blocked_evidence.length} 份`;
  view.blockedCount.className = `status-chip ${plan.blocked_evidence.length ? "warning" : "success"}`;
  view.blockedList.innerHTML = plan.blocked_evidence.length
    ? plan.blocked_evidence.map((item) => `
        <article class="blocked-card">
          <span class="blocked-icon" aria-hidden="true">×</span>
          <div>
            <strong>${escapeHtml(item.doc_id)}</strong>
            <span>${escapeHtml(blockedReasons[item.reason] || item.reason)} · v${item.version}</span>
          </div>
        </article>
      `).join("")
    : `<div class="empty-state"><span class="empty-icon">✓</span><div><strong>没有被额外拦截的资料</strong><p>当前身份可访问本次规划所需的全部资料。</p></div></div>`;
}

function renderChecks(checks) {
  view.checksGrid.innerHTML = Object.entries(checks)
    .map(([key, passed]) => `
      <article class="check-item">
        <span class="check-icon">${passed ? "✓" : "×"}</span>
        <div class="check-copy"><strong>${escapeHtml(checkLabels[key] || key)}</strong><span>${escapeHtml(checkNotes[key] || "自动断言")}</span></div>
        <span class="check-value ${passed ? "" : "is-error"}">${passed ? "通过" : "失败"}</span>
      </article>
    `)
    .join("");
}

function renderScenarios(scenarios) {
  view.scenarioCount.textContent = String(scenarios.length);
  view.scenarioList.innerHTML = scenarios
    .map((scenario) => `
      <div class="scenario-mini-row">
        <div><strong>${escapeHtml(scenario.name)}</strong><small>${escapeHtml(scenario.principal)} · ${escapeHtml(scenario.cache)}</small></div>
        <span class="status-chip ${scenario.denied?.length ? "warning" : "success"}">${scenario.denied?.length ? "拦截" : "通过"}</span>
      </div>
    `)
    .join("");
}

function renderValidation(data) {
  state.validation = data;
  const passed = Object.values(data.checks).filter(Boolean).length;
  view.navCheckCount.textContent = String(passed);
  view.checksSummary.textContent = `${passed} / ${Object.keys(data.checks).length} 通过`;
  view.checksSummary.className = `status-chip ${passed === Object.keys(data.checks).length ? "success" : "warning"}`;
  renderChecks(data.checks);
  renderScenarios(data.scenarios || []);
}

async function renderPlan(plan, { animate = true } = {}) {
  state.plan = plan;
  renderSummary(plan);
  renderItinerary(plan);
  renderWarnings(plan);
  renderEvidence(plan);
  renderChecks(plan.diagnostics.checks);
  if (animate) await revealTrace(plan.diagnostics.steps);
  else renderTrace(plan.diagnostics.steps);
  setProgress(100, "行程、证据与权限检查完成");
}

async function runPlan(event) {
  event?.preventDefault();
  if (state.busy) return;
  const request = collectRequest();
  if (!request.interests.length) {
    showToast("至少选择一个兴趣主题", "error");
    return;
  }
  setPlanBusy(true);
  view.agentTrace.innerHTML = `
    <li class="trace-placeholder"><span class="trace-dot">01</span><div><strong>接收旅行目标</strong><p>正在读取表单并解析自然语言需求。</p></div></li>
  `;
  try {
    setProgress(48, "正在查询天气与地点");
    const response = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    setProgress(78, "正在生成行程与证据");
    await renderPlan(payload);
    showToast("行程已生成，可以继续修改条件重新规划");
    if (payload.memory_update?.status === "updated") showToast("已更新长期偏好记忆");
    document.querySelector("#itinerary").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    view.agentStatus.textContent = "执行失败";
    view.agentStatus.className = "status-chip danger";
    view.agentError.textContent = error.message;
    view.agentError.classList.remove("hidden");
    setProgress(0, "规划未完成");
    showToast(`规划失败：${error.message}`, "error");
  } finally {
    setPlanBusy(false);
  }
}

async function runValidation() {
  view.validation.disabled = true;
  view.validation.textContent = "校验中...";
  try {
    const response = await fetch("/api/run", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderValidation(payload);
    showToast("阶段自动校验完成");
  } catch (error) {
    showToast(`校验失败：${error.message}`, "error");
  } finally {
    view.validation.disabled = false;
    view.validation.textContent = "重新校验";
  }
}

async function loadValidation() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderValidation(await response.json());
  } catch (error) {
    view.checksSummary.textContent = "阶段报告读取失败";
    view.checksSummary.className = "status-chip danger";
  }
}

function exportItinerary() {
  if (!state.plan) {
    showToast("还没有可导出的行程", "error");
    return;
  }
  const plan = state.plan;
  const lines = [
    `# ${plan.summary.title}`,
    "",
    plan.assistant_note,
    "",
    `- 出发日期：${plan.request.start_date}`,
    `- 同行身份：${plan.principal.name}（${plan.principal.label}）`,
    `- 预算：${money(plan.summary.budget)}`,
    `- 预计花费：${money(plan.summary.planned_total)}`,
    "",
  ];
  for (const day of plan.itinerary) {
    lines.push(`## D${day.day} ${day.date_label}`, "", `**天气：** ${day.weather.condition} ${day.weather.temperature}`, "");
    for (const item of day.items) {
      lines.push(`- **${item.time}** ${item.name}（${item.kind}）— ${item.reason}`);
    }
    lines.push("");
  }
  if (plan.evidence.length) {
    lines.push("## 证据", "");
    for (const item of plan.evidence) {
      lines.push(`- [${item.number}] ${item.title} · ${item.doc_id} · v${item.version}`);
    }
    lines.push("");
  }
  if (plan.warnings.length) {
    lines.push("## 注意事项", "");
    for (const warning of plan.warnings) lines.push(`- ${warning}`);
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "tripilot-itinerary.md";
  link.click();
  URL.revokeObjectURL(url);
  showToast("行程已导出为 Markdown");
}

async function loadBootstrap() {
  try {
    const response = await fetch("/api/bootstrap", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.bootstrap = payload;
    renderBootstrap();
    await loadValidation();
  } catch (error) {
    view.agentError.textContent = `工作台初始化失败：${error.message}`;
    view.agentError.classList.remove("hidden");
    view.agentStatus.textContent = "离线";
    view.agentStatus.className = "status-chip danger";
    showToast(`初始化失败：${error.message}`, "error");
  }
}

view.form.addEventListener("submit", runPlan);
view.run.addEventListener("click", runPlan);
view.export.addEventListener("click", exportItinerary);
view.validation.addEventListener("click", runValidation);
view.user.addEventListener("change", updateUserScope);
view.prompt.addEventListener("input", () => {
  view.promptCount.textContent = `${view.prompt.value.length} / 500`;
});
view.samples.addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt-index]");
  if (!button) return;
  view.prompt.value = state.bootstrap.sample_prompts[Number(button.dataset.promptIndex)];
  view.promptCount.textContent = `${view.prompt.value.length} / 500`;
  view.prompt.focus();
});
view.pace.addEventListener("click", (event) => {
  const button = event.target.closest("[data-pace]");
  if (button) setPace(button.dataset.pace);
});
view.interests.addEventListener("change", limitInterests);
document.querySelectorAll(".nav-item").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});

loadBootstrap();
