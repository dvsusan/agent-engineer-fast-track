// state 保存服务器最近一次返回的 TripPilot 会话。
let state = null;
// toastTimer 保存提示条消失前的计时器。
let toastTimer = null;

// 根据 id 获取页面元素。
const byId = (id) => document.getElementById(id);

// 转义用户输入，防止行程偏好被当成 HTML 执行。
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// 显示短暂的界面提示。
function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("is-visible");
  }, 2600);
}

// 设置按钮忙碌状态，防止用户重复提交。
function setBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
  }
}

// 调用 JSON API，并统一处理服务端错误。
async function requestJson(path, payload = null) {
  const options = {
    method: payload === null ? "GET" : "POST",
    headers: { Accept: "application/json" },
  };
  if (payload !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }

  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    if (data.state) render(data.state);
    throw new Error(data.error || "请求失败");
  }
  return data;
}

// 把机器状态转换成用户看得懂的中文标签。
function statusLabel(status) {
  const labels = {
    idle: "等待开始",
    running: "执行中",
    completed: "规划完成",
    failed: "执行失败",
    blocked: "依赖阻塞",
    cancelled: "已取消",
  };
  return labels[status] || status;
}

// 渲染状态徽标、指标和操作按钮。
function renderSummary(data) {
  const status = data.status || "idle";
  const badge = byId("status-badge");
  badge.textContent = statusLabel(status);
  badge.className = `status-badge status-${status}`;

  byId("metric-step").textContent =
    status === "idle" ? "—" : String(data.current_step);
  byId("metric-calls").textContent =
    status === "idle" ? "—" : String(data.tool_calls);
  byId("metric-budget").textContent =
    status === "idle" ? "—" : String(data.remaining_budget);

  const budget = Math.max(Number(data.request?.tool_budget || 10), 1);
  const remaining = status === "idle" ? budget : Number(data.remaining_budget);
  byId("budget-copy").textContent = `${remaining} / ${budget}`;
  byId("budget-bar").style.width = `${Math.max(
    0,
    Math.min(100, (remaining / budget) * 100),
  )}%`;

  const completed = status === "completed";
  byId("send-button").disabled = !completed || Boolean(data.compensation);
  byId("compensate-button").disabled =
    !data.receipt || data.receipt.status === "cancelled";
}

// 渲染六个计划任务及其依赖关系。
function renderTimeline(data) {
  const timeline = byId("timeline");
  if (!data.tasks || data.tasks.length === 0) {
    timeline.className = "timeline empty-timeline";
    timeline.innerHTML = `
      <div class="empty-illustration" aria-hidden="true">
        <span></span><span></span><span></span>
      </div>
      <p>填写旅行需求后，六个任务会在这里依次点亮。</p>
    `;
    return;
  }

  timeline.className = "timeline";
  timeline.innerHTML = data.tasks
    .map((task, index) => {
      const dependency = task.depends_on.length
        ? `等待 ${task.depends_on.join("、")}`
        : "起始任务";
      return `
        <div class="task-item is-${escapeHtml(task.status)}" style="animation-delay:${index * 55}ms">
          <span class="task-index">${task.status === "completed" ? "✓" : index + 1}</span>
          <div class="task-copy">
            <strong>${escapeHtml(task.purpose)}</strong>
            <small>${escapeHtml(dependency)}</small>
          </div>
          <span class="task-tool">${escapeHtml(task.tool)}</span>
        </div>
      `;
    })
    .join("");
}

// 渲染天气、地点和路线结果。
function renderItinerary(data) {
  const hasResults = data.results && Object.keys(data.results).length > 0;
  byId("itinerary-empty").classList.toggle("hidden", hasResults);
  byId("itinerary-content").classList.toggle("hidden", !hasResults);
  byId("itinerary-title").textContent = hasResults
    ? `${data.request.city}三日旅行计划`
    : "等待生成行程";
  byId("itinerary-city").textContent = data.request?.city || "—";

  if (!hasResults) return;

  const weather = data.request.dates.map(
    (dateText) => data.results[`weather-${dateText}`],
  );
  byId("weather-list").innerHTML = weather
    .map(
      (item, index) => `
        <div class="weather-item">
          <small>DAY ${index + 1} · ${escapeHtml(item.date)}</small>
          <strong>${escapeHtml(item.condition)}</strong>
          <span>${escapeHtml(item.temperature)}</span>
        </div>
      `,
    )
    .join("");

  byId("place-list").innerHTML = data.results.places
    .map(
      (place) => `
        <span class="place-chip">
          ${escapeHtml(place.name)}
          <small>${escapeHtml(place.feature)}</small>
        </span>
      `,
    )
    .join("");

  const route = data.results.route;
  byId("route-copy").textContent =
    `${route.origin} → ${route.destination} · 步行约 ${route.walking_minutes} 分钟`;
}

// 渲染发送回执和补偿状态。
function renderReceipt(data) {
  const receipt = data.receipt;
  const status = byId("receipt-status");
  status.textContent = receipt ? receipt.status : "未发送";
  byId("receipt-id").textContent = receipt?.receipt_id || "—";
  byId("send-attempts").textContent = receipt
    ? `${data.send_attempts} 次`
    : "—";
  byId("duplicate-flag").textContent = receipt
    ? receipt.duplicate
      ? "是 · 返回已有回执"
      : "否"
    : "—";
  byId("compensation-copy").textContent = data.compensation
    ? data.compensation.already_cancelled
      ? "已取消 · 重复补偿"
      : "取消成功"
    : "—";
}

// 渲染经过网关的任务审计轨迹。
function renderTrace(data) {
  const trace = byId("trace-list");
  if (!data.events || data.events.length === 0) {
    trace.innerHTML = '<p class="trace-empty">还没有工具调用记录。</p>';
    return;
  }

  trace.innerHTML = data.events
    .map(
      (event) => `
        <div class="trace-row">
          <span>${String(event.step).padStart(2, "0")}</span>
          <strong>${escapeHtml(event.task || "状态检查")}</strong>
          <code>remaining_budget = ${event.remaining_budget}</code>
          <span class="${event.ok ? "trace-ok" : ""}">
            ${event.ok ? "PASS" : "FAIL"}
          </span>
        </div>
      `,
    )
    .join("");
}

// 把完整服务端状态同步到所有界面区域。
function render(nextState) {
  state = nextState;
  renderSummary(nextState);
  renderTimeline(nextState);
  renderItinerary(nextState);
  renderReceipt(nextState);
  renderTrace(nextState);
}

// 从表单收集三日期旅行需求。
function readRequestForm() {
  const form = new FormData(byId("request-form"));
  return {
    city: String(form.get("city") || "").trim(),
    dates: [
      String(form.get("date1") || ""),
      String(form.get("date2") || ""),
      String(form.get("date3") || ""),
    ],
    preference: String(form.get("preference") || "").trim(),
    tool_budget: Number(form.get("tool_budget")),
  };
}

// 执行旅行规划和六个工具任务。
byId("request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("run-button");
  setBusy(button, true, "正在规划…");
  try {
    const nextState = await requestJson("/api/run", readRequestForm());
    render(nextState);
    showToast("六个任务已完成，行程卡已生成。");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(button, false);
  }
});

// 使用幂等键发送当前行程。
byId("send-button").addEventListener("click", async () => {
  const button = byId("send-button");
  setBusy(button, true, "正在发送…");
  try {
    const nextState = await requestJson("/api/send", {});
    render(nextState);
    showToast("行程发送完成，超时重试命中了同一个回执。");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(button, false);
    byId("send-button").disabled =
      state?.status !== "completed" || Boolean(state?.compensation);
  }
});

// 对已经发送的行程执行补偿。
byId("compensate-button").addEventListener("click", async () => {
  const button = byId("compensate-button");
  setBusy(button, true, "正在撤销…");
  try {
    const nextState = await requestJson("/api/compensate", {});
    render(nextState);
    showToast("补偿完成，行程状态已改为 cancelled。");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(button, false);
    byId("compensate-button").disabled =
      !state?.receipt || state.receipt.status === "cancelled";
  }
});

// 清空当前会话，但保留磁盘上的历史交付文件。
byId("reset-button").addEventListener("click", async () => {
  try {
    const nextState = await requestJson("/api/reset", {});
    render(nextState);
    showToast("当前会话已清空。");
  } catch (error) {
    showToast(error.message);
  }
});

// 首次打开页面时读取服务端初始状态。
requestJson("/api/state")
  .then(render)
  .catch((error) => showToast(error.message));
