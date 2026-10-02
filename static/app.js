/* ValueInvestor app — four views that follow the weekly cycle. Data: /api/* (Google Drive). */
const $view = document.getElementById("view");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const val = x => (x && typeof x === "object" && "value" in x) ? x.value : x;
const money = x => { x = val(x); return x == null || isNaN(x) ? "—" : "$" + Number(x).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); };
const pct = (x, d = 0) => { x = val(x); return x == null || isNaN(x) ? "—" : (Number(x) * 100).toFixed(d) + "%"; };
const num = (x, d = 1) => { x = val(x); return x == null || isNaN(x) ? "—" : Number(x).toFixed(d); };
const PCT_KEYS = /margin|roic|roe|growth|shares_yoy|yield/;
const fm = (key, v) => { v = val(v); if (v == null || v === "" || isNaN(v)) return "—"; return PCT_KEYS.test(key || "") ? (Number(v) * 100).toFixed(1) + "%" : key === "net_debt_ebitda" ? Number(v).toFixed(2) + "x" : key === "fcf" ? "$" + (Number(v) / 1e9).toFixed(2) + "B" : Number(v).toFixed(2); };
const chip = (text, cls) => text ? `<span class="chip ${cls || ""}">${esc(text)}</span>` : "";
const yahooA = (t, url) => `<a href="${esc(url || "https://finance.yahoo.com/quote/" + t)}" target="_blank" rel="noopener">Yahoo ↗</a>`;
const LIST = {A: "List A · value", B: "List B · −30% off high", C: "List C · growth + moat"};

async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"Content-Type": "application/json"}}, opts || {}));
  let body = null;
  try { body = await r.json(); } catch (e) { /* non-JSON */ }
  if (!r.ok || (body && body.ok === false)) throw new Error((body && body.error) || `${r.status} ${r.statusText}`);
  return body;
}
function toast(msg) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 3500);
}
function fail(e) { $view.innerHTML = `<div class="err">Could not load from Google Drive: ${esc(e.message)}</div>`; }

function ladder(r) {
  const p = val(r.price), lo = val(r.iv_low), base = val(r.iv_base), hi = val(r.iv_high), en = val(r.entry_price_target);
  if (![p, lo, base, hi].every(v => v != null && !isNaN(v))) return "";
  const min = Math.min(p, lo, en ?? lo) * 0.9, max = Math.max(p, hi) * 1.05;
  const x = v => ((v - min) / (max - min) * 100).toFixed(2) + "%";
  return `<div class="ladder" aria-label="Price ${money(p)} vs value ${money(lo)} to ${money(hi)}, entry ${money(en)}">
    <div class="rng" style="left:${x(lo)};width:calc(${x(hi)} - ${x(lo)})"></div>
    <div class="mk" style="left:${x(base)};background:var(--teal)"></div>
    ${en != null ? `<div class="mk" style="left:${x(en)};background:var(--violet)"></div>` : ""}
    <div class="mk" style="left:${x(p)};background:var(--amber);width:3px"></div>
  </div>
  <div class="row mono faint" style="font-size:12px">
    <span style="color:var(--amber)">price ${money(p)}</span><span style="color:var(--violet)">entry ${money(en)}</span>
    <span style="color:var(--teal)">base ${money(base)}</span><span>range ${money(lo)}–${money(hi)}</span>
  </div>`;
}

function companyCard(r, extra) {
  return `<div class="card">
    <div class="top"><a class="tk" href="#thesis/${esc(r.ticker)}">${esc(r.ticker)}</a>
      <span class="muted">${esc(r.company || "")}</span>
      ${chip(r.verdict, "v-" + r.verdict)} ${r.conviction != null ? chip("conv " + r.conviction) : ""}
      ${chip(r.approval, "ap-" + r.approval)} ${r.tracked ? chip(r.health, "h-" + r.health) : ""}</div>
    ${extra ? `<p class="muted" style="font-size:13px">${esc(extra)}</p>` : ""}
    ${ladder(r)}
    <div class="row" style="font-size:13px"><span class="mono">MoS ${pct(r.mos)}</span>
      ${r.below_entry ? chip("at/below entry", "h-Green") : ""}
      <a href="#thesis/${esc(r.ticker)}">Thesis →</a> ${yahooA(r.ticker, r.yahoo)}</div>
  </div>`;
}

// ── This week ────────────────────────────────────────────────────────
async function viewWeek() {
  const w = await api("/api/week");
  const c = w.counts;
  $view.innerHTML = `
  <div class="head"><span class="eyebrow">Week of ${esc(w.week_of)} · Singapore time</span><h1>This week</h1>
    <p class="muted">Tracked ${c.tracked} (G ${c.green} / A ${c.amber} / R ${c.red}) · drafts awaiting you ${c.drafts}</p></div>
  <div class="steps">${w.steps.map(s => `<div class="step"><span class="when">${esc(s.when)}</span><h3>${esc(s.name)}</h3>
     <span class="st ${esc(s.status)}">${esc(s.status)}</span><span class="muted" style="font-size:12.5px">${esc(s.detail)}</span></div>`).join("")}</div>
  <section class="head"><h2>1 · Companies to watch</h2><p class="muted">This week's deep dives plus tracked theses priced at or below their entry target.</p></section>
  <div class="grid">${w.watch.length ? w.watch.map(r => companyCard(r, r.why_watch)).join("") : `<div class="empty">No deep dives yet this week.</div>`}</div>
  <section class="head"><h2>2 · Thesis changes</h2><p class="muted">Kill criteria met, health changes and new issues on tracked theses.</p></section>
  ${w.changes.length ? `<div class="tbl"><table><thead><tr><th>Ticker</th><th>Health</th><th>What changed</th><th>Suggested action</th></tr></thead><tbody>
    ${w.changes.map(x => `<tr><td><a href="#thesis/${esc(x.ticker)}">${esc(x.ticker)}</a></td>
      <td>${x.health_prev ? esc(x.health_prev) + " → " : ""}${chip(x.health, "h-" + x.health)}</td>
      <td>${x.kill ? `<b class="bad">Kill criterion met:</b> ${esc(x.kill.criterion)} (${esc(x.kill.metric_key)} ${fm(x.kill.metric_key, x.kill.current)} vs ${esc(x.kill.direction)} ${fm(x.kill.metric_key, x.kill.threshold)})`
          : (x.issues || []).map(i => `${esc(i.severity || "")} · ${esc(i.detail || i.type || "")}`).join("<br>")}</td>
      <td>${esc(x.action || "")}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">No thesis changes this week.${c.tracked ? "" : " Nothing is tracked yet: approve a draft to start weekly kill-criteria checks."}</div>`}
  ${w.awaiting.length ? `<section class="head"><h2>Awaiting your decision</h2></section>
    <div class="row">${w.awaiting.map(r => `<a class="chip ap-pending" href="#thesis/${esc(r.ticker)}">${esc(r.ticker)} · ${esc(r.verdict || "")} ${r.conviction ?? ""}</a>`).join("")}</div>` : ""}`;
}

// ── Screen ───────────────────────────────────────────────────────────
let screenList = "A";
async function viewScreen() {
  const s = await api("/api/screen");
  if (!s.file) { $view.innerHTML = `<h1>Screen</h1><div class="empty">No screen file in Drive yet. The screen runs Saturday 08:52.</div>`; return; }
  const f = s.funnel || {};
  const queue = (s.queue || []).map(q => typeof q === "string" ? q : q.ticker);
  const render = () => {
    const rows = (s.lists[screenList] || []);
    const isB = screenList === "B", isC = screenList === "C";
    document.getElementById("lst").innerHTML = rows.length ? `<div class="tbl"><table><thead><tr>
      <th>#</th><th>Ticker</th><th>Sector</th><th>Price</th>${isB ? "<th>Off 52w high</th>" : ""}${isC ? "<th>3-yr rev growth</th><th>Gross margin</th><th>Rule of 40</th>" : "<th>Tests</th><th>FCF yield</th><th>ROIC</th>"}
      <th>Gate</th><th>Why</th><th></th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr><td class="n">${i + 1}</td>
        <td><b>${esc(r.ticker)}</b>${queue.includes(r.ticker) ? " " + chip("deep dive", "l-" + screenList) : ""}<div class="faint" style="font-size:12px">${esc(r.name || "")}</div></td>
        <td style="font-size:13px">${esc(r.sector || "")}${r.focus ? `<div class="faint">${esc(r.focus)}</div>` : ""}</td>
        <td class="n">${money(r.price)}</td>
        ${isB ? `<td class="n warn">−${pct(r.drawdown)}</td>` : ""}
        ${isC ? `<td class="n">${pct(r.rev_cagr_3y)}</td><td class="n">${pct(r.gross_margin)}</td><td class="n">${pct(r.rule_of_40)}</td>`
              : `<td class="n">${r.tests_passed ?? "—"}/9</td><td class="n">${pct(r.fcf_yield, 1)}</td><td class="n">${pct(r.roic)}</td>`}
        <td>${chip(r.gate, r.gate === "PASS" ? "h-Green" : "h-Amber")}</td>
        <td style="font-size:13px;max-width:340px">${esc(r.why || (r.flags || []).join("; "))}</td>
        <td>${yahooA(r.ticker, r.yahoo)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">This list is empty in this week's screen.</div>`;
    document.querySelectorAll("#lists button").forEach(b => b.classList.toggle("on", b.dataset.l === screenList));
    document.getElementById("rule").textContent = (s.rules || {})[screenList] || "";
  };
  $view.innerHTML = `<div class="head"><span class="eyebrow">${esc(s.file)}</span><h1>Screen</h1>
    <p class="muted mono" style="font-size:13px">${esc(f.nasdaq_listings ?? "—")} listings → ${esc(f.us_common_mcap_liquidity ?? "—")} US ≥$2B liquid → ${esc(f.with_sec_fundamentals ?? "—")} with SEC data · A passed ${esc(f.passed_framework_screen ?? "—")} · B ≥30% off ${esc(f.drawdown_ge_threshold ?? "—")} · C passed ${esc(f.passed_growth_screen ?? "—")}</p>
    ${queue.length ? `<p>Deep dives this week: <b>${esc(queue.join(", "))}</b> (1 per list)</p>` : ""}
    ${s.html_link ? `<p><a href="${esc(s.html_link)}" target="_blank" rel="noopener">Full screen report in Drive ↗</a></p>` : ""}</div>
    <div class="lists" id="lists">${["A", "B", "C"].map(l => `<button data-l="${l}">${LIST[l]} (${(s.lists[l] || []).length})</button>`).join("")}</div>
    <p class="muted" style="font-size:13px" id="rule"></p><div id="lst"></div>`;
  document.querySelectorAll("#lists button").forEach(b => b.onclick = () => { screenList = b.dataset.l; render(); });
  render();
}

// ── Theses list ──────────────────────────────────────────────────────
async function viewTheses() {
  const {theses} = await api("/api/theses");
  const groups = [["Awaiting your decision", t => t.approval === "pending"], ["Tracked", t => t.tracked],
                  ["Rejected / closed", t => t.approval === "rejected" || t.status === "Closed"]];
  $view.innerHTML = `<div class="head"><h1>Theses</h1><p class="muted">Every deep dive ends in a thesis with trackable kill criteria. Approve to start weekly tracking.</p></div>
    ${groups.map(([title, f]) => { const g = theses.filter(f); return `<section class="head"><h2>${title} (${g.length})</h2></section>
      ${g.length ? `<div class="grid">${g.map(r => companyCard(r, r.note)).join("")}</div>` : `<div class="empty">None.</div>`}`; }).join("")}`;
}

// ── One thesis ───────────────────────────────────────────────────────
async function viewThesis(ticker) {
  const d = await api("/api/thesis/" + encodeURIComponent(ticker));
  const th = d.thesis || {}, ix = d.index || {ticker};
  const dec = th.decision || {}, conv = dec.conviction || {}, pl = dec.price_ladder || {};
  const ms = th.monitor_state || {};
  const kstat = Object.fromEntries((ms.kill_status || []).map(k => [k.criterion, k]));
  const kills = (th.kill_criteria || []).map(k => Object.assign({}, k, kstat[k.criterion] || {}));
  const pending = ix.approval === "pending";
  $view.innerHTML = `
  <div class="head"><a href="#theses" class="muted">← Theses</a>
    <div class="row"><h1>${esc(ticker)}</h1><span class="muted">${esc(th.company || ix.company || "")}</span></div>
    <div class="row">${chip(dec.verdict || ix.verdict, "v-" + (dec.verdict || ix.verdict))} ${chip("conviction " + (conv.score ?? ix.conviction ?? "—"))}
      ${chip(ix.approval, "ap-" + ix.approval)} ${chip(th.status || ix.status)} ${ix.tracked ? chip(ix.health, "h-" + ix.health) : ""}
      <span class="muted" style="font-size:13px">${esc(th.sector || ix.sector || "")}</span></div>
    ${dec.one_liner ? `<p style="font-size:17px">${esc(dec.one_liner)}</p>` : ""}</div>
  ${pending ? `<div class="decide"><b>Your decision</b>
      <select id="st" aria-label="Status if approved"><option>Watch</option><option>Active</option></select>
      <input id="why" type="text" placeholder="Note (optional)" aria-label="Reason" style="flex:1;min-width:180px">
      <button class="go" id="ap">Approve &amp; track</button><button class="no" id="rj">Reject</button></div>` : ""}
  <div class="grid">
    <div class="box"><span class="eyebrow">Price vs value · ${esc(pl.company_type || "")}</span>${ladder(Object.assign({}, ix, {price: val(pl.price) ?? ix.price, entry_price_target: pl.entry_target ?? ix.entry_price_target, iv_low: pl.iv_low ?? ix.iv_low, iv_base: pl.iv_base ?? ix.iv_base, iv_high: pl.iv_high ?? ix.iv_high}))}
      <p class="mono" style="font-size:13px">Margin of safety ${pct(pl.current_mos ?? ix.mos)} vs required ${pct(pl.required_mos)}</p></div>
    <div class="box"><span class="eyebrow">Links</span>
      ${d.deep_dives.length ? d.deep_dives.map(x => `<a href="/deepdive/${esc(ticker)}/${esc(x.date)}" target="_blank" rel="noopener">Full deep dive ${esc(x.date)} ↗</a>`).join("") : "<span class='muted'>No deep-dive file</span>"}
      ${yahooA(ticker, d.yahoo)} ${d.thesis_link ? `<a href="${esc(d.thesis_link)}" target="_blank" rel="noopener">thesis.json in Drive ↗</a>` : ""}</div>
  </div>
  ${th.thesis_summary ? `<section class="box"><h2>Investment thesis</h2><p>${esc(th.thesis_summary)}</p></section>` : ""}
  <section class="head"><h2>Kill criteria</h2><p class="muted">Checked every Sunday once approved. Machine checks use SEC filings; judgement checks use filings and news.</p></section>
  ${kills.length ? `<div class="tbl"><table><thead><tr><th>Criterion</th><th>Metric</th><th>Trigger</th><th>Current</th><th>Check</th><th>Status</th></tr></thead><tbody>
    ${kills.map(k => `<tr class="${k.breached ? "kill-hit" : ""}"><td>${esc(k.criterion)}</td><td class="mono">${esc(k.metric_key || k.metric || "judgement")}</td>
      <td class="n">${k.threshold != null ? esc(k.direction || "below") + " " + fm(k.metric_key, k.threshold) : "—"}</td>
      <td class="n">${fm(k.metric_key, k.current)}</td><td style="font-size:13px">${esc(k.check || (k.metric_key ? "machine" : "judgement"))}</td>
      <td>${k.breached === true ? '<b class="bad">MET</b>' : k.breached === false ? '<span class="ok">clear</span>' : '<span class="faint">not checked yet</span>'}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">No kill criteria in this thesis.</div>`}
  <section class="head"><h2>Pillars</h2></section>
  ${(th.pillars || []).length ? `<div class="tbl"><table><thead><tr><th>Claim</th><th>KPI</th><th>Current</th><th>Target</th><th>Breach</th><th>Source</th></tr></thead><tbody>
    ${th.pillars.map(p => `<tr><td>${esc(p.claim)}<div class="faint" style="font-size:12.5px">${esc(p.evidence || "")}</div></td><td class="mono">${esc(p.kpi_key || p.kpi || "")}</td>
      <td class="n">${fm(p.kpi_key, p.current)}</td><td class="n">${fm(p.kpi_key, p.target)}</td><td class="n">${esc(p.breach_direction || "")} ${fm(p.kpi_key, p.breach_threshold)}</td>
      <td style="font-size:12.5px">${esc(p.source || "")} ${esc(p.as_of || "")}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No pillars.</div>`}
  <div class="grid">
    <div class="box"><h3>Catalysts</h3>${(th.catalysts || []).length ? `<ul class="plain">${th.catalysts.map(c => `<li><span class="mono">${esc(c.expected_date || "")}</span> ${esc(c.event)} <span class="faint">${esc(c.status || "")}</span></li>`).join("")}</ul>` : "<span class='muted'>None</span>"}</div>
    <div class="box"><h3>Key risks</h3>${(th.key_risks || []).length ? `<ul class="plain">${th.key_risks.map(r => `<li>${esc(r.risk)} <span class="faint">P ${esc(r.probability || "")} · impact ${esc(r.impact || "")}</span></li>`).join("")}</ul>` : "<span class='muted'>None</span>"}</div>
    <div class="box"><h3>Pre-mortem</h3>${(dec.pre_mortem || []).length ? `<ul class="plain">${dec.pre_mortem.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "<span class='muted'>None</span>"}</div>
  </div>
  ${(dec.checklist || []).length ? `<section class="head"><h2>Checklist</h2></section><div class="tbl"><table><tbody>${dec.checklist.map(c => `<tr><td class="mono">${esc(c.id)}</td>
     <td>${chip(c.state, c.state === "PASS" ? "h-Green" : c.state === "FAIL" ? "h-Red" : "")}</td><td style="font-size:13px">${esc(c.detail)}</td></tr>`).join("")}</tbody></table></div>` : ""}
  ${(conv.components || []).length ? `<section class="head"><h2>Conviction ${esc(conv.score)}</h2></section><div class="tbl"><table><thead><tr><th>Part</th><th>Weight</th><th>Score</th><th>Reason</th></tr></thead><tbody>
     ${conv.components.map(c => `<tr><td>${esc(c.name)}</td><td class="n">${esc(c.weight)}</td><td class="n">${esc(c.score_0_5)}/5</td><td style="font-size:13px">${esc(c.reason)} <span class="faint">${esc(c.source || "")}</span></td></tr>`).join("")}</tbody></table></div>` : ""}
  ${(th.history || []).length ? `<section class="head"><h2>History</h2></section><ul class="plain">${th.history.map(h => `<li><span class="mono">${esc(h.date)}</span> ${esc(h.what_changed)} <span class="faint">${esc(h.why || "")}</span></li>`).join("")}</ul>` : ""}`;
  if (pending) {
    const go = async action => {
      if (action === "reject" && !document.getElementById("why").value.trim()) { toast("Add a short reason before rejecting."); return; }
      document.getElementById("ap").disabled = document.getElementById("rj").disabled = true;
      try {
        await api(`/api/thesis/${encodeURIComponent(ticker)}/decision`, {method: "POST", body: JSON.stringify({
          action, status: document.getElementById("st").value, reason: document.getElementById("why").value})});
        toast(action === "approve" ? `${ticker} approved — tracked from next Sunday` : `${ticker} rejected`);
        route();
      } catch (e) { toast("Not saved: " + e.message); document.getElementById("ap").disabled = document.getElementById("rj").disabled = false; }
    };
    document.getElementById("ap").onclick = () => go("approve");
    document.getElementById("rj").onclick = () => go("reject");
  }
}

// ── Tracker ──────────────────────────────────────────────────────────
async function viewTracker() {
  const {tracked} = await api("/api/tracker");
  $view.innerHTML = `<div class="head"><span class="eyebrow">Checked Sunday 08:52</span><h1>Tracker</h1>
    <p class="muted">Approved theses only. A kill criterion met turns the thesis Red; you decide the action. Nothing is closed automatically.</p></div>
    ${tracked.length ? tracked.map(r => `<section class="card">
      <div class="top"><a class="tk" href="#thesis/${esc(r.ticker)}">${esc(r.ticker)}</a><span class="muted">${esc(r.company || "")}</span>
        ${chip(r.health, "h-" + r.health)} ${chip(r.status)} <span class="faint mono" style="font-size:12px">last check ${esc(r.last_checked || r.last_review || "—")}</span> ${yahooA(r.ticker, r.yahoo)}</div>
      ${ladder(r)}
      <div class="tbl"><table><thead><tr><th>Kill criterion</th><th>Trigger</th><th>Current</th><th>Status</th></tr></thead><tbody>
        ${r.kill_status.map(k => `<tr class="${k.breached ? "kill-hit" : ""}"><td>${esc(k.criterion)}<div class="faint mono" style="font-size:12px">${esc(k.metric_key || "judgement")}</div></td>
          <td class="n">${k.threshold != null ? esc(k.direction || "") + " " + fm(k.metric_key, k.threshold) : "—"}</td><td class="n">${fm(k.metric_key, k.current)}</td>
          <td>${k.breached === true ? '<b class="bad">MET</b>' : k.breached === false ? '<span class="ok">clear</span>' : '<span class="faint">pending</span>'}</td></tr>`).join("")}
      </tbody></table></div>
      ${r.open_issues.length ? `<ul class="plain">${r.open_issues.map(i => `<li>${chip(i.severity, i.severity === "high" ? "h-Red" : i.severity === "medium" ? "h-Amber" : "")} ${esc(i.detail)} → <b>${esc(i.recommended_action)}</b></li>`).join("")}</ul>` : `<p class="ok" style="font-size:13px">No open issues.</p>`}
    </section>`).join("") : `<div class="empty">Nothing is tracked yet. Open a thesis under Theses and choose Approve &amp; track.</div>`}`;
}

// ── router ───────────────────────────────────────────────────────────
async function route() {
  const h = (location.hash || "#week").slice(1);
  const [tab, arg] = h.split("/");
  document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === (tab === "thesis" ? "theses" : tab)));
  $view.innerHTML = `<p class="muted pad">Loading…</p>`;
  try {
    if (tab === "screen") await viewScreen();
    else if (tab === "theses") await viewTheses();
    else if (tab === "thesis" && arg) await viewThesis(decodeURIComponent(arg).toUpperCase());
    else if (tab === "tracker") await viewTracker();
    else await viewWeek();
  } catch (e) { fail(e); }
}
document.getElementById("refresh").onclick = async () => { try { await api("/api/refresh", {method: "POST"}); } catch (e) {} route(); };
window.addEventListener("hashchange", route);
route();
