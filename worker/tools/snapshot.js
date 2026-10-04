/* DOM snapshot for LLM agents. Evaluated in the page via page.evaluate(source, opts).
 *
 * Tags every visible interactive element with data-agent-ref="N" (re-assigned on each call) and
 * returns plain JSON: url, title, alerts, page text (headings marked, tables as pipe rows,
 * links/buttons annotated with their [ref]) and one record per interactive element.
 * Formatting into a compact string happens in snapshot.py.
 */
(opts) => {
  opts = opts || {};
  const MAX_ELEMENTS = opts.maxElements || 600;
  const MAX_TEXT_SCAN = opts.maxTextScan || 60000;
  const OPTION_CAP = opts.optionCap || 8;

  const norm = (s) => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const style = (el) => getComputedStyle(el);

  function isVisible(el) {
    if (!el.isConnected) return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    const st = style(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return false;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return true;
    // Custom-styled checkboxes/radios are often visually collapsed while their label is visible.
    if (el.tagName === 'INPUT' && /^(checkbox|radio|file)$/.test(el.type)) {
      const lab = (el.labels && el.labels[0]) || el.closest('label');
      return !!lab && lab.getBoundingClientRect().width > 0 && style(lab).visibility !== 'hidden';
    }
    return false;
  }

  // ---- text extraction -------------------------------------------------------------------
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'HEAD', 'META', 'LINK', 'IFRAME',
    'CANVAS', 'OBJECT', 'VIDEO', 'AUDIO', 'DATALIST', 'OPTION', 'OPTGROUP', 'INPUT', 'SELECT', 'TEXTAREA']);
  const ANNOTATE_ROLES = /^(button|link|tab|menuitem|checkbox|radio|switch)$/;

  function annotated(el) {
    if (!el.hasAttribute('data-agent-ref')) return false;
    const role = el.getAttribute('role');
    return el.tagName === 'A' || el.tagName === 'BUTTON' || el.tagName === 'SUMMARY' || (role && ANNOTATE_ROLES.test(role));
  }

  function walk(node, refs) {
    if (node.nodeType === 3) return node.nodeValue;
    if (node.nodeType !== 1) return '';
    const tag = node.tagName.toUpperCase();
    if (SKIP.has(tag)) return '';
    if (node.getAttribute('aria-hidden') === 'true') return '';
    const st = style(node);
    if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return '';
    if (st.overflow !== 'visible' && tag !== 'TR' && tag !== 'TD' && tag !== 'TH') {
      const r = node.getBoundingClientRect();  // clipped to nothing (e.g. height:0; overflow:hidden)
      if (r.width === 0 || r.height === 0) return '';
    }
    if (tag === 'IMG') {
      const alt = norm(node.getAttribute('alt'));
      return alt ? ' ' + alt + ' ' : '';
    }
    if (tag === 'BR') return '\n';
    if (tag === 'TABLE') return renderTable(node, refs);
    let inner = '';
    for (const child of node.childNodes) inner += walk(child, refs);
    if (/^H[1-6]$/.test(tag)) {
      const t = norm(inner);
      return t ? '\n' + '#'.repeat(Number(tag[1])) + ' ' + t + '\n' : '';
    }
    if (refs && annotated(node)) {
      return ' ' + norm(inner) + '[' + node.getAttribute('data-agent-ref') + '] ';
    }
    if (tag === 'LI') return '\n- ' + inner + '\n';
    const d = st.display;
    if (d === 'inline' || d === 'contents' || d.startsWith('inline-')) return inner;
    return '\n' + inner + '\n';
  }

  function renderTable(table, refs) {
    const lines = [];
    if (table.caption) {
      const cap = norm(walk(table.caption, refs));
      if (cap) lines.push(cap);
    }
    for (const row of table.rows) {
      if (style(row).display === 'none') continue;
      const cells = [];
      for (const cell of row.cells) {
        if (style(cell).display === 'none') continue;
        cells.push(norm(walk(cell, refs)));
      }
      if (cells.some((c) => c)) lines.push(cells.join(' | '));
    }
    return '\n' + lines.join('\n') + '\n';
  }

  function collapseLines(raw) {
    const out = [];
    for (const line of raw.split('\n')) {
      const t = norm(line);
      if (t && t !== out[out.length - 1]) out.push(t);
    }
    return out.join('\n');
  }

  // ---- accessible names ------------------------------------------------------------------
  const plain = (el) => norm(walk(el, false));

  function labelledBy(el) {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    return norm(ids.map((id) => { const n = document.getElementById(id); return n ? plain(n) : ''; }).join(' '));
  }

  function accName(el) {
    const tag = el.tagName.toLowerCase();
    let n = labelledBy(el);
    if (n) return n;
    n = norm(el.getAttribute('aria-label'));
    if (n) return n;
    const isInput = tag === 'input';
    const type = isInput ? (el.type || '').toLowerCase() : '';
    if (isInput && /^(submit|button|reset)$/.test(type)) {
      n = norm(el.value) || (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : '');
      if (n) return n;
    }
    if (isInput && type === 'image') {
      n = norm(el.getAttribute('alt')) || norm(el.value);
      if (n) return n;
    }
    if (isInput || tag === 'select' || tag === 'textarea') {
      const labels = el.labels ? Array.from(el.labels) : [];
      n = norm(labels.map(plain).join(' '));
      if (n) return n;
      const wrap = el.closest('label');
      if (wrap) { n = plain(wrap); if (n) return n; }
    }
    if (!isInput && tag !== 'select' && tag !== 'textarea') {
      n = plain(el);
      if (n) return n;
      const img = el.querySelector('img[alt], [aria-label], svg title');
      if (img) {
        n = norm(img.getAttribute('alt') || img.getAttribute('aria-label') || img.textContent);
        if (n) return n;
      }
    }
    n = norm(el.getAttribute('placeholder')) || norm(el.getAttribute('title')) || norm(el.getAttribute('alt'));
    if (n) return n;
    return norm(el.getAttribute('name')) || norm(el.getAttribute('value')) || '';
  }

  // ---- interactive elements --------------------------------------------------------------
  const INTERACTIVE = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=checkbox]', '[role=menuitem]', '[role=radio]',
    '[role=switch]', '[role=option]', '[role=combobox]', '[role=textbox]',
    '[onclick]', '[contenteditable=""]', '[contenteditable="true"]',
  ].join(',');

  for (const old of document.querySelectorAll('[data-agent-ref]')) old.removeAttribute('data-agent-ref');

  const candidates = [];
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (el === document.body || el === document.documentElement) continue;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    if (!isVisible(el)) continue;
    candidates.push(el);
    if (candidates.length >= MAX_ELEMENTS) break;
  }
  candidates.forEach((el, i) => el.setAttribute('data-agent-ref', String(i + 1)));
  const totalInteractive = candidates.length;

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return t;
      if (/^(button|submit|reset|image)$/.test(t)) return 'button';
      if (t === 'range') return 'slider';
      if (t === 'file') return 'file';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'button';
  }

  function hrefOf(el) {
    const raw = el.getAttribute('href');
    if (!raw || raw.startsWith('#') || /^javascript:/i.test(raw)) return '';
    try {
      const u = new URL(el.href, location.href);
      const path = u.pathname + u.search;
      return clip(u.origin === location.origin ? path : u.host + path, 70);
    } catch (e) { return ''; }
  }

  function isSubmitControl(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return el.type === 'submit';
    if (tag === 'input') return el.type === 'submit' || el.type === 'image';
    return false;
  }

  const formSubmitCache = new Map();
  function submitNamesOf(form) {
    if (!formSubmitCache.has(form)) {
      const names = [];
      for (const b of form.querySelectorAll('button, input[type=submit], input[type=image]')) {
        if (isSubmitControl(b) && isVisible(b)) names.push(clip(accName(b), 60));
      }
      formSubmitCache.set(form, names);
    }
    return formSubmitCache.get(form);
  }

  function errorMessageFor(el) {
    const ids = ((el.getAttribute('aria-errormessage') || '') + ' ' + (el.getAttribute('aria-describedby') || ''))
      .split(/\s+/).filter(Boolean);
    let msg = norm(ids.map((id) => { const n = document.getElementById(id); return n ? plain(n) : ''; }).join(' '));
    if (!msg) {
      const sib = el.nextElementSibling;
      if (sib && /error|invalid|help|hint/i.test(sib.className || '')) msg = plain(sib);
    }
    return clip(msg, 120);
  }

  const elements = [];
  const invalidFields = [];
  for (const el of candidates) {
    const ref = Number(el.getAttribute('data-agent-ref'));
    const tag = el.tagName.toLowerCase();
    const role = roleOf(el);
    const rec = { ref, tag, role, name: clip(accName(el), 80) };

    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      rec.type = t;
      if (role === 'textbox' && el.placeholder) rec.placeholder = clip(el.placeholder, 40);
    } else if (tag === 'textarea' && el.placeholder) {
      rec.placeholder = clip(el.placeholder, 40);
    }

    const flags = [];
    if (tag === 'input' && (rec.type === 'checkbox' || rec.type === 'radio')) {
      flags.push(el.checked ? 'checked' : 'unchecked');
    } else if (/^(checkbox|radio|switch)$/.test(role) && el.hasAttribute('aria-checked')) {
      flags.push(el.getAttribute('aria-checked') === 'true' ? 'checked' : 'unchecked');
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') flags.push('disabled');
    if (el.readOnly) flags.push('readonly');
    if (el.required || el.getAttribute('aria-required') === 'true') flags.push('required');
    if (el.hasAttribute('aria-expanded')) flags.push(el.getAttribute('aria-expanded') === 'true' ? 'expanded' : 'collapsed');
    if (role === 'tab' && el.getAttribute('aria-selected') === 'true') flags.push('selected-tab');
    if (el.getAttribute('aria-pressed') === 'true') flags.push('pressed');
    if (el.getAttribute('aria-invalid') === 'true') {
      flags.push('invalid');
      const msg = errorMessageFor(el);
      if (msg) rec.error = msg;
      invalidFields.push({ name: rec.name, msg });
    }
    if (flags.length) rec.flags = flags;

    if (tag === 'select') {
      const opts = Array.from(el.options);
      rec.optionCount = opts.length;
      rec.options = opts.slice(0, OPTION_CAP).map((o) => clip(norm(o.label || o.text), 40));
      rec.selected = opts.filter((o) => o.selected).map((o) => clip(norm(o.label || o.text), 40)).join(', ');
    } else if (tag === 'input' && rec.type !== 'checkbox' && rec.type !== 'radio' && role !== 'button' && rec.type !== 'file') {
      if (el.value) rec.value = rec.type === 'password' ? '••••' : clip(el.value, 80);
    } else if (tag === 'textarea') {
      if (el.value) rec.value = clip(norm(el.value), 80);
    } else if (el.isContentEditable && !/^(a|button)$/.test(tag)) {
      const v = norm(el.innerText);
      if (v) rec.value = clip(v, 80);
    }

    if (tag === 'a') {
      const h = hrefOf(el);
      if (h) rec.href = h;
    }

    // Context for risk classification.
    const form = el.form || el.closest('form');
    if (form) {
      rec.inForm = true;
      if (isSubmitControl(el)) rec.submit = true;
      else if (tag === 'input' || tag === 'select' || tag === 'textarea') rec.formSubmits = submitNamesOf(form);
    }
    elements.push(rec);
  }

  // ---- alerts ----------------------------------------------------------------------------
  const ALERT_SEL = '[role=alert], [role=status], [class*=error], [class*=alert], [class*=flash], ' +
    '[class*=toast], [class*=notice], [class*=success], [class*=warning], [class*=invalid-feedback]';
  const alerts = [];
  const accepted = [];
  const seenText = new Set();
  for (const el of document.querySelectorAll(ALERT_SEL)) {
    if (alerts.length >= 6) break;
    if (!isVisible(el)) continue;
    if (accepted.some((a) => a.contains(el))) continue;
    const role = el.getAttribute('role');
    const isRole = role === 'alert' || role === 'status';
    if (!isRole && el.querySelector('input, select, textarea, button')) continue;
    let text = plain(el);
    if (!text || seenText.has(text)) continue;
    if (!isRole && text.length > 300) continue;
    seenText.add(text);
    accepted.push(el);
    let kind;
    if (isRole) kind = '[role=' + role + ']';
    else {
      const tok = (el.className && el.className.baseVal === undefined ? String(el.className) : '')
        .split(/\s+/).find((c) => /error|alert|flash|toast|notice|success|warning|invalid-feedback/i.test(c));
      kind = '.' + (tok || 'alert');
    }
    alerts.push({ kind, text: clip(text, 220) });
  }
  for (const f of invalidFields) {
    if (alerts.length >= 8) break;
    if (f.msg && !seenText.has(f.msg)) {
      alerts.push({ kind: 'invalid field', text: clip(f.name + ': ' + f.msg, 220) });
    }
  }

  // ---- page text -------------------------------------------------------------------------
  const root = document.querySelector('main, [role=main]') || document.body;
  let text = collapseLines(walk(root, true));
  const textLen = text.length;
  if (text.length > MAX_TEXT_SCAN) text = text.slice(0, MAX_TEXT_SCAN);

  const ae = document.activeElement;
  const focus = ae && ae.getAttribute ? Number(ae.getAttribute('data-agent-ref')) || null : null;

  return {
    url: location.href,
    title: norm(document.title),
    alerts,
    text,
    textLen,
    elements,
    totalElements: totalInteractive,
    focus,
    scroll: { y: Math.round(window.scrollY), max: Math.max(0, Math.round(document.documentElement.scrollHeight - window.innerHeight)) },
  };
}
