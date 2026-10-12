const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const api = async (path, opts) => {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
};
const state = { filter: "", selected: null };

async function refreshStatus() {
  const s = await api("/api/status");
  $("#version").textContent = `v${s.version} · ${s.config.mode}`;
  $("#mode").value = s.config.mode;
  const pulse = $("#pulse"), halt = $("#halt");
  pulse.classList.toggle("halted", s.halted);
  halt.textContent = s.halted ? "RESUME" : "EMERGENCY STOP";
  halt.classList.toggle("resume", s.halted);
  const counts = s.task_counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  $("#task-count").textContent = total;
  $("#approval-count").textContent = s.pending_approvals;

  const grid = $("#runtime");
  grid.innerHTML = "";
  const models = (s.models || []).map(m => `${m.id} [${m.kind}]${m.available ? "" : " (unreachable)"}`);
  const tools = s.tool_usage || {};
  const stats = [
    ["tasks", Object.entries(counts).map(([k, v]) => `${k}:${v}`).join("  ") || "none"],
    ["models", models.join("\n") || "none"],
    ["model calls", `${(s.model_usage || {}).total_calls || 0} (${(s.model_usage || {}).fallbacks || 0} fallbacks)`],
    ["tool calls", `${tools.total_calls || 0} (${tools.failures || 0} failed)`],
    ["memory", `${s.memory.total} items · ${Object.entries(s.memory.by_kind || {}).map(([k, v]) => `${k}:${v}`).join(" ")}`],
    ["workspace", s.config.workspace],
    ["recovery", (s.recovery_history || []).slice(-3).map(r => `${r.kind}→${r.action}`).join("  ") || "none"],
    ["proposals", `${(s.proposals || []).length} pending`],
  ];
  for (const [k, v] of stats) {
    const box = el("div", "stat");
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
    grid.appendChild(box);
  }
  const select = $("#goal-select");
  const current = select.value;
  select.innerHTML = "";
  select.appendChild(el("option", "", "no goal")).value = "";
  for (const g of s.goals || []) {
    const opt = el("option", "", `${g.title} (${g.state})`);
    opt.value = g.id;
    select.appendChild(opt);
  }
  select.value = current;
}

async function refreshGoals() {
  const goals = await api("/api/goals");
  const box = $("#goals");
  box.innerHTML = "";
  if (!goals.length) { box.appendChild(el("p", "empty", "no goals yet")); return; }
  for (const g of goals) {
    const item = el("div", "item");
    const head = el("div", "title");
    head.appendChild(el("span", `pill ${g.state}`, g.state));
    head.appendChild(document.createTextNode(` ${g.title}`));
    item.appendChild(head);
    item.appendChild(el("div", "meta", g.objective));
    const p = g.progress || {};
    item.appendChild(el("div", "meta",
      `milestones ${g.milestones.length} · monitors ${g.monitors.length} · tasks ${p.completed || 0}/${p.total || 0} done · ${p.blocked || 0} blocked`));
    const row = el("div", "row");
    const detail = el("button", "small", "report");
    detail.onclick = async () => showDetail(await api(`/api/goals/${g.id}`));
    const cancel = el("button", "small", "cancel");
    cancel.onclick = async () => { await api(`/api/goals/${g.id}/cancel`, { method: "POST" }); refreshAll(); };
    row.append(detail, cancel);
    item.appendChild(row);
    box.appendChild(item);
  }
}

async function refreshApprovals() {
  const rows = await api("/api/approvals");
  const box = $("#approvals");
  box.innerHTML = "";
  $("#approval-count").textContent = rows.length;
  if (!rows.length) { box.appendChild(el("p", "empty", "nothing waiting")); return; }
  for (const a of rows) {
    const item = el("div", "item");
    item.appendChild(el("div", "title", a.action));
    item.appendChild(el("div", "meta", `scope ${a.scope} · risk ${a.risk} · ${a.detail}`));
    const row = el("div", "row");
    const yes = el("button", "small primary", "Approve");
    yes.onclick = async () => { await api(`/api/approvals/${a.id}/decide`, { method: "POST", body: JSON.stringify({ approve: true, note: "desktop" }) }); refreshAll(); };
    const no = el("button", "small", "Deny");
    no.onclick = async () => { await api(`/api/approvals/${a.id}/decide`, { method: "POST", body: JSON.stringify({ approve: false, note: "denied from desktop" }) }); refreshAll(); };
    row.append(yes, no);
    item.appendChild(row);
    box.appendChild(item);
  }
}

async function refreshTasks() {
  const query = state.filter ? `?state=${encodeURIComponent(state.filter)}` : "";
  const rows = await api(`/api/tasks${query}&limit=40`);
  const body = $("#tasks tbody");
  body.innerHTML = "";
  if (!rows.length) { body.innerHTML = `<tr><td colspan="5" class="empty">no tasks</td></tr>`; return; }
  for (const t of rows) {
    const tr = el("tr", "selectable");
    tr.appendChild(el("td")).appendChild(el("span", `pill ${t.state}`, t.state));
    tr.appendChild(el("td", "", t.verdict || "-"));
    tr.appendChild(el("td", "", t.phase));
    tr.appendChild(el("td", "", t.intent));
    const actions = el("td");
    const btn = el("button", "small ghost", "open");
    btn.onclick = async (ev) => { ev.stopPropagation(); showDetail(await api(`/api/tasks/${t.id}`)); };
    actions.appendChild(btn);
    tr.appendChild(actions);
    tr.onclick = async () => showDetail(await api(`/api/tasks/${t.id}`));
    body.appendChild(tr);
  }
}

async function refreshMemory() {
  const rows = await api("/api/memory?limit=15");
  const box = $("#memory");
  box.innerHTML = "";
  if (!rows.length) { box.appendChild(el("p", "empty", "no memories")); return; }
  for (const m of rows) {
    const item = el("div", "item");
    item.appendChild(el("div", "meta", `(${m.kind}${m.score !== undefined ? `, ${m.score}` : ""}) ${m.content}`));
    const row = el("div", "row");
    const del = el("button", "small ghost", "forget");
    del.onclick = async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); refreshMemory(); };
    row.appendChild(del);
    item.appendChild(row);
    box.appendChild(item);
  }
}

function showDetail(data) {
  const box = $("#task-detail");
  box.classList.remove("hidden");
  box.textContent = JSON.stringify(data, null, 2);
}

function logEvent(kind, payload) {
  const list = $("#events");
  const li = el("li");
  const stamp = new Date().toLocaleTimeString();
  li.innerHTML = `<b>${stamp}</b> ${kind} <span>${JSON.stringify(payload || {}).slice(0, 160)}</span>`;
  list.prepend(li);
  while (list.children.length > 120) list.removeChild(list.lastChild);
}

async function refreshAll() {
  await Promise.all([refreshStatus(), refreshGoals(), refreshApprovals(), refreshTasks(), refreshMemory()]);
}

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onopen = () => { $("#connection").textContent = "live"; $("#connection").classList.add("live"); };
  source.onerror = () => { $("#connection").textContent = "reconnecting"; $("#connection").classList.remove("live"); };
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.kind === "connected") return;
    logEvent(data.kind, data.payload);
    if (["task.phase", "task.completed", "task.blocked", "task.awaiting_approval",
         "goal.adopted", "goal.state", "system.halted", "system.resumed"].includes(data.kind)) {
      refreshAll();
    }
  };
}

document.querySelectorAll(".chip").forEach((chip) => {
  chip.onclick = () => {
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.filter = chip.dataset.filter;
    refreshTasks();
  };
});

$("#send").onclick = async () => {
  const intent = $("#intent").value.trim();
  if (!intent) return;
  $("#send").disabled = true;
  try {
    const task = await api("/api/request", {
      method: "POST",
      body: JSON.stringify({ intent, goal_id: $("#goal-select").value,
        priority: Number($("#priority").value || 5), run: $("#autorun").checked }),
    });
    showDetail(task);
    $("#intent").value = "";
    logEvent("request.sent", { task: task.id, state: task.state });
    await refreshAll();
  } catch (err) { logEvent("request.error", { error: String(err) }); }
  $("#send").disabled = false;
};

$("#halt").onclick = async () => {
  const halted = $("#halt").classList.contains("resume");
  await api(halted ? "/api/resume" : "/api/halt", { method: "POST" });
  logEvent(halted ? "system.resumed" : "system.halted", {});
  await refreshAll();
};

$("#mode").onchange = async (e) => {
  await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: e.target.value }) });
  logEvent("mode.changed", { mode: e.target.value });
  await refreshStatus();
};

$("#refresh").onclick = refreshAll;

$("#adopt-goal").onclick = async () => {
  const title = $("#goal-title").value.trim();
  const objective = $("#goal-objective").value.trim();
  if (!title || !objective) return;
  const milestones = $("#goal-milestones").value.split(",").map(s => s.trim()).filter(Boolean);
  let monitors = [];
  const raw = $("#goal-monitor").value.trim();
  if (raw) { try { monitors = [JSON.parse(raw)]; } catch { logEvent("goal.error", { error: "monitor JSON invalid" }); return; } }
  await api("/api/goals", { method: "POST", body: JSON.stringify({
    title, objective, milestones, monitors, autonomy: $("#goal-autonomy").value }) });
  $("#goal-title").value = $("#goal-objective").value = $("#goal-milestones").value = $("#goal-monitor").value = "";
  logEvent("goal.adopted", { title });
  await refreshAll();
};

$("#advance").onclick = async () => {
  await api("/api/goals/advance", { method: "POST", body: JSON.stringify({}) });
  logEvent("goals.advance", {});
  setTimeout(refreshAll, 800);
};

$("#recall").onclick = async () => {
  const q = $("#memory-query").value.trim();
  const rows = await api(`/api/memory?query=${encodeURIComponent(q)}&limit=10`);
  const box = $("#memory");
  box.innerHTML = "";
  for (const m of rows) {
    const item = el("div", "item");
    item.appendChild(el("div", "meta", `[${m.score}] (${m.kind}) ${m.content}`));
    box.appendChild(item);
  }
  if (!rows.length) box.appendChild(el("p", "empty", "no relevant memories"));
};

$("#remember").onclick = async () => {
  const content = $("#memory-new").value.trim();
  if (!content) return;
  await api("/api/memory", { method: "POST", body: JSON.stringify({ content, kind: "fact" }) });
  $("#memory-new").value = "";
  await refreshMemory();
};

$("#intent").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#send").click();
});

refreshAll().then(connectStream);
setInterval(refreshAll, 5000);
