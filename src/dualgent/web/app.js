const $ = (selector) => document.querySelector(selector);
const labels = {
  queued: "排队中",
  observing: "观察环境",
  thinking: "规划下一步",
  acting: "正在操作",
  verifying: "验证结果",
  needs_attention: "等待你的回复",
  succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已停止",
};
const activeStates = new Set([
  "queued",
  "observing",
  "thinking",
  "acting",
  "verifying",
]);
let token =
  new URLSearchParams(location.hash.slice(1)).get("token") ||
  sessionStorage.getItem("dualgent-token") ||
  "";
history.replaceState(null, "", location.pathname);
let current = null,
  selected = new Set(),
  surfaces = [],
  tasks = [],
  events = [],
  cursor = 0;
let busy = false,
  polling = false,
  initialized = false;
let lastTasksView = "";

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function showError(message) {
  $("#error").textContent = message;
  $("#error").hidden = !message;
}
async function api(path, options = {}) {
  const response = await fetch(`/api/${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (response.status === 401) {
    if (!$("#auth-dialog").open) $("#auth-dialog").showModal();
    throw new Error("请连接本机 Dualgent。");
  }
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string" ? data.detail : "请求未通过校验。",
    );
  return data;
}
function scopeLabel() {
  $("#scope-label").textContent = selected.size
    ? `已选择 ${selected.size} 个操作环境`
    : "先选择操作环境";
}
async function refreshSurfaces() {
  $("#refresh").disabled = true;
  try {
    const result = await api("surfaces");
    surfaces = result.surfaces;
    selected = new Set(
      [...selected].filter((id) => surfaces.some((s) => s.id === id)),
    );
    const root = $("#surfaces");
    root.replaceChildren();
    for (const [kind, title] of [
      ["web", "浏览器标签页"],
      ["desktop", "桌面窗口"],
    ]) {
      root.append(el("h3", title, "surface-group"));
      const group = surfaces.filter((s) => s.kind === kind);
      if (!group.length)
        root.append(
          el(
            "p",
            kind === "web"
              ? "连接扩展后，标签页会出现在这里。"
              : "暂无可用窗口。",
            "muted",
          ),
        );
      for (const surface of group) {
        const label = el("label", undefined, "surface-option");
        const input = el("input");
        input.type = "checkbox";
        input.checked = selected.has(surface.id);
        input.dataset.surfaceId = surface.id;
        input.addEventListener("change", () => {
          if (input.checked && selected.size >= 4) {
            input.checked = false;
            showError("每个任务最多选择 4 个环境。");
            return;
          }
          input.checked
            ? selected.add(surface.id)
            : selected.delete(surface.id);
          scopeLabel();
        });
        const content = el("span");
        content.append(
          el("strong", surface.title || "未命名页面"),
          el("small", surface.url || surface.application),
        );
        label.append(input, content);
        root.append(label);
      }
    }
    $("#environment-errors").textContent = result.errors.join("\n");
    scopeLabel();
  } finally {
    $("#refresh").disabled = false;
  }
}
function renderTasks() {
  const view = JSON.stringify([
    current?.id,
    tasks.map((t) => [t.id, t.state, t.goal]),
  ]);
  if (view === lastTasksView) return;
  lastTasksView = view;
  $("#tasks").replaceChildren();
  for (const task of tasks) {
    const button = el(
      "button",
      undefined,
      `task-item${current?.id === task.id ? " active" : ""}`,
    );
    button.append(
      el("span", task.goal, "task-name"),
      el("span", labels[task.state], "task-meta"),
    );
    button.addEventListener("click", () => selectTask(task));
    $("#tasks").append(button);
  }
}
async function selectTask(task) {
  current = task;
  events = [];
  cursor = 0;
  selected = new Set(
    task.surfaces
      .filter((s) => surfaces.some((live) => live.id === s.id))
      .map((s) => s.id),
  );
  scopeLabel();
  $("#timeline").replaceChildren();
  $("#observation").textContent = "";
  $("#observed-surface").textContent = "执行后显示环境中的实际内容。";
  $("#welcome").hidden = true;
  renderTasks();
  renderState();
  await refreshTask();
}
function renderState() {
  $("#task-title").textContent = current ? current.goal : "交互工作台";
  $("#task-state").textContent = current
    ? current.cancel_requested && activeStates.has(current.state)
      ? "正在停止"
      : labels[current.state]
    : "";
  $("#task-state").className = `state ${current?.state || ""}`;
  $("#cancel").hidden =
    !current ||
    (!activeStates.has(current.state) && current.state !== "needs_attention");
  $("#cancel").disabled = Boolean(current?.cancel_requested);
  const attention = current?.state === "needs_attention";
  $("#attention").hidden = !attention;
  $("#attention").textContent = attention ? current.result : "";
  $("#prompt").placeholder = attention
    ? "补充信息，或说明你已检查到的结果…"
    : "描述任务，以及你希望看到的最终结果…";
  $("#send").textContent = attention ? "继续任务" : "开始任务 ↵";
  $("#send").disabled =
    busy || Boolean(current && activeStates.has(current.state));
  for (const input of document.querySelectorAll(".surface-option input")) {
    input.checked = selected.has(input.dataset.surfaceId);
    input.disabled = Boolean(
      current && (activeStates.has(current.state) || attention),
    );
  }
}
function renderEvents() {
  const root = $("#timeline");
  root.replaceChildren();
  let step = null;
  for (const event of events) {
    const d = event.data;
    if (event.kind === "created")
      root.append(el("div", d.goal, "message user"));
    if (event.kind === "user_note")
      root.append(el("div", d.text, "message user"));
    if (event.kind === "decision") {
      if (d.plan?.length && !root.querySelector(".plan")) {
        const plan = el("ol", undefined, "plan");
        d.plan.forEach((p) => plan.append(el("li", p)));
        root.append(plan);
      }
      step = el("article", undefined, "step");
      step.append(
        el("p", d.summary),
        el(
          "small",
          d.action
            ? `${{ click: "点击", fill: "填写", press: "按键", scroll: "滚动", navigate: "打开页面" }[d.action.kind]} · ${surfaces.find((s) => s.id === d.surface_id)?.title || current?.surfaces.find((s) => s.id === d.surface_id)?.title || "所选环境"}`
            : {
                finish: "检查最终结果",
                ask: "需要你的输入",
                observe: "重新观察",
              }[d.kind],
        ),
      );
      root.append(step);
    }
    if (event.kind === "receipt" && step && d.status !== "dispatched")
      step.append(el("span", d.detail, "check fail"));
    if (event.kind === "verification" && step) {
      step.append(
        el(
          "span",
          d.passed ? "✓ 已通过环境验证" : "尚未满足预期，重新观察并调整",
          `check${d.passed ? "" : " fail"}`,
        ),
      );
      const details = el("details");
      details.append(el("summary", "查看验证依据"));
      const lines = d.results
        .map(
          (r) =>
            `${r.passed ? "✓" : "×"} ${r.check.kind}: ${r.check.expected}${r.check.value !== null ? ` = ${r.check.value}` : ""}\n${typeof r.evidence === "string" ? r.evidence.slice(0, 1200) : JSON.stringify(r.evidence, null, 2)}`,
        )
        .join("\n\n");
      details.append(el("pre", lines));
      step.append(details);
    }
    if (
      event.kind === "state" &&
      d.detail &&
      ["succeeded", "failed", "cancelled"].includes(d.state)
    )
      root.append(el("div", d.detail, "result"));
    if (event.kind === "cleanup_error")
      root.append(el("div", `环境释放失败：${d.detail}`, "message note"));
    if (event.kind === "feedback")
      root.append(el("div", d.detail, "message note"));
  }
  const observation = [...events]
    .reverse()
    .find((e) => e.kind === "observation");
  if (observation) {
    $("#observed-surface").textContent = observation.data.surface.title;
    $("#observation").textContent = observation.data.text;
  }
}
async function refreshTask() {
  if (!current) return;
  const id = current.id;
  const [task, incoming] = await Promise.all([
    api(`tasks/${id}`),
    api(`tasks/${id}/events?after=${cursor}`),
  ]);
  if (current?.id !== id) return;
  current = task;
  if (incoming.length) {
    const conversation = $("#conversation");
    const nearBottom =
      conversation.scrollHeight -
        conversation.scrollTop -
        conversation.clientHeight <
      100;
    events.push(...incoming);
    cursor = incoming.at(-1).seq;
    renderEvents();
    if (nearBottom) conversation.scrollTop = conversation.scrollHeight;
  }
  renderState();
}
async function status() {
  const result = await api("status");
  $("#connection-dot").classList.toggle("ready", result.model_ready);
  $("#connection-label").textContent = result.model_ready
    ? `${result.model} 已就绪`
    : "请设置模型 API Key";
}
async function initialize() {
  await status();
  sessionStorage.setItem("dualgent-token", token);
  await refreshSurfaces();
  tasks = await api("tasks");
  renderTasks();
  if (!initialized && tasks.length) await selectTask(tasks[0]);
  initialized = true;
  showError("");
}
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("#auth-token").value.trim();
  try {
    await initialize();
    $("#auth-dialog").close();
    $("#auth-token").value = "";
  } catch (error) {
    $("#auth-error").textContent = error.message;
  }
});
$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || (current && activeStates.has(current.state))) return;
  const prompt = $("#prompt").value.trim();
  if (!prompt) return;
  busy = true;
  renderState();
  showError("");
  try {
    if (current?.state === "needs_attention") {
      await api(`tasks/${current.id}/resume`, {
        method: "POST",
        body: JSON.stringify({ note: prompt }),
      });
      await refreshTask();
    } else {
      if (!selected.size)
        throw new Error("请选择至少一个浏览器标签页或桌面窗口。");
      const task = await api("tasks", {
        method: "POST",
        body: JSON.stringify({ goal: prompt, surface_ids: [...selected] }),
      });
      tasks = await api("tasks");
      await selectTask(task);
    }
    $("#prompt").value = "";
  } catch (error) {
    showError(error.message);
  } finally {
    busy = false;
    renderState();
  }
});
$("#prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});
function newTask() {
  current = null;
  events = [];
  cursor = 0;
  $("#timeline").replaceChildren();
  $("#welcome").hidden = false;
  $("#prompt").value = "";
  showError("");
  renderTasks();
  renderState();
  $("#prompt").focus();
}
$("#new-task").addEventListener("click", newTask);
$("#new-task-mobile").addEventListener("click", newTask);
$("#example").addEventListener("click", () => {
  $("#prompt").value = "填写当前表单，并确认页面显示保存成功。";
  $("#prompt").focus();
});
$("#refresh").addEventListener("click", () =>
  refreshSurfaces().catch((e) => showError(e.message)),
);
$("#cancel").addEventListener("click", async () => {
  try {
    await api(`tasks/${current.id}/cancel`, { method: "POST" });
    await refreshTask();
  } catch (error) {
    showError(error.message);
  }
});
let pairing;
$("#pair").addEventListener("click", async () => {
  try {
    pairing = await api("connection");
    $("#extension-path").textContent = pairing.extension_path;
    $("#pair-port").textContent = `本机端口：${pairing.port}`;
    $("#pair-dialog").showModal();
  } catch (error) {
    showError(error.message);
  }
});
$("#copy-code").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(pairing.token);
    $("#copy-status").textContent = "已复制";
  } catch {
    $("#copy-status").textContent = "无法访问剪贴板，请从本机连接码文件读取。";
  }
});
$("#close-pair").addEventListener("click", () => {
  $("#pair-dialog").close();
  refreshSurfaces().catch((e) => showError(e.message));
});
setInterval(async () => {
  if (polling || !initialized) return;
  polling = true;
  try {
    await refreshTask();
    tasks = await api("tasks");
    renderTasks();
    await status();
  } catch (error) {
    showError(error.message);
  } finally {
    polling = false;
  }
}, 1000);
if (token) initialize().catch((error) => showError(error.message));
else $("#auth-dialog").showModal();
