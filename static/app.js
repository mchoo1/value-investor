// ═══════════════════════════════════════════════════════════════
// Value Investor — Frontend
// ═══════════════════════════════════════════════════════════════

let charts = {};
let screenerRan = false;

// ── DCF input scale hints ─────────────────────────────────────────
function fmtHint(v, prefix) {
  if (!v && v !== 0) return "";
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e12) return `${sign}${prefix}${(abs/1e12).toFixed(2)}T`;
  if (abs >= 1e9)  return `${sign}${prefix}${(abs/1e9).toFixed(2)}B`;
  if (abs >= 1e6)  return `${sign}${prefix}${(abs/1e6).toFixed(2)}M`;
  if (abs >= 1e3)  return `${sign}${prefix}${(abs/1e3).toFixed(1)}K`;
  return `${sign}${prefix}${abs}`;
}
function updateDcfHint(inputId, hintId, prefix) {
  const v = parseFloat(document.getElementById(inputId).value);
  const el = document.getElementById(hintId);
  if (!el) return;
  el.textContent = isNaN(v) ? "" : "= " + fmtHint(v, prefix);
}

// ── Safe API fetch with timeout + error handling ─────────────────
async function api(path, method = "GET", body = null, timeoutMs = 60000) {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
      signal: ctrl.signal,
    };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    clearTimeout(timer);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Request timed out");
    throw e;
  }
}

// ── Navigation ──────────────────────────────────────────────────
function showSection(name) {
  document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
  document.getElementById("section-" + name).classList.add("active");
  document.getElementById("nav-" + name).classList.add("active");
  if (name === "dashboard") loadDashboard();
  if (name === "screener" && !screenerRan) { screenerRan = true; runScreener(); }
  if (name === "thesis") loadThesisList();
  if (name === "tracker") loadWeeklyTracker();
  if (name === "portfolio") loadPortfolio();
}

function quickLookup() {
  const t = document.getElementById("quickSearch").value.trim().toUpperCase();
  if (!t) return;
  document.getElementById("researchTicker").value = t;
  showSection("research");
  loadResearch();
}

// ── Formatters ──────────────────────────────────────────────────
function fmt(n, d = 2) {
  if (n === null || n === undefined || n === 0) return "—";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtBig(n) {
  if (!n) return "—";
  const a = Math.abs(n);
  if (a >= 1e12) return (n / 1e12).toFixed(1) + "T";
  if (a >= 1e9)  return (n / 1e9).toFixed(1) + "B";
  if (a >= 1e6)  return (n / 1e6).toFixed(1) + "M";
  return Number(n).toLocaleString();
}
function metricRow(label, value) {
  return `<div class="flex justify-between border-b border-slate-800 py-1.5">
    <span class="text-slate-400">${label}</span>
    <span class="text-white font-medium">${value}</span>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════
// DASHBOARD — each panel loads independently
// ═══════════════════════════════════════════════════════════════
function loadDashboard() {
  document.getElementById("dashDate").textContent = "Updated: " + new Date().toLocaleString();
  // Load DB-backed panels immediately (fast, no network calls)
  loadWatchlistPanel();
  loadPortfolioPanel();
  loadThesisPanel();
  // Delay market data by 1s so the fast panels render first,
  // and yfinance network call doesn't race against SQLite queries
  setTimeout(loadMarketOverview, 1000);
}

async function loadWatchlistPanel() {
  const el = document.getElementById("dashWatchlist");
  try {
    const wl = await api("/api/watchlist");
    if (!wl.length) {
      el.innerHTML = `<div class="text-slate-600 text-sm text-center py-4">No stocks yet. Use Screener to find ideas.</div>`;
    } else {
      el.innerHTML = wl.map(w => {
        const meta = _parseWatchNotes(w.notes);
        const scoreBadge = meta.score != null
          ? `<span class="text-slate-500 text-xs ml-1.5">Score:${meta.score}</span>` : '';
        const mosBadge = meta.mos != null
          ? `<span class="text-xs ml-1.5 ${meta.mos>=20?'text-emerald-400':meta.mos>=0?'text-yellow-400':'text-red-400'}">${meta.mos>0?'+':''}${meta.mos}% MoS</span>` : '';
        return `
        <div class="flex items-center justify-between py-1.5 border-b border-slate-800">
          <div>
            <span class="font-semibold text-white text-sm">${w.ticker}</span>
            <span class="text-slate-500 text-xs ml-2">${w.name || ""}</span>
            ${scoreBadge}${mosBadge}
          </div>
          <div class="flex gap-3">
            <button onclick="openResearch('${w.ticker}')" class="text-blue-400 text-xs hover:underline">Research</button>
            <button onclick="removeFromWatchlist('${w.ticker}', this)" class="text-red-400 text-xs">✕</button>
          </div>
        </div>`;
      }).join("");
    }
  } catch (e) {
    el.innerHTML = `<div class="text-red-400 text-xs">Error loading watchlist: ${e.message}</div>`;
  }
}

async function loadPortfolioPanel() {
  const el = document.getElementById("dashPortfolio");
  try {
    // Use lightweight endpoint — no live price fetch for dashboard
    const positions = await api("/api/portfolio/snapshot");
    if (!positions.length) {
      el.innerHTML = `<div class="text-slate-600 text-sm text-center py-4">No open positions.</div>`;
      return;
    }
    const open = positions.filter(p => p.status === "open");
    const totalCost = open.reduce((s, p) => s + (p.entry_price * p.shares || 0), 0);
    el.innerHTML = `
      <div class="flex justify-between text-sm border-b border-slate-700 pb-2 mb-2">
        <span class="text-slate-400">Open Positions</span><span class="font-bold text-white">${open.length}</span>
      </div>
      <div class="flex justify-between text-sm border-b border-slate-700 pb-2 mb-2">
        <span class="text-slate-400">Cost Basis</span><span class="font-bold text-white">${fmtBig(totalCost)}</span>
      </div>
      <div class="mt-2 space-y-1.5">
        ${open.slice(0, 5).map(p => `
          <div class="flex justify-between text-xs">
            <span class="text-slate-300 font-medium">${p.ticker}</span>
            <span class="text-slate-400">${fmt(p.entry_price)} × ${p.shares}</span>
          </div>`).join("")}
      </div>
      <button onclick="showSection('portfolio')" class="mt-3 text-blue-400 text-xs hover:underline">View Portfolio →</button>`;
  } catch (e) {
    el.innerHTML = `<div class="text-red-400 text-xs">Error: ${e.message}</div>`;
  }
}

async function loadThesisPanel() {
  const el = document.getElementById("dashTheses");
  try {
    const theses = await api("/api/thesis");
    const active = theses.filter(t => t.status === "active");
    if (!active.length) {
      el.innerHTML = `<div class="text-slate-600 text-sm text-center py-4">No active theses yet.</div>`;
      return;
    }
    el.innerHTML = active.slice(0, 6).map(t => {
      const mos = t.intrinsic_value && t.current_price
        ? (((t.intrinsic_value - t.current_price) / t.intrinsic_value) * 100).toFixed(0) : null;
      return `
        <div class="flex items-center justify-between py-2 border-b border-slate-800 cursor-pointer hover:bg-slate-800 rounded px-1"
             onclick="showSection('thesis');setTimeout(()=>viewThesisDetail(${t.id}),100)">
          <div>
            <span class="font-semibold text-white text-sm">${t.ticker}</span>
            <span class="text-slate-500 text-xs ml-2 truncate" style="max-width:120px;display:inline-block">${t.title || ""}</span>
          </div>
          ${mos !== null ? `<span class="${mos > 0 ? 'pill-green' : 'pill-red'}">${mos > 0 ? "+" : ""}${mos}% MoS</span>` : ""}
        </div>`;
    }).join("");
  } catch (e) {
    el.innerHTML = `<div class="text-red-400 text-xs">Error: ${e.message}</div>`;
  }
}

async function loadMarketOverview() {
  try {
    const market = await api("/api/market/overview");
    const map = { "S&P 500": "idx-sp500", "Dow Jones": "idx-dow", "NASDAQ": "idx-nasdaq", "STI": "idx-sti", "VIX": "idx-vix" };
    Object.entries(map).forEach(([k, id]) => {
      const el = document.getElementById(id);
      if (!el || !market[k]) return;
      const { price, change_pct: chg } = market[k];
      const color = (chg || 0) >= 0 ? "text-emerald-400" : "text-red-400";
      el.innerHTML = `<div class="font-bold">${price ? fmt(price, price > 100 ? 0 : 2) : "—"}</div>
        ${chg !== undefined ? `<div class="${color} text-xs">${chg >= 0 ? "+" : ""}${chg.toFixed(1)}%</div>` : ""}`;
    });
  } catch (e) { /* market data is optional — fail silently */ }
}

async function removeFromWatchlist(ticker, btn) {
  try {
    await api("/api/watchlist/" + ticker, "DELETE");
    btn.closest("div").remove();
  } catch (e) { alert("Error: " + e.message); }
}

function openResearch(ticker) {
  document.getElementById("researchTicker").value = ticker;
  showSection("research");
  loadResearch();
}

// ═══════════════════════════════════════════════════════════════
// SCREENER — auto-runs on first open
// ═══════════════════════════════════════════════════════════════
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("screenMarket").addEventListener("change", function () {
    document.getElementById("customTickersDiv").classList.toggle("hidden", this.value !== "custom");
  });
});

// Sort state for screener table
let _screenerData = [];
let _sortCol = "score";
let _sortAsc = false;
let _screenerPage = 1;
const _PAGE_SIZE = 50;
let _watchlistTickers = new Set();  // drives "Watching" badge
let _priorPickTickers = new Set();  // drives "★ Prior" badge — tickers ever in thesis
let _queuedTickers    = new Set();  // drives "⏱ Queued" badge — in research queue
let _screenerSelected = new Set();  // checkboxes for bulk-add

// ── Strategy presets ─────────────────────────────────────────────
function applyScreenerPreset(preset) {
  const ids = ['fMaxPe','fMaxPb','fMinRoe','fMaxDe',
               'fMinMarketCap','fMinFcfYield','fMinRevGrowth','fMaxNetDebtEbitda','fMinRoic'];
  ids.forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  document.getElementById('fFcfPositive').checked  = false;
  document.getElementById('fInsiderBuying').checked = false;
  if (preset === '5x') {
    // 5X Compounders: growth-first, no P/E screen, min 20% revenue growth, high ROIC
    document.getElementById('fMinRevGrowth').value = '20';
    document.getElementById('fMinRoe').value       = '12';
    document.getElementById('fMinRoic').value      = '15';
  } else if (preset === 'value') {
    // Value Dislocations: large cap, FCF-positive, clean balance sheet, positive ROIC
    document.getElementById('fMinMarketCap').value     = '20';
    document.getElementById('fMinFcfYield').value      = '4';
    document.getElementById('fMaxNetDebtEbitda').value = '3';
    document.getElementById('fMaxPe').value            = '20';
    document.getElementById('fMinRoic').value          = '10';
    document.getElementById('fFcfPositive').checked    = true;
  }
  // Highlight the active preset button
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('border-emerald-500','text-emerald-400'));
  if (preset !== 'clear') {
    const btn = document.getElementById('preset-' + preset);
    if (btn) btn.classList.add('border-emerald-500','text-emerald-400');
  }
}

// ── Bulk-add helpers ─────────────────────────────────────────────
function toggleScreenerRow(ticker, cb) {
  if (cb.checked) _screenerSelected.add(ticker);
  else _screenerSelected.delete(ticker);
  _updateBulkBar();
}
function toggleSelectAll(cb) {
  const pageStart = (_screenerPage - 1) * _PAGE_SIZE;
  const pageRows  = _screenerData.slice(pageStart, pageStart + _PAGE_SIZE);
  pageRows.forEach(r => {
    if (cb.checked) _screenerSelected.add(r.ticker);
    else _screenerSelected.delete(r.ticker);
  });
  _updateBulkBar();
  renderScreenerTable(_screenerData);
}
function _updateBulkBar() {
  let bar = document.getElementById('screenerBulkBar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'screenerBulkBar';
    bar.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#1e293b;border-top:1px solid #334155;padding:10px 24px;display:flex;align-items:center;gap:12px;z-index:200;';
    document.body.appendChild(bar);
  }
  if (_screenerSelected.size === 0) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  const newOnes = [..._screenerSelected].filter(t => !_watchlistTickers.has(t)).length;
  bar.innerHTML = `
    <span class="text-white text-sm font-semibold">${_screenerSelected.size} selected</span>
    <button onclick="addSelectedToWatchlist()" class="btn-primary text-xs py-1.5 px-4">
      + Add ${newOnes} to Watchlist</button>
    <button onclick="queueSelectedForResearch()" class="btn-secondary text-xs py-1.5 px-4">
      ⏱ Queue for AI Research</button>
    <button onclick="_screenerSelected.clear();_updateBulkBar();renderScreenerTable(_screenerData)"
      class="text-slate-400 text-xs hover:text-white ml-auto">✕ Clear</button>`;
}

// ── Watchlist note builder ────────────────────────────────────────
function buildScreenerNotes(r) {
  const parts = [];
  if (r.score      != null) parts.push(`Score:${r.score}`);
  if (r.pe_ratio   != null) parts.push(`P/E:${r.pe_ratio}`);
  if (r.graham     != null) parts.push(`Graham:$${r.graham}`);
  if (r.graham && r.price) {
    const mos = Math.round((r.graham - r.price) / r.graham * 100);
    parts.push(`MoS:${mos}%`);
  }
  if (r.ev_revenue != null) parts.push(`EV/Rev:${r.ev_revenue}x`);
  if (r.ev_ebitda  != null) parts.push(`EV/EBITDA:${r.ev_ebitda}x`);
  if (r.roe        != null) parts.push(`ROE:${r.roe}%`);
  if (r.fcf_yield  != null) parts.push(`FCF:${r.fcf_yield}%`);
  return parts.join(' | ');
}
function _parseWatchNotes(notes) {
  if (!notes) return {};
  const score = (notes.match(/Score:(\d+)/)   || [])[1];
  const mos   = (notes.match(/MoS:(-?\d+)%/)  || [])[1];
  return { score: score ? +score : null, mos: mos ? +mos : null };
}

// ── Bulk watchlist add ────────────────────────────────────────────
async function addSelectedToWatchlist() {
  const bar = document.getElementById('screenerBulkBar');
  const tickers = [..._screenerSelected].filter(t => !_watchlistTickers.has(t));
  if (!tickers.length) { _screenerSelected.clear(); _updateBulkBar(); return; }
  const rowMap = Object.fromEntries(_screenerData.map(r => [r.ticker, r]));
  if (bar) bar.innerHTML = `<span class="text-slate-300 text-sm">Adding ${tickers.length} stocks…</span>`;
  let added = 0;
  for (const ticker of tickers) {
    const r = rowMap[ticker] || {};
    try {
      await api('/api/watchlist', 'POST', { ticker, name: r.name||'', market:'US', notes: buildScreenerNotes(r) });
      _watchlistTickers.add(ticker);
      added++;
    } catch (_) {}
  }
  _screenerSelected.clear(); _updateBulkBar();
  renderScreenerTable(_screenerData);
  const st = document.getElementById('screenStatus');
  if (st) { const prev = st.innerHTML; st.innerHTML = `✓ Added ${added} to watchlist`; setTimeout(()=>st.innerHTML=prev, 2500); }
}

// ── Bulk research queue ───────────────────────────────────────────
async function queueSelectedForResearch() {
  const tickers = [..._screenerSelected].filter(t => !_queuedTickers.has(t));
  const rowMap  = Object.fromEntries(_screenerData.map(r => [r.ticker, r]));
  for (const ticker of tickers) {
    const r = rowMap[ticker] || {};
    try {
      await api('/api/research-queue', 'POST', { ticker, notes: `Screener score:${r.score||0}` });
      _queuedTickers.add(ticker);
    } catch (_) {}
  }
  _screenerSelected.clear(); _updateBulkBar();
  renderScreenerTable(_screenerData);
  const st = document.getElementById('screenStatus');
  if (st) { const prev = st.innerHTML; st.innerHTML = `⏱ ${tickers.length} tickers queued for Monday's AI run`; setTimeout(()=>st.innerHTML=prev, 2500); }
}

function sortScreener(col) {
  if (_sortCol === col) {
    _sortAsc = !_sortAsc;
  } else {
    _sortCol = col;
    _sortAsc = col === "ticker" || col === "name" || col === "sector";
  }
  _screenerPage = 1;
  const sorted = [..._screenerData].sort((a, b) => {
    let av = a[col], bv = b[col];
    if (av === null || av === undefined) av = _sortAsc ? Infinity : -Infinity;
    if (bv === null || bv === undefined) bv = _sortAsc ? Infinity : -Infinity;
    if (typeof av === "string") return _sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return _sortAsc ? av - bv : bv - av;
  });
  renderScreenerTable(sorted);
}

function setScreenerPage(n, data) {
  _screenerPage = n;
  renderScreenerTable(data || _screenerData);
}

async function runScreener() {
  const market  = document.getElementById("screenMarket").value;
  const sector  = document.getElementById("screenSector").value;
  const customRaw = document.getElementById("customTickers").value;
  const custom_tickers = customRaw ? customRaw.split(/[,\s]+/).filter(Boolean) : [];
  const filters = {
    max_pe:              parseFloat(document.getElementById("fMaxPe").value)          || null,
    max_pb:              parseFloat(document.getElementById("fMaxPb").value)          || null,
    min_roe:             parseFloat(document.getElementById("fMinRoe").value)         || null,
    max_de:              parseFloat(document.getElementById("fMaxDe").value)          || null,
    min_market_cap:      parseFloat(document.getElementById("fMinMarketCap")?.value)  || null,
    min_fcf_yield:       parseFloat(document.getElementById("fMinFcfYield")?.value)   || null,
    min_roic:            parseFloat(document.getElementById("fMinRoic")?.value)       || null,
    min_rev_growth:      parseFloat(document.getElementById("fMinRevGrowth")?.value)  || null,
    max_net_debt_ebitda: parseFloat(document.getElementById("fMaxNetDebtEbitda")?.value) || null,
    require_positive_fcf: document.getElementById("fFcfPositive").checked,
    insider_buying_only:  document.getElementById("fInsiderBuying").checked,
  };

  const universe = market === "SGX" ? "30 STI stocks" :
    custom_tickers.length ? `${custom_tickers.length} custom tickers` : "US stocks";

  document.getElementById("screenLoader").classList.remove("hidden");
  document.getElementById("screenStatus").textContent = `Screening ${universe}${sector !== "All" ? " · " + sector : ""} — please wait...`;
  document.getElementById("screenResults").innerHTML = "";

  try {
    const data = await api("/api/screen", "POST", { market, filters, custom_tickers, sector }, 600000);
    _screenerData = data.results;
    _sortCol = "score";
    _sortAsc = false;
    _screenerSelected.clear();
    // Fetch watchlist, prior picks, and queue in parallel to drive row badges
    Promise.all([
      api("/api/watchlist").catch(()=>[]),
      api("/api/thesis/tickers").catch(()=>[]),
      api("/api/research-queue").catch(()=>[]),
    ]).then(([wl, priorTickers, queue]) => {
      _watchlistTickers = new Set(wl.map(w => w.ticker));
      _priorPickTickers = new Set(priorTickers);
      _queuedTickers    = new Set(queue.map(q => q.ticker));
      renderScreenerTable(_screenerData);
    });
    document.getElementById("screenLoader").classList.add("hidden");
    const cloudNote = data.cloud_mode
      ? ` · <span title="Cloud mode: top 100 curated US stocks. Enter specific tickers in Custom Tickers for a broader search." style="cursor:help;opacity:.7">☁ cloud (100 stocks)</span>`
      : "";
    document.getElementById("screenStatus").innerHTML =
      `✓ ${data.results.length} stocks passed filters out of ${data.screened} screened${cloudNote}`;
    renderScreenerResults(data.results);
  } catch (e) {
    document.getElementById("screenLoader").classList.add("hidden");
    document.getElementById("screenStatus").textContent = "⚠ Error: " + e.message;
    document.getElementById("screenResults").innerHTML =
      `<div class="card text-red-400 text-sm p-4">Screener error: ${e.message}<br><br>` +
      `Try entering specific tickers in the <b>Custom Tickers</b> field above instead of scanning all stocks.</div>`;
  }
}

function thCol(label, col, extra = "") {
  const arrow = _sortCol === col ? (_sortAsc ? " ▲" : " ▼") : " ⇅";
  return `<th class="pb-3 cursor-pointer hover:text-white select-none ${extra}" onclick="sortScreener('${col}')">${label}<span class="text-slate-600 text-xs">${arrow}</span></th>`;
}

function renderScreenerResults(results) {
  if (!results.length) {
    document.getElementById("screenResults").innerHTML =
      `<div class="card text-center py-12 text-slate-500">No stocks matched. Try relaxing the filters.</div>`;
    return;
  }
  document.getElementById("screenResults").innerHTML = `<div class="card" id="screenerTableWrap"></div>`;
  renderScreenerTable(results);
}

function _valColor(val, price) {
  if (!val || !price) return "text-slate-400";
  return val >= price ? "text-emerald-400" : "text-red-400";
}

function renderScreenerTable(results) {
  // Pre-compute all derived valuation columns so sorting works
  results = results.map(r => {
    const eps  = r.eps_ttm || (r.pe_ratio && r.price ? r.price / r.pe_ratio : null);
    const bvps = (r.pb_ratio && r.pb_ratio > 0 && r.price) ? r.price / r.pb_ratio : null;
    const g    = Math.min(Math.max(r.revenue_growth || 0, 2), 40); // growth cap 2–40%

    // Graham: √(22.5 × EPS × BVPS)
    const graham = (eps > 0 && bvps > 0) ? parseFloat(Math.sqrt(22.5 * eps * bvps).toFixed(2)) : null;
    // P/E Method: EPS × 15x
    const pe_val = eps ? parseFloat((eps * 15).toFixed(2)) : null;
    // Peter Lynch 5-yr: EPS × (1+g)^5 × 15, discounted back at 10% pa
    const lynch  = eps ? parseFloat((eps * Math.pow(1 + g/100, 5) * 15 / Math.pow(1.1, 5)).toFixed(2)) : null;

    return { ...r, _eps: eps, graham, pe_val, lynch };
  });

  const wrap = document.getElementById("screenerTableWrap");
  if (!wrap) return;
  // Pagination
  const totalPages = Math.max(1, Math.ceil(results.length / _PAGE_SIZE));
  _screenerPage = Math.min(_screenerPage, totalPages);
  const pageStart = (_screenerPage - 1) * _PAGE_SIZE;
  const pageRows  = results.slice(pageStart, pageStart + _PAGE_SIZE);

  // Pagination controls HTML
  const pageButtons = Array.from({length: totalPages}, (_, i) => i + 1).map(p =>
    `<button onclick="setScreenerPage(${p}, _screenerData)" class="px-2 py-1 rounded text-xs ${p === _screenerPage ? 'bg-emerald-600 text-white' : 'text-slate-400 hover:text-white'}">${p}</button>`
  ).join("");

  wrap.innerHTML = `
    <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
      <h2 class="font-semibold text-white">${results.length} stocks matched
        <span class="text-slate-500 text-xs font-normal ml-2">showing ${pageStart+1}–${Math.min(pageStart+_PAGE_SIZE, results.length)}</span>
      </h2>
      <span class="text-slate-500 text-xs">Click column headers to sort · scroll right for all columns</span>
    </div>
    <div class="overflow-x-auto" style="-webkit-overflow-scrolling:touch">
      <table class="text-sm" style="min-width:1850px;width:100%">
        <thead>
          <tr class="text-slate-400 text-xs border-b border-slate-700">
            <th class="pb-3 pr-2 whitespace-nowrap text-left">
              <input type="checkbox" id="selectAllRows" title="Select all on page"
                onchange="toggleSelectAll(this)"
                class="accent-emerald-500 cursor-pointer">
            </th>
            ${thCol("Ticker",    "ticker",          "text-left pr-2 whitespace-nowrap")}
            ${thCol("Company",   "name",            "text-left whitespace-nowrap")}
            ${thCol("Sector",    "sector",          "text-left whitespace-nowrap")}
            ${thCol("Price",     "price",           "text-right whitespace-nowrap")}
            ${thCol("Mkt Cap",   "market_cap",      "text-right whitespace-nowrap")}
            ${thCol("Revenue",   "revenue",         "text-right whitespace-nowrap")}
            ${thCol("Net Inc",   "net_income",      "text-right whitespace-nowrap")}
            ${thCol("Margin%",   "net_margin",      "text-right whitespace-nowrap")}
            ${thCol("Rev Gr%",   "revenue_growth",  "text-right whitespace-nowrap")}
            ${thCol("EPS",       "eps_ttm",         "text-right whitespace-nowrap")}
            ${thCol("P/E",       "pe_ratio",        "text-right whitespace-nowrap")}
            ${thCol("EV/Rev",    "ev_revenue",      "text-right whitespace-nowrap")}
            ${thCol("EV/EBITDA", "ev_ebitda",       "text-right whitespace-nowrap")}
            ${thCol("Graham",    "graham",          "text-right whitespace-nowrap")}
            ${thCol("P/E Val",   "pe_val",          "text-right whitespace-nowrap")}
            ${thCol("Lynch 5yr", "lynch",           "text-right whitespace-nowrap")}
            ${thCol("P/B",       "pb_ratio",        "text-right whitespace-nowrap")}
            ${thCol("ROE%",      "roe",             "text-right whitespace-nowrap")}
            ${thCol("D/E",       "debt_to_equity",  "text-right whitespace-nowrap")}
            ${thCol("FCF Yld",   "fcf_yield",       "text-right whitespace-nowrap")}
            ${thCol("ROIC%",     "roic",            "text-right whitespace-nowrap")}
            ${thCol("Moat",      "moat_score",      "text-right whitespace-nowrap")}
            ${thCol("Score",     "score",           "text-right whitespace-nowrap")}
            <th class="pb-3 text-right whitespace-nowrap">Action</th>
          </tr>
        </thead>
        <tbody>
          ${pageRows.map(r => {
            const bd = r.score_breakdown || {};
            const tip = `P/E:${bd.pe||0} P/B:${bd.pb||0} ROE:${bd.roe||0} D/E:${bd.de||0} FCF:${bd.fcf_yield||0}`;
            const moatColor = r.moat_rating==="Wide"?"text-emerald-400 bg-emerald-900":r.moat_rating==="Narrow"?"text-yellow-400 bg-yellow-900":"text-slate-400 bg-slate-800";
            const moatIcon  = r.moat_rating==="Wide"?"🏰":r.moat_rating==="Narrow"?"🧱":"—";
            const vc = v => _valColor(v, r.price);
            const isWatching = _watchlistTickers.has(r.ticker);
            const isPrior    = _priorPickTickers.has(r.ticker);
            const isQueued   = _queuedTickers.has(r.ticker);
            const isSelected = _screenerSelected.has(r.ticker);
            // Action cell: badges + buttons
            const watchBtn = isWatching
              ? `<span class="text-emerald-400 text-xs" title="Already in watchlist">👁 Watching</span>`
              : `<button onclick="event.stopPropagation();addToWatchlistFromScreen('${r.ticker}',this)"
                   class="text-blue-400 text-xs hover:underline">Watch</button>`;
            const priorBadge  = isPrior  ? `<span class="text-amber-400 text-xs" title="In prior AI research">★ Prior</span>` : '';
            const queueBadge  = isQueued ? `<span class="text-violet-400 text-xs" title="Queued for Monday AI run">⏱</span>` : '';
            return `
              <tr class="table-row border-b border-slate-800 cursor-pointer ${isPrior?'bg-amber-950/20':''}"
                  onclick="openResearch('${r.ticker}')">
                <td class="py-2 pr-2" onclick="event.stopPropagation()">
                  <input type="checkbox" ${isSelected?'checked':''} class="accent-emerald-500 cursor-pointer"
                    onchange="toggleScreenerRow('${r.ticker}',this)">
                </td>
                <td class="py-2 pr-2 font-bold text-white whitespace-nowrap">
                  ${r.ticker}${queueBadge ? ' '+queueBadge : ''}
                </td>
                <td class="py-2 text-slate-400 text-xs whitespace-nowrap" style="max-width:140px;overflow:hidden;text-overflow:ellipsis" title="${r.name}">${r.name}</td>
                <td class="py-2 text-slate-400 text-xs whitespace-nowrap">${r.sector !== "N/A" ? r.sector : "—"}</td>
                <td class="py-2 text-right text-white whitespace-nowrap">${fmt(r.price)}</td>
                <td class="py-2 text-right text-slate-300 whitespace-nowrap">${fmtBig(r.market_cap)}</td>
                <td class="py-2 text-right text-slate-300 whitespace-nowrap">${fmtBig(r.revenue)}</td>
                <td class="py-2 text-right whitespace-nowrap ${(r.net_income||0)>=0?'text-emerald-400':'text-red-400'}">${fmtBig(r.net_income)}</td>
                <td class="py-2 text-right whitespace-nowrap ${r.net_margin>=15?'text-emerald-400':r.net_margin>=8?'text-yellow-400':'text-slate-400'}">${r.net_margin?r.net_margin+"%":"—"}</td>
                <td class="py-2 text-right whitespace-nowrap ${r.revenue_growth>=10?'text-emerald-400':r.revenue_growth>=0?'text-slate-300':'text-red-400'}">${r.revenue_growth!==null?r.revenue_growth+"%":"—"}</td>
                <td class="py-2 text-right whitespace-nowrap text-slate-300">${r.eps_ttm!=null?"$"+r.eps_ttm:"—"}</td>
                <td class="py-2 text-right whitespace-nowrap">${r.pe_ratio??"—"}</td>
                <td class="py-2 text-right whitespace-nowrap text-slate-300" title="EV/Revenue">${r.ev_revenue?r.ev_revenue+'x':"—"}</td>
                <td class="py-2 text-right whitespace-nowrap text-slate-300" title="EV/EBITDA">${r.ev_ebitda?r.ev_ebitda+'x':"—"}</td>
                <td class="py-2 text-right whitespace-nowrap font-semibold ${vc(r.graham)}" title="Graham: √(22.5 × EPS × Book Value/Share)">${r.graham?fmt(r.graham):"—"}</td>
                <td class="py-2 text-right whitespace-nowrap font-semibold ${vc(r.pe_val)}"   title="P/E Method: EPS × 15x">${r.pe_val?fmt(r.pe_val):"—"}</td>
                <td class="py-2 text-right whitespace-nowrap font-semibold ${vc(r.lynch)}"    title="Peter Lynch 5-yr">${r.lynch?fmt(r.lynch):"—"}</td>
                <td class="py-2 text-right whitespace-nowrap">${r.pb_ratio??"—"}</td>
                <td class="py-2 text-right whitespace-nowrap ${(r.roe||0)>=15?'text-emerald-400':(r.roe||0)>=10?'text-yellow-400':'text-slate-400'}">${r.roe?r.roe+"%":"—"}</td>
                <td class="py-2 text-right whitespace-nowrap ${(r.debt_to_equity||99)<=0.5?'text-emerald-400':(r.debt_to_equity||99)<=1?'text-yellow-400':'text-slate-400'}">${r.debt_to_equity??"—"}</td>
                <td class="py-2 text-right whitespace-nowrap ${(r.fcf_yield||0)>=5?'text-emerald-400':'text-slate-300'}">${r.fcf_yield?r.fcf_yield+"%":"—"}</td>
                <td class="py-2 text-right whitespace-nowrap ${(r.roic||0)>=15?'text-emerald-400':(r.roic||0)>=10?'text-yellow-400':'text-slate-400'}" title="Return on Invested Capital">${r.roic!=null?r.roic+"%":"—"}</td>
                <td class="py-2 text-right whitespace-nowrap">
                  <span class="text-xs px-1.5 py-0.5 rounded font-medium ${moatColor}" title="Moat score: ${r.moat_score||0}/100">${moatIcon} ${r.moat_rating||"—"}</span>
                </td>
                <td class="py-2 text-right whitespace-nowrap" title="Score breakdown — ${tip}">
                  <div class="flex items-center justify-end gap-1.5">
                    <div class="w-12 h-1.5 rounded bg-slate-700"><div class="h-1.5 rounded bg-emerald-500" style="width:${r.score||0}%"></div></div>
                    <span class="text-emerald-400 font-bold text-xs w-6">${r.score||0}</span>
                  </div>
                </td>
                <td class="py-2 text-right" onclick="event.stopPropagation()">
                  <div class="flex flex-col items-end gap-0.5">
                    ${priorBadge}
                    ${watchBtn}
                    <button onclick="startThesisFromScreener('${r.ticker}','${r.name.replace(/'/g,"")}',${r.price||0})"
                      class="text-emerald-400 text-xs hover:underline">Thesis</button>
                  </div>
                </td>
              </tr>`;
          }).join("")}
          </tbody>
        </table>
      </div>
    </div>
    ${totalPages > 1 ? `
    <div class="flex items-center justify-between mt-4 flex-wrap gap-2">
      <div class="text-slate-500 text-xs">Page ${_screenerPage} of ${totalPages} &nbsp;·&nbsp; ${results.length} stocks</div>
      <div class="flex items-center gap-1 flex-wrap">
        <button onclick="setScreenerPage(Math.max(1,_screenerPage-1), _screenerData)"
          class="px-3 py-1 rounded text-xs ${_screenerPage<=1?'text-slate-600 cursor-default':'text-slate-400 hover:text-white'}"
          ${_screenerPage<=1?"disabled":""}>← Prev</button>
        ${pageButtons}
        <button onclick="setScreenerPage(Math.min(${totalPages},_screenerPage+1), _screenerData)"
          class="px-3 py-1 rounded text-xs ${_screenerPage>=totalPages?'text-slate-600 cursor-default':'text-slate-400 hover:text-white'}"
          ${_screenerPage>=totalPages?"disabled":""}>Next →</button>
      </div>
    </div>` : ""}
    <div class="mt-3 text-xs text-slate-600">
      Graham: √(22.5 × EPS × Book Value/Share) &nbsp;·&nbsp;
      P/E Val: EPS × 15x &nbsp;·&nbsp;
      Lynch 5yr: EPS × (1+g)⁵ × 15 discounted at 10% &nbsp;·&nbsp;
      <span class="text-emerald-400">Green</span> = above price &nbsp;
      <span class="text-red-400">Red</span> = below price
    </div>`;
}

async function addToWatchlistFromScreen(ticker, btn) {
  if (_watchlistTickers.has(ticker)) return;
  const r    = _screenerData.find(x => x.ticker === ticker) || {};
  const name  = r.name || ticker;
  const notes = buildScreenerNotes(r);
  const orig  = btn.textContent;
  btn.textContent = '…'; btn.disabled = true;
  try {
    await api('/api/watchlist', 'POST', { ticker, name, market:'US', notes });
    _watchlistTickers.add(ticker);
    btn.textContent = '✓ Added';
    btn.className   = 'text-emerald-400 text-xs cursor-default';
    btn.disabled    = true;
    // Refresh panel on dashboard quietly
    loadWatchlistPanel().catch(()=>{});
  } catch (e) {
    btn.textContent = orig; btn.disabled = false;
    btn.title = 'Error: ' + e.message;
  }
}

function startThesisFromScreener(ticker, name, price) {
  showSection("thesis");
  setTimeout(() => {
    document.getElementById("thTicker").value = ticker;
    document.getElementById("thTitle").value  = ticker + " — Investment Thesis";
    document.getElementById("thCurrentPrice").value = price;
    document.getElementById("thEditId").value = "";
    document.getElementById("thesisFormModal").classList.remove("hidden");
  }, 200);
}

// ═══════════════════════════════════════════════════════════════
// RESEARCH HUB
// ═══════════════════════════════════════════════════════════════
let _currentResearchTicker = null;

function switchResTab(tab) {
  ["overview","insiders","catalysts","redflags","competitors"].forEach(t => {
    const panel = document.getElementById(t === "overview" ? "researchContent" :
      t === "redflags" ? "resRedFlags" : `res${t.charAt(0).toUpperCase()+t.slice(1)}`);
    const btn   = document.getElementById("rt-" + t);
    if (panel) panel.classList.toggle("hidden", t !== tab);
    if (btn)   btn.classList.toggle("active", t === tab);
  });
  if (!_currentResearchTicker) return;
  if (tab === "insiders"    && document.getElementById("insidersContent").textContent.includes("Run analysis"))    loadInsidersPanel(_currentResearchTicker);
  if (tab === "catalysts"   && document.getElementById("catalystsContent").textContent.includes("Run analysis"))   loadCatalystsPanel(_currentResearchTicker);
  if (tab === "redflags"    && document.getElementById("redflagsContent").textContent.includes("Run analysis"))    loadRedFlagsPanel(_currentResearchTicker);
  if (tab === "competitors" && document.getElementById("competitorsContent").textContent.includes("Run analysis")) loadCompetitorsPanel(_currentResearchTicker);
}

async function loadResearch() {
  let ticker = document.getElementById("researchTicker").value.trim().toUpperCase();
  const suffix = document.getElementById("researchMarket").value;
  if (!ticker) return;
  if (suffix && !ticker.endsWith(suffix)) ticker += suffix;
  _currentResearchTicker = ticker;

  // Reset all sub-panels so they reload fresh
  ["insidersContent","catalystsContent","redflagsContent","competitorsContent"].forEach(id => {
    document.getElementById(id).innerHTML = `<div class="text-slate-500 text-center py-8">Run analysis first.</div>`;
  });
  switchResTab("overview");

  document.getElementById("resLoader").classList.remove("hidden");
  document.getElementById("researchContent").innerHTML =
    `<div class="flex flex-col items-center py-20 gap-3"><div class="loader"></div><div class="text-slate-500 text-sm">Fetching data for ${ticker}...</div></div>`;

  try {
    const [info, hist] = await Promise.all([
      api("/api/stock/" + ticker),
      api("/api/stock/" + ticker + "/financials"),
    ]);
    document.getElementById("resLoader").classList.add("hidden");
    if (info.error) {
      document.getElementById("researchContent").innerHTML =
        `<div class="card text-red-400">Could not fetch data for <strong>${ticker}</strong>: ${info.error}</div>`;
      return;
    }
    renderResearch(info, hist);
  } catch (e) {
    document.getElementById("resLoader").classList.add("hidden");
    document.getElementById("researchContent").innerHTML =
      `<div class="card text-red-400">Error: ${e.message}</div>`;
  }
}

function renderResearch(info, hist) {
  const price = info.current_price || 0;
  const target = info.target_mean_price || 0;
  const upsidePct = target && price ? ((target - price) / price * 100).toFixed(1) : null;
  const roe    = info.roe     ? (info.roe * 100).toFixed(1) : null;
  const netM   = info.net_margin ? (info.net_margin * 100).toFixed(1) : null;
  const grossM = info.gross_margin ? (info.gross_margin * 100).toFixed(1) : null;
  const de     = info.debt_to_equity ? (info.debt_to_equity / 100).toFixed(2) : null;

  document.getElementById("researchContent").innerHTML = `
    <div class="flex items-start justify-between mb-5">
      <div>
        <h2 class="text-2xl font-bold text-white">${info.name || info.ticker}</h2>
        <div class="flex items-center flex-wrap gap-2 mt-1">
          <span class="text-slate-400 text-sm font-mono">${info.ticker}</span>
          ${info.sector ? `<span class="pill-blue">${info.sector}</span>` : ""}
          ${info.industry ? `<span class="text-slate-500 text-xs">${info.industry}</span>` : ""}
          ${info.country ? `<span class="text-slate-500 text-xs">${info.country}</span>` : ""}
        </div>
      </div>
      <div class="text-right flex-shrink-0 ml-4">
        <div class="text-3xl font-bold text-white">${info.currency || "$"} ${fmt(price)}</div>
        <div class="text-slate-400 text-sm mt-0.5">Mkt Cap: ${fmtBig(info.market_cap)}</div>
        <div class="flex gap-2 justify-end mt-2">
          <button onclick="addToWatchlistFromScreen('${info.ticker}','${(info.name||"").replace(/'/g,"")}')" class="btn-secondary text-xs py-1 px-3">+ Watchlist</button>
          <button onclick="prefillValuation('${info.ticker}',${info.eps_ttm||0},${info.free_cashflow||0},${info.shares_outstanding||0},${(info.total_debt||0)-(info.total_cash||0)},${price})" class="btn-primary text-xs py-1 px-3">Value It →</button>
        </div>
      </div>
    </div>

    <div class="card mb-5 py-3 px-4">
      <div class="flex items-center flex-wrap gap-x-0 divide-x divide-slate-700 text-sm">
        ${[
          ["Price",         `${info.currency||"$"} ${fmt(price)}`,                         "text-white font-bold"],
          ["Mkt Cap",       fmtBig(info.market_cap),                                       "text-white"],
          ["P/E (TTM)",     info.pe_ratio  ? fmt(info.pe_ratio,1)  : "—",                  "text-white"],
          ["Fwd P/E",       info.forward_pe? fmt(info.forward_pe,1): "—",                  "text-white"],
          ["P/B",           info.pb_ratio  ? fmt(info.pb_ratio,2)  : "—",                  "text-white"],
          ["ROE",           roe ? roe+"%" : "—",    roe>=15?"text-emerald-400":roe>=10?"text-yellow-400":"text-slate-300"],
          ["D/E",           de!==null?de:"—",       de<=0.5?"text-emerald-400":de<=1.5?"text-yellow-400":"text-red-400"],
          ["Rev Growth",    info.revenue_growth ? (info.revenue_growth*100).toFixed(1)+"%" : "—",
                            info.revenue_growth>=0.1?"text-emerald-400":info.revenue_growth>=0?"text-yellow-400":"text-red-400"],
          ["Net Margin",    netM ? netM+"%" : "—",  netM>=15?"text-emerald-400":netM>=8?"text-yellow-400":"text-slate-300"],
          ["Gross Margin",  grossM ? grossM+"%" : "—", "text-white"],
          ["FCF",           fmtBig(info.free_cashflow), info.free_cashflow>0?"text-emerald-400":"text-slate-300"],
          ["Beta",          info.beta ? fmt(info.beta,2) : "—",                            "text-white"],
          ["Analyst Tgt",   target ? `${info.currency||"$"}${fmt(target)}${upsidePct?` (${upsidePct>0?"+":""}${upsidePct}%)`:""}` : "—",
                            upsidePct>10?"text-emerald-400":upsidePct>0?"text-yellow-400":"text-red-400"],
        ].map(([label, val, color]) => `
          <div class="flex flex-col items-center px-4 py-1 min-w-0">
            <div class="text-slate-500 text-xs whitespace-nowrap">${label}</div>
            <div class="font-semibold ${color||"text-white"} whitespace-nowrap text-xs mt-0.5">${val}</div>
          </div>`).join("")}
      </div>
    </div>

    <div class="grid grid-cols-2 gap-5 mb-5">
      <div class="card">
        <h3 class="font-semibold text-white mb-3">Business Overview</h3>
        <p class="text-slate-400 text-sm leading-relaxed">${(info.description||"No description available.").slice(0,600)}${info.description?.length > 600 ? "..." : ""}</p>
        <div class="mt-3 grid grid-cols-2 gap-1 text-xs">
          ${[
            ["Recommendation", info.recommendation || "—"],
            ["# Analysts", info.analyst_count || "—"],
            ["Beta", info.beta ? fmt(info.beta,2) : "—"],
            ["Div Yield", info.dividend_yield ? (info.dividend_yield*100).toFixed(2)+"%" : "—"],
            ["Insider Own.", info.insider_ownership ? (info.insider_ownership*100).toFixed(1)+"%" : "—"],
            ["Inst. Own.", info.institutional_ownership ? (info.institutional_ownership*100).toFixed(1)+"%" : "—"],
          ].map(([l,v]) => `<div class="flex gap-1"><span class="text-slate-500">${l}:</span><span class="text-white capitalize">${v}</span></div>`).join("")}
        </div>
      </div>
      <div class="card">
        <h3 class="font-semibold text-white mb-3">Financial Health</h3>
        <div class="text-sm">
          ${metricRow("Revenue Growth (YoY)", info.revenue_growth ? (info.revenue_growth*100).toFixed(1)+"%" : "—")}
          ${metricRow("Gross Margin", grossM ? grossM+"%" : "—")}
          ${metricRow("Operating Margin", info.operating_margin ? (info.operating_margin*100).toFixed(1)+"%" : "—")}
          ${metricRow("Net Margin", netM ? netM+"%" : "—")}
          ${metricRow("Free Cash Flow", fmtBig(info.free_cashflow))}
          ${metricRow("Total Debt", fmtBig(info.total_debt))}
          ${metricRow("Total Cash", fmtBig(info.total_cash))}
          ${metricRow("Current Ratio", info.current_ratio ? fmt(info.current_ratio,2) : "—")}
        </div>
      </div>
    </div>

    ${hist.summary?.length ? `
    <div class="card mb-5">
      <h3 class="font-semibold text-white mb-4">5-Year Financial History</h3>
      <div class="grid grid-cols-2 gap-5">
        <div><canvas id="revChart" height="180"></canvas></div>
        <div><canvas id="marginChart" height="180"></canvas></div>
      </div>
    </div>` : ""}

    <div class="card mb-5">
      <h3 class="font-semibold text-white mb-3">52-Week Price Range</h3>
      <div class="flex justify-between text-xs text-slate-500 mb-1">
        <span>Low: ${fmt(info["52w_low"])}</span>
        <span>Current: <strong class="text-white">${fmt(price)}</strong></span>
        <span>High: ${fmt(info["52w_high"])}</span>
      </div>
      ${info["52w_low"] && info["52w_high"] ? `
      <div class="relative h-2 bg-slate-700 rounded-full">
        <div class="absolute h-2 bg-emerald-500 rounded-full" style="width:${Math.min(100,Math.max(2,((price-info["52w_low"])/(info["52w_high"]-info["52w_low"])*100))).toFixed(1)}%"></div>
      </div>` : ""}
      <div class="mt-2 text-xs text-slate-500">
        ${info["52w_high"] ? `${(((price-info["52w_high"])/info["52w_high"])*100).toFixed(1)}% from 52-week high` : ""}
      </div>
    </div>

    <!-- Risk + Moat + Price Targets row (lazy-loaded) -->
    <div class="grid grid-cols-3 gap-4 mb-5">
      <div id="riskRatingCard" class="card"><div class="text-slate-500 text-xs text-center py-4">Loading risk rating…</div></div>
      <div id="moatRatingCard"  class="card"><div class="text-slate-500 text-xs text-center py-4">Loading moat rating…</div></div>
      <div id="priceTargetsCard" class="card"><div class="text-slate-500 text-xs text-center py-4">Loading price targets…</div></div>
    </div>

    <div class="flex flex-wrap gap-3">
      <button onclick="prefillValuation('${info.ticker}',${info.eps_ttm||0},${info.free_cashflow||0},${info.shares_outstanding||0},${(info.total_debt||0)-(info.total_cash||0)},${price})" class="btn-primary">Run Valuation →</button>
      <button onclick="prefillThesis('${info.ticker}','${(info.name||"").replace(/'/g,"")}',${price})" class="btn-secondary">Write Thesis →</button>
      <button onclick="startThesisFromScreener('${info.ticker}','${(info.name||"").replace(/'/g,"")}',${price})" class="btn-secondary">+ Build Thesis</button>
    </div>`;

  if (hist.summary?.length) {
    setTimeout(() => { renderRevenueChart(hist.summary); renderMarginChart(hist.summary); }, 100);
  }
  // Lazy-load the 3 rating cards
  setTimeout(() => {
    loadRiskCard(info.ticker);
    loadMoatCard(info.ticker);
    loadTargetsCard(info.ticker, price);
  }, 300);
}

async function loadRiskCard(ticker) {
  const el = document.getElementById("riskRatingCard");
  if (!el) return;
  try {
    const d = await api(`/api/stock/${ticker}/risk`);
    const ratingColor = d.rating==="Low" ? "text-emerald-400" : d.rating==="Medium" ? "text-yellow-400" : "text-red-400";
    const ratingBg    = d.rating==="Low" ? "bg-emerald-900" : d.rating==="Medium" ? "bg-yellow-900" : "bg-red-900";
    el.innerHTML = `
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-white text-sm">⚠️ Risk Rating</h3>
        <span class="text-xs font-bold px-2 py-0.5 rounded ${ratingColor} ${ratingBg}">${d.rating}</span>
      </div>
      <div class="text-xs text-slate-400 mb-3 leading-relaxed">${d.summary}</div>
      <div class="space-y-1.5">
        ${(d.factors||[]).map(f => `
          <div>
            <div class="flex justify-between text-xs mb-0.5">
              <span class="text-slate-400">${f.name}</span>
              <span class="${f.pts >= f.max*0.7 ? 'text-red-400' : f.pts >= f.max*0.4 ? 'text-yellow-400' : 'text-emerald-400'}">${f.pts}/${f.max}</span>
            </div>
            <div class="h-1 rounded bg-slate-700">
              <div class="h-1 rounded ${f.pts>=f.max*0.7?'bg-red-500':f.pts>=f.max*0.4?'bg-yellow-500':'bg-emerald-500'}" style="width:${(f.pts/f.max*100).toFixed(0)}%"></div>
            </div>
            <div class="text-slate-500 text-xs mt-0.5">${f.note}</div>
          </div>`).join("")}
      </div>`;
  } catch(e) { el.innerHTML = `<div class="text-red-400 text-xs">Error loading risk rating</div>`; }
}

async function loadMoatCard(ticker) {
  const el = document.getElementById("moatRatingCard");
  if (!el) return;
  try {
    const d = await api(`/api/stock/${ticker}/moat`);
    const ratingColor = d.rating==="Wide" ? "text-emerald-400" : d.rating==="Narrow" ? "text-yellow-400" : "text-slate-400";
    const ratingBg    = d.rating==="Wide" ? "bg-emerald-900" : d.rating==="Narrow" ? "bg-yellow-900" : "bg-slate-800";
    const moatIcon    = d.rating==="Wide" ? "🏰" : d.rating==="Narrow" ? "🧱" : "⛔";
    el.innerHTML = `
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-white text-sm">${moatIcon} Competitive Moat</h3>
        <span class="text-xs font-bold px-2 py-0.5 rounded ${ratingColor} ${ratingBg}">${d.rating} · ${d.score}/100</span>
      </div>
      <div class="text-xs text-slate-400 mb-3 leading-relaxed">${d.summary}</div>
      <div class="space-y-1.5">
        ${(d.factors||[]).map(f => `
          <div>
            <div class="flex justify-between text-xs mb-0.5">
              <span class="text-slate-400">${f.name}</span>
              <span class="${f.pts >= f.max*0.7 ? 'text-emerald-400' : f.pts >= f.max*0.4 ? 'text-yellow-400' : 'text-slate-400'}">${f.pts}/${f.max}</span>
            </div>
            <div class="h-1 rounded bg-slate-700">
              <div class="h-1 rounded bg-blue-500" style="width:${(f.pts/f.max*100).toFixed(0)}%"></div>
            </div>
            <div class="text-slate-500 text-xs mt-0.5">${f.note}</div>
          </div>`).join("")}
      </div>`;
  } catch(e) { el.innerHTML = `<div class="text-red-400 text-xs">Error loading moat rating</div>`; }
}

async function loadTargetsCard(ticker, currentPrice) {
  const el = document.getElementById("priceTargetsCard");
  if (!el) return;
  try {
    const d = await api(`/api/stock/${ticker}/targets`);
    function targetRow(label, target, upside, color) {
      if (!target) return "";
      return `<div class="flex items-center justify-between py-1.5 border-b border-slate-800">
        <span class="text-xs text-slate-400">${label}</span>
        <div class="text-right">
          <span class="font-bold ${color}">${fmt(target)}</span>
          ${upside !== null ? `<span class="text-xs ml-1 ${upside>=0?'text-emerald-400':'text-red-400'}">(${upside>0?'+':''}${upside}%)</span>` : ""}
        </div>
      </div>`;
    }
    el.innerHTML = `
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-white text-sm">🎯 Price Targets</h3>
        <span class="text-xs text-slate-400">Current: <strong class="text-white">${fmt(currentPrice)}</strong></span>
      </div>
      ${targetRow("🐻 Bear Target", d.bear_target, d.bear_upside, "text-red-400")}
      ${targetRow("📊 Base Target", d.base_target, d.base_upside, "text-amber-400")}
      ${targetRow("🐂 Bull Target", d.bull_target, d.bull_upside, "text-emerald-400")}
      <div class="mt-3 text-xs text-slate-500">
        Blended from analyst consensus + P/E band + DCF models.
        ${d.analyst_mid ? `Analyst consensus: ${fmt(d.analyst_mid)}` : ""}
      </div>`;
  } catch(e) { el.innerHTML = `<div class="text-red-400 text-xs">Error loading targets</div>`; }
}

function renderRevenueChart(summary) {
  if (charts.rev) charts.rev.destroy();
  const ctx = document.getElementById("revChart")?.getContext("2d");
  if (!ctx) return;
  const chartOpts = { responsive: true, plugins: { legend: { labels: { color: "#94a3b8", font: { size: 11 } } } }, scales: { x: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } }, y: { ticks: { color: "#94a3b8", callback: v => v + "B" }, grid: { color: "#1e293b" } } } };
  charts.rev = new Chart(ctx, { type: "bar", data: { labels: summary.map(r => r.year), datasets: [
    { label: "Revenue", data: summary.map(r => +((r.revenue||0)/1e9).toFixed(1)), backgroundColor: "#3b82f6", borderRadius: 3 },
    { label: "FCF", data: summary.map(r => +((r.fcf||0)/1e9).toFixed(1)), backgroundColor: "#10b981", borderRadius: 3 },
    { label: "Net Income", data: summary.map(r => +((r.net_income||0)/1e9).toFixed(1)), backgroundColor: "#8b5cf6", borderRadius: 3 },
  ]}, options: chartOpts });
}

function renderMarginChart(summary) {
  if (charts.margin) charts.margin.destroy();
  const ctx = document.getElementById("marginChart")?.getContext("2d");
  if (!ctx) return;
  const chartOpts = { responsive: true, plugins: { legend: { labels: { color: "#94a3b8", font: { size: 11 } } } }, scales: { x: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } }, y: { ticks: { color: "#94a3b8", callback: v => v + "%" }, grid: { color: "#1e293b" } } } };
  charts.margin = new Chart(ctx, { type: "line", data: { labels: summary.map(r => r.year), datasets: [
    { label: "Gross Margin %", data: summary.map(r => r.gross_margin), borderColor: "#3b82f6", backgroundColor: "transparent", tension: 0.3 },
    { label: "Net Margin %", data: summary.map(r => r.net_margin), borderColor: "#10b981", backgroundColor: "transparent", tension: 0.3 },
    { label: "Op Margin %", data: summary.map(r => r.operating_margin), borderColor: "#f59e0b", backgroundColor: "transparent", tension: 0.3 },
  ]}, options: chartOpts });
}

function prefillValuation(ticker, eps, fcf, shares, netDebt, price) {
  const vt = document.getElementById("valTicker"); if (vt) vt.value = ticker;
  document.getElementById("dcfFcf").value = fcf;
  document.getElementById("dcfShares").value = shares;
  document.getElementById("dcfNetDebt").value = netDebt;
  document.getElementById("dcfPrice").value = price;
  const qt = document.getElementById("quickTicker"); if (qt) qt.value = ticker;
  const qe = document.getElementById("quickEps");    if (qe) qe.value = eps ? eps.toFixed(2) : "";
  const qp = document.getElementById("quickPrice");  if (qp) qp.value = price;
  showSection("valuation");
}

function prefillThesis(ticker, name, price) {
  document.getElementById("thTicker").value = ticker;
  document.getElementById("thTitle").value = ticker + " — Investment Thesis";
  document.getElementById("thCurrentPrice").value = price;
  document.getElementById("thEditId").value = "";
  document.getElementById("thesisFormModal").classList.remove("hidden");
}

// ═══════════════════════════════════════════════════════════════
// INSIDERS PANEL
// ═══════════════════════════════════════════════════════════════
async function loadInsidersPanel(ticker) {
  const el = document.getElementById("insidersContent");
  el.innerHTML = `<div class="flex justify-center py-10"><div class="loader"></div></div>`;
  try {
    const d = await api(`/api/stock/${ticker}/insiders`);
    const sigColor = {
      strong_buy: "text-emerald-400", buy: "text-emerald-300",
      mixed: "text-yellow-400", sell: "text-red-400",
      neutral: "text-slate-400", no_data: "text-slate-500"
    };
    const sigLabel = {
      strong_buy: "🟢 Strong Insider Buying", buy: "🟩 Net Insider Buying",
      mixed: "🟡 Mixed Activity", sell: "🔴 Net Insider Selling",
      neutral: "⚪ No Recent Activity", no_data: "— No Data"
    };
    const buysHtml = d.buys.length ? d.buys.map(b => `
      <tr class="border-b border-slate-800">
        <td class="py-2 text-white text-xs">${b.date}</td>
        <td class="py-2 text-slate-300 text-xs">${b.insider}</td>
        <td class="py-2 text-slate-400 text-xs">${b.title}</td>
        <td class="py-2 text-right text-emerald-400 text-xs font-medium">${b.shares.toLocaleString()} shares</td>
        <td class="py-2 text-right text-emerald-400 text-xs">${b.value ? "$" + fmtBig(b.value) : "—"}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="py-4 text-slate-500 text-center text-sm">No insider purchases in last 60 days</td></tr>`;
    const sellsHtml = d.sells.length ? d.sells.map(s => `
      <tr class="border-b border-slate-800">
        <td class="py-2 text-white text-xs">${s.date}</td>
        <td class="py-2 text-slate-300 text-xs">${s.insider}</td>
        <td class="py-2 text-slate-400 text-xs">${s.title}</td>
        <td class="py-2 text-right text-red-400 text-xs">${s.shares.toLocaleString()} shares</td>
        <td class="py-2 text-right text-red-400 text-xs">${s.value ? "$" + fmtBig(s.value) : "—"}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="py-4 text-slate-500 text-center text-sm">No insider sales in last 60 days</td></tr>`;
    // Build full 2-year history table
    const historyHtml = (d.all_txns && d.all_txns.length)
      ? d.all_txns.map(t => {
          const isBuy  = /purchase|buy|acquisition|exercise/i.test(t.transaction || "");
          const isSell = /sale|sell/i.test(t.transaction || "");
          const txnColor = isBuy ? "text-emerald-400" : isSell ? "text-red-400" : "text-slate-400";
          const recentBadge = t.recent ? `<span class="ml-1 text-xs bg-emerald-900 text-emerald-300 rounded px-1 py-0.5">NEW</span>` : "";
          return `<tr class="border-b border-slate-800 hover:bg-slate-800/40 transition-colors">
            <td class="py-2 text-xs text-slate-400 pr-3 whitespace-nowrap">${t.date}${recentBadge}</td>
            <td class="py-2 text-xs text-white pr-3">${t.insider || "—"}</td>
            <td class="py-2 text-xs text-slate-400 pr-3">${t.title || "—"}</td>
            <td class="py-2 text-xs ${txnColor} pr-3">${t.transaction || "—"}</td>
            <td class="py-2 text-xs text-right ${txnColor} pr-3">${t.shares ? t.shares.toLocaleString() : "—"}</td>
            <td class="py-2 text-xs text-right text-slate-300">${t.value ? "$" + fmtBig(t.value) : "—"}</td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="6" class="py-6 text-slate-500 text-center text-sm">No insider transaction history available</td></tr>`;

    el.innerHTML = `
      <div class="flex items-center gap-3 mb-5">
        <span class="text-lg font-bold ${sigColor[d.net_signal] || 'text-slate-400'}">${sigLabel[d.net_signal] || "—"}</span>
        <span class="text-slate-500 text-sm">${d.summary}</span>
      </div>
      ${d.rationale ? `<div class="metric-box mb-5 text-sm text-slate-300 leading-relaxed">${d.rationale}</div>` : ""}
      <div class="grid grid-cols-2 gap-6 mb-8">
        <div>
          <h3 class="text-white font-semibold mb-3 text-sm">Purchases (last 60 days)</h3>
          <table class="w-full"><thead><tr class="text-slate-500 text-xs"><th class="text-left pb-2">Date</th><th class="text-left pb-2">Name</th><th class="text-left pb-2">Title</th><th class="text-right pb-2">Shares</th><th class="text-right pb-2">Value</th></tr></thead><tbody>${buysHtml}</tbody></table>
        </div>
        <div>
          <h3 class="text-white font-semibold mb-3 text-sm">Sales (last 60 days)</h3>
          <table class="w-full"><thead><tr class="text-slate-500 text-xs"><th class="text-left pb-2">Date</th><th class="text-left pb-2">Name</th><th class="text-left pb-2">Title</th><th class="text-right pb-2">Shares</th><th class="text-right pb-2">Value</th></tr></thead><tbody>${sellsHtml}</tbody></table>
        </div>
      </div>
      <div>
        <h3 class="text-white font-semibold mb-3 text-sm">Full Transaction History (up to 2 years)</h3>
        <div class="overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr class="text-slate-500 text-xs border-b border-slate-700">
                <th class="text-left pb-2 pr-3">Date</th>
                <th class="text-left pb-2 pr-3">Insider</th>
                <th class="text-left pb-2 pr-3">Relationship</th>
                <th class="text-left pb-2 pr-3">Transaction</th>
                <th class="text-right pb-2 pr-3">Shares</th>
                <th class="text-right pb-2">Value</th>
              </tr>
            </thead>
            <tbody>${historyHtml}</tbody>
          </table>
        </div>
      </div>`;
  } catch(e) {
    el.innerHTML = `<div class="text-red-400 text-sm">Error: ${e.message}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
// CATALYSTS PANEL
// ═══════════════════════════════════════════════════════════════
async function loadCatalystsPanel(ticker) {
  const el = document.getElementById("catalystsContent");
  el.innerHTML = `<div class="flex justify-center py-10"><div class="loader"></div></div>`;
  try {
    const d = await api(`/api/stock/${ticker}/catalysts`);
    const catalystNews = d.news.filter(n => n.is_catalyst);
    const otherNews    = d.news.filter(n => !n.is_catalyst);
    el.innerHTML = `
      <div class="grid grid-cols-2 gap-4 mb-6">
        <div class="metric-box">
          <div class="text-slate-400 text-xs mb-1">Next Earnings Date</div>
          <div class="text-xl font-bold text-white">${d.earnings_date || "—"}</div>
          <div class="text-slate-500 text-xs mt-1">Earnings releases are the biggest single catalyst for short-term price movement.</div>
        </div>
        <div class="metric-box">
          <div class="text-slate-400 text-xs mb-1">Ex-Dividend Date</div>
          <div class="text-xl font-bold text-white">${d.ex_dividend || "—"}</div>
          <div class="text-slate-500 text-xs mt-1">Share price typically drops by the dividend amount on ex-div date.</div>
        </div>
      </div>
      ${catalystNews.length ? `
        <h3 class="text-white font-semibold mb-3">🚀 Catalyst News (last 60 days)</h3>
        <div class="space-y-2 mb-5">
          ${catalystNews.map(n => `
            <a href="${n.url}" target="_blank" class="block metric-box hover:border-emerald-600 transition-colors">
              <div class="text-xs text-slate-500 mb-1">${n.date}</div>
              <div class="text-sm text-white">${n.title}</div>
            </a>`).join("")}
        </div>` : ""}
      ${otherNews.length ? `
        <h3 class="text-slate-400 font-semibold mb-3 text-sm">Other Recent News</h3>
        <div class="space-y-1">
          ${otherNews.slice(0,5).map(n => `
            <a href="${n.url}" target="_blank" class="block text-xs text-slate-400 hover:text-white py-1 border-b border-slate-800">${n.date} — ${n.title}</a>`).join("")}
        </div>` : ""}`;
  } catch(e) {
    el.innerHTML = `<div class="text-red-400 text-sm">Error: ${e.message}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
// RED FLAGS PANEL
// ═══════════════════════════════════════════════════════════════
async function loadRedFlagsPanel(ticker) {
  const el = document.getElementById("redflagsContent");
  el.innerHTML = `<div class="flex justify-center py-10"><div class="loader"></div></div>`;
  try {
    const d = await api(`/api/stock/${ticker}/redflags`);
    const sevColor = { high: "border-red-500 bg-red-950", medium: "border-yellow-500 bg-yellow-950" };
    const sevLabel = { high: "🔴 High", medium: "🟡 Medium" };
    const flagsHtml = d.flags.length
      ? d.flags.map(f => `
          <div class="border-l-4 rounded-r p-3 mb-3 ${sevColor[f.severity] || 'border-slate-600 bg-slate-800'}">
            <div class="flex items-center gap-2 mb-1">
              <span class="text-xs font-bold ${f.severity === 'high' ? 'text-red-400' : 'text-yellow-400'}">${sevLabel[f.severity]}</span>
              <span class="text-slate-400 text-xs uppercase tracking-wide">${f.type}</span>
            </div>
            <div class="text-sm text-slate-200">${f.flag}</div>
          </div>`).join("")
      : `<div class="metric-box text-center py-6 text-emerald-400 font-semibold">✅ No significant red flags detected from quantitative metrics.</div>`;
    const negNewsHtml = d.negative_news.length
      ? d.negative_news.map(n => `
          <a href="${n.url}" target="_blank" class="block metric-box hover:border-red-600 transition-colors mb-2">
            <div class="text-xs text-slate-500 mb-1">${n.date}</div>
            <div class="text-sm text-red-300">${n.title}</div>
          </a>`).join("")
      : `<div class="text-slate-500 text-sm text-center py-4">No negative news flagged in the last 60 days.</div>`;
    el.innerHTML = `
      <div class="mb-2 p-3 bg-slate-800 rounded text-xs text-slate-400">
        ⚠ Red flags are derived from financial ratios and news headlines. Always do your own primary research before investing.
      </div>
      <h3 class="text-white font-semibold mb-3 mt-4">Quantitative Red Flags</h3>
      ${flagsHtml}
      <h3 class="text-white font-semibold mb-3 mt-5">⚠ Negative News (last 60 days)</h3>
      ${negNewsHtml}`;
  } catch(e) {
    el.innerHTML = `<div class="text-red-400 text-sm">Error: ${e.message}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
// COMPETITORS PANEL
// ═══════════════════════════════════════════════════════════════
async function loadCompetitorsPanel(ticker) {
  const el = document.getElementById("competitorsContent");
  el.innerHTML = `<div class="flex flex-col items-center py-10 gap-3"><div class="loader"></div><div class="text-slate-500 text-sm">Finding competitors by industry — this may take 30–60 seconds...</div></div>`;
  try {
    const comps = await api(`/api/stock/${ticker}/competitors`, "GET", null, 180000);
    if (!comps.length) {
      el.innerHTML = `<div class="text-slate-500 text-center py-8">No competitors found in our universe for this stock's industry.</div>`;
      return;
    }
    const best = comps[0]; // sorted by growth-value score
    const bestReason = best.pe_ratio && best.revenue_growth > 0
      ? `${best.name} (${best.ticker}) has the best growth-to-valuation ratio with ${best.revenue_growth}% revenue growth at a P/E of ${best.pe_ratio}x — implying a PEG-like score of ${best.growth_value_score}. At this valuation, you are paying less per unit of growth than peers, suggesting potential undervaluation if growth sustains.`
      : `${best.name} (${best.ticker}) ranks highest on our growth-value metric within this peer group.`;
    el.innerHTML = `
      <div class="metric-box mb-5 border-emerald-600">
        <div class="text-xs text-emerald-400 font-semibold mb-1">💡 Best Growth-Value Opportunity</div>
        <div class="text-sm text-slate-200 leading-relaxed">${bestReason}</div>
      </div>
      <div class="space-y-4">
        ${comps.map((c, i) => {
          const catalystNews = c.recent_news.filter(n => n.is_catalyst);
          return `
          <div class="border border-slate-700 rounded-xl p-4 ${i === 0 ? 'border-emerald-700' : ''}">
            <div class="flex items-start justify-between mb-3">
              <div>
                <span class="font-bold text-white text-base">${c.ticker}</span>
                <span class="text-slate-400 text-sm ml-2">${c.name}</span>
                ${i === 0 ? '<span class="pill-green ml-2 text-xs">Best value</span>' : ""}
              </div>
              <div class="text-right">
                <div class="text-slate-400 text-xs">Mkt Cap</div>
                <div class="font-bold text-white">${fmtBig(c.market_cap)}</div>
              </div>
            </div>
            <div class="grid grid-cols-5 gap-3 text-center mb-3">
              ${[
                ["Rev Growth", c.revenue_growth !== null ? c.revenue_growth + "%" : "—", c.revenue_growth > 10 ? "text-emerald-400" : c.revenue_growth > 0 ? "text-slate-300" : "text-red-400"],
                ["P/E", c.pe_ratio || "—", "text-slate-300"],
                ["P/B", c.pb_ratio || "—", "text-slate-300"],
                ["Net Margin", c.net_margin ? c.net_margin + "%" : "—", c.net_margin > 15 ? "text-emerald-400" : "text-slate-300"],
                ["ROE", c.roe ? c.roe + "%" : "—", c.roe > 15 ? "text-emerald-400" : "text-slate-300"],
              ].map(([l,v,cls]) => `<div class="metric-box"><div class="text-slate-500 text-xs">${l}</div><div class="${cls} font-semibold">${v}</div></div>`).join("")}
            </div>
            ${catalystNews.length ? `
              <div class="mt-2">
                <div class="text-xs text-slate-500 mb-1">Recent news (last 60 days)</div>
                ${catalystNews.slice(0,3).map(n => `
                  <a href="${n.url}" target="_blank" class="block text-xs text-blue-400 hover:underline py-0.5 truncate">${n.date} — ${n.title}</a>`).join("")}
              </div>` : ""}
            <button onclick="openResearch('${c.ticker}')" class="mt-3 text-blue-400 text-xs hover:underline">Research ${c.ticker} →</button>
          </div>`;
        }).join("")}
      </div>`;
  } catch(e) {
    el.innerHTML = `<div class="text-red-400 text-sm">Error: ${e.message}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
// VALUATION ENGINE
// ═══════════════════════════════════════════════════════════════
// ── Valuation Summary store ──────────────────────────────────────
window._valSummary = { dcf: null, quick: null, comparable: null };

function switchValTab(tab) {
  ["dcf","quick","comparable","exit-multiple","summary"].forEach(t => {
    document.getElementById("valTab-"+t).classList.toggle("hidden", t !== tab);
    document.getElementById("vt-"+t).classList.toggle("active", t === tab);
  });
  if (tab === "summary") renderValSummary();
  if (tab === "exit-multiple") loadExitMultipleModel();
}

function updateValSummary(method, data) {
  window._valSummary[method] = data;
  // Refresh summary panel if it's currently visible
  const summaryTab = document.getElementById("valTab-summary");
  if (summaryTab && !summaryTab.classList.contains("hidden")) renderValSummary();
}

function renderValSummary() {
  const el = document.getElementById("valSummaryContent");
  const s = window._valSummary;
  const hasAny = s.dcf || s.quick || s.comparable;
  if (!hasAny) {
    el.innerHTML = `<div class="card text-center py-16 text-slate-500">
      <div class="text-4xl mb-3">📊</div>
      <div class="text-base mb-2">No results yet</div>
      <div class="text-sm text-slate-600">Run the DCF, Quick Valuation, and/or Comparable models — results will appear here automatically.</div>
    </div>`;
    return;
  }

  // Collect all individual intrinsic value estimates
  const estimates = [];
  if (s.dcf) {
    ["bear","base","bull"].forEach(k => {
      if (s.dcf[k]?.intrinsic_value) estimates.push({
        method: `DCF (${k})`, value: s.dcf[k].intrinsic_value,
        color: k==="bear"?"text-red-400":k==="bull"?"text-emerald-400":"text-slate-300"
      });
    });
    if (!s.dcf.bear && s.dcf.intrinsic_value) estimates.push({
      method: "DCF", value: s.dcf.intrinsic_value, color: "text-emerald-400"
    });
  }
  if (s.quick) {
    ["graham","pe","lynch"].forEach(k => {
      if (s.quick[k]) estimates.push({
        method: `Quick (${k})`, value: s.quick[k], color: "text-blue-400"
      });
    });
  }
  if (s.comparable) {
    if (s.comparable.pe_value)  estimates.push({ method: "Comp (P/E)",      value: s.comparable.pe_value,  color: "text-purple-400" });
    if (s.comparable.ev_value)  estimates.push({ method: "Comp (EV/EBITDA)",value: s.comparable.ev_value,  color: "text-purple-400" });
    if (s.comparable.blended)   estimates.push({ method: "Comp (Blended)",  value: s.comparable.blended,   color: "text-purple-300" });
  }

  const currentPrice = parseFloat(document.getElementById("dcfPrice")?.value) || 0;
  const vals = estimates.map(e => e.value).filter(v => v > 0);
  const consensusLow  = vals.length ? Math.min(...vals) : null;
  const consensusHigh = vals.length ? Math.max(...vals) : null;
  const consensusMid  = vals.length ? (vals.reduce((a,b)=>a+b,0)/vals.length) : null;

  const rangeBar = (consensusLow && consensusHigh && currentPrice) ? (() => {
    const lo = Math.min(consensusLow * 0.8, currentPrice * 0.8);
    const hi = Math.max(consensusHigh * 1.1, currentPrice * 1.1);
    const range = hi - lo;
    const pricePos = Math.max(0, Math.min(100, ((currentPrice - lo) / range) * 100)).toFixed(1);
    return `
      <div class="mt-4">
        <div class="flex justify-between text-xs text-slate-500 mb-1">
          <span>Low est: ${fmt(consensusLow)}</span>
          <span class="text-white font-medium">Current: ${fmt(currentPrice)}</span>
          <span>High est: ${fmt(consensusHigh)}</span>
        </div>
        <div class="relative h-3 bg-slate-700 rounded-full">
          <div class="absolute h-3 rounded-full bg-emerald-800"
               style="left:${Math.max(0,((consensusLow-lo)/range*100)).toFixed(1)}%;width:${((consensusHigh-consensusLow)/range*100).toFixed(1)}%"></div>
          <div class="absolute w-1 h-3 bg-white rounded" style="left:${pricePos}%;transform:translateX(-50%)"></div>
        </div>
        <div class="text-center text-xs text-slate-500 mt-1">
          ${currentPrice < consensusLow ? `<span class="text-emerald-400 font-semibold">Price is below all estimates — potential value opportunity</span>`
            : currentPrice > consensusHigh ? `<span class="text-red-400 font-semibold">Price exceeds all estimates — appears fully valued</span>`
            : `<span class="text-yellow-400 font-semibold">Price is within the estimated range</span>`}
        </div>
      </div>`;
  })() : "";

  el.innerHTML = `
    <div class="space-y-5">
      <div class="card">
        <h3 class="font-semibold text-white mb-4">Consensus Intrinsic Value Range</h3>
        <div class="grid grid-cols-3 gap-4 text-center mb-2">
          <div>
            <div class="text-slate-400 text-xs mb-1">Floor (Most Conservative)</div>
            <div class="text-2xl font-bold text-red-400">${consensusLow ? fmt(consensusLow) : "—"}</div>
          </div>
          <div>
            <div class="text-slate-400 text-xs mb-1">Average of All Estimates</div>
            <div class="text-2xl font-bold text-amber-400">${consensusMid ? fmt(consensusMid) : "—"}</div>
          </div>
          <div>
            <div class="text-slate-400 text-xs mb-1">Ceiling (Most Optimistic)</div>
            <div class="text-2xl font-bold text-emerald-400">${consensusHigh ? fmt(consensusHigh) : "—"}</div>
          </div>
        </div>
        ${rangeBar}
      </div>

      <div class="card">
        <h3 class="font-semibold text-white mb-4">All Estimates Breakdown</h3>
        <table class="w-full text-sm">
          <thead>
            <tr class="text-slate-500 text-xs border-b border-slate-700">
              <th class="text-left pb-2">Method</th>
              <th class="text-right pb-2">Intrinsic Value</th>
              ${currentPrice ? `<th class="text-right pb-2">vs Current (${fmt(currentPrice)})</th>` : ""}
              <th class="text-right pb-2">Signal</th>
            </tr>
          </thead>
          <tbody>
            ${estimates.map(e => {
              const mos = currentPrice ? ((e.value - currentPrice) / e.value * 100) : null;
              const signal = mos !== null ? getSignal(mos) : null;
              return `<tr class="border-b border-slate-800">
                <td class="py-2 text-slate-300">${e.method}</td>
                <td class="py-2 text-right font-bold ${e.color}">${fmt(e.value)}</td>
                ${currentPrice ? `<td class="py-2 text-right text-xs ${mos>0?"text-emerald-400":"text-red-400"}">${mos!==null?`${mos>0?"+":""}${mos.toFixed(1)}% MoS`:"—"}</td>` : ""}
                <td class="py-2 text-right text-xs">${signal ? `<span class="signal-${signal.key}">${signal.label.split("—")[0].trim()}</span>` : "—"}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    </div>`;
}

// ── Master valuation loader — fills ALL tabs and runs all models ─
async function loadValTicker() {
  const ticker = (document.getElementById("valTicker").value || "").trim().toUpperCase();
  if (!ticker) { alert("Please enter a ticker symbol first."); return; }

  const btn    = document.getElementById("valLoadBtn");
  const status = document.getElementById("valTickerStatus");
  btn.disabled = true;
  btn.textContent = "Loading…";
  status.textContent = `Fetching data for ${ticker}…`;

  try {
    const info = await api("/api/stock/" + ticker, "GET", null, 30000);
    if (info.error) throw new Error(info.error);

    const fcf     = info.free_cashflow      || 0;
    const shares  = info.shares_outstanding || 0;
    const netDebt = (info.total_debt || 0) - (info.total_cash || 0);
    const price   = info.current_price      || 0;
    const eps     = info.eps_ttm            || 0;
    const growth  = info.earnings_growth    ? Math.round(info.earnings_growth * 100) : 8;

    // ── Populate DCF inputs ──────────────────────────────────────
    document.getElementById("dcfFcf").value     = fcf;
    document.getElementById("dcfShares").value  = shares;
    document.getElementById("dcfNetDebt").value = netDebt;
    document.getElementById("dcfPrice").value   = price ? price.toFixed(2) : "";
    // Update scale hints immediately after fill
    updateDcfHint("dcfFcf",     "dcfFcfHint",    "$");
    updateDcfHint("dcfShares",  "dcfSharesHint", "");
    updateDcfHint("dcfNetDebt", "dcfDebtHint",   "$");

    // ── Populate Quick Valuation inputs ─────────────────────────
    const qTicker = document.getElementById("quickTicker"); if (qTicker) qTicker.value = ticker;
    const qEps    = document.getElementById("quickEps");    if (qEps)    qEps.value    = eps ? eps.toFixed(2) : "";
    const qPrice  = document.getElementById("quickPrice");  if (qPrice)  qPrice.value  = price || "";
    const qGrowth = document.getElementById("quickGrowth"); if (qGrowth) qGrowth.value = growth;

    // ── Populate Comparable inputs ───────────────────────────────
    const cTicker  = document.getElementById("compTicker");  if (cTicker)  cTicker.value  = ticker;
    const cEps     = document.getElementById("compEps");     if (cEps)     cEps.value     = eps ? eps.toFixed(2) : "";
    const cPrice   = document.getElementById("compPrice");   if (cPrice)   cPrice.value   = price || "";
    const cShares  = document.getElementById("compShares");  if (cShares)  cShares.value  = shares || "";
    const cNetDebt = document.getElementById("compNetDebt"); if (cNetDebt) cNetDebt.value = netDebt;
    // Try to get EBITDA from financials
    try {
      const fin = await api("/api/stock/" + ticker + "/financials", "GET", null, 20000);
      if (fin.summary?.length) {
        const latest  = fin.summary[fin.summary.length - 1];
        const cEbitda = document.getElementById("compEbitda");
        if (cEbitda && latest.ebitda) cEbitda.value = latest.ebitda;
      }
    } catch (_) {}

    const fcfStr = fcf ? ` | FCF: $${(fcf/1e9).toFixed(1)}B` : " | FCF: N/A (enter manually)";
    status.innerHTML = `<span class="text-emerald-400 font-semibold">✓ ${ticker}${info.name ? " — " + info.name : ""}</span>` +
      ` | Price: $${price.toFixed(2)}${fcfStr}` +
      ` | Shares: ${(shares/1e9).toFixed(2)}B` +
      ` | Net Debt: ${netDebt >= 0 ? "+" : ""}$${(netDebt/1e9).toFixed(1)}B`;

    // ── Auto-run all 3 DCF scenarios ─────────────────────────────
    if (fcf && shares) {
      await runDCF();
    } else {
      document.getElementById("dcfResults").innerHTML =
        `<div class="card text-amber-400 p-4 text-sm">⚠ FCF data not available for ${ticker}. ` +
        `Enter Current FCF manually above, then click <strong>↺ Re-run</strong>.</div>`;
    }

    // ── Auto-run Quick Valuation ─────────────────────────────────
    if (eps) { try { await runQuick(); } catch (_) {} }

  } catch (e) {
    status.innerHTML = `<span class="text-red-400">✗ ${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Load & Run All →";
  }
}

async function autofillDCF() {
  // Keep for backwards compatibility — delegate to master loader
  const t = document.getElementById("valTicker");
  if (t && !t.value) {
    const old = document.getElementById("dcfTicker");
    if (old && old.value) t.value = old.value;
  }
  await loadValTicker();
}

async function runDCF() {
  const sharedFcf   = parseFloat(document.getElementById("dcfFcf").value);
  const sharedShare = parseFloat(document.getElementById("dcfShares").value);
  const sharedDebt  = parseFloat(document.getElementById("dcfNetDebt").value) || 0;
  const price       = parseFloat(document.getElementById("dcfPrice").value) || 0;

  if (!sharedFcf || !sharedShare) {
    alert("Please fill in FCF and Shares (use the Fill button to auto-populate from a ticker).");
    return;
  }

  const scenarios = {
    bear: { growth_1_5: parseFloat(document.getElementById("dcfG1Bear").value),
            growth_6_10: parseFloat(document.getElementById("dcfG2Bear").value),
            wacc: parseFloat(document.getElementById("dcfWaccBear").value),
            terminal_growth: parseFloat(document.getElementById("dcfTgBear").value) },
    base: { growth_1_5: parseFloat(document.getElementById("dcfG1").value),
            growth_6_10: parseFloat(document.getElementById("dcfG2").value),
            wacc: parseFloat(document.getElementById("dcfWacc").value),
            terminal_growth: parseFloat(document.getElementById("dcfTg").value) },
    bull: { growth_1_5: parseFloat(document.getElementById("dcfG1Bull").value),
            growth_6_10: parseFloat(document.getElementById("dcfG2Bull").value),
            wacc: parseFloat(document.getElementById("dcfWaccBull").value),
            terminal_growth: parseFloat(document.getElementById("dcfTgBull").value) },
  };

  document.getElementById("dcfResults").innerHTML =
    `<div class="flex justify-center py-10"><div class="loader"></div><span class="text-slate-500 text-sm ml-3">Running 3 scenarios...</span></div>`;

  try {
    const [bearRes, baseRes, bullRes] = await Promise.all(
      ["bear","base","bull"].map(k => api("/api/valuation/dcf", "POST", {
        current_fcf: sharedFcf, shares: sharedShare, net_debt: sharedDebt, ...scenarios[k]
      }))
    );
    await renderDCFResults({ bear: bearRes, base: baseRes, bull: bullRes }, price);
  } catch (e) {
    document.getElementById("dcfResults").innerHTML = `<div class="card text-red-400 p-4">Error: ${e.message}</div>`;
  }
}

function getSignal(mos) {
  if (mos >= 30) return { key: "strong_buy",                label: "🟢 STRONG BUY — >30% Margin of Safety" };
  if (mos >= 15) return { key: "buy",                       label: "🟢 BUY — 15–30% Margin of Safety" };
  if (mos >= 0)  return { key: "fairly_valued",             label: "🟡 FAIRLY VALUED — Limited Margin of Safety" };
  if (mos >= -20) return { key: "overvalued",               label: "🔴 OVERVALUED — Trading Above Intrinsic Value" };
  return          { key: "significantly_overvalued",        label: "🔴 SIGNIFICANTLY OVERVALUED" };
}

async function renderDCFResults(scenarios, currentPrice) {
  // Support both old single-result and new 3-scenario format
  if (scenarios.intrinsic_value !== undefined) {
    // Legacy single result path
    const res = scenarios;
    const iv = res.intrinsic_value;
    const mos = currentPrice ? ((iv - currentPrice) / iv * 100) : null;
    scenarios = { bear: res, base: res, bull: res };
  }

  const scenarioMeta = {
    bear: { label: "🐻 Bear",  border: "border-red-800",     headerColor: "text-red-400"     },
    base: { label: "📊 Base",  border: "border-slate-600",   headerColor: "text-slate-300"   },
    bull: { label: "🐂 Bull",  border: "border-emerald-800", headerColor: "text-emerald-400" },
  };

  function scenarioCard(key, res) {
    if (res.error) return `<div class="card border ${scenarioMeta[key].border} text-red-400 p-4">${res.error}</div>`;
    const iv  = res.intrinsic_value;
    const mos = currentPrice ? ((iv - currentPrice) / iv * 100) : null;
    const signal = mos !== null ? getSignal(mos) : null;
    const mosColor = mos > 0 ? "text-emerald-400" : "text-red-400";
    return `
      <div class="card border ${scenarioMeta[key].border}">
        <h3 class="font-bold ${scenarioMeta[key].headerColor} mb-4">${scenarioMeta[key].label}</h3>
        <div class="text-center mb-4">
          <div class="text-slate-400 text-xs mb-1">Intrinsic Value / Share</div>
          <div class="text-4xl font-bold text-emerald-400">${fmt(iv)}</div>
          ${currentPrice ? `
          <div class="mt-2 text-xs text-slate-400">Margin of Safety:
            <span class="font-bold ${mosColor}">${mos !== null ? mos.toFixed(1)+"%" : "—"}</span>
          </div>` : ""}
          ${signal ? `<div class="mt-2 text-xs font-semibold signal-${signal.key}">${signal.label}</div>` : ""}
        </div>
        <div class="text-xs bg-slate-900 rounded-lg p-3 space-y-1 text-slate-400">
          <div class="flex justify-between"><span>PV of FCFs:</span><span class="text-white">${fmtBig(res.pv_fcfs)}</span></div>
          <div class="flex justify-between"><span>PV Terminal:</span><span class="text-white">${fmtBig(res.pv_terminal)}</span></div>
          <div class="flex justify-between"><span>Terminal % of EV:</span><span class="text-white">${res.tv_pct_of_ev}%</span></div>
          <div class="flex justify-between"><span>Buy at 30% MoS:</span><span class="text-emerald-400 font-bold">${fmt(iv * 0.7)}</span></div>
          <div class="flex justify-between"><span>Enterprise Value:</span><span class="text-white">${fmtBig(res.enterprise_value)}</span></div>
        </div>
      </div>`;
  }

  // Range summary
  const ivValues = ["bear","base","bull"].map(k => scenarios[k]?.intrinsic_value).filter(v => v > 0);
  const ivMin = ivValues.length ? Math.min(...ivValues) : null;
  const ivMax = ivValues.length ? Math.max(...ivValues) : null;
  const ivMid = scenarios.base?.intrinsic_value || null;

  const rangeSummary = (ivMin && ivMax) ? `
    <div class="card mb-5">
      <div class="flex items-center justify-between flex-wrap gap-4">
        <div>
          <div class="text-xs text-slate-400 mb-1">Intrinsic Value Range</div>
          <div class="text-2xl font-bold text-white">${fmt(ivMin)} <span class="text-slate-500 text-lg">–</span> ${fmt(ivMax)}</div>
          ${ivMid ? `<div class="text-xs text-slate-500 mt-1">Base case: <span class="text-amber-400 font-semibold">${fmt(ivMid)}</span></div>` : ""}
        </div>
        ${currentPrice ? `
        <div class="text-right">
          <div class="text-xs text-slate-400 mb-1">Current Price vs Range</div>
          <div class="text-2xl font-bold ${currentPrice <= ivMin ? "text-emerald-400" : currentPrice <= ivMax ? "text-yellow-400" : "text-red-400"}">${fmt(currentPrice)}</div>
          <div class="text-xs text-slate-500 mt-1">${currentPrice <= ivMin ? "Below bear — deep value" : currentPrice <= ivMax ? "Within range — selective" : "Above bull — fully valued"}</div>
        </div>` : ""}
      </div>
    </div>` : "";

  // Base case projections table
  const projTable = scenarios.base?.projections?.length ? `
    <div class="card mt-5">
      <h3 class="font-semibold text-white mb-3">Base Case — 10-Year FCF Projections</h3>
      <div class="overflow-x-auto">
        <table class="w-full text-xs text-center">
          <thead><tr class="text-slate-400 border-b border-slate-700">
            ${["Year",...scenarios.base.projections.map(p=>`Y${p.year}`)].map(h=>`<th class="pb-2 pr-2">${h}</th>`).join("")}
          </tr></thead>
          <tbody>
            <tr class="border-b border-slate-800">${["FCF",...scenarios.base.projections.map(p=>fmtBig(p.fcf))].map(v=>`<td class="py-2 pr-2 text-white">${v}</td>`).join("")}</tr>
            <tr>${["PV",...scenarios.base.projections.map(p=>fmtBig(p.pv))].map(v=>`<td class="py-2 pr-2 text-blue-400">${v}</td>`).join("")}</tr>
          </tbody>
        </table>
      </div>
    </div>` : "";

  // Base sensitivity table
  const sensTable = scenarios.base?.sensitivity?.length ? `
    <div class="card mt-5">
      <h3 class="font-semibold text-white mb-1">Base Case — Sensitivity Table (Intrinsic Value / Share)</h3>
      <div class="text-xs text-slate-500 mb-3">Rows: WACC ± 2% &nbsp;|&nbsp; Columns: Terminal Growth Rate ± 1%</div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs text-center">
          <thead><tr class="text-slate-400 border-b border-slate-700">
            <th class="pb-2 text-left">WACC \\ TGR</th>
            ${Object.keys(scenarios.base.sensitivity[0]?.values||{}).map(k=>`<th class="pb-2">${k}%</th>`).join("")}
          </tr></thead>
          <tbody>
            ${scenarios.base.sensitivity.map(row=>`
              <tr class="border-b border-slate-800">
                <td class="py-2 text-left text-slate-400">${row.wacc}%</td>
                ${Object.values(row.values).map(v=>{
                  if(v==="N/A") return `<td class="py-2 text-red-400">N/A</td>`;
                  const base_iv = scenarios.base.intrinsic_value;
                  const cls = v>=base_iv*1.2?"text-emerald-400":v<=base_iv*0.8?"text-red-400":"text-white";
                  return `<td class="py-2 ${cls}">${fmt(v)}</td>`;
                }).join("")}
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>` : "";

  // ── Revenue projection (5yr) from base scenario inputs ──────
  const fcf = parseFloat(document.getElementById("dcfFcf").value) || 0;
  const g1  = parseFloat(document.getElementById("dcfG1").value)  || 0;
  const ticker = (document.getElementById("valTicker") || document.getElementById("dcfTicker") || {value:""}).value.trim().toUpperCase();

  // Fetch actual revenue from stock info if ticker present
  let revProjection = "";
  if (ticker) {
    try {
      const info = await api("/api/stock/" + ticker);
      const baseRev = info.total_revenue || 0;
      if (baseRev > 0) {
        const revRows = [];
        let r = baseRev;
        for (let y = 1; y <= 5; y++) {
          r *= (1 + g1 / 100);
          revRows.push({ year: `Y+${y}`, rev: r });
        }
        revProjection = `
          <div class="card mt-5">
            <h3 class="font-semibold text-white mb-1">5-Year Revenue Projection (Base Case)</h3>
            <div class="text-xs text-slate-500 mb-3">Assuming ${g1}% annual FCF growth applied to current revenue of ${fmtBig(baseRev)}</div>
            <div class="grid grid-cols-6 gap-2 text-center">
              <div class="metric-box py-2">
                <div class="text-slate-400 text-xs mb-1">Current</div>
                <div class="font-bold text-white text-sm">${fmtBig(baseRev)}</div>
              </div>
              ${revRows.map(r2 => `
                <div class="metric-box py-2">
                  <div class="text-slate-400 text-xs mb-1">${r2.year}</div>
                  <div class="font-bold text-emerald-400 text-sm">${fmtBig(r2.rev)}</div>
                </div>`).join("")}
            </div>
          </div>`;
      }
    } catch(e) { /* silent fail */ }
  }

  // ── Verdict ──────────────────────────────────────────────────
  const baseIV = scenarios.base?.intrinsic_value;
  let verdictHtml = "";
  if (baseIV && currentPrice) {
    const mosBase = ((baseIV - currentPrice) / baseIV * 100);
    const bearIV  = scenarios.bear?.intrinsic_value;
    const bullIV  = scenarios.bull?.intrinsic_value;
    let verdict, verdictColor, verdictBg, explanation;
    if (mosBase >= 30) {
      verdict = "🟢 UNDERVALUED"; verdictColor = "text-emerald-400"; verdictBg = "bg-emerald-950 border-emerald-700";
      explanation = `Trading at ${mosBase.toFixed(1)}% below intrinsic value. Even in the bear case (${fmt(bearIV)}), the stock appears to offer a margin of safety. Strong buy zone for a value investor.`;
    } else if (mosBase >= 10) {
      verdict = "🟡 MILDLY UNDERVALUED"; verdictColor = "text-yellow-400"; verdictBg = "bg-yellow-950 border-yellow-700";
      explanation = `Trading ${mosBase.toFixed(1)}% below base case intrinsic value — some upside exists but limited margin of safety. Consider waiting for a better entry or sizing conservatively.`;
    } else if (mosBase >= -10) {
      verdict = "⚪ FAIRLY VALUED"; verdictColor = "text-slate-300"; verdictBg = "bg-slate-800 border-slate-600";
      explanation = `Price is within ±10% of intrinsic value. The stock is priced in at current expectations. Only buy if you have high conviction on bull-case assumptions.`;
    } else {
      verdict = "🔴 OVERVALUED"; verdictColor = "text-red-400"; verdictBg = "bg-red-950 border-red-700";
      explanation = `Trading ${Math.abs(mosBase).toFixed(1)}% above base case intrinsic value. Even the bull case (${fmt(bullIV)}) is near or below current price. High risk of capital loss if growth disappoints.`;
    }
    verdictHtml = `
      <div class="card border mt-5 ${verdictBg}">
        <div class="text-xl font-bold ${verdictColor} mb-2">${verdict}</div>
        <div class="text-sm text-slate-300 leading-relaxed">${explanation}</div>
        <div class="mt-3 grid grid-cols-3 gap-3 text-center text-xs text-slate-500">
          <div>Bear IV: <span class="text-red-400 font-semibold">${fmt(bearIV)}</span></div>
          <div>Base IV: <span class="text-amber-400 font-semibold">${fmt(baseIV)}</span></div>
          <div>Bull IV: <span class="text-emerald-400 font-semibold">${fmt(bullIV)}</span></div>
        </div>
      </div>`;
  }

  // ── Key Assumptions That Could Break The Model ───────────────
  const g2Base = parseFloat(document.getElementById("dcfG2").value) || 0;
  const waccBase = parseFloat(document.getElementById("dcfWacc").value) || 10;
  const tgBase   = parseFloat(document.getElementById("dcfTg").value)   || 2.5;

  const keyAssumptions = `
    <div class="card mt-5">
      <h3 class="font-semibold text-white mb-3">🔑 Key Assumptions That Could Break This Model</h3>
      <div class="space-y-3 text-sm">
        ${[
          { icon: "📉", title: "FCF Growth Disappointment",
            text: `Model assumes ${g1}% FCF growth in years 1-5. If growth falls to the bear case (${parseFloat(document.getElementById("dcfG1Bear").value)||3}%), intrinsic value drops to ${fmt(scenarios.bear?.intrinsic_value)}. A single bad earnings cycle can derail the thesis.` },
          { icon: "📈", title: "Interest Rate / WACC Sensitivity",
            text: `Base WACC of ${waccBase}%. If rates rise +2% (WACC=${waccBase+2}%), the discount effect alone can cut intrinsic value by 15-25%. High-multiple stocks are especially rate-sensitive.` },
          { icon: "🏁", title: "Terminal Growth Rate Assumption",
            text: `Terminal growth of ${tgBase}% assumes the business grows forever at roughly GDP rate. If the business matures faster or faces disruption, terminal value (which drives ${scenarios.base?.tv_pct_of_ev||"N/A"}% of enterprise value in the base case) collapses.` },
          { icon: "🏦", title: "Capital Structure Changes",
            text: "Model uses current net debt. Significant new debt issuance, share dilution, or a large acquisition can alter the equity value per share even if enterprise value stays constant." },
          { icon: "🔄", title: "FCF vs Reported Earnings",
            text: "DCF uses free cash flow — if management changes capex or working capital policies (e.g., aggressive capex ramp), reported FCF can drop sharply without a real change in underlying business quality." },
        ].map(a => `
          <div class="flex gap-3 p-3 bg-slate-900 rounded-lg">
            <div class="text-xl flex-shrink-0">${a.icon}</div>
            <div>
              <div class="font-semibold text-white mb-1">${a.title}</div>
              <div class="text-slate-400 text-xs leading-relaxed">${a.text}</div>
            </div>
          </div>`).join("")}
      </div>
    </div>`;

  document.getElementById("dcfResults").innerHTML = `
    <div>
      ${rangeSummary}
      <div class="grid grid-cols-3 gap-4">
        ${scenarioCard("bear", scenarios.bear)}
        ${scenarioCard("base", scenarios.base)}
        ${scenarioCard("bull", scenarios.bull)}
      </div>
      ${verdictHtml}
      ${revProjection}
      ${projTable}
      ${sensTable}
      ${keyAssumptions}
    </div>`;

  // Push to valuation summary tab
  updateValSummary("dcf", scenarios);
}

async function autofillQuick() {
  // Sync shared ticker then delegate
  const qt = document.getElementById("quickTicker");
  const vt = document.getElementById("valTicker");
  const ticker = (qt?.value || vt?.value || "").trim().toUpperCase();
  if (!ticker) return;
  if (vt) vt.value = ticker;
  await loadValTicker();
}

async function runQuick() {
  const body = {
    eps: parseFloat(document.getElementById("quickEps").value),
    reasonable_pe: parseFloat(document.getElementById("quickPe").value),
    growth_rate: parseFloat(document.getElementById("quickGrowth").value),
  };
  const price = parseFloat(document.getElementById("quickPrice").value) || 0;
  if (!body.eps) { alert("Please enter EPS."); return; }
  const res = await api("/api/valuation/quick", "POST", body);
  const methods = [
    { name: "Graham Formula", value: res.graham, desc: "EPS × (8.5 + 2g) × 4.4 / AAA yield" },
    { name: "P/E Method", value: res.pe_method, desc: `EPS × ${body.reasonable_pe}x P/E` },
    { name: "Peter Lynch (5-yr)", value: res.lynch_5yr, desc: "Forward EPS × 15 in 5 years" },
    { name: "Average", value: res.average, desc: "Mean of all three methods" },
  ];
  document.getElementById("quickResults").innerHTML = `<div class="space-y-3">
    ${methods.map(m => {
      const mos = price && m.value ? ((m.value-price)/m.value*100) : null;
      return `<div class="card flex justify-between items-center">
        <div><div class="font-semibold text-white">${m.name}</div><div class="text-slate-500 text-xs">${m.desc}</div></div>
        <div class="text-right">
          <div class="text-xl font-bold text-emerald-400">${fmt(m.value)}</div>
          ${mos!==null?`<div class="text-xs ${mos>0?'text-emerald-400':'text-red-400'}">${mos>0?"+":""}${mos.toFixed(1)}% MoS</div>`:""}
        </div>
      </div>`;
    }).join("")}
    ${price?`<div class="card text-center text-sm text-slate-400">Current Price: <span class="text-white font-bold">${fmt(price)}</span></div>`:""}
  </div>`;
  updateValSummary("quick", { graham: res.graham, pe: res.pe_method, lynch: res.lynch_5yr });
}

async function autofillComp() {
  // Sync shared ticker then delegate
  const ct = document.getElementById("compTicker");
  const vt = document.getElementById("valTicker");
  const ticker = (ct?.value || vt?.value || "").trim().toUpperCase();
  if (!ticker) return;
  if (vt) vt.value = ticker;
  await loadValTicker();
}

async function runComp() {
  const body = {
    eps: parseFloat(document.getElementById("compEps").value)||0,
    ebitda: parseFloat(document.getElementById("compEbitda").value)||0,
    net_debt: parseFloat(document.getElementById("compNetDebt").value)||0,
    shares: parseFloat(document.getElementById("compShares").value)||1,
    sector_pe: parseFloat(document.getElementById("compSectorPe").value)||15,
    sector_ev_ebitda: parseFloat(document.getElementById("compSectorEv").value)||12,
  };
  const price = parseFloat(document.getElementById("compPrice").value)||0;
  const res = await api("/api/valuation/comparable", "POST", body);
  document.getElementById("compResults").innerHTML = `<div class="space-y-3">
    ${[["P/E Based",res.pe_based,`Sector P/E: ${body.sector_pe}x`],
       ["EV/EBITDA Based",res.ev_ebitda_based,`Sector EV/EBITDA: ${body.sector_ev_ebitda}x`],
       ["Average",res.average,"Mean of both methods"]].map(([name,val,desc])=>{
      const mos = price&&val ? ((val-price)/val*100) : null;
      return `<div class="card flex justify-between items-center">
        <div><div class="font-semibold text-white">${name}</div><div class="text-slate-500 text-xs">${desc}</div></div>
        <div class="text-right">
          <div class="text-xl font-bold ${val>price?'text-emerald-400':'text-red-400'}">${fmt(val)}</div>
          ${mos!==null?`<div class="text-xs ${mos>0?'text-emerald-400':'text-red-400'}">${mos>0?"+":""}${mos.toFixed(1)}% MoS</div>`:""}
        </div>
      </div>`;
    }).join("")}
    ${price?`<div class="card text-center text-sm text-slate-400">Current Price: <span class="text-white font-bold">${fmt(price)}</span></div>`:""}
  </div>`;
  updateValSummary("comparable", { pe_value: res.pe_based, ev_value: res.ev_ebitda_based, blended: res.average });
}

// ═══════════════════════════════════════════════════════════════
// WEEKLY THESIS TRACKER
// ═══════════════════════════════════════════════════════════════
async function loadWeeklyTracker() {
  const el = document.getElementById("weeklyTrackerContent");
  el.innerHTML = `<div class="flex justify-center py-12"><div class="loader"></div><span class="text-slate-500 text-sm ml-3">Loading live data for all theses…</span></div>`;
  try {
    const data = await api("/api/thesis/weekly-tracker", "GET", null, 120000);
    if (!data.length) {
      el.innerHTML = `<div class="card text-center py-12 text-slate-500">
        <div class="text-4xl mb-3">📋</div>
        <div class="mb-2">No active theses yet.</div>
        <div class="text-sm">Go to <a class="text-emerald-400 cursor-pointer" onclick="showSection('thesis')">Thesis</a> to write your first investment thesis, or use the "Thesis" button in the Screener.</div>
      </div>`;
      return;
    }
    el.innerHTML = `<div class="space-y-6">${data.map(t => renderTrackerCard(t)).join("")}</div>`;
  } catch(e) {
    el.innerHTML = `<div class="card text-red-400 text-sm p-4">Error: ${e.message}</div>`;
  }
}

function renderTrackerCard(t) {
  const priceChg = t.price_change_since_entry;
  const priceColor = priceChg > 5 ? "text-emerald-400" : priceChg < -5 ? "text-red-400" : "text-yellow-400";
  const mosColor  = (t.mos_now||0) > 20 ? "text-emerald-400" : (t.mos_now||0) > 0 ? "text-yellow-400" : "text-red-400";
  const statusColors = { active: "bg-emerald-900 text-emerald-300", monitoring: "bg-yellow-900 text-yellow-300", closed: "bg-slate-700 text-slate-400" };

  function metricDelta(label, entryVal, currentVal, higherIsBetter=true) {
    if (entryVal == null && currentVal == null) return "";
    const delta = (currentVal != null && entryVal != null) ? currentVal - entryVal : null;
    const improved = delta !== null ? (higherIsBetter ? delta >= 0 : delta <= 0) : null;
    const deltaStr = delta !== null ? `<span class="${improved ? 'text-emerald-400' : 'text-red-400'} text-xs ml-1">(${delta > 0 ? '+' : ''}${delta.toFixed(1)})</span>` : "";
    return `<div class="metric-box text-center py-2 flex-1">
      <div class="text-slate-500 text-xs mb-0.5">${label}</div>
      <div class="font-semibold text-white text-sm">${currentVal != null ? currentVal : "—"}${deltaStr}</div>
      ${entryVal != null ? `<div class="text-slate-600 text-xs">Entry: ${entryVal}</div>` : ""}
    </div>`;
  }

  const newsHtml = (t.recent_news||[]).length
    ? `<div class="mt-4">
        <h4 class="text-xs font-semibold text-slate-400 mb-2">📰 News This Week</h4>
        <div class="space-y-1">
          ${t.recent_news.slice(0,4).map(n => `
            <a href="${n.url}" target="_blank" class="block text-xs text-slate-400 hover:text-white border-b border-slate-800 pb-1">${n.date} — ${n.title}</a>`).join("")}
        </div>
      </div>`
    : `<div class="mt-4 text-xs text-slate-600">No news this week.</div>`;

  return `
    <div class="card">
      <div class="flex items-start justify-between mb-4 flex-wrap gap-2">
        <div>
          <div class="flex items-center gap-2 mb-1">
            <h3 class="font-bold text-white text-lg cursor-pointer hover:text-emerald-400" onclick="openResearch('${t.ticker}')">${t.ticker}</h3>
            <span class="text-xs px-2 py-0.5 rounded ${statusColors[t.status] || statusColors.active}">${t.status||"active"}</span>
          </div>
          <div class="text-slate-400 text-sm">${t.title || ""}</div>
          <div class="text-slate-500 text-xs mt-0.5">Written ${t.created_date||"—"} · Updated ${t.updated_date||"—"}</div>
        </div>
        <div class="text-right">
          <div class="text-2xl font-bold text-white">${fmt(t.current_price_live)}</div>
          <div class="${priceColor} text-sm font-medium">${priceChg != null ? (priceChg > 0 ? '+' : '') + priceChg + '% since entry' : '—'}</div>
          ${t.mos_now != null ? `<div class="${mosColor} text-xs">${t.mos_now > 0 ? '▲' : '▼'} ${Math.abs(t.mos_now)}% MoS vs target</div>` : ""}
        </div>
      </div>

      <!-- Key Metrics vs Entry -->
      <div class="flex gap-2 flex-wrap mb-2">
        ${metricDelta("P/E", t.entry_pe ? (+t.entry_pe).toFixed(1) : null, t.current_pe ? (+t.current_pe).toFixed(1) : null, false)}
        ${metricDelta("ROE %", t.entry_roe ? (+t.entry_roe).toFixed(1) : null, t.current_roe, true)}
        ${metricDelta("Rev Growth %", t.entry_revenue_growth ? (+t.entry_revenue_growth).toFixed(1) : null, t.current_rev_growth, true)}
        ${metricDelta("Net Margin %", t.entry_net_margin ? (+t.entry_net_margin).toFixed(1) : null, t.current_net_margin, true)}
      </div>
      <div class="text-xs text-slate-600 mb-3">Green/red delta = change since thesis was written. Higher is better for ROE, margins, growth. Lower is better for P/E.</div>

      ${newsHtml}

      <div class="flex gap-2 mt-4">
        <button onclick="openResearch('${t.ticker}')" class="btn-secondary text-xs py-1 px-3">Research →</button>
        <button onclick="prefillValuation('${t.ticker}',0,0,0,0,${t.current_price_live||0})" class="btn-secondary text-xs py-1 px-3">Value It →</button>
      </div>
    </div>`;
}

// ═══════════════════════════════════════════════════════════════
// THESIS TRACKER
// ═══════════════════════════════════════════════════════════════
let selectedMoats = [];

function toggleMoat(btn, type) {
  btn.classList.toggle("selected");
  selectedMoats = btn.classList.contains("selected")
    ? [...new Set([...selectedMoats, type])]
    : selectedMoats.filter(m => m !== type);
  document.getElementById("thMoatType").value = selectedMoats.join(",");
}

async function loadThesisList() {
  const el = document.getElementById("thesisList");
  try {
    const theses = await api("/api/thesis");
    if (!theses.length) {
      el.innerHTML = `<div class="card text-center text-slate-500 py-10 text-sm">No theses yet.<br>Click "+ New Thesis" to start tracking your investment ideas.</div>`;
      return;
    }
    el.innerHTML = theses.map(t => {
      const mos = t.intrinsic_value && t.current_price
        ? (((t.intrinsic_value - t.current_price) / t.intrinsic_value) * 100).toFixed(0) : null;
      return `
        <div class="card cursor-pointer hover:border-emerald-600 transition-colors mb-3" onclick="viewThesisDetail(${t.id})">
          <div class="flex justify-between items-start">
            <div>
              <div class="font-bold text-white">${t.ticker}</div>
              <div class="text-slate-400 text-xs mt-0.5 truncate" style="max-width:160px">${t.title || ""}</div>
            </div>
            <div class="flex flex-col items-end gap-1">
              <span class="${t.status === 'active' ? 'pill-green' : 'pill-blue'}">${t.status}</span>
              ${mos !== null ? `<span class="${mos > 0 ? 'pill-green' : 'pill-red'}">${mos > 0 ? "+" : ""}${mos}% MoS</span>` : ""}
            </div>
          </div>
          <div class="text-slate-600 text-xs mt-2">${t.updated_date || t.created_date || ""}</div>
        </div>`;
    }).join("");
  } catch (e) {
    el.innerHTML = `<div class="card text-red-400 text-sm p-4">Error: ${e.message}</div>`;
  }
}

async function viewThesisDetail(id) {
  const [theses, reviews] = await Promise.all([api("/api/thesis"), api("/api/reviews")]);
  const t = theses.find(x => x.id === id);
  if (!t) return;
  const myReviews = reviews.filter(r => r.ticker === t.ticker);
  const mos = t.intrinsic_value && t.current_price
    ? (((t.intrinsic_value - t.current_price) / t.intrinsic_value) * 100).toFixed(1) : null;

  document.getElementById("thesisDetail").innerHTML = `
    <div class="space-y-4">
      <div class="card">
        <div class="flex justify-between items-start mb-3">
          <div><h2 class="text-xl font-bold text-white">${t.ticker}</h2><div class="text-slate-400 text-sm mt-1">${t.title || ""}</div></div>
          <div class="flex gap-2">
            <button onclick="openWeeklyReview(${t.id},'${t.ticker}')" class="btn-primary text-sm">+ Weekly Review</button>
            <button onclick="editThesis(${t.id})" class="btn-secondary text-sm">Edit</button>
            <button onclick="deleteThesis(${t.id})" class="btn-danger">Delete</button>
          </div>
        </div>
        <div class="grid grid-cols-4 gap-3 text-sm">
          <div class="metric-box text-center"><div class="text-slate-400 text-xs mb-1">Intrinsic Value</div><div class="text-emerald-400 font-bold text-lg">${t.intrinsic_value ? fmt(t.intrinsic_value) : "—"}</div></div>
          <div class="metric-box text-center"><div class="text-slate-400 text-xs mb-1">Entry Price</div><div class="text-white font-bold text-lg">${t.current_price ? fmt(t.current_price) : "—"}</div></div>
          <div class="metric-box text-center"><div class="text-slate-400 text-xs mb-1">Margin of Safety</div><div class="font-bold text-lg ${mos > 0 ? 'text-emerald-400' : 'text-red-400'}">${mos !== null ? mos + "%" : "—"}</div></div>
          <div class="metric-box text-center"><div class="text-slate-400 text-xs mb-1">Status</div><div class="${t.status === 'active' ? 'text-emerald-400' : 'text-slate-300'} font-bold capitalize">${t.status}</div></div>
        </div>
      </div>
      <div class="card"><h3 class="font-semibold text-white mb-2">Investment Case</h3><p class="text-slate-300 text-sm leading-relaxed whitespace-pre-line">${t.investment_case || "No investment case written yet."}</p></div>
      <div class="grid grid-cols-2 gap-4">
        <div class="card"><h3 class="font-semibold text-white mb-2">Moat</h3><div class="text-sm">${metricRow("Type", t.moat_type||"—")}${metricRow("Rating", t.moat_rating||"—")}</div></div>
        <div class="card"><h3 class="font-semibold text-white mb-2">Assumptions</h3><div class="text-sm">${metricRow("Revenue Growth",t.revenue_growth_assumption?t.revenue_growth_assumption+"%/yr":"—")}${metricRow("Margin",t.margin_assumption?t.margin_assumption+"%":"—")}${metricRow("WACC",t.wacc_assumption?t.wacc_assumption+"%":"—")}${metricRow("Terminal Growth",t.terminal_growth?t.terminal_growth+"%":"—")}</div></div>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div class="card"><h3 class="font-semibold text-emerald-400 mb-2">Buy Trigger</h3><p class="text-slate-300 text-sm whitespace-pre-line">${t.buy_trigger||"Not specified."}</p></div>
        <div class="card"><h3 class="font-semibold text-red-400 mb-2">Sell / Thesis Break</h3><p class="text-slate-300 text-sm whitespace-pre-line">${t.sell_trigger||"Not specified."}</p></div>
      </div>
      <div class="card"><h3 class="font-semibold text-amber-400 mb-2">Risk Factors</h3><p class="text-slate-300 text-sm whitespace-pre-line">${t.risk_factors||"No risks documented."}</p></div>
      <div class="card">
        <div class="flex items-center justify-between mb-3"><h3 class="font-semibold text-white">Weekly Reviews (${myReviews.length})</h3></div>
        ${myReviews.length === 0 ? `<div class="text-slate-500 text-sm text-center py-4">No reviews yet. Click "+ Weekly Review" to start tracking.</div>` :
          myReviews.map(r => `
            <div class="review-card ${r.thesis_intact ? 'intact' : 'broken'} bg-slate-900 rounded-lg p-3 mb-2 text-sm">
              <div class="flex justify-between items-center mb-1">
                <span class="font-semibold text-white">${r.review_date}</span>
                <div class="flex gap-2">
                  <span class="${r.action==='buy'?'pill-green':r.action==='sell'?'pill-red':'pill-blue'}">${r.action}</span>
                  <span class="text-amber-400">${"★".repeat(r.confidence||0)}</span>
                </div>
              </div>
              <div class="grid grid-cols-3 gap-2 text-xs text-slate-400 mb-1">
                <span>Price: <span class="text-white">${r.current_price?fmt(r.current_price):"—"}</span></span>
                <span>Target: <span class="text-white">${r.target_price?fmt(r.target_price):"—"}</span></span>
                <span>Thesis: <span class="${r.thesis_intact?'text-emerald-400':'text-red-400'}">${r.thesis_intact?"Intact ✓":"Broken ✗"}</span></span>
              </div>
              ${r.new_developments?`<div class="text-slate-300 text-xs"><strong>Dev:</strong> ${r.new_developments}</div>`:""}
              ${r.assumption_changes?`<div class="text-amber-400 text-xs mt-1"><strong>Assumption changes:</strong> ${r.assumption_changes}</div>`:""}
            </div>`).join("")}
      </div>
    </div>`;
}

function openThesisForm() {
  ["thTicker","thTitle","thCase","thBuyTrigger","thSellTrigger","thRisks","thIv","thCurrentPrice","thRevGrowth","thMargin","thWacc","thTg"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("thEditId").value = "";
  document.querySelectorAll(".moat-btn").forEach(b => b.classList.remove("selected"));
  selectedMoats = [];
  document.getElementById("thMoatType").value = "";
  document.getElementById("thesisFormModal").classList.remove("hidden");
}

function closeThesisForm() { document.getElementById("thesisFormModal").classList.add("hidden"); }

async function editThesis(id) {
  const theses = await api("/api/thesis");
  const t = theses.find(x => x.id === id);
  if (!t) return;
  document.getElementById("thTicker").value = t.ticker;
  document.getElementById("thTitle").value = t.title || "";
  document.getElementById("thCase").value = t.investment_case || "";
  document.getElementById("thBuyTrigger").value = t.buy_trigger || "";
  document.getElementById("thSellTrigger").value = t.sell_trigger || "";
  document.getElementById("thRisks").value = t.risk_factors || "";
  document.getElementById("thIv").value = t.intrinsic_value || "";
  document.getElementById("thCurrentPrice").value = t.current_price || "";
  document.getElementById("thRevGrowth").value = t.revenue_growth_assumption || "";
  document.getElementById("thMargin").value = t.margin_assumption || "";
  document.getElementById("thWacc").value = t.wacc_assumption || "";
  document.getElementById("thTg").value = t.terminal_growth || "";
  document.getElementById("thMoatRating").value = t.moat_rating || "narrow";
  document.getElementById("thStatus").value = t.status || "active";
  document.getElementById("thEditId").value = t.id;
  selectedMoats = (t.moat_type || "").split(",").filter(Boolean);
  document.getElementById("thMoatType").value = t.moat_type || "";
  document.querySelectorAll(".moat-btn").forEach(b => {
    const type = b.getAttribute("onclick")?.match(/'([^']+)'/)?.[1];
    b.classList.toggle("selected", selectedMoats.includes(type));
  });
  document.getElementById("thesisFormModal").classList.remove("hidden");
}

async function saveThesis() {
  const data = {
    ticker: document.getElementById("thTicker").value.trim().toUpperCase(),
    title: document.getElementById("thTitle").value,
    investment_case: document.getElementById("thCase").value,
    moat_type: document.getElementById("thMoatType").value || selectedMoats.join(","),
    moat_rating: document.getElementById("thMoatRating").value,
    revenue_growth_assumption: parseFloat(document.getElementById("thRevGrowth").value) || null,
    margin_assumption: parseFloat(document.getElementById("thMargin").value) || null,
    wacc_assumption: parseFloat(document.getElementById("thWacc").value) || null,
    terminal_growth: parseFloat(document.getElementById("thTg").value) || null,
    intrinsic_value: parseFloat(document.getElementById("thIv").value) || null,
    current_price: parseFloat(document.getElementById("thCurrentPrice").value) || null,
    buy_trigger: document.getElementById("thBuyTrigger").value,
    sell_trigger: document.getElementById("thSellTrigger").value,
    risk_factors: document.getElementById("thRisks").value,
    status: document.getElementById("thStatus").value,
  };
  if (!data.ticker) { alert("Ticker is required."); return; }
  await api("/api/thesis", "POST", data);
  closeThesisForm();
  loadThesisList();
}

async function deleteThesis(id) {
  if (!confirm("Delete this thesis? This cannot be undone.")) return;
  await api("/api/thesis/" + id, "DELETE");
  loadThesisList();
  document.getElementById("thesisDetail").innerHTML = `<div class="card text-center py-16 text-slate-500"><div class="text-4xl mb-3">📝</div><div>Select a thesis to view</div></div>`;
}

// ── Weekly Review ────────────────────────────────────────────────
function openWeeklyReview(thesisId, ticker) {
  document.getElementById("reviewTicker").textContent = ticker;
  document.getElementById("rvThesisId").value = thesisId;
  document.getElementById("rvTickerHidden").value = ticker;
  document.getElementById("rvDate").value = new Date().toISOString().slice(0,10);
  ["rvPrice","rvTarget","rvDevelopments","rvAssumptions","rvNotes"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("rvThesisIntact").checked = true;
  document.getElementById("rvRevOnTrack").checked = true;
  document.getElementById("rvMarginOnTrack").checked = true;
  setConf(3);
  document.querySelectorAll("[data-action]").forEach(b => b.classList.toggle("selected", b.dataset.action === "hold"));
  document.getElementById("rvAction").value = "hold";
  document.getElementById("reviewModal").classList.remove("hidden");
}

function closeReview() { document.getElementById("reviewModal").classList.add("hidden"); }

function setAction(btn, action) {
  document.querySelectorAll("[data-action]").forEach(b => b.classList.remove("selected"));
  btn.classList.add("selected");
  document.getElementById("rvAction").value = action;
}

function setConf(level) {
  document.getElementById("rvConf").value = level;
  document.querySelectorAll(".confidence-star").forEach((s, i) => s.classList.toggle("lit", i < level));
}

async function saveReview() {
  const data = {
    thesis_id: parseInt(document.getElementById("rvThesisId").value) || null,
    ticker: document.getElementById("rvTickerHidden").value,
    review_date: document.getElementById("rvDate").value,
    current_price: parseFloat(document.getElementById("rvPrice").value) || null,
    target_price: parseFloat(document.getElementById("rvTarget").value) || null,
    thesis_intact: document.getElementById("rvThesisIntact").checked ? 1 : 0,
    revenue_on_track: document.getElementById("rvRevOnTrack").checked ? 1 : 0,
    margin_on_track: document.getElementById("rvMarginOnTrack").checked ? 1 : 0,
    new_developments: document.getElementById("rvDevelopments").value,
    assumption_changes: document.getElementById("rvAssumptions").value,
    action: document.getElementById("rvAction").value,
    confidence: parseInt(document.getElementById("rvConf").value),
    notes: document.getElementById("rvNotes").value,
  };
  await api("/api/reviews", "POST", data);
  closeReview();
  const tid = parseInt(document.getElementById("rvThesisId").value);
  if (tid) viewThesisDetail(tid);
}

// ═══════════════════════════════════════════════════════════════
// PORTFOLIO
// ═══════════════════════════════════════════════════════════════
async function loadPortfolio() {
  document.getElementById("portfolioTable").innerHTML = `<div class="flex justify-center py-10"><div class="loader"></div></div>`;
  try {
    const positions = await api("/api/portfolio");
    const open = positions.filter(p => p.status === "open");
    const totalCost = open.reduce((s,p) => s+(p.cost_basis||0), 0);
    const totalVal  = open.reduce((s,p) => s+(p.current_value||0), 0);
    const gainLoss  = totalVal - totalCost;
    const pct = totalCost ? (gainLoss/totalCost*100).toFixed(1) : 0;

    document.getElementById("portfolioSummary").innerHTML = `
      <div class="card text-center"><div class="text-slate-400 text-xs mb-1">Positions</div><div class="text-2xl font-bold text-white">${open.length}</div></div>
      <div class="card text-center"><div class="text-slate-400 text-xs mb-1">Total Cost</div><div class="text-2xl font-bold text-white">${fmtBig(totalCost)}</div></div>
      <div class="card text-center"><div class="text-slate-400 text-xs mb-1">Market Value</div><div class="text-2xl font-bold text-white">${fmtBig(totalVal)}</div></div>
      <div class="card text-center"><div class="text-slate-400 text-xs mb-1">Unrealised P&L</div><div class="text-2xl font-bold ${gainLoss>=0?'text-emerald-400':'text-red-400'}">${gainLoss>=0?"+":""}${fmtBig(gainLoss)} <span class="text-sm">(${pct>=0?"+":""}${pct}%)</span></div></div>`;

    if (!positions.length) {
      document.getElementById("portfolioTable").innerHTML = `<div class="text-center py-12 text-slate-500">No positions yet. Click "+ Add Position".</div>`;
      return;
    }
    document.getElementById("portfolioTable").innerHTML = `
      <h2 class="font-semibold text-white mb-4">Open Positions</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead><tr class="text-slate-400 text-xs border-b border-slate-700">
            <th class="text-left pb-3">Ticker</th><th class="text-right pb-3">Entry</th><th class="text-right pb-3">Current</th>
            <th class="text-right pb-3">Shares</th><th class="text-right pb-3">Cost</th><th class="text-right pb-3">Value</th>
            <th class="text-right pb-3">P&L</th><th class="text-right pb-3">Target</th><th class="text-right pb-3">vs Target</th><th class="text-right pb-3"></th>
          </tr></thead>
          <tbody>
            ${open.map(p => `
              <tr class="table-row border-b border-slate-800">
                <td class="py-3"><div class="font-bold text-white">${p.ticker}</div><div class="text-slate-500 text-xs">${p.entry_date||""}</div></td>
                <td class="py-3 text-right">${fmt(p.entry_price)}</td>
                <td class="py-3 text-right">${p.current_price?fmt(p.current_price):"—"}</td>
                <td class="py-3 text-right">${p.shares}</td>
                <td class="py-3 text-right">${fmtBig(p.cost_basis)}</td>
                <td class="py-3 text-right">${fmtBig(p.current_value)}</td>
                <td class="py-3 text-right">
                  <div class="${(p.gain_loss||0)>=0?'text-emerald-400':'text-red-400'} font-semibold">${(p.gain_loss||0)>=0?"+":""}${fmtBig(p.gain_loss)}</div>
                  <div class="${(p.gain_loss_pct||0)>=0?'text-emerald-400':'text-red-400'} text-xs">${(p.gain_loss_pct||0)>=0?"+":""}${fmt(p.gain_loss_pct,1)}%</div>
                </td>
                <td class="py-3 text-right text-amber-400">${fmt(p.target_price)}</td>
                <td class="py-3 text-right ${(p.vs_target||0)>0?'text-emerald-400':'text-slate-400'}">${p.vs_target!==undefined?(p.vs_target>0?"+":"")+p.vs_target+"%":"—"}</td>
                <td class="py-3 text-right">
                  <div class="flex gap-2 justify-end">
                    <button onclick="openResearch('${p.ticker}')" class="text-blue-400 text-xs">Research</button>
                    <button onclick="deletePosition(${p.id})" class="text-red-400 text-xs">✕</button>
                  </div>
                </td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
  } catch (e) {
    document.getElementById("portfolioTable").innerHTML = `<div class="text-red-400 text-sm p-4">Error loading portfolio: ${e.message}</div>`;
  }
}

function openPortfolioForm() {
  document.getElementById("pfDate").value = new Date().toISOString().slice(0,10);
  document.getElementById("portfolioFormModal").classList.remove("hidden");
}

function closePortfolioForm() { document.getElementById("portfolioFormModal").classList.add("hidden"); }

async function savePosition() {
  const data = {
    ticker: document.getElementById("pfTicker").value.trim().toUpperCase(),
    name: document.getElementById("pfName").value,
    entry_price: parseFloat(document.getElementById("pfEntry").value),
    shares: parseFloat(document.getElementById("pfShares").value),
    entry_date: document.getElementById("pfDate").value,
    target_price: parseFloat(document.getElementById("pfTarget").value)||0,
    stop_loss: parseFloat(document.getElementById("pfStop").value)||0,
    notes: document.getElementById("pfNotes").value,
  };
  if (!data.ticker || !data.entry_price || !data.shares) { alert("Ticker, entry price and shares are required."); return; }
  await api("/api/portfolio", "POST", data);
  closePortfolioForm();
  loadPortfolio();
}

async function deletePosition(id) {
  if (!confirm("Remove this position?")) return;
  await api("/api/portfolio/" + id, "DELETE");
  loadPortfolio();
}

// ═══════════════════════════════════════════════════════════════
// EXIT MULTIPLE VALUATION MODEL
// ═══════════════════════════════════════════════════════════════
let _emMetrics = null; // cached metrics-history payload

async function loadExitMultipleModel() {
  const ticker = (document.getElementById("valTicker")?.value || "").trim().toUpperCase();
  if (!ticker) return;

  const btn = document.getElementById("emLoadBtn");
  if (btn) btn.textContent = "Loading…";

  try {
    _emMetrics = await api(`/api/stock/${ticker}/metrics-history`);
    const m = _emMetrics;

    // Render historical anchors table
    const fmtPct = (v, suffix="%") => v != null ? `${v.toFixed(1)}${suffix}` : "—";
    const row = (label, k) => {
      const v1 = m[k]?.["1y"], v5 = m[k]?.["5y"];
      return `<div class="grid grid-cols-3 items-center py-1.5 border-b border-slate-800">
        <span class="text-slate-400">${label}</span>
        <span class="text-center font-medium ${colorPct(v1)}">${fmtPct(v1)}</span>
        <span class="text-center font-medium ${colorPct(v5)}">${fmtPct(v5)}</span>
      </div>`;
    };
    const colorPct = v => v==null?"text-slate-500":v>=15?"text-emerald-400":v>=5?"text-yellow-400":"text-red-400";

    document.getElementById("emAnchors").innerHTML = `
      <div class="grid grid-cols-3 items-center pb-1.5 border-b border-slate-700 text-xs font-semibold text-slate-400">
        <span>Metric</span><span class="text-center">1Y (TTM)</span><span class="text-center">5Y Avg</span>
      </div>
      ${row("Revenue Growth", "rev_growth")}
      ${row("Net Margin", "net_margin")}
      ${row("FCF Margin", "fcf_margin")}
      ${row("Op Margin", "op_margin")}
      ${row("ROIC", "roic")}
    `;

    // Current metrics strip
    const revTtm = m.revenue_ttm ? (m.revenue_ttm >= 1e9 ? `$${(m.revenue_ttm/1e9).toFixed(1)}B` : `$${(m.revenue_ttm/1e6).toFixed(0)}M`) : "—";
    document.getElementById("emCurPrice").textContent = m.current_price ? `$${m.current_price.toFixed(2)}` : "—";
    document.getElementById("emEpsTtm").textContent   = m.eps_ttm ? `$${m.eps_ttm.toFixed(2)}` : "—";
    document.getElementById("emEpsFwd").textContent   = m.eps_forward ? `$${m.eps_forward.toFixed(2)}` : "—";
    document.getElementById("emPe").textContent       = m.pe_ratio ? `${m.pe_ratio.toFixed(1)}x` : "—";
    document.getElementById("emFwdPe").textContent    = m.forward_pe ? `${m.forward_pe.toFixed(1)}x` : "—";
    document.getElementById("emRevTtm").textContent   = revTtm;
    document.getElementById("emCurrentMetrics").classList.remove("hidden");

    // Auto-suggest exit multiples from historical P/E
    const pe = m.pe_ratio;
    if (pe && !document.getElementById("emPeBase").value) {
      document.getElementById("emPeBase").value = pe.toFixed(0);
      document.getElementById("emPeBear").value = Math.max(8, (pe * 0.7).toFixed(0));
      document.getElementById("emPeBull").value = (pe * 1.3).toFixed(0);
    }
    // Suggest FCF multiples (typically premium to P/E for asset-light cos)
    if (!document.getElementById("emFcfBase").value) {
      const base = pe ? Math.round(pe * 1.1) : 25;
      document.getElementById("emFcfBase").value = base;
      document.getElementById("emFcfBear").value = Math.max(10, Math.round(base * 0.7));
      document.getElementById("emFcfBull").value = Math.round(base * 1.35);
    }
    // Auto-fill EPS growth from 5Y rev growth as a proxy if blank
    if (!document.getElementById("emEpsGrowth").value && m.rev_growth?.["5y"] != null) {
      document.getElementById("emEpsGrowth").value = Math.max(0, m.rev_growth["5y"]).toFixed(0);
    }
    if (!document.getElementById("emFcfGrowth").value && m.fcf_margin?.["5y"] != null) {
      document.getElementById("emFcfGrowth").value = Math.max(0, (m.rev_growth?.["5y"] || 10) * 0.85).toFixed(0);
    }
    // FCF per share: derive from FCF margin and revenue
    if (!document.getElementById("emFcfPerShare").value && m.fcf_margin?.["1y"] != null && m.revenue_ttm && m.shares) {
      const fcfPerShare = (m.fcf_margin["1y"] / 100) * m.revenue_ttm / m.shares;
      if (fcfPerShare > 0) document.getElementById("emFcfPerShare").value = fcfPerShare.toFixed(2);
    }
  } catch(e) {
    document.getElementById("emAnchors").innerHTML = `<div class="text-red-400 text-xs">Failed to load data for ${ticker}</div>`;
  } finally {
    if (btn) btn.textContent = "↺ Refresh";
  }
}

function calcExitMultiple() {
  const ticker  = (document.getElementById("valTicker")?.value || "").trim().toUpperCase();
  const price   = _emMetrics?.current_price || 0;
  const years   = parseInt(document.getElementById("emYears").value) || 3;
  const epsNow  = _emMetrics?.eps_ttm || 0;
  const epsGrowth = parseFloat(document.getElementById("emEpsGrowth").value) / 100 || 0;
  const fcfNow  = parseFloat(document.getElementById("emFcfPerShare").value) || 0;
  const fcfGrowth = parseFloat(document.getElementById("emFcfGrowth").value) / 100 || 0;
  const reqRet  = parseFloat(document.getElementById("emRequiredReturn").value) / 100 || 0.10;

  const peBear = parseFloat(document.getElementById("emPeBear").value) || 0;
  const peBase = parseFloat(document.getElementById("emPeBase").value) || 0;
  const peBull = parseFloat(document.getElementById("emPeBull").value) || 0;
  const fcfBear = parseFloat(document.getElementById("emFcfBear").value) || 0;
  const fcfBase = parseFloat(document.getElementById("emFcfBase").value) || 0;
  const fcfBull = parseFloat(document.getElementById("emFcfBull").value) || 0;

  if (!epsNow && !fcfNow) { alert("No EPS or FCF/share available — please load a ticker first."); return; }

  // Future EPS and FCF per share
  const epsF = v => epsNow * Math.pow(1 + epsGrowth, years);
  const fcfF = v => fcfNow * Math.pow(1 + fcfGrowth, years);

  // Implied price = future metric × exit multiple
  // Annual return = (impliedPrice / currentPrice)^(1/years) - 1
  const annReturn = (impliedPrice) => price > 0 ? ((Math.pow(impliedPrice / price, 1/years) - 1) * 100).toFixed(1) : null;
  const reqBadge  = (ret) => {
    const r = parseFloat(ret);
    return r >= reqRet*100*1.2 ? "text-emerald-400 font-bold" : r >= reqRet*100 ? "text-yellow-400" : "text-red-400";
  };

  const scenarioCard = (label, impliedPrice, colorClass) => {
    if (!impliedPrice) return "";
    const ret = annReturn(impliedPrice);
    const retClass = ret != null ? reqBadge(ret) : "text-slate-400";
    const mos = price > 0 ? (((impliedPrice - price) / price) * 100).toFixed(1) : null;
    return `<div class="flex items-center justify-between bg-slate-800 rounded px-3 py-2">
      <span class="${colorClass} text-sm font-semibold w-14">${label}</span>
      <span class="text-white font-bold text-base">$${impliedPrice.toFixed(2)}</span>
      <span class="${retClass} text-sm">${ret != null ? (ret>0?"+":"")+ret+"% /yr" : "—"}</span>
      <span class="text-slate-400 text-xs">${mos != null ? (mos>0?"+":"")+mos+"% MoS" : ""}</span>
    </div>`;
  };

  const epsFuture = epsF();
  const fcfFuture = fcfF();

  const peHtml = [
    scenarioCard("Bear 🐻", peBear ? epsFuture * peBear : null, "text-red-400"),
    scenarioCard("Base ⚖️", peBase ? epsFuture * peBase : null, "text-slate-200"),
    scenarioCard("Bull 🐂", peBull ? epsFuture * peBull : null, "text-emerald-400"),
  ].join("");

  const fcfHtml = [
    scenarioCard("Bear 🐻", fcfBear ? fcfFuture * fcfBear : null, "text-red-400"),
    scenarioCard("Base ⚖️", fcfBase ? fcfFuture * fcfBase : null, "text-slate-200"),
    scenarioCard("Bull 🐂", fcfBull ? fcfFuture * fcfBull : null, "text-emerald-400"),
  ].join("");

  document.getElementById("emPeResults").innerHTML  = peHtml  || `<div class="text-slate-500 text-sm">Enter P/E exit multiples above.</div>`;
  document.getElementById("emFcfResults").innerHTML = fcfHtml || `<div class="text-slate-500 text-sm">Enter P/FCF exit multiples and FCF/share above.</div>`;

  document.getElementById("emResultNote").innerHTML = price
    ? `${ticker || "Stock"} @ $${price.toFixed(2)} today &nbsp;·&nbsp; ${years}-yr holding &nbsp;·&nbsp; EPS ${epsNow.toFixed(2)} × ${(epsGrowth*100).toFixed(0)}%/yr → $${epsFuture.toFixed(2)} &nbsp;·&nbsp; Required return: ${(reqRet*100).toFixed(0)}%/yr`
    : "";

  document.getElementById("emResults").classList.remove("hidden");
}

// ─── Init ────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  loadDashboard();
});
