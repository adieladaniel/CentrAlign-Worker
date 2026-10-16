/* CentrAlign Worker dashboard, multi-user mode only: the Admin page (members, organization settings, environment,
 * activity) and the Credentials page. Loaded after app.js and uses its helpers ($, h, api, toast, S, showView).
 * Text always goes through textContent / text nodes (see h()). Secret fields are write-only: this page never receives
 * a credential value from the server. */
"use strict";

const ROLE_ORDER = ["viewer", "approver", "operator", "admin", "owner"];
const ROLE_HELP = {
  viewer: "watch runs, read-only",
  approver: "answer approvals (no starting runs)",
  operator: "start runs (approve only if the organization allows it)",
  admin: "manage members, settings, credentials, environment",
  owner: "everything, including other owners",
};
const roleRank = (role) => ROLE_ORDER.indexOf(role);

/* ------------------------------------------------------------------ small helpers */

function fmtWhen(iso) {
  const t = Date.parse(iso);
  return t ? new Date(t).toLocaleString() : "never";
}

function dataTable(table, headers, rows, empty) {
  table.replaceChildren(
    h("thead", null, h("tr", null, headers.map((x) => h("th", null, x)))),
    h("tbody", null, rows.length ? rows : h("tr", null, h("td", { colspan: headers.length, class: "muted" }, empty))));
}

async function guarded(action, okMessage) {
  try {
    const result = await action();
    if (okMessage) toast(okMessage, "ok");
    return result;
  } catch (err) {
    toast(err.message, "error");
    return null;
  }
}

/* ------------------------------------------------------------------ admin page */

async function loadAdminPage() {
  const myRank = roleRank(S.me.org.role);
  $("member-role").replaceChildren(...ROLE_ORDER.filter((r) => roleRank(r) <= myRank)
    .map((r) => h("option", { value: r, title: ROLE_HELP[r], selected: r === "operator" }, `${r} - ${ROLE_HELP[r]}`)));
  await Promise.all([loadMembers(), loadSettings(), loadEnvironment(), loadSystem(), loadUsage(), loadAudit()]);
}

async function loadMembers() {
  const members = await guarded(() => api("GET", "/api/admin/members"));
  if (!members) return;
  const me = S.me.user.id, myRank = roleRank(S.me.org.role);
  dataTable($("members-table"), ["Person", "Role", "Status", "Last sign-in", ""], members.map((m) => {
    const self = m.user_id === me;
    const locked = self || roleRank(m.role) > myRank; // nobody edits themselves; admins cannot touch owners
    const select = h("select", { disabled: locked, "aria-label": `Role of ${m.email}` },
      ROLE_ORDER.filter((r) => roleRank(r) <= myRank || r === m.role).map((r) => h("option", { value: r, selected: r === m.role }, r)));
    select.addEventListener("change", async () => {
      const done = await guarded(() => api("PATCH", `/api/admin/members/${m.user_id}`, { role: select.value }), `Role of ${m.email} is now ${select.value}`);
      if (!done) select.value = m.role;
      loadMembers();
    });
    const toggle = h("button", { class: "btn btn-small-ghost", type: "button", disabled: locked }, m.is_active ? "Deactivate" : "Reactivate");
    toggle.addEventListener("click", async () => {
      if (m.is_active && !confirm(`Deactivate ${m.email}? They are signed out and lose access to this organization.`)) return;
      await guarded(() => api("PATCH", `/api/admin/members/${m.user_id}`, { is_active: !m.is_active }),
        m.is_active ? `${m.email} deactivated` : `${m.email} reactivated`);
      loadMembers();
    });
    return h("tr", { class: m.is_active ? "" : "inactive" },
      h("td", null, h("div", { class: "cell-main" }, m.display_name || m.email, self ? h("span", { class: "tag" }, "you") : null),
        m.display_name ? h("div", { class: "muted small" }, m.email) : null),
      h("td", null, select), h("td", null, h("span", { class: `tag ${m.is_active ? "tag-ok" : "tag-warn"}` }, m.is_active ? "active" : "deactivated")),
      h("td", { class: "muted small" }, fmtWhen(m.last_login_at)), h("td", null, toggle));
  }), "No members.");
}

function initMemberForm() {
  $("member-add-toggle").addEventListener("click", () => $("member-form").classList.toggle("hidden"));
  $("member-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = { email: $("member-email").value.trim(), display_name: $("member-name").value.trim(), role: $("member-role").value };
    if ($("member-password").value) body.password = $("member-password").value;
    const res = await guarded(() => api("POST", "/api/admin/members", body));
    if (!res) return;
    $("member-form").reset();
    $("member-form").classList.add("hidden");
    const note = $("member-secret");
    if (res.initial_password) {
      note.replaceChildren(h("b", null, `${res.email} was added as ${res.role}. `), "Initial password (shown only once, copy it now): ",
        h("code", { class: "secret-once" }, res.initial_password));
    } else {
      note.textContent = res.created_user
        ? `${res.email} was added as ${res.role} with the password you chose.`
        : `${res.email} already had an account and now has access to this organization as ${res.role}. Their password is unchanged.`;
    }
    note.classList.remove("hidden");
    loadMembers();
  });
}

/* settings */

const SETTING_FIELDS = [
  { key: "approval_mode", label: "Approval mode", kind: "select", options: ["auto", "confirm_writes", "confirm_all"],
    help: "The minimum confirmation for every run: auto asks only for irreversible actions. Operators can ask for stricter, never looser." },
  { key: "separation_of_duties", label: "Separation of duties", kind: "bool", help: "The person who started a run cannot approve its irreversible actions." },
  { key: "sod_for_writes", label: "... also for writes", kind: "bool", help: "Extends separation of duties to write actions." },
  { key: "operators_can_approve", label: "Operators can approve", kind: "bool", help: "Otherwise only approvers, admins and owners can." },
  { key: "approval_timeout_s", label: "Approval timeout (s)", kind: "number", min: 30, max: 604800, help: "A pending approval or question is dropped (counts as no response) after this long." },
  { key: "max_concurrent_runs", label: "Concurrent runs", kind: "number", min: 1, max: 50, help: "Runs of this organization executing at the same time. Further runs wait in the queue for a free slot." },
  { key: "max_runs_per_day", label: "Runs per day", kind: "number", min: 0, max: 100000, help: "0 = unlimited. Counted per UTC day." },
  { key: "max_tokens_per_day", label: "LLM tokens per day", kind: "number", min: 0, max: 10000000000, help: "Input + output tokens for the whole organization per UTC day. Runs stop with 'needs attention' when it is used up. 0 = unlimited." },
  { key: "max_llm_calls_per_day", label: "LLM calls per day", kind: "number", min: 0, max: 10000000, help: "Model calls for the whole organization per UTC day. 0 = unlimited." },
  { key: "gemini_models", label: "Gemini models", kind: "list", help: "Comma-separated fallback chain, tried in order. Empty = the deployment's default chain. An own API key goes into the credential named 'gemini' (kind: API key)." },
  { key: "memory_recall_limit", label: "Memory notes per run", kind: "number", min: 0, max: 20, help: "How many organization-memory notes an agent receives at the start of a run and per recall. 0 switches organization memory off (nothing recalled, nothing saved)." },
  { key: "memory_ttl_days", label: "Memory retention (days)", kind: "number", min: 0, max: 3650, help: "A note nobody has used for this many days expires (it is no longer recalled; admins can still see it). 0 = never." },
  { key: "max_steps", label: "Max steps per run", kind: "number", min: 1, max: 500, help: "Real actions before the agent is stopped with partial progress." },
  { key: "run_timeout_s", label: "Run time limit (s)", kind: "number", min: 30, max: 86400, help: "Wall-clock limit per run; waiting for a human does not count." },
];

async function loadSettings() {
  const res = await guarded(() => api("GET", "/api/admin/settings"));
  if (!res) return;
  const form = $("settings-form");
  const inputs = {};
  const rows = SETTING_FIELDS.map((f) => {
    const value = res.settings[f.key];
    let input;
    if (f.kind === "select") input = h("select", null, f.options.map((o) => h("option", { value: o, selected: o === value }, o)));
    else if (f.kind === "bool") input = h("input", { type: "checkbox", checked: !!value });
    else if (f.kind === "list") input = h("input", { type: "text", value: (value || []).join(", "), placeholder: "default chain", maxlength: 800 });
    else input = h("input", { type: "number", min: f.min, max: f.max, step: 1, value });
    inputs[f.key] = input;
    return h("label", { class: "setting" }, h("span", { class: "field-label" }, f.label), input, h("span", { class: "hint" }, f.help));
  });
  const save = h("button", { class: "btn btn-primary fit", type: "submit" }, "Save settings");
  form.replaceChildren(...rows, h("div", { class: "actions" }, save));
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const body = {};
    SETTING_FIELDS.forEach((f) => {
      const el = inputs[f.key];
      body[f.key] = f.kind === "bool" ? el.checked : f.kind === "number" ? Number(el.value)
        : f.kind === "list" ? el.value.split(",").map((x) => x.trim()).filter(Boolean) : el.value;
    });
    const out = await guarded(() => api("PUT", "/api/admin/settings", body), "Settings saved");
    if (out) { loadSettings(); loadAudit(); refreshMe(); }
  };
}

// The approval policy is part of /api/auth/me; reload it after a change so approval cards use the new rules.
async function refreshMe() {
  try {
    const me = await api("GET", "/api/auth/me");
    S.me = me;
    applyMe();
  } catch (err) { /* the next poll will notice */ }
}

/* environment and activity */

async function loadEnvironment() {
  const env = await guarded(() => api("GET", "/api/admin/environment"));
  if (!env) return;
  $("env-status").textContent = env.provisioned
    ? `This organization has its own simulated company (${env.tenant_id}). Its logins are in the credential vault. Provisioning again is safe and keeps the data; use "Reset environment now" on the dashboard to wipe it.`
    : `No simulated company yet. Provisioning creates a private copy of the mail, vendor portal and ERP for this organization (the simulator at ${env.sim_base_url} must be running).`;
  $("env-provision").textContent = env.provisioned ? "Provision again (refresh logins)" : "Provision environment";
}

async function loadAudit() {
  const rows = await guarded(() => api("GET", "/api/admin/audit?limit=30"));
  if (!rows) return;
  dataTable($("audit-table"), ["When", "Who", "Action", "Details"], rows.map((r) => h("tr", null,
    h("td", { class: "muted small nowrap" }, fmtWhen(r.ts)), h("td", null, r.user || "-"), h("td", { class: "mono small" }, r.action),
    h("td", { class: "muted small" }, auditSummary(r)))), "Nothing recorded yet.");
}

function auditSummary(r) {
  const d = r.details || {};
  const bits = [];
  if (r.target_id) bits.push(r.target_type ? `${r.target_type} ${String(r.target_id).slice(0, 12)}` : String(r.target_id));
  if (d.email) bits.push(d.email);
  if (d.role) bits.push(`role ${d.role}`);
  if (d.approved !== undefined && d.approved !== null) bits.push(d.approved ? "approved" : "denied");
  if (d.risk) bits.push(d.risk);
  if (d.changes || d.changed) bits.push(Object.entries(d.changes || d.changed).map(([k, v]) => `${k}: ${v[0]} -> ${v[1]}`).join(", "));
  return bits.join(" · ");
}

/* ------------------------------------------------------------------ usage */

const numFmt = new Intl.NumberFormat();
const SVG_NS = "http://www.w3.org/2000/svg";

// The "Usage" card is built here (not in index.html) and placed before the activity card.
function ensureUsageCard() {
  if ($("usage-card")) return;
  const days = h("select", { id: "usage-days", "aria-label": "Period" },
    [7, 14, 30].map((d) => h("option", { value: d }, `Last ${d} days`)));
  days.addEventListener("change", loadUsage);
  const card = h("div", { class: "card", id: "usage-card" },
    h("div", { class: "card-head" }, h("h2", null, "LLM usage"), days),
    h("p", { id: "usage-me", class: "muted small" }),
    h("div", { id: "usage-meters" }),
    h("div", { class: "table-wrap" }, h("table", { id: "usage-daily", class: "data-table" })),
    h("div", { class: "usage-split" },
      h("div", { class: "table-wrap" }, h("table", { id: "usage-users", class: "data-table" })),
      h("div", { class: "table-wrap" }, h("table", { id: "usage-models", class: "data-table" }))));
  const inner = document.querySelector("#view-admin .page-inner");
  inner.insertBefore(card, inner.lastElementChild);
}

function usageMeter(label, used, limit, unit) {
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const level = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
  const text = limit ? `${numFmt.format(used)} of ${numFmt.format(limit)} ${unit} (${pct}%)` : `${numFmt.format(used)} ${unit} (no limit)`;
  return h("div", { class: "usage-meter" },
    h("div", { class: "usage-meter-head" }, h("span", { class: "field-label" }, label), h("span", { class: "muted small" }, text)),
    h("div", { class: "usage-track", role: "progressbar", "aria-label": label, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct },
      h("div", { class: `usage-fill ${level}`, style: `width:${pct}%` })));
}

function usageBar(value, max) { // a tiny inline SVG bar
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", "120");
  svg.setAttribute("height", "10");
  svg.setAttribute("aria-hidden", "true");
  const rect = document.createElementNS(SVG_NS, "rect");
  rect.setAttribute("height", "10");
  rect.setAttribute("rx", "3");
  rect.setAttribute("width", String(max ? Math.max(value ? 2 : 0, Math.round((value / max) * 120)) : 0));
  rect.setAttribute("class", "usage-bar");
  svg.append(rect);
  return svg;
}

async function loadUsage() {
  ensureUsageCard();
  const days = Number($("usage-days").value) || 7;
  const [report, mine] = await Promise.all([guarded(() => api("GET", `/api/admin/usage?days=${days}`)), guarded(() => api("GET", "/api/usage/me"))]);
  if (mine) $("usage-me").textContent = `You used ${numFmt.format(mine.calls)} calls and ${numFmt.format(mine.tokens)} tokens today (UTC).`;
  if (!report) return;
  const t = report.today, lim = report.limits;
  $("usage-meters").replaceChildren(
    usageMeter("Tokens today", t.tokens, lim.max_tokens_per_day, "tokens"),
    usageMeter("Calls today", t.calls, lim.max_llm_calls_per_day, "calls"),
    h("p", { class: "hint" }, `Budgets reset at ${fmtWhen(report.resets_at)}. Change them under Organization settings.`));
  const peak = Math.max(0, ...report.daily.map((d) => d.tokens));
  dataTable($("usage-daily"), ["Day (UTC)", "Calls", "Input tokens", "Output tokens", ""],
    report.daily.slice().reverse().map((d) => h("tr", null, h("td", null, d.day), h("td", null, numFmt.format(d.calls)),
      h("td", null, numFmt.format(d.input_tokens)), h("td", null, numFmt.format(d.output_tokens)), h("td", null, usageBar(d.tokens, peak)))),
    "No usage recorded.");
  dataTable($("usage-users"), ["Top users", "Calls", "Tokens"], report.by_user.map((u) => h("tr", null,
    h("td", null, u.name || "(system)"), h("td", null, numFmt.format(u.calls)), h("td", null, numFmt.format(u.tokens)))), "No usage recorded.");
  dataTable($("usage-models"), ["Models", "Calls", "Tokens"], report.by_model.map((m) => h("tr", null,
    h("td", { class: "mono small" }, m.model), h("td", null, numFmt.format(m.calls)), h("td", null, numFmt.format(m.tokens)))), "No usage recorded.");
}

/* ------------------------------------------------------------------ system (read-only) */

const SYSTEM_REFRESH_MS = 15000;
let systemTimer = null;

// The "System" card is built here and placed before the activity card. It refreshes every 15 s while the Admin page is open.
function ensureSystemCard() {
  if ($("system-card")) return;
  const card = h("div", { class: "card", id: "system-card" },
    h("div", { class: "card-head" }, h("h2", null, "System"), h("span", { id: "system-scope", class: "muted small" })),
    h("dl", { id: "system-stats", class: "stats" }),
    h("div", { class: "table-wrap" }, h("table", { id: "system-queue", class: "data-table" })),
    h("div", { class: "table-wrap" }, h("table", { id: "system-workers", class: "data-table" })),
    h("p", { id: "system-note", class: "hint" }));
  const inner = document.querySelector("#view-admin .page-inner");
  inner.insertBefore(card, inner.lastElementChild);
  if (!systemTimer) {
    systemTimer = setInterval(() => {
      const open = !$("view-admin").classList.contains("hidden") && document.visibilityState === "visible";
      if (open) loadSystem();
    }, SYSTEM_REFRESH_MS);
  }
}

function fmtSeconds(s) {
  if (s === null || s === undefined) return "-";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${Math.round(s % 60)} s` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

async function loadSystem() {
  ensureSystemCard();
  let data;
  try {
    data = await api("GET", "/api/admin/system");
  } catch (err) {
    $("system-note").textContent = `System summary unavailable: ${err.message}`;
    return;
  }
  const tile = (label, value) => h("div", { class: "stat" }, h("dt", null, label), h("dd", null, String(value)));
  const done = data.finished_24h.by_status;
  $("system-scope").textContent = data.scope === "platform" ? "all organizations" : `${data.org.name}`;
  $("system-stats").replaceChildren(
    tile("Queued", data.totals.queued), tile("Running", data.totals.running), tile("Waiting for a person", data.totals.waiting),
    tile("Active workers", data.workers.active),
    tile("Finished (24 h)", data.finished_24h.total),
    tile("Succeeded (24 h)", done.succeeded || 0),
    tile("Run time p50", fmtSeconds(data.run_seconds_24h.p50)), tile("Run time p95", fmtSeconds(data.run_seconds_24h.p95)));
  dataTable($("system-queue"), ["Organization", "Queued", "Oldest queued", "Running", "Waiting"],
    data.queue.map((q) => h("tr", null, h("td", null, q.name || q.org), h("td", null, q.queued),
      h("td", null, q.queued ? fmtSeconds(q.oldest_queued_s) : "-"), h("td", null, q.running), h("td", null, q.waiting))),
    "Nothing queued or running.");
  dataTable($("system-workers"), ["Worker", "Runs held", "Last heartbeat"],
    data.workers.list.map((w) => h("tr", null, h("td", { class: "mono small" }, w.worker), h("td", null, w.runs),
      h("td", null, `${fmtSeconds(w.heartbeat_age_s)} ago`))),
    "No worker holds a run right now.");
  const failed = Object.entries(done).filter(([k]) => k !== "succeeded").map(([k, n]) => `${n} ${k.replace("_", " ")}`);
  $("system-note").textContent = `Refreshes every 15 s. ${data.workers.note}. Run times are wall time, approval waits included.` +
    (failed.length ? ` Last 24 h besides succeeded: ${failed.join(", ")}.` : "");
}

/* ------------------------------------------------------------------ credentials page */

const CRED_FIELDS = {
  login: [{ name: "username", label: "Username", secret: false }, { name: "password", label: "Password", secret: true }],
  api_key: [{ name: "api_key", label: "API key", secret: true }, { name: "header_name", label: "Header name", secret: false, placeholder: "X-API-Key" }],
  header: [{ name: "header_name", label: "Header name", secret: false }, { name: "value", label: "Header value", secret: true }],
};
let credEditing = null; // the credential being edited (metadata only), or null when adding

function renderCredFields() {
  const kind = $("cred-kind").value;
  $("cred-fields").replaceChildren(...CRED_FIELDS[kind].map((f) => {
    const keep = credEditing && credEditing.kind === kind && credEditing.fields.includes(f.name);
    return h("label", { class: "field" }, h("span", { class: "field-label" }, f.label + (f.secret ? " (write-only)" : "")),
      h("input", { type: f.secret ? "password" : "text", "data-field": f.name, autocomplete: "new-password", maxlength: 4096,
        placeholder: keep ? "unchanged - type to replace" : (f.placeholder || "") }));
  }));
}

function openCredForm(cred) {
  credEditing = cred || null;
  $("cred-form").classList.remove("hidden");
  $("cred-form-title").textContent = cred ? `Edit credential "${cred.name}"` : "Add credential";
  $("cred-name").value = cred ? cred.name : "";
  $("cred-name").readOnly = !!cred;
  $("cred-kind").value = cred ? cred.kind : "login";
  $("cred-description").value = cred ? cred.description : "";
  $("cred-url").value = cred ? cred.url : "";
  $("cred-host").value = cred ? cred.host : "";
  renderCredFields();
  $("cred-form").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadCredentialsPage() {
  const list = await guarded(() => api("GET", "/api/admin/credentials"));
  if (!list) return;
  dataTable($("cred-table"), ["Name", "Kind", "Description", "Host", "Fields", "Updated", ""], list.map((c) => {
    const edit = h("button", { class: "btn btn-small-ghost", type: "button" }, "Edit");
    edit.addEventListener("click", () => openCredForm(c));
    const del = h("button", { class: "btn btn-small-ghost danger", type: "button" }, "Delete");
    del.addEventListener("click", async () => {
      if (!confirm(`Delete the credential "${c.name}"? Runs that need it will no longer find it.`)) return;
      await guarded(() => api("DELETE", `/api/admin/credentials/${encodeURIComponent(c.name)}`), `Deleted ${c.name}`);
      loadCredentialsPage();
    });
    return h("tr", null, h("td", { class: "mono" }, c.name), h("td", null, c.kind), h("td", null, c.description || ""),
      h("td", { class: "mono small" }, c.host || (c.url ? "" : "any")), h("td", { class: "muted small" }, c.fields.join(", ")),
      h("td", { class: "muted small nowrap" }, fmtWhen(c.updated_at)), h("td", { class: "nowrap" }, edit, " ", del));
  }), "No credentials stored.");
}

function initCredentialForm() {
  $("cred-add").addEventListener("click", () => openCredForm(null));
  $("cred-cancel").addEventListener("click", () => { $("cred-form").classList.add("hidden"); credEditing = null; });
  $("cred-kind").addEventListener("change", renderCredFields);
  $("cred-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fields = {};
    $("cred-fields").querySelectorAll("input[data-field]").forEach((el) => { fields[el.dataset.field] = el.value; });
    const body = { kind: $("cred-kind").value, fields, description: $("cred-description").value.trim(),
      url: $("cred-url").value.trim(), host: $("cred-host").value.trim() };
    const name = $("cred-name").value.trim();
    const saved = await guarded(() => api("PUT", `/api/admin/credentials/${encodeURIComponent(name)}`, body), `Saved ${name}`);
    if (!saved) return;
    $("cred-form").reset();
    $("cred-form").classList.add("hidden");
    credEditing = null;
    loadCredentialsPage();
  });
}

/* ------------------------------------------------------------------ wiring */

window.loadAdminPage = loadAdminPage;
window.loadCredentialsPage = loadCredentialsPage;

document.addEventListener("DOMContentLoaded", () => {
  initMemberForm();
  initCredentialForm();
  $("env-provision").addEventListener("click", async () => {
    $("env-provision").disabled = true;
    const res = await guarded(() => api("POST", "/api/admin/environment/provision"), "Environment provisioned");
    $("env-provision").disabled = false;
    if (res) { loadEnvironment(); loadAudit(); refreshMe(); }
  });
  $("audit-refresh").addEventListener("click", loadAudit);
});
