"use strict";
/* qr2pain Webfrontend – ohne Build-Schritt, ohne externe Abhängigkeiten */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nf = new Intl.NumberFormat("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (v) => (v == null || v === "" ? "–" : nf.format(Number(v)));
const dt = (s) => (s ? s.slice(0, 10).split("-").reverse().join(".") : "–");
const dtt = (s) => (s ? `${dt(s)} ${s.slice(11, 16)}` : "");
const today = () => new Date().toISOString().slice(0, 10);
const compact = (v) => {
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toLocaleString("de-CH", { maximumFractionDigits: 1 })} Mio`;
  if (a >= 1e3) return `${(v / 1e3).toLocaleString("de-CH", { maximumFractionDigits: a >= 1e4 ? 0 : 1 })}k`;
  return v.toLocaleString("de-CH", { maximumFractionDigits: 0 });
};
const fmtIban = (s) => (s || "").replace(/\s+/g, "").replace(/(.{4})/g, "$1 ").trim();
const fmtRef = (r, t) => (t === "QRR" && r ? r.replace(/^(\d{2})(\d{5})(\d{5})(\d{5})(\d{5})(\d{5})$/, "$1 $2 $3 $4 $5 $6") : r || "");

const S = { me: null, list: [], filter: "ready", q: "", sort: ["due", 1], sel: new Set(), cur: null, stats: null, ccy: null };

// ================================================================ API
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin", headers: { "X-Requested-With": "qr2pain" } };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(`api/${path}`, init);
  if (r.status === 401 && path !== "login") { showLogin(); throw new Error("Nicht angemeldet"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    const d = data?.detail;
    const e = new Error(Array.isArray(d) ? d.join("\n") : typeof d === "string" ? d : `Fehler ${r.status}`);
    e.list = Array.isArray(d) ? d : null;
    throw e;
  }
  return data;
}

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = `toast${err ? " err" : ""}`;
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 8000 : 4000);
}

function confirmModal(title, html, okLabel = "OK", danger = false) {
  return new Promise((res) => {
    $("#m-title").textContent = title;
    $("#m-body").innerHTML = html;
    const ok = $("#m-ok");
    ok.textContent = okLabel;
    ok.className = `btn ${danger ? "danger" : "primary"}`;
    ok.hidden = !okLabel;
    $("#modal").hidden = false;
    const done = (v) => { $("#modal").hidden = true; ok.onclick = $("#m-cancel").onclick = null; res(v); };
    ok.onclick = () => done(true);
    $("#m-cancel").onclick = () => done(false);
    (okLabel ? ok : $("#m-cancel")).focus();
  });
}

// ================================================================ Login
function showLogin() {
  $("#app").hidden = true; $("#drawer").hidden = true; $("#scrim").hidden = true;
  $("#login").hidden = false;
  $("#login-form [name=username]").focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  $("#login-error").textContent = "";
  try {
    await api("login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    ev.target.reset();
    start();
  } catch (e) { $("#login-error").textContent = e.message; }
});

$("#btn-logout").addEventListener("click", async () => { await api("logout", { method: "POST" }).catch(() => {}); showLogin(); });

async function start() {
  try { S.me = await api("me"); } catch { return; }
  $("#login").hidden = true; $("#app").hidden = false;
  $("#user").textContent = S.me.user;
  await loadList();
  pollSync(true);
}

// ================================================================ Navigation
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
function showView(v) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === v));
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${v}`));
  if (v === "stats") loadStats();
  if (v === "exports") loadExports();
  if (v === "payments") loadList();
}

// ================================================================ Sync
$("#btn-sync").addEventListener("click", async () => {
  try { await api("sync", { method: "POST" }); pollSync(); } catch (e) { toast(e.message, true); }
});
let syncTimer = null;
async function pollSync(silent = false) {
  clearTimeout(syncTimer);
  let st;
  try { st = await api("sync"); } catch { return; }
  const el = $("#sync-status");
  if (st.running) {
    $("#btn-sync").disabled = true;
    el.textContent = `Lade aus paperless… ${st.done}/${st.total || "?"}`;
    syncTimer = setTimeout(() => pollSync(), 1000);
    S.syncing = true;
  } else {
    $("#btn-sync").disabled = false;
    el.textContent = st.finished_at ? `Stand ${dtt(st.finished_at)}` : "";
    el.title = st.message || "";
    if (S.syncing) {
      S.syncing = false;
      toast(st.message || "Fertig", !!st.failed);
      (st.errors || []).forEach((m) => toast(m, true));
      const v = $(".tabs [aria-selected=true]").dataset.view;
      showView(v);
    } else if (!silent) { /* nichts */ }
  }
}

// ================================================================ Zahlungsliste
async function loadList() {
  try { S.list = await api("invoices?status=open"); } catch (e) { return toast(e.message, true); }
  const keys = new Set(allUnits().map((u) => u.key));
  S.sel.forEach((k) => keys.has(k) || S.sel.delete(k));
  renderList();
  if (S.cur && S.list.some((x) => x.doc_id === S.cur)) openDrawer(S.cur, true);
}

const FILTERS = {
  ready: (x) => x.exportable,
  problem: (x) => !x.held && x.errors.length > 0,
  held: (x) => x.held,
  all: () => true,
};
const SORTS = {
  creditor: (x) => (x.effective.creditor.name || x.title || "").toLowerCase(),
  reference: (x) => x.effective.reference || "",
  due: (x) => x.due_date || "9999",
  exec: (x) => x.effective.execution_date || "9999",
  amount: (x) => Number(x.effective.amount || 0),
};

$$("#filter button").forEach((b) => b.addEventListener("click", () => {
  S.filter = b.dataset.f;
  $$("#filter button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  renderList();
}));
$("#search").addEventListener("input", (e) => { S.q = e.target.value.toLowerCase(); renderList(); });
$$("#tbl th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  S.sort = [k, S.sort[0] === k ? -S.sort[1] : 1];
  renderList();
}));

function visible() {
  const f = FILTERS[S.filter];
  const [k, dir] = S.sort;
  return S.list
    .filter(f)
    .filter((x) => !S.q || [x.effective.creditor.name, x.title, x.correspondent, x.effective.reference,
      x.effective.message, x.effective.iban, String(x.doc_id)].join(" ").toLowerCase().includes(S.q))
    .sort((a, b) => (SORTS[k](a) > SORTS[k](b) ? dir : SORTS[k](a) < SORTS[k](b) ? -dir : 0));
}

// Zahlungseinheiten: ganze Rechnung oder einzelne offene Raten
function units(x) {
  if (!x.split) return [{ key: String(x.doc_id), x, part: null, amount: x.effective.amount,
    date: x.effective.execution_date, ccy: x.effective.currency, ok: x.exportable }];
  return x.parts.filter((p) => !p.exported).map((p) => ({ key: `${x.doc_id}:${p.no}`, x, part: p,
    amount: p.amount, date: p.date, ccy: x.effective.currency, ok: p.exportable }));
}
const allUnits = () => S.list.flatMap(units);
const selUnits = () => allUnits().filter((u) => S.sel.has(u.key));

function badges(x) {
  const b = [];
  if (x.held) b.push(`<span class="badge held">Zurückgestellt</span>`);
  if (x.errors.length) {
    const dup = x.errors.some((e) => e.startsWith("Mögliches Duplikat"));
    b.push(`<span class="badge err" title="${esc(x.errors.join("\n"))}">${dup ? "Duplikat?" : "Fehler"}</span>`);
  }
  if (x.warnings.length) b.push(`<span class="badge warn" title="${esc(x.warnings.join("\n"))}">Hinweis</span>`);
  if (x.overridden.length) b.push(`<span class="badge chg" title="Geändert: ${esc(x.overridden.join(", "))}">Geändert</span>`);
  if (x.split) b.push(`<span class="badge split">Raten ${x.parts_done}/${x.parts.length}</span>`);
  if (x.exportable && !x.split) b.unshift(`<span class="badge ok">Bereit</span>`);
  return `<div class="badges">${b.join("")}</div>`;
}

function partBadge(x, p) {
  if (p.exported) return `<span class="badge">Exportiert #${p.export_id}</span>`;
  if (p.errors.length) return `<span class="badge err" title="${esc(p.errors.join("\n"))}">${esc(p.errors[0])}</span>`;
  if (p.exportable) return `<span class="badge ok">Bereit</span>`;
  return x.held ? `<span class="badge held">Zurückgestellt</span>` : `<span class="badge err">Rechnung prüfen</span>`;
}

function renderList() {
  for (const [k, f] of Object.entries(FILTERS)) {
    const n = S.list.filter(f).length;
    const p = $(`#filter [data-c=${k}]`);
    p.textContent = n ? String(n) : "";
  }
  $("#count-open").textContent = S.list.length || "";
  $$("#tbl th[data-sort]").forEach((th) => th.classList.toggle("asc", th.dataset.sort === S.sort[0] && S.sort[1] > 0));
  $$("#tbl th[data-sort]").forEach((th) => th.classList.toggle("desc", th.dataset.sort === S.sort[0] && S.sort[1] < 0));

  const rows = visible();
  const t = today();
  $("#tbl tbody").innerHTML = rows.map((x) => {
    const e = x.effective, o = x.original || {};
    const amtChanged = x.overridden.includes("amount");
    const overdue = x.due_date && x.due_date < t && !x.split;
    const us = units(x);
    const nSel = us.filter((u) => S.sel.has(u.key)).length;
    const main = `<tr data-id="${x.doc_id}" class="${S.cur === x.doc_id ? "active" : ""} ${x.split ? "has-parts" : ""}">
      <td class="chk"><input type="checkbox" data-doc="${x.doc_id}" ${us.length && nSel === us.length ? "checked" : ""}
        ${nSel && nSel < us.length ? 'data-indet="1"' : ""} ${us.length ? "" : "disabled"} aria-label="Auswählen"></td>
      <td><div>${esc(e.creditor.name || x.correspondent || "–")}</div><div class="sub">#${x.doc_id} · ${esc(x.title)}</div></td>
      <td class="hide-sm"><div class="mono">${esc(fmtRef(e.reference, e.ref_type) || "—")}</div>
        <div class="sub">${esc(e.message || "")}</div></td>
      <td><div class="${overdue ? "overdue-txt" : ""}">${dt(x.due_date)}</div>
        <div class="sub">${x.due_estimated && x.due_date ? "geschätzt" : overdue ? "überfällig" : ""}</div></td>
      <td>${x.split ? `<div>${x.parts.length} Raten</div><div class="sub">nächste ${dt(e.execution_date)}</div>`
        : `<div class="${x.overridden.includes("execution_date") ? "changed-val" : ""}">${dt(e.execution_date)}</div>`}</td>
      <td class="num"><span class="ccy">${esc(e.currency)}</span><span class="${amtChanged ? "changed-val" : ""}">${money(e.amount)}</span>
        ${x.split ? `<div class="sub">offen ${money(x.open_amount)}</div>`
          : amtChanged && o.amount ? `<div class="sub">QR ${money(o.amount)}</div>` : x.amount_source === "paperless-Feld" ? `<div class="sub">aus paperless</div>` : ""}</td>
      <td>${badges(x)}</td></tr>`;
    const subs = x.split ? x.parts.map((p) => {
      const key = `${x.doc_id}:${p.no}`;
      return `<tr class="part ${p.exported ? "done" : ""}" data-id="${x.doc_id}">
        <td class="chk">${p.exported ? "" : `<input type="checkbox" data-key="${key}" ${S.sel.has(key) ? "checked" : ""} aria-label="Rate ${p.no} auswählen">`}</td>
        <td colspan="2" class="part-label">Rate ${p.no} von ${p.of}</td>
        <td></td>
        <td>${dt(p.date)}</td>
        <td class="num"><span class="ccy">${esc(e.currency)}</span>${money(p.amount)}</td>
        <td><div class="badges">${partBadge(x, p)}</div></td></tr>`;
    }).join("") : "";
    return main + subs;
  }).join("");
  $$("#tbl tbody input[data-indet]").forEach((cb) => (cb.indeterminate = true));
  $("#empty").hidden = rows.length > 0;
  $("#empty").textContent = S.list.length ? "Keine Rechnungen in dieser Ansicht." :
    "Keine offenen Rechnungen. Setze in paperless das Tag «zu zahlen» und klicke «Aus paperless laden».";
  const vis = rows.flatMap(units);
  $("#chk-all").checked = vis.length > 0 && vis.every((u) => S.sel.has(u.key));
  renderSel();
}

$("#tbl tbody").addEventListener("click", (ev) => {
  const cb = ev.target.closest("input[type=checkbox]");
  if (cb) {
    if (cb.dataset.key) {
      cb.checked ? S.sel.add(cb.dataset.key) : S.sel.delete(cb.dataset.key);
    } else {
      const x = S.list.find((y) => y.doc_id === Number(cb.dataset.doc));
      units(x).forEach((u) => (cb.checked ? S.sel.add(u.key) : S.sel.delete(u.key)));
    }
    renderList();
    return;
  }
  const tr = ev.target.closest("tr[data-id]");
  if (tr) openDrawer(Number(tr.dataset.id));
});
$("#chk-all").addEventListener("change", (ev) => {
  visible().flatMap(units).forEach((u) => (ev.target.checked ? S.sel.add(u.key) : S.sel.delete(u.key)));
  renderList();
});

function sums(us) {
  const s = {};
  us.forEach((u) => (s[u.ccy] = (s[u.ccy] || 0) + Number(u.amount || 0)));
  return Object.entries(s).map(([c, v]) => `${c} ${money(v)}`).join(" · ");
}

function renderSel() {
  const us = selUnits();
  $("#selbar").hidden = us.length === 0;
  if (!us.length) return;
  const ok = us.filter((u) => u.ok);
  const docs = new Set(us.map((u) => u.x.doc_id));
  const nParts = us.filter((u) => u.part).length;
  $("#sel-info").innerHTML = `<b>${us.length}</b> Zahlung${us.length > 1 ? "en" : ""} ausgewählt`
    + (nParts ? ` <span class="muted">(davon ${nParts} Rate${nParts > 1 ? "n" : ""})</span>` : "") + ` · ${esc(sums(us))}`;
  $("#btn-export").textContent = `pain.001 erstellen (${ok.length})`;
  $("#btn-export").disabled = ok.length === 0;
  const xs = S.list.filter((x) => docs.has(x.doc_id));
  $("#btn-hold").hidden = !xs.some((x) => !x.held);
  $("#btn-release").hidden = !xs.some((x) => x.held);
}

async function setHeld(ids, held) {
  try {
    await api("invoices/hold", { method: "POST", body: { ids, held } });
    toast(held ? `${ids.length} zurückgestellt` : `${ids.length} freigegeben`);
    await loadList();
  } catch (e) { toast(e.message, true); }
}
const selDocs = () => [...new Set(selUnits().map((u) => u.x.doc_id))];
$("#btn-hold").addEventListener("click", () => setHeld(selDocs(), true));
$("#btn-release").addEventListener("click", () => setHeld(selDocs(), false));

// ================================================================ Export
$("#btn-export").addEventListener("click", async () => {
  const us = selUnits();
  const ok = us.filter((u) => u.ok);
  const skip = us.filter((u) => !u.ok);
  const byDate = {};
  ok.forEach((u) => {
    const k = `${u.date}|${u.ccy}`;
    byDate[k] = byDate[k] || { n: 0, s: 0 };
    byDate[k].n++; byDate[k].s += Number(u.amount);
  });
  const warn = ok.filter((u) => u.x.warnings.length);
  const nParts = ok.filter((u) => u.part).length;
  const html = `
    <p>Belastungskonto <b>${esc(S.me.debtor.name)}</b>, <span class="mono">${esc(fmtIban(S.me.debtor.iban))}</span></p>
    <table class="grid"><thead><tr><th>Ausführung</th><th>Währung</th><th class="num">Anzahl</th><th class="num">Summe</th></tr></thead><tbody>
    ${Object.entries(byDate).sort().map(([k, v]) => { const [d, c] = k.split("|");
      return `<tr><td>${dt(d)}</td><td>${c}</td><td class="num">${v.n}</td><td class="num">${money(v.s)}</td></tr>`; }).join("")}
    </tbody></table>
    ${nParts ? `<p class="msg info"><b>i</b><span>${nParts} Rate${nParts > 1 ? "n" : ""} enthalten. Die Rechnung gilt in paperless erst nach der letzten Rate als exportiert.</span></p>` : ""}
    ${warn.length ? `<p class="msg warn"><b>!</b><span>${warn.length} Zahlung(en) mit Hinweisen (z.&nbsp;B. Betrag geändert, nach Fälligkeit).</span></p>` : ""}
    ${skip.length ? `<p class="msg err"><b>✕</b><span>${skip.length} ausgewählte Zahlung(en) werden <b>nicht</b> exportiert (Fehler oder zurückgestellt).</span></p>` : ""}
    <p class="muted small">Lade die Datei danach im E-Banking hoch. Falls der Upload scheitert, kannst du den Export
    unter «Exporte» rückgängig machen.</p>`;
  if (!(await confirmModal(`pain.001 mit ${ok.length} Zahlungen erstellen`, html, "Erstellen & herunterladen"))) return;
  try {
    const r = await api("exports", { method: "POST", body: { items: ok.map((u) => u.key) } });
    download(r.id);
    toast(`Export #${r.id} erstellt: ${r.count} Zahlungen`);
    r.warnings.forEach((w) => toast(w, true));
    ok.forEach((u) => S.sel.delete(u.key));
    await loadList();
  } catch (e) {
    confirmModal("Export nicht möglich", `<ul>${(e.list || [e.message]).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`, "");
  }
});

function download(id) {
  const a = document.createElement("a");
  a.href = `api/exports/${id}/xml`;
  a.download = "";
  document.body.append(a); a.click(); a.remove();
}

// ================================================================ Detailansicht
$("#d-close").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#modal").hidden) closeDrawer(); });
function closeDrawer() {
  $("#drawer").hidden = true; $("#scrim").hidden = true; S.cur = null;
  $("#d-doc").innerHTML = ""; delete $("#d-doc").dataset.loaded;
  $$("#tbl tr.active").forEach((r) => r.classList.remove("active"));
}
$$(".subtabs button").forEach((b) => b.addEventListener("click", () => subtab(b.dataset.dt)));
function subtab(t) {
  $$(".subtabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.dt === t));
  ["pay", "doc", "log"].forEach((k) => ($(`#d-${k}`).hidden = k !== t));
  if (t === "doc" && !$("#d-doc").dataset.loaded) loadPreview(S.cur);
}

async function loadPreview(id) {
  const el = $("#d-doc");
  el.dataset.loaded = "1";
  el.innerHTML = `<p class="muted pad">Vorschau wird geladen…</p>`;
  try {
    const { pages } = await api(`invoices/${id}/pages`);
    if (S.cur !== id) return;
    const n = Math.min(pages, 10);
    el.innerHTML = `<div class="doc-bar"><span class="muted small">${pages} Seite${pages > 1 ? "n" : ""}${pages > n ? ` (erste ${n} angezeigt)` : ""}</span>
      <a class="btn" href="api/invoices/${id}/pdf" target="_blank" rel="noopener">PDF öffnen ↗</a></div>
      <div class="pages">${Array.from({ length: n }, (_, i) => i + 1)
        .map((p) => `<img src="api/invoices/${id}/page/${p}.png" alt="Seite ${p}" loading="lazy">`).join("")}</div>`;
  } catch (e) {
    el.innerHTML = `<p class="msg err pad"><b>✕</b><span>${esc(e.message)}</span></p>`;
  }
}

// Formularfelder: [override-Key, Label, Klasse, Wert(eff), Original, Typ]
function fields(x) {
  const e = x.effective, o = x.original, c = e.creditor, oc = o?.creditor || {};
  return {
    pay: [
      ["amount", "Betrag", "s2", e.amount ?? "", o?.amount ?? null, "amount"],
      ["currency", "Währung", "s1", e.currency, o?.currency ?? null, "ccy"],
      ...(x.split ? [] : [["execution_date", "Ausführungsdatum", "s3", e.execution_date ?? "", null, "date"]]),
    ],
    cred: [
      ["iban", "IBAN / QR-IBAN", "", fmtIban(e.iban), o ? fmtIban(o.iban) : null, "iban"],
      ["creditor_name", "Name", "", c.name, o ? oc.name : null],
      ["creditor_street", "Strasse", "s4", c.street, o ? oc.street : null],
      ["creditor_building", "Nr.", "s2", c.building, o ? oc.building : null],
      ["creditor_postal_code", "PLZ", "s2", c.postal_code, o ? oc.postal_code : null],
      ["creditor_town", "Ort", "s3", c.town, o ? oc.town : null],
      ["creditor_country", "Land", "s1", c.country, o ? oc.country : null],
    ],
    ref: [
      ["reference", "Referenz (QR-Referenz, RF… oder leer)", "", fmtRef(e.reference, e.ref_type), o ? fmtRef(o.reference, o.ref_type) : null, "ref"],
      ["message", "Mitteilung", "", e.message, o ? o.message : null],
    ],
  };
}

function fieldHtml(x, [key, label, cls, val, orig, type]) {
  const changed = x.overridden.includes(key);
  let input;
  if (type === "ccy") {
    input = `<select name="${key}">${["CHF", "EUR"].map((v) => `<option ${v === val ? "selected" : ""}>${v}</option>`).join("")}</select>`;
  } else if (type === "date") {
    input = `<input name="${key}" type="date" value="${esc(val)}" min="${today()}">`;
  } else {
    input = `<input name="${key}" value="${esc(val)}" ${type === "amount" ? 'inputmode="decimal"' : ""} ${type === "iban" || type === "ref" ? 'class="mono" spellcheck="false"' : ""}>`;
  }
  let hint = "";
  if (key === "execution_date") hint = changed ? "manuell gesetzt" : "aus Fälligkeit berechnet";
  else if (key === "amount" && !changed) hint = x.amount_source ? `aus ${x.amount_source}` : "";
  else if (changed) hint = `QR: ${orig === null ? "–" : esc(orig || "(leer)")}`;
  const reset = changed ? `<button type="button" class="btn link" data-reset="${key}">Zurücksetzen</button>` : "";
  return `<label class="f ${cls} ${changed ? "changed" : ""}">${label}${input}<span class="hint"><span>${hint}</span>${reset}</span></label>`;
}

async function openDrawer(id, keepTab = false) {
  let x;
  try { x = await api(`invoices/${id}`); } catch (e) { return toast(e.message, true); }
  const prev = S.cur;
  S.cur = id;
  $$("#tbl tbody tr").forEach((r) => r.classList.toggle("active", Number(r.dataset.id) === id));
  $("#drawer").hidden = false; $("#scrim").hidden = window.innerWidth > 1100;
  if (!keepTab || prev !== id) { $("#d-doc").innerHTML = ""; delete $("#d-doc").dataset.loaded; }
  if (!keepTab) subtab("pay");
  const e = x.effective;
  $("#d-title").textContent = e.creditor.name || x.title;
  $("#d-sub").textContent = `${e.currency} ${money(e.amount)} · Dokument #${x.doc_id}${x.asn ? ` · ASN ${x.asn}` : ""}`;
  const F = fields(x);
  const ro = x.status !== "open";
  const lock = ro || x.parts_done > 0;   // nach der ersten exportierten Rate nur noch Raten änderbar
  const dupErr = x.errors.some((m) => m.startsWith("Mögliches Duplikat")) || x.overrides.dup_ok;
  $("#d-pay").innerHTML = `
    <div class="msgs">
      ${x.errors.map((m) => `<div class="msg err"><b>✕</b><span>${esc(m)}</span></div>`).join("")}
      ${x.warnings.map((m) => `<div class="msg warn"><b>!</b><span>${esc(m)}</span></div>`).join("")}
    </div>
    <form id="d-form" autocomplete="off">
      ${x.parts_done > 0 && !ro ? `<p class="msg info"><b>i</b><span>${x.parts_done} Rate(n) bereits exportiert – Betrag und Empfängerdaten sind gesperrt, offene Raten bleiben änderbar.</span></p>` : ""}
      <fieldset ${lock ? "disabled" : ""}><legend>Zahlung</legend><div class="fgrid">${F.pay.map((f) => fieldHtml(x, f)).join("")}
        ${x.split ? `<p class="f s3 muted small split-hint">Ausführungsdatum pro Rate, siehe unten</p>` : ""}</div></fieldset>
      <fieldset ${ro ? "disabled" : ""} class="split-fs"><legend>Aufteilung in Raten</legend><div id="d-split"></div></fieldset>
      <fieldset ${lock ? "disabled" : ""}><legend>Empfänger</legend><div class="fgrid">${F.cred.map((f) => fieldHtml(x, f)).join("")}</div></fieldset>
      <fieldset ${lock ? "disabled" : ""}><legend>Referenz</legend><div class="fgrid">${F.ref.map((f) => fieldHtml(x, f)).join("")}
        ${e.bill_info ? `<label class="f">Rechnungsinformationen<input value="${esc(e.bill_info)}" class="mono" readonly></label>` : ""}</div></fieldset>
      <fieldset ${ro ? "disabled" : ""}><legend>Intern</legend><div class="fgrid">
        <label class="f">Kommentar <textarea name="comment" rows="2" placeholder="z. B. Skonto 2 % abgezogen">${esc(x.overrides.comment || "")}</textarea></label>
        ${dupErr ? `<label class="check f"><input type="checkbox" name="dup_ok" ${x.overrides.dup_ok ? "checked" : ""}> Kein Duplikat – trotzdem zahlen</label>` : ""}
      </div></fieldset>
      <dl class="kv">
        <dt>Dokument</dt><dd>#${x.doc_id} · ${esc(x.title)}</dd>
        <dt>Korrespondent</dt><dd>${esc(x.correspondent || "–")}</dd>
        <dt>Dokumentdatum</dt><dd>${dt(x.created)}</dd>
        <dt>Fällig</dt><dd>${dt(x.due_date)}${x.due_estimated && x.due_date ? " (geschätzt aus Zahlungsfrist)" : ""}</dd>
        <dt>Kontotyp</dt><dd>${e.iban ? (e.qr_iban ? "QR-IBAN" : "IBAN") : "–"} · Referenztyp ${esc(e.ref_type)}</dd>
      </dl>
      <div class="actions">
        ${ro ? `<span class="muted">Exportiert mit Export #${x.export_id}</span>` : `
        <button class="btn primary" type="submit">Speichern</button>
        ${x.overridden.length && !lock ? `<button class="btn" type="button" id="d-resetall">Alle Korrekturen verwerfen</button>` : ""}
        <button class="btn" type="button" id="d-hold">${x.held ? "Freigeben" : "Zurückstellen"}</button>`}
        <a class="btn ghost" href="${esc(S.me.paperless_url)}/documents/${x.doc_id}/details" target="_blank" rel="noopener">In paperless öffnen ↗</a>
      </div>
    </form>`;
  $("#d-log").innerHTML = x.audit.length
    ? `<ul class="log">${x.audit.map((a) => `<li><div><b>${esc(a.action)}</b> · ${esc(a.user)}</div>
        <div class="when">${dtt(a.ts)}</div>${a.detail ? `<div class="small">${esc(a.detail)}</div>` : ""}</li>`).join("")}</ul>`
    : `<p class="muted">Noch keine Einträge.</p>`;

  if (S.planDoc !== x.doc_id || !keepTab) S.plan = null;
  S.planDoc = x.doc_id;
  renderPlan(x);
  if (ro) return;
  const form = $("#d-form");
  form.addEventListener("submit", (ev) => { ev.preventDefault(); save(x, form); });
  $$("[data-reset]", form).forEach((b) => b.addEventListener("click", () => patch(x.doc_id, { [b.dataset.reset]: null })));
  $("#d-resetall")?.addEventListener("click", async () => {
    if (await confirmModal("Alle Korrekturen verwerfen?", "<p>Es gelten wieder die Werte aus dem QR-Code.</p>", "Verwerfen", true)) {
      patch(x.doc_id, Object.fromEntries(x.overridden.map((k) => [k, null])));
    }
  });
  $("#d-hold").addEventListener("click", async () => { await setHeld([x.doc_id], !x.held); });
}

// ================================================================ Raten
const pad2 = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
function addInterval(iso, i, mode) {
  const [y, m, d] = iso.split("-").map(Number);
  let r;
  if (mode === "m" || mode === "q") {
    const step = mode === "q" ? 3 : 1;
    const last = new Date(Date.UTC(y, m - 1 + step * i + 1, 0)).getUTCDate();
    r = new Date(Date.UTC(y, m - 1 + step * i, Math.min(d, last)));
  } else {
    r = new Date(Date.UTC(y, m - 1, d + i * (mode === "w" ? 7 : 14)));
  }
  while (r.getUTCDay() === 0 || r.getUTCDay() === 6) r.setUTCDate(r.getUTCDate() + 1);  // Wochenende -> Montag
  return isoOf(r);
}
const cents = (v) => Math.round(Number(String(v ?? "").replace(/['’\s]/g, "").replace(",", ".")) * 100) || 0;
const fromCents = (c) => (c / 100).toFixed(2);

function distribute(rows, total) {
  // verteilt den noch offenen Betrag gleichmässig auf die offenen Raten, Rundungsrest auf die letzte
  const done = rows.filter((r) => r.exported).reduce((s, r) => s + cents(r.amount), 0);
  const open = rows.filter((r) => !r.exported);
  if (!open.length) return;
  const rest = cents(total) - done, base = Math.floor(rest / open.length);
  open.forEach((r, i) => (r.amount = fromCents(i === open.length - 1 ? rest - base * (open.length - 1) : base)));
}

function renderPlan(x) {
  const el = $("#d-split");
  if (!el) return;
  const ro = x.status !== "open";
  const e = x.effective;
  if (!S.plan && x.split) S.plan = { rows: x.parts.map((p) => ({ ...p })), dirty: false };
  if (!S.plan) {
    el.innerHTML = ro ? `<p class="muted small">Nicht aufgeteilt.</p>` : `
      <p class="muted small">Grössere Beträge auf mehrere Zahlungen verteilen. In paperless wird das Tag «Ratenzahlung» gesetzt.</p>
      <div class="fgrid">
        <label class="f s1">Raten<input type="number" id="g-n" min="2" max="60" value="3"></label>
        <label class="f s2">Abstand<select id="g-int">
          <option value="m">monatlich</option><option value="2w">alle 2 Wochen</option>
          <option value="w">wöchentlich</option><option value="q">vierteljährlich</option></select></label>
        <label class="f s3">Erste Rate am<input type="date" id="g-first" value="${esc(e.execution_date || today())}" min="${today()}"></label>
      </div>
      <button type="button" class="btn" id="g-go" ${e.amount ? "" : "disabled"}>Raten vorschlagen</button>`;
    $("#g-go")?.addEventListener("click", () => {
      const n = Math.max(2, Math.min(60, Number($("#g-n").value) || 2));
      const first = $("#g-first").value || today();
      const rows = Array.from({ length: n }, (_, i) => ({ date: addInterval(first, i, $("#g-int").value), amount: "0", exported: false }));
      distribute(rows, e.amount);
      S.plan = { rows, dirty: true };
      renderPlan(x);
    });
    return;
  }
  const rows = S.plan.rows;
  const sum = rows.reduce((s, r) => s + cents(r.amount), 0), diff = cents(e.amount) - sum;
  el.innerHTML = `
    <table class="grid plan"><thead><tr><th>Rate</th><th>Ausführung</th><th class="num">Betrag ${esc(e.currency)}</th><th></th></tr></thead><tbody>
    ${rows.map((r, i) => r.exported
      ? `<tr class="done"><td>${i + 1}</td><td>${dt(r.date)}</td><td class="num">${money(r.amount)}</td>
           <td><span class="badge">Export #${r.export_id}</span></td></tr>`
      : `<tr><td>${i + 1}</td>
           <td><input type="date" data-i="${i}" data-f="date" value="${esc(r.date || "")}" min="${today()}" ${ro ? "disabled" : ""}></td>
           <td class="num"><input data-i="${i}" data-f="amount" value="${esc(r.amount)}" inputmode="decimal" class="num-in" ${ro ? "disabled" : ""}></td>
           <td>${ro || rows.filter((y) => !y.exported).length <= 1 ? "" : `<button type="button" class="btn link" data-del="${i}" aria-label="Rate ${i + 1} entfernen">Entfernen</button>`}</td></tr>`).join("")}
    </tbody><tfoot><tr><td colspan="2">Summe</td><td class="num"><b>${money(fromCents(sum))}</b></td>
      <td class="${diff ? "diff-bad" : "diff-ok"}">${diff ? `Differenz ${money(fromCents(diff))}` : "✓ stimmt"}</td></tr></tfoot></table>
    ${ro ? "" : `<div class="plan-actions">
      <button type="button" class="btn" id="p-add">+ Rate</button>
      <button type="button" class="btn" id="p-dist">Offenen Betrag gleichmässig verteilen</button>
      <span class="spacer"></span>
      ${S.plan.dirty ? `<button type="button" class="btn" id="p-cancel">Verwerfen</button>` : ""}
      ${!S.plan.dirty && x.split && !x.parts_done ? `<button type="button" class="btn danger" id="p-remove">Aufteilung entfernen</button>` : ""}
      <button type="button" class="btn primary" id="p-save" ${S.plan.dirty ? "" : "disabled"}>Aufteilung speichern</button></div>`}`;
  if (ro) return;
  $$("#d-split input[data-i]").forEach((inp) => inp.addEventListener("change", () => {
    rows[Number(inp.dataset.i)][inp.dataset.f] = inp.value.trim();
    S.plan.dirty = true;
    renderPlan(x);
  }));
  $$("#d-split [data-del]").forEach((b) => b.addEventListener("click", () => {
    rows.splice(Number(b.dataset.del), 1); S.plan.dirty = true; renderPlan(x);
  }));
  $("#p-add").addEventListener("click", () => {
    const last = rows[rows.length - 1];
    rows.push({ date: addInterval(last?.date || today(), 1, "m"), amount: "0", exported: false });
    distribute(rows, e.amount); S.plan.dirty = true; renderPlan(x);
  });
  $("#p-dist").addEventListener("click", () => { distribute(rows, e.amount); S.plan.dirty = true; renderPlan(x); });
  $("#p-cancel")?.addEventListener("click", () => { S.plan = null; renderPlan(x); });
  $("#p-remove")?.addEventListener("click", async () => {
    if (!(await confirmModal("Aufteilung entfernen?", "<p>Die Rechnung wird wieder als eine Zahlung geführt.</p>", "Entfernen", true))) return;
    S.plan = null; await patch(x.doc_id, { installments: null });
  });
  $("#p-save").addEventListener("click", async () => {
    if (rows.length < 2) return toast("Mindestens zwei Raten nötig", true);
    const plan = rows.map((r) => ({ amount: fromCents(cents(r.amount)), date: r.date }));
    S.plan = null;
    await patch(x.doc_id, { installments: plan });
  });
}

const norm = (k, v) => {
  v = String(v ?? "").trim();
  if (k === "iban" || k === "reference") return v.replace(/\s+/g, "").toUpperCase();
  if (k === "amount") return v === "" ? "" : Number(v.replace(/['’\s]/g, "").replace(",", ".")).toFixed(2);
  if (k === "creditor_country") return v.toUpperCase();
  return v;
};

async function save(x, form) {
  const changes = {};
  const all = Object.values(fields(x)).flat();
  for (const [key, , , val, orig] of all) {
    const now = norm(key, form.elements[key].value);
    if (now === norm(key, val)) continue;
    changes[key] = orig !== null && now === norm(key, orig) ? null : form.elements[key].value.trim();
    if (key === "iban" || key === "reference") changes[key] = changes[key] === null ? null : now;
  }
  const cmt = form.elements.comment.value.trim();
  if (cmt !== (x.overrides.comment || "")) changes.comment = cmt || null;
  if (form.elements.dup_ok && form.elements.dup_ok.checked !== !!x.overrides.dup_ok) changes.dup_ok = form.elements.dup_ok.checked || null;
  if (!Object.keys(changes).length) return toast("Keine Änderungen");
  await patch(x.doc_id, changes);
}

async function patch(id, changes) {
  try {
    const r = await api(`invoices/${id}`, { method: "PATCH", body: changes });
    toast("Gespeichert");
    (r.tag_warnings || []).forEach((w) => toast(w, true));
    await loadList();
    await openDrawer(id, true);
  } catch (e) { toast(e.message, true); }
}

// ================================================================ Auswertungen
async function loadStats() {
  try { S.stats = await api("stats"); } catch (e) { return toast(e.message, true); }
  const cs = S.stats.currencies;
  if (!S.ccy || !cs.includes(S.ccy)) S.ccy = cs.includes("CHF") ? "CHF" : cs[0];
  $("#ccy").innerHTML = cs.map((c) => `<button data-c="${c}" aria-pressed="${c === S.ccy}">${c}</button>`).join("");
  $$("#ccy button").forEach((b) => b.addEventListener("click", () => { S.ccy = b.dataset.c; loadStats(); }));
  renderStats();
}

function renderStats() {
  const st = S.stats, c = S.ccy, k = st.kpi;
  const v = (o) => Number(o?.[c] || 0);
  $("#tiles").innerHTML = [
    ["Offen zur Zahlung", `${c} ${money(v(k.open))}`, `${k.open_count} Rechnungen, ${k.exportable} bereit`],
    ["Fällig in 7 Tagen", `${c} ${money(v(k.due7))}`, "inkl. überfällige"],
    ["Überfällig", `${c} ${money(v(k.overdue))}`, `${k.overdue_count} Rechnungen`, k.overdue_count > 0],
    ["Zurückgestellt", `${c} ${money(v(k.held))}`, `${k.held_count} Rechnungen`],
    ["Mit Problemen", String(k.errors), "Fehler oder Duplikate", k.errors > 0],
  ].map(([l, val, f, alert]) => `<div class="tile ${alert ? "alert" : ""}"><div class="label">${l}</div>
      <div class="value">${esc(val)}</div><div class="foot">${esc(f)}</div></div>`).join("");

  barChart($("#ch-liq"), st.liquidity.map((b) => {
    const val = v(b.sum);
    let range = "";
    if (b.from) { const f = new Date(b.from); const t = new Date(f); t.setDate(t.getDate() + 6);
      range = ` (${dt(b.from)} – ${dt(t.toISOString())})`; }
    return { label: b.label, value: val, cls: b.key === "overdue" ? "overdue" : "",
      tip: `${b.label}${range}: ${c} ${money(val)}` };
  }), { height: 220, empty: `Keine offenen Zahlungen in ${c}` });

  hbars($("#ch-cred"), st.creditors.filter((x) => v(x.sum) > 0).sort((a, b) => v(b.sum) - v(a.sum)).slice(0, 8)
    .map((x) => ({ name: x.name, value: v(x.sum), meta: `${x.count} Rechnung${x.count > 1 ? "en" : ""}${x.oldest_due ? `, älteste fällig ${dt(x.oldest_due)}` : ""}` })),
    c, "Keine offenen Rechnungen in " + c);

  const mn = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];
  barChart($("#ch-hist"), st.history.map((h) => {
    const [y, m] = h.month.split("-");
    const val = v(h.sum);
    return { label: mn[Number(m) - 1], value: val, tip: `${mn[Number(m) - 1]} ${y}: ${c} ${money(val)}` };
  }), { height: 200, labels: false, empty: `Noch keine ausgeführten Zahlungen in ${c}` });

  hbars($("#ch-top"), st.top_creditors.filter((x) => v(x.sum) > 0).sort((a, b) => v(b.sum) - v(a.sum)).slice(0, 8)
    .map((x) => ({ name: x.name, value: v(x.sum), meta: `${x.count} Zahlung${x.count > 1 ? "en" : ""}` })),
    c, "Noch keine Exporte in " + c);

  const kinds = { duplicate: "Duplikat?", scan: "QR nicht lesbar", error: "Fehler" };
  $("#issues").innerHTML = st.issues.length ? st.issues.map((i) => `
    <div class="issue" data-id="${i.doc_id}"><div><span class="badge err">${kinds[i.kind]}</span>
      <span class="t">${esc(i.creditor || i.title)}</span> <span class="muted small">#${i.doc_id}</span></div>
      <div class="muted small">${esc(i.messages.join(" · "))}</div></div>`).join("")
    : `<p class="muted">Keine Probleme. 👍</p>`;
  $$("#issues .issue").forEach((el) => el.addEventListener("click", () => {
    showView("payments"); S.filter = "problem";
    $$("#filter button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.f === "problem"));
    openDrawer(Number(el.dataset.id));
  }));
}

function niceMax(m) {
  if (m <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(m));
  for (const s of [1, 2, 2.5, 5, 10]) if (s * p >= m) return s * p;
  return 10 * p;
}

function barChart(el, data, { height = 220, labels = true, empty = "Keine Daten" } = {}) {
  el._chart = [data, { height, labels, empty }];
  if (!data.some((d) => d.value > 0)) { el.innerHTML = `<p class="muted chart-empty">${esc(empty)}</p>`; return; }
  const W = Math.max(300, el.clientWidth || 720), H = height, pl = 48, pr = 8, pt = 18, pb = 26;
  const max = niceMax(Math.max(0, ...data.map((d) => d.value)));
  const iw = W - pl - pr, ih = H - pt - pb, n = data.length;
  const slot = iw / n, bw = Math.min(46, slot * 0.62);
  const y = (v) => pt + ih - (v / max) * ih;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => t * max);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Balkendiagramm">`;
  ticks.forEach((t) => {
    svg += `<line class="${t === 0 ? "baseline" : "gridline"}" x1="${pl}" x2="${W - pr}" y1="${y(t)}" y2="${y(t)}"/>`;
    svg += `<text class="axis" x="${pl - 8}" y="${y(t) + 4}" text-anchor="end">${compact(t)}</text>`;
  });
  data.forEach((d, i) => {
    const cx = pl + slot * i + slot / 2, x = cx - bw / 2, top = y(d.value), h = pt + ih - top;
    const r = Math.min(4, h / 2, bw / 2);
    svg += `<g class="col" data-i="${i}"><rect class="hit" x="${pl + slot * i}" y="${pt}" width="${slot}" height="${ih}"/>`;
    if (d.value > 0) {
      svg += `<path class="bar ${d.cls || ""}" d="M${x},${pt + ih} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${pt + ih} Z"/>`;
      if (labels) svg += `<text class="vlabel" x="${cx}" y="${top - 5}" text-anchor="middle">${compact(d.value)}</text>`;
    }
    if (n <= 12 || i % 2 === 0) svg += `<text class="axis" x="${cx}" y="${H - 8}" text-anchor="middle">${esc(d.label)}</text>`;
    svg += "</g>";
  });
  svg += "</svg>";
  el.innerHTML = svg + `<div class="tip" hidden></div>`;
  const tip = $(".tip", el);
  $$("g.col", el).forEach((g) => {
    g.addEventListener("mouseenter", () => {
      const d = data[g.dataset.i];
      tip.textContent = d.tip;
      const r = g.querySelector(".hit").getBoundingClientRect(), er = el.getBoundingClientRect();
      const barTop = g.querySelector(".bar")?.getBoundingClientRect().top ?? (er.top + er.height * 0.8);
      tip.style.left = `${r.left - er.left + r.width / 2}px`;
      tip.style.top = `${barTop - er.top}px`;
      tip.hidden = false;
    });
    g.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}

function hbars(el, items, ccy, emptyText) {
  if (!items.length) { el.innerHTML = `<p class="muted">${esc(emptyText)}</p>`; return; }
  const max = Math.max(...items.map((x) => x.value));
  el.innerHTML = items.map((x) => `<div class="hbar" title="${esc(x.name)}: ${ccy} ${money(x.value)}">
      <span class="name">${esc(x.name)}</span><span class="val">${ccy} ${money(x.value)}</span>
      <div class="track"><div class="fill" style="width:${(x.value / max) * 100}%"></div></div>
      <span class="meta">${esc(x.meta)}</span></div>`).join("");
}

let resizeT;
window.addEventListener("resize", () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => $$(".chart").forEach((el) => el._chart && barChart(el, ...el._chart)), 150);
});

// ================================================================ Exporte
async function loadExports() {
  let rows;
  try { rows = await api("exports"); } catch (e) { return toast(e.message, true); }
  $("#exports").innerHTML = rows.length ? rows.map((x) => {
    const s = {};
    x.items.forEach((i) => (s[i.currency] = (s[i.currency] || 0) + Number(i.amount)));
    const tot = Object.entries(s).map(([c, v]) => `${c} ${money(v)}`).join(" · ");
    return `<div class="exp ${x.reverted_at ? "reverted" : ""}">
      <div class="exp-head">
        <span class="t">Export #${x.id}</span>
        <span class="muted">${dtt(x.created_at)} · ${esc(x.created_by)}</span>
        <span>${x.items.length} Zahlungen · <b class="num">${tot}</b></span>
        ${x.reverted_at ? `<span class="badge">Rückgängig ${dt(x.reverted_at)} · ${esc(x.reverted_by)}</span>` : ""}
        ${x.hidden ? `<span class="badge" title="Diese Positionen gehören zu Dokumenten, die du in paperless nicht sehen darfst">+ ${x.hidden} Position${x.hidden > 1 ? "en" : ""} ohne Berechtigung</span>` : ""}
        <span class="spacer"></span>
        ${x.hidden ? "" : `<a class="btn" href="api/exports/${x.id}/xml" download>XML herunterladen</a>
        ${x.reverted_at ? "" : `<button class="btn danger" data-revert="${x.id}">Rückgängig</button>`}`}
      </div>
      <details><summary>${esc(x.filename)} · MsgId <span class="mono">${esc(x.msg_id)}</span></summary>
        <table class="grid"><thead><tr><th>Dok.</th><th>Empfänger</th><th>Referenz</th><th>Ausführung</th><th class="num">Betrag</th></tr></thead><tbody>
        ${x.items.map((i) => `<tr><td>#${i.doc_id}${i.part ? ` <span class="badge split">Rate ${i.part}</span>` : ""}</td><td>${esc(i.creditor)}</td><td class="mono">${esc(i.reference || "—")}</td>
          <td>${dt(i.exec_date)}</td><td class="num"><span class="ccy">${i.currency}</span>${money(i.amount)}</td></tr>`).join("")}
        </tbody></table></details></div>`;
  }).join("") : `<div class="empty">Noch keine Exporte.</div>`;
  $$("[data-revert]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.revert;
    const ok = await confirmModal(`Export #${id} rückgängig machen?`,
      `<p>Die Rechnungen werden wieder als offen geführt und in paperless zurück auf «zu zahlen» gesetzt.</p>
       <p><b>Nur verwenden, wenn die Datei nicht bei der Bank ausgeführt wurde</b> – sonst droht eine Doppelzahlung.</p>`,
      "Rückgängig machen", true);
    if (!ok) return;
    try {
      const r = await api(`exports/${id}/revert`, { method: "POST" });
      toast(`Export #${id} rückgängig gemacht (${r.count} Rechnungen)`);
      r.warnings.forEach((w) => toast(w, true));
      loadExports();
    } catch (e) { toast(e.message, true); }
  }));
}

// ================================================================ Statuszeile
async function showVersion() {
  try {
    const r = await fetch("api/health", { credentials: "same-origin" });
    const h = await r.json();
    const el = $("#version");
    el.textContent = `qr2pain ${h.version}`;
    el.title = `Version ${h.version} – Versionshinweise auf GitHub`;
  } catch { /* Statuszeile ist optional */ }
}

// ================================================================ Start
showVersion();
start();
