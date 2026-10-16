/* CentrAlign Worker dashboard, multi-user mode only: the approvals inbox.
 * Loaded after app.js and admin.js; uses their helpers ($, h, api, toast, S, showView, loadRun, riskTag, kvTable,
 * contextBlock). All text goes through textContent / text nodes (see h()).
 *
 * Live updates: the server pushes {"type": "changed"} over /api/approvals/stream whenever an approval of the
 * organization is raised or resolved; this page then refetches GET /api/approvals. A WebSocket beats polling every
 * few seconds here because approvals are rare and time-sensitive (a person is waiting for the agent, or the agent for
 * a person): the push costs nothing while idle and shows a new card at once. While the socket is down the page polls
 * every 15 s, so it degrades instead of going stale. */
"use strict";

const AP = { items: [], ws: null, token: 0, poll: null, ticker: null, pending: null, running: false, fetchedAt: 0 };

const actionable = (i) => i.can_respond && (i.can_approve || i.kind === "question");

function updateApprovalsBadge() {
  const n = AP.items.filter(actionable).length;
  const badge = $("approvals-badge");
  badge.textContent = String(n);
  badge.classList.toggle("hidden", n === 0);
}

async function refreshApprovals() {
  if (!AP.running) return;
  try {
    AP.items = await api("GET", "/api/approvals?status=pending");
    AP.fetchedAt = Date.now();
  } catch (err) { return; /* signed out, or the server is restarting: the next push or poll retries */ }
  updateApprovalsBadge();
  if (!$("view-approvals").classList.contains("hidden")) renderApprovals();
}

function scheduleRefresh() {
  clearTimeout(AP.pending);
  AP.pending = setTimeout(refreshApprovals, 120); // a burst of changes is one refetch
}

function connectApprovalStream() {
  const token = ++AP.token;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/approvals/stream`);
  AP.ws = ws;
  ws.onopen = () => { if (token === AP.token) { $("approvals-live").textContent = "live"; refreshApprovals(); } };
  ws.onmessage = (msg) => {
    if (token !== AP.token) return;
    let data = {};
    try { data = JSON.parse(msg.data); } catch (e) { return; }
    if (data.type === "changed") scheduleRefresh();
  };
  ws.onclose = () => {
    if (token !== AP.token || !AP.running) return;
    $("approvals-live").textContent = "reconnecting... (refreshing every 15 s)";
    setTimeout(() => { if (token === AP.token && AP.running) connectApprovalStream(); }, 3000);
  };
}

function startApprovals() {
  if (AP.running) return;
  AP.running = true;
  refreshApprovals();
  connectApprovalStream();
  AP.poll = setInterval(() => { if (!AP.ws || AP.ws.readyState !== WebSocket.OPEN) refreshApprovals(); }, 15000);
  AP.ticker = setInterval(tickApprovalTimes, 5000);
}

function stopApprovals() {
  AP.running = false;
  AP.token += 1;
  clearInterval(AP.poll); clearInterval(AP.ticker); clearTimeout(AP.pending);
  if (AP.ws) { AP.ws.onclose = null; try { AP.ws.close(); } catch (e) { /* ignore */ } AP.ws = null; }
  AP.items = [];
  updateApprovalsBadge();
}

/* ------------------------------------------------------------------ rendering */

function fmtAge(secs) {
  if (secs < 60) return `${secs} s`;
  if (secs < 3600) return `${Math.floor(secs / 60)} min`;
  return `${Math.floor(secs / 3600)} h ${Math.floor((secs % 3600) / 60)} min`;
}

function timeLabels(item) {
  const elapsed = Math.floor((Date.now() - AP.fetchedAt) / 1000);
  const age = item.age_s + elapsed;
  const left = item.expires_in_s == null ? null : item.expires_in_s - elapsed;
  return { age: `waiting ${fmtAge(age)}`, left: left == null ? "no deadline" : left > 0 ? `expires in ${fmtAge(left)}` : "expired", soon: left != null && left < 300 };
}

function tickApprovalTimes() {
  document.querySelectorAll(".approval-item").forEach((el) => {
    const item = AP.items.find((i) => i.id === el.dataset.id);
    if (!item) return;
    const t = timeLabels(item);
    const meta = el.querySelector(".approval-meta");
    if (meta) meta.replaceChildren(`${t.age} · `, h("span", { class: t.soon ? "soon" : "" }, t.left));
  });
}

async function answerApproval(item, body, buttons) {
  buttons.forEach((b) => (b.disabled = true));
  try {
    await api("POST", `/api/runs/${encodeURIComponent(item.run_id)}/respond`, { request_id: item.id, ...body });
    toast(item.kind === "approval" ? (body.approved ? "Approved" : "Denied") : "Answer sent", "ok");
  } catch (err) {
    toast(`Could not send response: ${err.message}`, "error");
  }
  refreshApprovals(); // the server also pushes; this makes the list right even when the socket is down
}

function openRunFromInbox(item) {
  showView("dashboard");
  loadRun(item.run_id);
}

function approvalCard(item) {
  const t = timeLabels(item);
  const payload = item.payload || {};
  const by = item.requested_by ? ` · run started by ${item.requested_by.name || item.requested_by.email}` : "";
  const open = h("button", { type: "button", title: "Open this run's timeline", onclick: () => openRunFromInbox(item) }, item.run_task || "(run)");
  const head = h("div", { class: "approval-head" },
    h("div", { class: "approval-run" }, open, h("span", { class: "muted" }, by)),
    h("div", { class: "approval-meta" }, `${t.age} · `, h("span", { class: t.soon ? "soon" : "" }, t.left)));
  const note = h("input", { type: "text", placeholder: item.kind === "approval" ? "Optional note (e.g. reason for denying)" : "Type your answer..." });
  const reason = item.reason ? h("div", { class: "rights-note small" }, h("div", { class: "rights-reason" }, item.reason)) : null;
  let controls;
  let buttons;
  if (item.kind === "approval") {
    const approve = h("button", { class: "btn btn-approve", type: "button", disabled: !item.can_approve }, "Approve");
    const deny = h("button", { class: "btn btn-deny", type: "button", disabled: !item.can_respond }, "Deny");
    note.disabled = !item.can_respond;
    approve.addEventListener("click", () => answerApproval(item, { approved: true, note: note.value }, buttons));
    deny.addEventListener("click", () => answerApproval(item, { approved: false, note: note.value }, buttons));
    buttons = [approve, deny];
    controls = h("div", { class: "pending-buttons" }, approve, deny, note);
  } else {
    const send = h("button", { class: "btn btn-approve", type: "button", disabled: !item.can_respond }, "Send answer");
    const options = (payload.options || []).map((o) => h("button", { class: "btn btn-option", type: "button", disabled: !item.can_respond }, o));
    buttons = [send, ...options];
    note.disabled = !item.can_respond;
    options.forEach((b) => b.addEventListener("click", () => answerApproval(item, { answer: b.textContent }, buttons)));
    const submit = () => { if (note.value.trim()) answerApproval(item, { answer: note.value.trim() }, buttons); };
    send.addEventListener("click", submit);
    note.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
    controls = h("div", null, options.length ? h("div", { class: "option-row" }, options) : null, h("div", { class: "answer-row" }, note, send));
  }
  const body = item.kind === "approval"
    ? [h("div", null, riskTag(item.risk), " ", h("span", { class: "muted small" }, `tool: ${item.tool}`)),
       h("div", { class: "pending-action" }, item.action || item.tool), kvTable(payload.args), contextBlock(payload.context)]
    : [h("div", { class: "pending-action" }, item.action)];
  return h("div", { class: `approval-item ${item.kind}`, "data-id": item.id }, head, body, reason, controls);
}

function renderApprovals() {
  const list = $("approvals-list");
  if (!AP.items.length) {
    delete list.dataset.ids;
    list.replaceChildren(h("div", { class: "muted" }, "Nothing is waiting for an answer."));
    return;
  }
  // Keep what someone is typing: rebuild only when the set of items changed.
  const ids = AP.items.map((i) => i.id).join(",");
  if (list.dataset.ids === ids) { tickApprovalTimes(); return; }
  list.dataset.ids = ids;
  list.replaceChildren(...AP.items.map(approvalCard));
}

async function loadApprovalsPage() {
  await refreshApprovals();
  renderApprovals();
}

window.startApprovals = startApprovals;
window.stopApprovals = stopApprovals;
window.loadApprovalsPage = loadApprovalsPage;
