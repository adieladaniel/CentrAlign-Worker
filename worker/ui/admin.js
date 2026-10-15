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
  await Promise.all([loadMembers(), loadSettings(), loadEnvironment(), loadAudit()]);
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
  { key: "max_runs_per_day", label: "Runs per day", kind: "number", min: 0, max: 100000, help: "0 = unlimited. Counted per UTC day." },
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
      body[f.key] = f.kind === "bool" ? el.checked : f.kind === "number" ? Number(el.value) : el.value;
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
