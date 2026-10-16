/* CentrAlign Worker dashboard, multi-user mode only: the organization memory page.
 * Loaded after app.js and admin.js; uses their helpers ($, h, api, toast, showView, loadRun, dataTable, fmtWhen, guarded).
 * Every member can read and search; admins can edit a note's content, scope and expiry, delete it, and expire stale
 * notes. Notes are never created here: only verified runs write them (see the note on the page). All text goes through
 * textContent / text nodes (see h()). */
"use strict";

const MEM = { editing: null, canEdit: false };
const MEM_HEADERS = ["Key", "Note", "Scope", "Source run", "Uses", "Last used", "Created by", ""];

function memAge(item) {
  if (item.age_days == null) return "";
  return item.age_days < 1 ? "written today" : `written ${item.age_days} d ago`;
}

function openSourceRun(runId) {
  showView("dashboard");
  loadRun(runId);
}

function dateInputValue(iso) {
  return iso ? iso.slice(0, 10) : "";
}

function memoryRow(item) {
  if (MEM.editing === item.id) return memoryEditRow(item);
  const run = item.source_run_id
    ? h("a", { class: "run-link", href: "#", title: item.source || "Open the run that learned this", onclick: (ev) => { ev.preventDefault(); openSourceRun(item.source_run_id); } }, item.source_run_id.slice(0, 8))
    : h("span", { class: "muted small" }, item.source ? item.source.slice(0, 30) : "-");
  const actions = MEM.canEdit ? h("div", { class: "mem-actions" },
    h("button", { class: "btn btn-small-ghost", type: "button", onclick: () => { MEM.editing = item.id; loadMemoryPage(); } }, "Edit"),
    h("button", { class: "btn btn-small-ghost", type: "button", onclick: () => deleteMemory(item) }, "Delete")) : null;
  return h("tr", { class: item.expired ? "expired" : "" },
    h("td", null, h("div", { class: "cell-main mono small" }, item.key), item.expired ? h("span", { class: "tag tag-warn" }, "expired") : null),
    h("td", { class: "mem-content" }, item.content, h("div", { class: "muted small" }, memAge(item))),
    h("td", null, item.scope ? h("span", { class: "tag" }, item.scope) : h("span", { class: "muted" }, "-")),
    h("td", null, run),
    h("td", null, String(item.use_count)),
    h("td", { class: "muted small nowrap" }, item.last_used_at ? fmtWhen(item.last_used_at) : "never"),
    h("td", { class: "small" }, item.created_by_name || "-"),
    h("td", null, actions));
}

function memoryEditRow(item) {
  const content = h("textarea", { maxlength: 500, "aria-label": "Note" }, item.content);
  const scope = h("input", { type: "text", maxlength: 40, value: item.scope, "aria-label": "Scope" });
  const expires = h("input", { type: "date", value: dateInputValue(item.expires_at), "aria-label": "Expires on" });
  const save = h("button", { class: "btn btn-primary fit", type: "button" }, "Save");
  const cancel = h("button", { class: "btn btn-ghost fit", type: "button" }, "Cancel");
  cancel.addEventListener("click", () => { MEM.editing = null; loadMemoryPage(); });
  save.addEventListener("click", async () => {
    const body = { content: content.value, scope: scope.value };
    // An empty date keeps "no expiry"; a date sets the note to expire at the end of that day (UTC).
    body.expires_at = expires.value ? `${expires.value}T23:59:59Z` : null;
    const done = await guarded(() => api("PATCH", `/api/memory/${item.id}`, body), "Note saved");
    if (done) { MEM.editing = null; loadMemoryPage(); }
  });
  return h("tr", null,
    h("td", null, h("div", { class: "cell-main mono small" }, item.key)),
    h("td", { class: "mem-content" }, content, h("div", { class: "mem-edit-row" }, h("span", { class: "field-label" }, "Expires on"), expires, save, cancel)),
    h("td", null, scope), h("td", { colspan: 5 }, h("span", { class: "muted small" }, "Leave the date empty for no expiry.")));
}

async function deleteMemory(item) {
  if (!confirm(`Delete the note "${item.key}"? Agents will no longer recall it.`)) return;
  const done = await guarded(() => api("DELETE", `/api/memory/${item.id}`), `Deleted ${item.key}`);
  if (done) loadMemoryPage();
}

async function loadMemoryPage() {
  const params = new URLSearchParams();
  if ($("memory-q").value.trim()) params.set("q", $("memory-q").value.trim());
  if ($("memory-scope").value.trim()) params.set("scope", $("memory-scope").value.trim());
  const res = await guarded(() => api("GET", `/api/memory?${params}`));
  if (!res) return;
  MEM.canEdit = res.can_edit;
  $("memory-expire").classList.toggle("hidden", !res.can_edit);
  $("memory-policy").textContent = res.enabled
    ? `Each run recalls up to ${res.recall_limit} note(s); notes unused for ${res.ttl_days || "unlimited"} ${res.ttl_days ? "days expire" : "days"} (change both under Admin > Organization settings).`
    : "Organization memory is switched off for this organization (memory_recall_limit is 0): agents neither recall nor save notes.";
  dataTable($("memory-table"), MEM_HEADERS, res.items.map(memoryRow),
    params.size ? "No note matches." : "No notes yet. They appear after an agent run is independently verified and its agent marked something as durable.");
}

function initMemoryPage() {
  $("memory-search").addEventListener("submit", (ev) => { ev.preventDefault(); MEM.editing = null; loadMemoryPage(); });
  $("memory-expire").addEventListener("click", async () => {
    const res = await guarded(() => api("POST", "/api/memory/expire"));
    if (!res) return;
    toast(res.expired ? `Expired ${res.expired} note(s): ${res.keys.join(", ")}` : "No note is stale.", "ok");
    loadMemoryPage();
  });
}

window.loadMemoryPage = loadMemoryPage;
document.addEventListener("DOMContentLoaded", initMemoryPage);
