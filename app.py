"""ValueInvestor v2: Screen, Watchlist and Book on top of the v2_ tables in Neon.

Local:  set DATABASE_URL=...   then   python app.py   ->  http://127.0.0.1:5000
Vercel: needs DATABASE_URL and (recommended) APP_PASSWORD in the project's environment variables.
"""
import os, math, json, datetime as dt
from functools import wraps

import psycopg2, psycopg2.extras
from flask import Flask, request, redirect, url_for, render_template_string, Response, abort, flash
from markupsafe import Markup

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "vi-v2-local")
DB_URL = os.environ.get("DATABASE_URL", "")
WATCH_CAP, CORE_CAP, BOOK_TARGET = 50, 20, 20

SECTIONS = [
    ("Industry outlook", [
        ("industry_map", "Map"), ("size_growth", "Size and growth"), ("structure", "Structure"),
        ("demand_supply", "Demand and supply"), ("industry_risks", "Risk map"),
        ("outlook", "Outlook by horizon"), ("winners", "What winners look like")]),
    ("Company", [
        ("business", "Business"), ("right_to_win", "Right to win"), ("moat", "Moat"),
        ("tam_competitors", "TAM and competitors"), ("management", "Management fit"),
        ("financials", "Financial quality"), ("company_risks", "Company risks")]),
    ("Decision", [
        ("variant_view", "Variant view"), ("valuation", "Valuation"),
        ("pillars", "Pillars and kill criteria"), ("catalysts_flow", "Catalysts and flow map"),
        ("premortem", "Pre-mortem and conviction")]),
]
FUNNELS = [("quant_value", "Quant value"), ("fallen_quality", "Fallen quality"), ("sharp_drop", "Sharp drop"),
           ("fast_growth", "Fast growth"), ("accelerating", "Accelerating"), ("smart_money", "Smart money"),
           ("share_taker", "Share taker"), ("momentum_leader", "Momentum leader"), ("estimates_rising", "Estimates rising"),
           ("insider_buying", "Insider buying"), ("activist", "Activist 13D")]
VALUE_FUNNELS = ["quant_value", "fallen_quality", "sharp_drop", "insider_buying", "activist"]
GROWTH_FUNNELS = ["fast_growth", "accelerating", "smart_money", "share_taker", "momentum_leader", "estimates_rising"]
TAG_CLASS = {"strengthens": "good", "weakens": "warn", "breaks": "bad", "no change": "muted"}


# ------------------------------------------------------------------ database
def db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def q(sql, params=()):
    with db() as conn, conn.cursor() as c:
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql, params=()):
    with db() as conn, conn.cursor() as c:
        c.execute(sql, params)


_schema_ok = False


@app.before_request
def ensure_schema():
    """Small additive columns the app needs; safe to run repeatedly."""
    global _schema_ok
    if _schema_ok or not DB_URL:
        return
    x("""ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS level_since DATE;
         ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS health TEXT;
         ALTER TABLE v2_alerts ADD COLUMN IF NOT EXISTS note TEXT;
         ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS style TEXT;
         CREATE TABLE IF NOT EXISTS v2_settings (key TEXT PRIMARY KEY, value JSONB, updated_at TIMESTAMPTZ DEFAULT now());
         CREATE TABLE IF NOT EXISTS v2_calendar (id SERIAL PRIMARY KEY, date DATE NOT NULL, time_et TEXT, kind TEXT, ticker TEXT,
             title TEXT NOT NULL, detail JSONB, source TEXT, UNIQUE (date, kind, title));
         CREATE TABLE IF NOT EXISTS v2_decisions (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, decided_at TIMESTAMPTZ DEFAULT now(),
             action TEXT, decision TEXT, price NUMERIC, shares NUMERIC, note TEXT, alert_id INT, spy_price NUMERIC);
         ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS growth_score NUMERIC;""")
    _schema_ok = True


# ------------------------------------------------------------------ password
@app.before_request
def password_gate():
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        return
    a = request.authorization
    if not a or a.password != pw:
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="ValueInvestor"'})


# ------------------------------------------------------------------ lookups
def latest_prices(tickers):
    if not tickers:
        return {}
    out = {r["ticker"]: float(r["close"]) for r in q(
        "SELECT DISTINCT ON (ticker) ticker, close FROM v2_prices WHERE ticker = ANY(%s) "
        "ORDER BY ticker, date DESC", (tickers,))}
    missing = [t for t in tickers if t not in out]
    if missing:
        for r in q("SELECT DISTINCT ON (ticker) ticker, metrics->'px'->>'price' AS p FROM v2_screen_scores "
                   "WHERE ticker = ANY(%s) ORDER BY ticker, run_date DESC", (missing,)):
            if r["p"]:
                out[r["ticker"]] = float(r["p"])
    return out


def latest_by(table, order, tickers):
    if not tickers:
        return {}
    return {r["ticker"]: r for r in q(
        f"SELECT DISTINCT ON (ticker) * FROM {table} WHERE ticker = ANY(%s) ORDER BY ticker, {order}", (tickers,))}


def open_alerts(tickers):
    if not tickers:
        return {}
    return {r["ticker"]: r["n"] for r in q(
        "SELECT ticker, COUNT(*) AS n FROM v2_alerts WHERE NOT addressed AND ticker = ANY(%s) GROUP BY ticker",
        (tickers,))}


def dcf_iv(fcf_ps, g1, g2, tg, dr):
    """10-year two-stage DCF per share. Rates in percent."""
    g1, g2, tg, dr = g1 / 100, g2 / 100, tg / 100, dr / 100
    if dr <= tg:
        return None
    pv, f = 0.0, fcf_ps
    for yr in range(1, 11):
        f *= 1 + (g1 if yr <= 5 else g2)
        pv += f / (1 + dr) ** yr
    return pv + f * (1 + tg) / (dr - tg) / (1 + dr) ** 10


def tam_calc(tam, price):
    """TAM target values (reference only; never feeds the entry target or signals).
    Each scenario: segments [{name, tam_bn, share_pct, net_margin_pct}], other_rev_bn, exit_pe, shares_m, dr, years."""
    out = []
    for sc in (tam or {}).get("scenarios") or []:
        try:
            segs = sc.get("segments") or []
            rows = [{**g, "rev_bn": float(g["tam_bn"]) * float(g["share_pct"]) / 100} for g in segs]
            for g in rows:
                g["ni_bn"] = g["rev_bn"] * float(g["net_margin_pct"]) / 100
            other = float(sc.get("other_rev_bn") or 0)
            other_ni = other * float(sc.get("other_margin_pct") or 0) / 100
            rev, ni = sum(g["rev_bn"] for g in rows) + other, sum(g["ni_bn"] for g in rows) + other_ni
            yrs, dr, pe, sh = int(sc["years"]), float(sc["dr"]), float(sc["exit_pe"]), float(sc["shares_m"])
            future = ni * pe * 1000 / sh
            today = future / (1 + dr / 100) ** yrs
            irr = ((future / price) ** (1 / yrs) - 1) if price and future > 0 else None
            out.append({**sc, "rows": rows, "rev_bn": rev, "ni_bn": ni, "future": future, "today": today, "irr": irr,
                        "zone": future / 1.15 ** yrs,
                        "vs_price": (today / price - 1) if price else None})
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return out


def num(v):
    try:
        return float(v)
    except Exception:          # None, text, or a missing template value
        return None


# ------------------------------------------------------------------ template filters
@app.template_filter("money")
def money(v):
    v = num(v)
    return "—" if v is None else (f"${v:,.0f}" if abs(v) >= 1000 else f"${v:,.2f}")


@app.template_filter("pct")
def pct(v, signed=False):
    v = num(v)
    if v is None:
        return "—"
    return f"{v:+.0%}" if signed else f"{v:.0%}"


@app.template_filter("d")
def fmt_date(v):
    if not v:
        return "—"
    if isinstance(v, str):
        v = dt.date.fromisoformat(v[:10])
    return v.strftime("%d %b %Y")


@app.template_filter("n1")
def n1(v):
    v = num(v)
    return "—" if v is None else f"{v:.0f}"


# ------------------------------------------------------------------ layout
LAYOUT = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · ValueInvestor</title>
<style>
:root{--bg:#0e1116;--panel:#161b22;--panel2:#1c222b;--line:#2a313c;--ink:#e6edf3;--mute:#8b949e;
--accent:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149;--chip:#21262d;--r:10px}
@media (prefers-color-scheme: light){:root:not([data-theme=dark]){--bg:#f6f8fa;--panel:#fff;--panel2:#f3f5f8;
--line:#d0d7de;--ink:#1f2328;--mute:#59636e;--accent:#0969da;--good:#1a7f37;--warn:#9a6700;--bad:#cf222e;--chip:#eef1f4}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{max-width:1200px;margin:0 auto;padding:10px 16px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.2px}.tabs{display:flex;gap:4px}
.tabs a{padding:6px 12px;border-radius:8px;color:var(--mute);font-weight:600}
.tabs a.on{background:var(--panel2);color:var(--ink)}.tabs a:hover{text-decoration:none;color:var(--ink)}
.count{font-size:12px;color:var(--mute);font-weight:500;margin-left:4px}
main{max-width:1200px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}h3{font-size:14px;margin:0 0 6px}
.sub{color:var(--mute);margin:0 0 18px}
.grid{display:grid;gap:12px}.g4{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px}
.tile .v{font-size:24px;font-weight:700}.tile .k{color:var(--mute);font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;background:var(--panel2);white-space:nowrap}
tr:last-child td{border-bottom:none}td.r,th.r{text-align:right}.tablewrap{overflow-x:auto}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px;color:var(--mute);margin:1px 2px 1px 0;white-space:nowrap}
.chip.good{color:var(--good)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}.chip.acc{color:var(--accent)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mute);margin-right:6px}
.dot.green{background:var(--good)}.dot.amber{background:var(--warn)}.dot.red{background:var(--bad)}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--mute)}
button,.btn{font:inherit;font-size:13px;padding:5px 11px;border-radius:8px;border:1px solid var(--line);
background:var(--panel2);color:var(--ink);cursor:pointer}button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
input,select,textarea{font:inherit;font-size:13px;padding:6px 8px;border-radius:8px;border:1px solid var(--line);
background:var(--bg);color:var(--ink)}textarea{width:100%;min-height:60px}
form.inline{display:inline}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.flash{background:var(--panel2);border:1px solid var(--warn);color:var(--ink);padding:10px 12px;border-radius:8px;margin-bottom:14px}
.matrix td.c{text-align:center}.hit{display:inline-block;width:14px;height:14px;border-radius:4px;background:var(--accent)}
.miss{display:inline-block;width:14px;height:14px;border-radius:4px;border:1px solid var(--line)}
.sec{border-top:1px solid var(--line);padding:12px 0}.sec:first-of-type{border-top:none}
.sec .body{white-space:pre-wrap}.empty{color:var(--mute);font-style:italic}
details summary{cursor:pointer;color:var(--mute)}.cmt{border-left:2px solid var(--line);padding:2px 10px;margin:6px 0;color:var(--mute)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px}.kv .k{color:var(--mute)}
.search{width:220px}
</style></head><body>
<header><div class="bar"><span class="brand">ValueInvestor</span>
<nav class="tabs">
<a href="{{ url_for('decide') }}" class="{{ 'on' if tab=='decide' }}">Decide</a>
<a href="{{ url_for('screen') }}" class="{{ 'on' if tab=='screen' }}">Screen</a>
<a href="{{ url_for('watchlist') }}" class="{{ 'on' if tab=='watchlist' }}">Watchlist</a>
<a href="{{ url_for('book') }}" class="{{ 'on' if tab=='book' }}">Book</a>
<a href="{{ url_for('outlook') }}" class="{{ 'on' if tab=='outlook' }}">Outlook</a></nav></div></header>
<main>{% with m = get_flashed_messages() %}{% for x in m %}<div class="flash">{{ x }}</div>{% endfor %}{% endwith %}
{{ body }}</main>
<script>
document.querySelectorAll('[data-filter]').forEach(function(inp){inp.addEventListener('input',function(){
var t=inp.value.toLowerCase();document.querySelectorAll(inp.dataset.filter+' tbody tr').forEach(function(tr){
tr.style.display=tr.textContent.toLowerCase().indexOf(t)>-1?'':'none'})})});
</script></body></html>"""


def page(tab, title, tpl, **ctx):
    body = render_template_string(tpl, **ctx)
    return render_template_string(LAYOUT, tab=tab, title=title, body=Markup(body))


def funnel_chips(f):
    f = f or {}
    if isinstance(f, str):
        f = json.loads(f)
    return Markup("".join(f'<span class="chip acc">{label}</span>' for k, label in FUNNELS if f.get(k)))


app.jinja_env.globals.update(funnel_chips=funnel_chips, FUNNELS=FUNNELS, TAG_CLASS=TAG_CLASS,
                             VALUE_FUNNELS=VALUE_FUNNELS, GROWTH_FUNNELS=GROWTH_FUNNELS)


# ------------------------------------------------------------------ Screen
SCREEN_TPL = r"""
<style>
.mkt{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.mkt .v{font-size:19px}
.up{color:var(--good)}.dn{color:var(--bad)}
.gauge{height:10px;border-radius:999px;background:linear-gradient(90deg,var(--good),#9be9a8 20%,var(--warn) 50%,#f0883e 80%,var(--bad));position:relative;margin:10px 0 4px}
.gauge i{position:absolute;top:-4px;width:3px;height:18px;background:var(--ink);border-radius:2px}
.fbar{display:grid;grid-template-columns:150px 1fr 60px;gap:8px;align-items:center;margin:4px 0;font-size:13px}
.fbar .b{height:16px;border-radius:4px;background:var(--accent);opacity:.85;min-width:2px}.fbar .n{text-align:right;font-weight:600}
.top{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(360px,1fr))}
.top .card h3{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px}
.mini{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0}
.mini div{background:var(--panel2);border-radius:8px;padding:6px 8px}.mini b{display:block;font-size:14px}.mini span{font-size:11px;color:var(--mute)}
.rc{font-size:13px;border-top:1px solid var(--line);padding-top:8px;margin-top:8px}.rc div{margin:2px 0}
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;align-items:end}
.filters label{font-size:12px;color:var(--mute);display:block;margin-bottom:3px}.filters input,.filters select{width:100%}
.filters label.cb{display:flex;gap:6px;align-items:center;color:var(--ink);font-size:13px}.filters label.cb input{width:auto}
.presets button{margin:0 6px 6px 0}.presets button.on{border-color:var(--accent);color:var(--accent)}
#res{min-width:2300px}#res th{cursor:pointer;user-select:none}#res th.sorted{color:var(--accent)}
#res td{white-space:nowrap;padding:6px 8px;font-size:13px}#res tbody tr:hover{background:var(--panel2)}
#res td.name{max-width:170px;overflow:hidden;text-overflow:ellipsis}
.sticky{position:sticky;left:0;background:var(--panel);z-index:1}
.pager button{min-width:34px}.pager button.on{border-color:var(--accent);color:var(--accent)}
.bulk{position:sticky;bottom:12px;z-index:4;display:none;background:var(--panel2);border:1px solid var(--accent);border-radius:10px;padding:10px 14px;margin-top:10px}
</style>

<h1>Screen</h1>
<div class="row" style="margin:-4px 0 10px"><a class="btn {{ 'primary' }}" href="{{ url_for('screen') }}">Value</a><a class="btn" href="{{ url_for('screen', track='growth') }}">Growth</a></div>
<p class="sub">{% if run %}Weekly screen of {{ run.date|d }} · {{ st.with_fundamentals or run.funnel_counts.scored }} US stocks scored · data: SEC filings, Nasdaq, Yahoo, FRED (all free){% else %}No screen has run yet. The Saturday run fills this page.{% endif %}</p>

{% if run %}
{% if mkt.indices or mkt.buffett %}
<h2>Market context</h2>
<div class="grid mkt">
{% for i in mkt.indices %}<div class="card tile"><div class="k">{{ i.label }}</div>
<div class="v">{% if i.symbol == '^TNX' %}{{ '%.2f'|format(i.last) }}%{% else %}{{ '{:,.0f}'.format(i.last) if i.last > 100 else '%.2f'|format(i.last) }}{% endif %}</div>
<div class="k"><span class="{{ 'up' if i.chg_1w >= 0 else 'dn' }}">{{ i.chg_1w|pct(true) }} 1w</span>{% if i.chg_ytd is not none %} · <span class="{{ 'up' if i.chg_ytd >= 0 else 'dn' }}">{{ i.chg_ytd|pct(true) }} YTD</span>{% endif %}</div>
{{ spark(i.spark) }}</div>{% endfor %}
{% if mkt.buffett %}{% set b = mkt.buffett %}<div class="card tile" style="grid-column:span 2">
<div class="k">Buffett indicator (US equities / GDP)</div>
<div class="v">{{ (b.ratio_est*100)|round|int }}% <span style="font-size:13px;font-weight:600" class="{{ 'up' if b.percentile < 0.5 else 'dn' }}">{{ b.zone }}</span></div>
<div class="gauge"><i style="left:calc({{ (b.percentile*100)|round(1) }}% - 1px)"></i></div>
<div class="k">{{ (b.percentile*100)|round|int }}th percentile since 1970 · median {{ (b.median*100)|round|int }}% · last quarter {{ b.quarter|d }} rolled forward with the S&amp;P · zones {{ b.zones_status }}</div></div>
{% else %}<div class="card tile"><div class="k">Buffett indicator</div><div class="empty">FRED unavailable this run</div></div>{% endif %}
</div>{% endif %}

<div class="grid g2" style="margin-top:4px">
<div><h2>This week's funnel</h2><div class="card">
{% set top = st.listings or 1 %}
{% for k,label in [('listings','All Nasdaq/NYSE listings'),('us_2b','US, market cap ≥ $2B'),('priced','With 1y price history'),('liquid','Trades ≥ $10M a day'),('with_fundamentals','With SEC financials'),('gate_pass','Passed balance-sheet gates'),('any_funnel','Hit at least one funnel'),('top10','Top 10'),('cleared_bar','Cleared deep-dive bar')] %}
{% if st.get(k) is not none %}<div class="fbar"><span>{{ label }}</span><div><div class="b" style="width:{{ (100*st[k]/top)|round(1) }}%"></div></div><span class="n">{{ '{:,}'.format(st[k]) }}</span></div>{% endif %}{% endfor %}
<div class="row" style="margin-top:10px">{% for k,label in FUNNELS if k in VALUE_FUNNELS %}{% if run.funnel_counts.get(k) is not none %}<span class="chip acc">{{ label }} {{ run.funnel_counts[k] }}</span>{% endif %}{% endfor %}</div>
</div></div>
<div><h2>Where the value is, by sector</h2><div class="tablewrap"><table><thead><tr><th>Sector</th><th class="r">Scored</th><th class="r">Funnel hits</th>
<th class="r">Median P/E</th><th class="r">Median FCF yield</th><th class="r">Median off high</th></tr></thead><tbody>
{% for s in sectors %}<tr><td><a href="#results" onclick="setSector({{ s.sector|tojson }})">{{ s.sector }}</a></td><td class="r">{{ s.n }}</td>
<td class="r"><b>{{ s.hits }}</b> <span class="muted">({{ s.hit_rate|pct }})</span></td><td class="r">{{ '%.1f'|format(s.pe) if s.pe else '—' }}</td>
<td class="r">{{ s.fcf|pct }}</td><td class="r">{{ s.dd|pct }}</td></tr>{% endfor %}</tbody></table></div></div>
</div>

<h2>Value top 10 this week</h2>
<div class="top">
{% for t in top10 %}{% set m = t.m %}<div class="card">
<h3><span><a href="{{ url_for('name', ticker=t.ticker) }}">{{ loop.index }}. {{ t.ticker }}</a> <span class="muted" style="font-weight:400">{{ (m.name or '')|cname }}</span></span><span>{{ t.score|n1 }}</span></h3>
<div class="muted" style="font-size:12px">{{ m.sector }}{% if m.industry %} · {{ m.industry }}{% endif %}</div>
<div style="margin-top:6px">{{ funnel_chips(t.funnels) }}{% if t.promote %}<span class="chip good">Clears bar</span>{% endif %}
{% if t.status == 'deep_dive_requested' %}<span class="chip acc">Deep dive requested</span>{% endif %}</div>
<div class="mini">
<div><span>Price</span><b>{{ m.px.price|money }}</b></div><div><span>Off 52w high</span><b class="dn">{{ m.px.drawdown|pct }}</b></div>
<div><span>P/E</span><b>{{ '%.1f'|format(m.fin.pe) if m.fin.pe else '—' }}</b></div><div><span>FCF yield</span><b>{{ m.fin.fcf_yield|pct }}</b></div>
<div><span>ROIC 5y avg</span><b>{{ m.fin.roc_avg|pct }}</b></div><div><span>EV/EBITDA</span><b>{{ '%.1fx'|format(m.fin.ev_ebitda) if m.fin.ev_ebitda else '—' }}</b></div>
<div><span>Graham value</span><b class="{{ 'up' if m.fin.graham and m.fin.graham > m.px.price }}">{{ m.fin.graham|money }}</b></div>
<div><span>Tests passed</span><b>{{ m.fin.tests_passed }}/{{ m.fin.tests_available }}</b></div></div>
{{ spark(m.px.spark) }}
<div class="rc">{% if t.reason_card %}{% set rc = t.reason_card %}
<div><b>Why cheap:</b> {{ rc.why_cheap }}</div><div><b>Who is selling:</b> {{ rc.who_selling }}</div>
<div><b>Market missing:</b> {{ rc.market_missing }}</div><div><b>Buyers return when:</b> {{ rc.buyers_back }}</div>
{% else %}<span class="empty">Reason card pending: written by Saturday's reason-card task.</span>{% endif %}</div>
<div class="row" style="margin-top:10px">
{% if t.status != 'deep_dive_requested' %}<form class="inline" method="post" action="{{ url_for('deepdive', ticker=t.ticker) }}"><button>Request deep dive</button></form>{% endif %}
<form class="inline" method="post" action="{{ url_for('move', ticker=t.ticker) }}"><input type="hidden" name="to" value="watchlist"><button>+ Watch</button></form>
<a class="btn" href="{{ url_for('name', ticker=t.ticker) }}">Details</a></div>
</div>{% endfor %}</div>

<h2 id="results">All results <span class="muted" style="font-weight:400" id="matched"></span></h2>
<div class="card">
<div class="presets" id="presets"></div>
<div class="filters">
<div><label>Search</label><input id="fq" placeholder="Ticker or company"></div>
<div><label>Sector</label><select id="fsector"><option value="">All sectors</option>{% for s in sectors %}<option>{{ s.sector }}</option>{% endfor %}</select></div>
<div><label>Funnel</label><select id="ffunnel"><option value="">Any (or none)</option><option value="any">Hit at least one</option>{% for k,label in FUNNELS %}{% if run.funnel_counts.get(k) is not none %}<option value="{{ k }}">{{ label }}</option>{% endif %}{% endfor %}</select></div>
<div><label>Max P/E</label><input id="f_pe" type="number" placeholder="e.g. 20"></div>
<div><label>Max P/B</label><input id="f_pb" type="number" placeholder="e.g. 3"></div>
<div><label>Max EV/EBITDA</label><input id="f_eve" type="number" placeholder="e.g. 12"></div>
<div><label>Min FCF yield %</label><input id="f_fy" type="number" placeholder="e.g. 5"></div>
<div><label>Min ROIC 5y avg %</label><input id="f_roc" type="number" placeholder="e.g. 12"></div>
<div><label>Min ROE %</label><input id="f_roe" type="number" placeholder="e.g. 12"></div>
<div><label>Min rev CAGR 5y %</label><input id="f_g" type="number" placeholder="e.g. 5"></div>
<div><label>Max debt / equity</label><input id="f_de" type="number" placeholder="e.g. 1"></div>
<div><label>Max net debt / EBITDA</label><input id="f_nd" type="number" placeholder="e.g. 3"></div>
<div><label>Min market cap $B</label><input id="f_mc" type="number" placeholder="e.g. 10"></div>
<div><label>Min off 52w high %</label><input id="f_dd" type="number" placeholder="e.g. 30"></div>
<div><label class="cb"><input type="checkbox" id="f_gate"> Passed balance-sheet gates</label><label class="cb"><input type="checkbox" id="f_new"> Hide Watchlist / Book / rejected</label></div>
<div class="row"><button onclick="resetFilters()">Reset</button><button onclick="exportCsv()">Export CSV</button></div>
</div></div>
<div class="row pager" id="pager" style="margin:10px 0"></div>
<div class="tablewrap"><table id="res"><thead><tr id="hdr"></tr></thead><tbody id="rows"></tbody></table></div>
<div class="bulk" id="bulk"><form method="post" action="{{ url_for('bulk') }}" class="row" onsubmit="return fillBulk(this)">
<b id="selcount"></b><input type="hidden" name="tickers"><button name="action" value="watch" class="primary">+ Add to Watchlist</button>
<button name="action" value="deepdive">Request deep dive</button><button type="button" onclick="SEL.clear();render()">Clear</button></form></div>
{% endif %}

<h2>Names on record <span class="muted" style="font-weight:400">{{ names|length }} · none ever deleted</span></h2>
<details><summary>Show every name ever screened, with status, first seen and last screened dates</summary>
<div class="row" style="margin:10px 0"><input class="search" placeholder="Search…" data-filter="#all">
{% for s,c in status_counts %}<span class="chip">{{ s }} {{ c }}</span>{% endfor %}</div>
<div class="tablewrap"><table id="all"><thead><tr><th>Ticker</th><th>Name</th><th>Status</th><th class="r">Score</th>
<th>Funnels</th><th>Themes</th><th>Last screened</th><th>First seen</th><th>Reject reason</th></tr></thead><tbody>
{% for n in names %}<tr>
<td><a href="{{ url_for('name', ticker=n.ticker) }}"><b>{{ n.ticker }}</b></a></td><td>{{ n.name or '' }}</td>
<td><span class="chip {{ 'bad' if n.status=='rejected' else ('good' if n.status=='candidate' else '') }}">{{ n.status }}</span></td>
<td class="r">{{ n.score|n1 }}</td><td>{{ funnel_chips(n.funnels) }}</td>
<td>{% for th in n.themes or [] %}<span class="chip">{{ th }}</span>{% endfor %}</td>
<td>{{ n.last_screened|d }}</td><td class="muted">{{ n.first_seen|d }}</td><td class="muted">{{ n.reject_reason or '' }}</td>
</tr>{% endfor %}</tbody></table></div></details>

{% if run %}
<script>
const DATA = {{ rows|tojson }};
const FUN = {{ FUNNELS|tojson }};
const SEL = new Set();
let sortKey = 'score', sortDir = -1, pageNo = 1; const PER = 50;
const $ = id => document.getElementById(id);
const big = v => v == null ? '—' : (Math.abs(v) >= 1e12 ? '$' + (v/1e12).toFixed(2) + 'T' : Math.abs(v) >= 1e9 ? '$' + (v/1e9).toFixed(1) + 'B' : '$' + (v/1e6).toFixed(0) + 'M');
const money = v => v == null ? '—' : '$' + (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(2));
const pc = v => v == null ? '—' : (v*100).toFixed(1) + '%';
const x1 = v => v == null ? '—' : v.toFixed(1) + 'x';
const n1 = v => v == null ? '—' : v.toFixed(1);
const g = (v, good, ok, low) => v == null ? '' : (low ? (v <= good ? 'good' : v <= ok ? 'warn' : '') : (v >= good ? 'good' : v >= ok ? 'warn' : ''));
const vsPrice = r => v => v == null || r.price == null ? '' : (v > r.price * 1.2 ? 'good' : v >= r.price ? 'warn' : 'bad');
const COLS = [
 ['ticker','Ticker',r=>`<a href="/name/${r.ticker}"><b>${r.ticker}</b></a>${r.level !== 'screen' ? ` <span class="chip acc">${r.level}</span>` : r.status === 'rejected' ? ' <span class="chip bad">rejected</span>' : ''}`,'','sticky'],
 ['name','Company',r=>r.name||'','','name'],
 ['sector','Sector',r=>r.sector||'—'],
 ['price','Price',r=>money(r.price),'r'],
 ['mcap','Mkt cap',r=>big(r.mcap),'r'],
 ['revenue','Revenue',r=>big(r.revenue),'r'],
 ['net_income','Net income',r=>`<span class="${r.net_income<0?'bad':''}">${big(r.net_income)}</span>`,'r'],
 ['net_margin','Net margin',r=>`<span class="${g(r.net_margin,.15,.08)}">${pc(r.net_margin)}</span>`,'r'],
 ['rev_growth_1y','Rev growth 1y',r=>`<span class="${r.rev_growth_1y<0?'bad':g(r.rev_growth_1y,.10,0)}">${pc(r.rev_growth_1y)}</span>`,'r'],
 ['rev_cagr','Rev CAGR 5y',r=>`<span class="${g(r.rev_cagr,.10,.05)}">${pc(r.rev_cagr)}</span>`,'r'],
 ['eps','EPS',r=>money(r.eps),'r'],
 ['pe','P/E',r=>`<span class="${g(r.pe,15,20,1)}">${n1(r.pe)}</span>`,'r'],
 ['pb','P/B',r=>`<span class="${g(r.pb,1.5,3,1)}">${n1(r.pb)}</span>`,'r'],
 ['ev_rev','EV/Rev',r=>x1(r.ev_rev),'r'],
 ['ev_ebitda','EV/EBITDA',r=>`<span class="${g(r.ev_ebitda,8,12,1)}">${x1(r.ev_ebitda)}</span>`,'r'],
 ['fcf_yield','FCF yield',r=>`<span class="${g(r.fcf_yield,.08,.05)}">${pc(r.fcf_yield)}</span>`,'r'],
 ['roe','ROE',r=>`<span class="${g(r.roe,.15,.10)}">${pc(r.roe)}</span>`,'r'],
 ['roc_avg','ROIC 5y avg',r=>`<span class="${g(r.roc_avg,.15,.10)}">${pc(r.roc_avg)}</span>`,'r'],
 ['de','Debt/Eq',r=>`<span class="${g(r.de,.5,1,1)}">${n1(r.de)}</span>`,'r'],
 ['nd_ebitda','ND/EBITDA',r=>`<span class="${r.nd_ebitda!=null && r.nd_ebitda>3?'bad':g(r.nd_ebitda,1,3,1)}">${x1(r.nd_ebitda)}</span>`,'r'],
 ['interest_cover','Int. cover',r=>`<span class="${r.interest_cover!=null && r.interest_cover<3?'bad':''}">${x1(r.interest_cover)}</span>`,'r'],
 ['graham','Graham',r=>`<span class="${vsPrice(r)(r.graham)}" title="√(22.5 × EPS × book value per share)">${money(r.graham)}</span>`,'r'],
 ['pe_value','P/E value',r=>`<span class="${vsPrice(r)(r.pe_value)}" title="EPS × 15">${money(r.pe_value)}</span>`,'r'],
 ['lynch_5y','Lynch 5y',r=>`<span class="${vsPrice(r)(r.lynch_5y)}" title="EPS grown 5y at revenue CAGR (2–40%) × 15, discounted at 10%">${money(r.lynch_5y)}</span>`,'r'],
 ['drawdown','Off high',r=>`<span class="${r.drawdown>=.3?'bad':''}">${pc(r.drawdown)}</span>`,'r'],
 ['vs_ma200','vs 200d',r=>`<span class="${r.vs_ma200<0?'bad':'good'}">${pc(r.vs_ma200)}</span>`,'r'],
 ['ret_3m','3m return',r=>`<span class="${r.ret_3m<0?'bad':'good'}">${pc(r.ret_3m)}</span>`,'r'],
 ['share_change_5y','Shares 5y',r=>`<span class="${r.share_change_5y>0.03?'bad':r.share_change_5y<0?'good':''}">${pc(r.share_change_5y)}</span>`,'r'],
 ['tests_passed','Tests',r=>`${r.gate_pass?'<span class="good" title="Passed balance-sheet gates">●</span>':'<span class="bad" title="Failed a balance-sheet gate">●</span>'} ${r.tests_passed}/${r.tests_available}`,'r'],
 ['hits','Funnels',r=>FUN.filter(f=>r.funnels[f[0]]).map(f=>`<span class="chip acc">${f[1]}</span>`).join('')],
 ['flow','Flow',r=>n1(r.flow),'r'],
 ['score','Score',r=>`<b>${n1(r.score)}</b>`,'r'],
];
const PRESETS = {
 'Classic value': {f_pe:15, f_pb:2, f_de:1, f_gate:true},
 'Quality at a fair price': {f_roc:15, f_pe:25, f_fy:4},
 'Deep value': {f_eve:8, f_fy:8},
 'Cash machines': {f_fy:6, f_nd:1, f_roe:15},
 'Fallen quality': {f_roc:15, f_dd:30},
 'Compounders': {f_g:10, f_roc:15, f_de:1},
};
const NUM = {f_pe:['pe',1], f_pb:['pb',1], f_eve:['ev_ebitda',1], f_fy:['fcf_yield',0,100], f_roc:['roc_avg',0,100], f_roe:['roe',0,100],
             f_g:['rev_cagr',0,100], f_de:['de',1], f_nd:['nd_ebitda',1], f_mc:['mcap',0,1e-9], f_dd:['drawdown',0,100]};
function filtered(){
  const q = $('fq').value.trim().toLowerCase(), sec = $('fsector').value, fun = $('ffunnel').value;
  const lim = {}; for (const id in NUM){ const v = parseFloat($(id).value); if (!isNaN(v)) lim[id] = v; }
  return DATA.filter(r => {
    if (q && !(r.ticker.toLowerCase().includes(q) || (r.name||'').toLowerCase().includes(q))) return false;
    if (sec && r.sector !== sec) return false;
    if (fun === 'any' && !r.hits) return false;
    if (fun && fun !== 'any' && !r.funnels[fun]) return false;
    if ($('f_gate').checked && !r.gate_pass) return false;
    if ($('f_new').checked && (r.level !== 'screen' || r.status === 'rejected')) return false;
    for (const id in lim){
      const [k, isMax, scale] = NUM[id]; let v = r[k]; if (v == null) return false;
      v = scale ? v * scale : v;
      if (isMax ? v > lim[id] : v < lim[id]) return false;
    }
    return true;
  });
}
function render(){
  const rows = filtered().sort((a,b)=>{const x=a[sortKey], y=b[sortKey]; if (x==null) return 1; if (y==null) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * sortDir;});
  const pages = Math.max(1, Math.ceil(rows.length / PER)); pageNo = Math.min(pageNo, pages);
  $('matched').textContent = `${rows.length.toLocaleString()} of ${DATA.length.toLocaleString()} match`;
  $('hdr').innerHTML = '<th class="sticky"><input type="checkbox" onclick="pageSel(this.checked)"></th>' +
    COLS.map(c => `<th class="${c[3]||''} ${c[0]===sortKey?'sorted':''}" onclick="sortBy('${c[0]}')">${c[1]}${c[0]===sortKey?(sortDir<0?' ▼':' ▲'):''}</th>`).join('');
  const slice = rows.slice((pageNo-1)*PER, pageNo*PER); window._page = slice;
  $('rows').innerHTML = slice.map(r => `<tr><td class="sticky"><input type="checkbox" ${SEL.has(r.ticker)?'checked':''} onclick="toggleSel('${r.ticker}',this.checked)"></td>` +
    COLS.map(c => `<td class="${c[3]||''} ${c[4]||''}">${c[2](r)}</td>`).join('') + '</tr>').join('') ||
    `<tr><td colspan="${COLS.length+1}" class="empty">No stocks match these filters.</td></tr>`;
  let pb = ''; for (let i=1; i<=pages; i++) if (i<=3 || i>pages-2 || Math.abs(i-pageNo)<=2) pb += `<button class="${i===pageNo?'on':''}" onclick="pageNo=${i};render()">${i}</button>`; else if (!pb.endsWith('…')) pb += '…';
  $('pager').innerHTML = pages > 1 ? pb : '';
  $('bulk').style.display = SEL.size ? 'block' : 'none'; $('selcount').textContent = `${SEL.size} selected`;
  window._filtered = rows;
}
function sortBy(k){ if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = ['ticker','name','sector'].includes(k) ? 1 : -1; } render(); }
function toggleSel(t, on){ on ? SEL.add(t) : SEL.delete(t); render(); }
function pageSel(on){ window._page.forEach(r => on ? SEL.add(r.ticker) : SEL.delete(r.ticker)); render(); }
function fillBulk(f){ f.tickers.value = [...SEL].join(','); return SEL.size > 0; }
function setSector(s){ $('fsector').value = s; pageNo = 1; render(); }
function resetFilters(){ document.querySelectorAll('.filters input').forEach(i => i.type === 'checkbox' ? i.checked = false : i.value = '');
  document.querySelectorAll('.filters select').forEach(s => s.value = ''); document.querySelectorAll('#presets button').forEach(b => b.classList.remove('on')); pageNo = 1; render(); }
function applyPreset(name, btn){ resetFilters(); const p = PRESETS[name];
  for (const id in p) typeof p[id] === 'boolean' ? $(id).checked = p[id] : $(id).value = p[id]; btn.classList.add('on'); render(); }
function exportCsv(){ const keys = COLS.map(c=>c[0]).filter(k=>k!=='hits');
  const lines = [keys.concat(['funnels']).join(',')].concat(window._filtered.map(r => keys.map(k => r[k] == null ? '' : JSON.stringify(r[k])).concat(
    [JSON.stringify(FUN.filter(f=>r.funnels[f[0]]).map(f=>f[1]).join('; '))]).join(',')));
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([lines.join('\n')], {type:'text/csv'}));
  a.download = 'screen-{{ run.date }}.csv'; a.click(); }
$('presets').innerHTML = '<span class="muted" style="margin-right:8px">Presets:</span>' + Object.keys(PRESETS).map(n => `<button onclick="applyPreset('${n}', this)">${n}</button>`).join('');
document.querySelectorAll('.filters input, .filters select').forEach(el => el.addEventListener('input', () => { pageNo = 1; render(); }));
render();
</script>{% endif %}
"""


def spark_svg(vals, w=160, h=34):
    vals = [v for v in (vals or []) if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    hi = hi if hi > lo else lo + 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - 2 - (v - lo) / (hi - lo) * (h - 4):.1f}" for i, v in enumerate(vals))
    col = "var(--good)" if vals[-1] >= vals[0] else "var(--bad)"
    return Markup(f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none" aria-hidden="true">'
                  f'<polyline fill="none" stroke="{col}" stroke-width="1.5" points="{pts}"/></svg>')


app.jinja_env.globals["spark"] = spark_svg


import re as _re
_SUFFIX = _re.compile(r"\s+(Class [A-Z]\s+)?(Common|Ordinary|Capital)\s+(Stock|Shares)\b.*$|\s+Common Stock$", _re.I)


def clean_name(n):
    return _SUFFIX.sub("", n or "").strip() or n


app.jinja_env.filters["cname"] = clean_name


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


@app.route("/")
def home():
    return redirect(url_for("screen"))


@app.route("/screen")
def screen():
    if request.args.get("track") == "growth":
        return screen_growth()
    names = q("SELECT * FROM v2_names WHERE level = 'screen' ORDER BY score DESC NULLS LAST, ticker")
    counts = {}
    for n in names:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    ctx = dict(names=names, status_counts=sorted(counts.items(), key=lambda kv: -kv[1]),
               run=None, rows=[], top10=[], sectors=[], mkt={}, st={})
    run = q1("SELECT * FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    if run:
        run["date"] = run["run_date"].isoformat()
        info = {r["ticker"]: r for r in q("SELECT ticker, level, status, reason_card FROM v2_names")}
        scores = q("SELECT ticker, score, flow_score, funnels, metrics FROM v2_screen_scores WHERE run_date = %s",
                   (run["run_date"],))
        rows, by_t = [], {}
        for s in scores:
            m = s["metrics"] or {}
            fin, px = m.get("fin") or {}, m.get("px") or {}
            n = info.get(s["ticker"], {})
            f = s["funnels"] or {}
            row = {"ticker": s["ticker"], "name": clean_name(m.get("name")), "sector": m.get("sector") or "Other",
                   "mcap": m.get("mcap"), "score": num(s["score"]), "flow": num(s["flow_score"]),
                   "funnels": f, "hits": sum(1 for k in VALUE_FUNNELS if f.get(k)),
                   "level": n.get("level", "screen"), "status": n.get("status", "new"),
                   "gate_pass": fin.get("gate_pass"), "tests_passed": fin.get("tests_passed"),
                   "tests_available": fin.get("tests_available")}
            for k in ("revenue", "net_income", "net_margin", "rev_growth_1y", "rev_cagr", "eps", "pe", "pb",
                      "ev_rev", "ev_ebitda", "fcf_yield", "roe", "roc_avg", "de", "nd_ebitda", "interest_cover",
                      "graham", "pe_value", "lynch_5y", "share_change_5y"):
                row[k] = fin.get(k)
            for k in ("price", "drawdown", "vs_ma200", "ret_3m"):
                row[k] = px.get(k)
            for k, v in row.items():                     # trim payload: 4 significant decimals
                if isinstance(v, float):
                    row[k] = round(v) if abs(v) >= 1e5 else float(f"{v:.4g}")
            rows.append(row)
            by_t[s["ticker"]] = {"m": m, "n": n}
        sectors = {}
        for r in rows:
            sectors.setdefault(r["sector"], []).append(r)
        sec_rows = sorted(({"sector": k, "n": len(v), "hits": sum(1 for r in v if r["hits"]),
                            "hit_rate": sum(1 for r in v if r["hits"]) / len(v),
                            "pe": median(r["pe"] for r in v), "fcf": median(r["fcf_yield"] for r in v),
                            "dd": median(r["drawdown"] for r in v)} for k, v in sectors.items()),
                          key=lambda s: -s["hits"])
        top10 = []
        for t in run.get("top10") or []:
            d = by_t.get(t["ticker"], {})
            if d:
                top10.append({**t, "m": d["m"], "status": d["n"].get("status"), "reason_card": d["n"].get("reason_card")})
        ctx.update(run=run, rows=rows, top10=top10, sectors=sec_rows, mkt=run.get("market") or {},
                   st=run.get("stages") or {})
    return page("screen", "Screen", SCREEN_TPL, **ctx)


GROWTH_TPL = r"""
<style>
.top{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(380px,1fr))}
.top .card h3{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px}
.parts{display:grid;grid-template-columns:110px 1fr 34px;gap:4px 8px;align-items:center;font-size:12px;margin:10px 0}
.parts .b{height:8px;border-radius:4px;background:var(--panel2)}.parts .b i{display:block;height:8px;border-radius:4px;background:var(--accent)}
.mini{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0}
.mini div{background:var(--panel2);border-radius:8px;padding:6px 8px}.mini b{display:block;font-size:14px}.mini span{font-size:11px;color:var(--mute)}
.flags{margin-top:6px}.flags .chip{background:rgba(210,153,34,.15);color:var(--warn)}
.rc{font-size:13px;border-top:1px solid var(--line);padding-top:8px;margin-top:8px}.rc div{margin:2px 0}
.fbar{display:grid;grid-template-columns:170px 1fr 60px;gap:8px;align-items:center;margin:4px 0;font-size:13px}
.fbar .b{height:16px;border-radius:4px;background:var(--accent);opacity:.85;min-width:2px}.fbar .n{text-align:right;font-weight:600}
.up{color:var(--good)}.dn{color:var(--bad)}
#gres th{cursor:pointer;user-select:none}#gres td{white-space:nowrap;font-size:13px;padding:6px 8px}
</style>
<h1>Screen</h1>
<div class="row" style="margin:-4px 0 10px"><a class="btn" href="{{ url_for('screen') }}">Value</a><a class="btn primary" href="{{ url_for('screen', track='growth') }}">Growth</a></div>
<p class="sub">{% if run %}Growth track of {{ run.date|d }} · US, market cap ≥ $1B · any one growth funnel gets a name in, with no price or profit filter ·
scored on moat 30, growth and acceleration 25, industry acceleration 25, smart money 10, valuation 10 (+5 for a strong moat in an accelerating industry) · risks are flagged, never used to exclude{% else %}No screen has run yet.{% endif %}</p>
{% if run %}
<div class="grid g2">
<div><h2>This week's growth funnel</h2><div class="card">
{% set top = st.listings or 1 %}
{% for k,label in [('listings','All listings'),('us_1b','US, market cap ≥ $1B'),('with_fundamentals','With SEC financials'),('growth_any_funnel','Hit a growth funnel'),('growth_top10','Top 10'),('growth_cleared_bar','Deep dives this week')] %}
{% if st.get(k) is not none %}<div class="fbar"><span>{{ label }}</span><div><div class="b" style="width:{{ (100*st[k]/top)|round(1) }}%"></div></div><span class="n">{{ '{:,}'.format(st[k]) }}</span></div>{% endif %}{% endfor %}
<div class="row" style="margin-top:10px">{% for k,label in FUNNELS if k in GROWTH_FUNNELS %}{% if run.funnel_counts.get(k) is not none %}<span class="chip acc">{{ label }} {{ run.funnel_counts[k] }}</span>{% endif %}{% endfor %}</div>
<p class="muted" style="font-size:12px;margin-bottom:0"><b>Fast growth</b>: revenue +20% (latest quarter or year) or +15% a year over 5 years ·
<b>Accelerating</b>: growth 10%+ and rising 5+ points · <b>Smart money</b>: 2+ growth funds bought last quarter (13F), or 4+ hold with none selling ·
<b>Share taker</b>: in one of your themes and growing 10+ points faster than its industry · <b>Momentum leader</b>: 20%+ ahead of the S&amp;P over 6 months, near its high, growing 10%+ ·
<b>Estimates rising</b>: analysts' next-year EPS up 5%+ in 90 days, more upgrades than cuts (checked for the best 60) · foreign companies listed in the US are included</p>
</div></div>
<div><h2>Industry acceleration: your themes vs the S&amp;P (6 months)</h2><div class="card">
{% for th, v in (mkt.theme_strength_6m or {}).items() %}<div class="fbar"><span>{{ th }}</span><div><div class="b" style="width:{{ ((v + 0.4) / 0.8 * 100)|round(1) if v is not none else 0 }}%;background:{{ 'var(--good)' if v and v > 0 else 'var(--bad)' }}"></div></div><span class="n {{ 'up' if v and v > 0 else 'dn' }}">{{ v|pct(true) }}</span></div>
{% else %}<p class="empty">Theme strength arrives with the next Saturday screen.</p>{% endfor %}
<p class="muted" style="font-size:12px;margin-bottom:0">Industry score also uses each company's peer group: median revenue growth and whether it is speeding up.</p>
</div></div></div>

<h2>Growth top 10 this week</h2>
<div class="top">
{% for t in top10 %}{% set m = t.m %}{% set g = m.growth or {} %}<div class="card">
<h3><span><a href="{{ url_for('name', ticker=t.ticker) }}">{{ loop.index }}. {{ t.ticker }}</a> <span class="muted" style="font-weight:400">{{ (m.name or '')|cname }}</span></span><span>{{ t.score|n1 }}</span></h3>
<div class="muted" style="font-size:12px">{{ m.industry or m.sector }}{% if g.themes %} · {{ g.themes|join(', ') }}{% endif %}</div>
<div style="margin-top:6px">{{ funnel_chips(t.funnels) }}{% if t.promote %}<span class="chip good">Deep dive this week</span>{% endif %}
{% if t.status == 'deep_dive_requested' %}<span class="chip acc">Deep dive requested</span>{% endif %}</div>
<div class="parts">{% for k,label in [('moat','Moat'),('growth','Growth'),('industry','Industry'),('smart','Smart money'),('valuation','Valuation')] %}
<span>{{ label }}</span><div class="b"><i style="width:{{ (t.parts or {}).get(k, 0) }}%"></i></div><b>{{ (t.parts or {}).get(k, 0)|round|int }}</b>{% endfor %}</div>
<div class="mini">
<div><span>Revenue growth</span><b>{{ g.g_now|pct }}</b></div><div><span>Acceleration</span><b class="{{ 'up' if g.accel and g.accel > 0 else 'dn' }}">{{ ('%+.0f pts'|format(g.accel*100)) if g.accel is not none else '—' }}</b></div>
<div><span>Gross margin</span><b>{{ m.fin.gross_margin|pct }}</b></div><div><span>Rule of 40</span><b>{{ ((g.rule_of_40 or 0)*100)|round|int if g.rule_of_40 is not none else '—' }}</b></div>
<div><span>Peers' growth</span><b>{{ g.peer_growth|pct }}</b></div><div><span>vs S&amp;P 6m</span><b class="{{ 'up' if g.rs_6m and g.rs_6m > 0 else 'dn' }}">{{ g.rs_6m|pct(true) }}</b></div>
<div><span>EV / sales</span><b>{{ '%.1fx'|format(m.fin.ev_rev) if m.fin.ev_rev else '—' }}</b></div><div><span>Price</span><b>{{ m.px.price|money }}</b></div></div>
{% if g.smart_money %}{% set sm = g.smart_money %}<div style="font-size:12px"><b>Smart money ({{ sm.period }}):</b>
{% if sm.new %}new: {{ sm.new|join(', ') }}. {% endif %}{% if sm.added %}added: {{ sm.added|join(', ') }}. {% endif %}{% if sm.trimmed or sm.sold %}<span class="dn">cut/sold: {{ (sm.trimmed + sm.sold)|join(', ') }}.</span> {% endif %}
{{ sm.holders|length }} growth funds hold{% if sm.inst_change is not none and sm.inst_change < 1 %} · big institutions {{ sm.inst_change|pct(true) }}{% endif %}</div>{% endif %}
{% if g.insiders %}<div style="font-size:12px"><b>Insiders bought</b> ${{ '{:,.1f}'.format(g.insiders.value/1e6) }}M ({{ g.insiders.buyers|length }} people{% if g.insiders.ceo_cfo %}, incl. CEO/CFO/chair{% endif %}), last {{ g.insiders.last }}</div>{% endif %}
{% if g.activist %}<div style="font-size:12px"><b>Activist 13D</b> filed {{ g.activist.last }}{% if g.activist.filers %} by {{ g.activist.filers|join(', ') }}{% endif %}</div>{% endif %}
{% if g.estimates and g.estimates.chg_90d is not none %}<div style="font-size:12px"><b>Next-year EPS estimate</b> {{ g.estimates.chg_90d|pct(true) }} in 90 days ({{ g.estimates.up_30d or 0 }} up / {{ g.estimates.down_30d or 0 }} down in 30 days)</div>{% endif %}
<div class="flags">{% for f in t.flags or [] %}<span class="chip">⚠ {{ f }}</span>{% else %}<span class="muted" style="font-size:12px">No risk flags from the numbers.</span>{% endfor %}</div>
<div class="rc">{% if t.reason_card %}{% set rc = t.reason_card %}
<div><b>Why it can keep compounding:</b> {{ rc.why_cheap }}</div><div><b>Who owns / is buying:</b> {{ rc.who_selling }}</div>
<div><b>What the market may underestimate:</b> {{ rc.market_missing }}</div><div><b>Biggest risk and what to watch:</b> {{ rc.buyers_back }}</div>
{% else %}<span class="empty">Growth card pending: written by Sunday's overnight task.</span>{% endif %}</div>
<div class="row" style="margin-top:10px">
{% if t.status != 'deep_dive_requested' %}<form class="inline" method="post" action="{{ url_for('deepdive', ticker=t.ticker) }}"><button>Request deep dive</button></form>{% endif %}
<form class="inline" method="post" action="{{ url_for('move', ticker=t.ticker) }}"><input type="hidden" name="to" value="watchlist"><button>+ Watch</button></form>
<a class="btn" href="{{ url_for('name', ticker=t.ticker) }}">Details</a></div>
</div>{% else %}<p class="empty">The growth track fills after the next Saturday screen.</p>{% endfor %}</div>

<h2>All growth-funnel names <span class="muted" style="font-weight:400">({{ rows|length }})</span></h2>
<div class="card"><div class="row"><input id="gq" placeholder="Ticker, company or industry" oninput="gfilter()" style="min-width:260px">
<select id="gf" onchange="gfilter()"><option value="">Any growth funnel</option>{% for k,label in FUNNELS if k in GROWTH_FUNNELS %}<option value="{{ k }}">{{ label }}</option>{% endfor %}</select>
<label class="muted"><input type="checkbox" id="gnf" onchange="gfilter()"> Hide names with risk flags</label></div></div>
<div class="tablewrap"><table id="gres"><thead><tr>
{% for h in ['Ticker','Company','Industry','Score','Moat','Growth','Industry score','Smart','Valuation','Rev growth','Accel (pts)','Gross margin','Rule of 40','EV/sales','vs S&P 6m','Mkt cap $B','Funnels','Risks'] %}<th onclick="gsort({{ loop.index0 }})">{{ h }}</th>{% endfor %}</tr></thead><tbody>
{% for r in rows %}<tr data-f="{{ r.fun|join(' ') }}" data-flag="{{ 1 if r.flags else 0 }}">
<td><a href="{{ url_for('name', ticker=r.ticker) }}">{{ r.ticker }}</a></td><td>{{ (r.name or '')|cname|truncate(26, True, '…') }}</td><td class="muted">{{ (r.industry or '')|truncate(28, True, '…') }}</td>
<td class="r"><b>{{ r.score|n1 }}</b></td>{% for k in ['moat','growth','industry','smart','valuation'] %}<td class="r">{{ r.parts[k]|round|int }}</td>{% endfor %}
<td class="r" data-v="{{ r.g or 0 }}">{{ r.g|pct }}</td><td class="r" data-v="{{ r.accel or 0 }}">{{ ('%+.0f'|format(r.accel*100)) if r.accel is not none else '—' }}</td>
<td class="r" data-v="{{ r.gm or 0 }}">{{ r.gm|pct }}</td><td class="r">{{ ((r.r40 or 0)*100)|round|int if r.r40 is not none else '—' }}</td>
<td class="r" data-v="{{ r.evs or 0 }}">{{ '%.1f'|format(r.evs) if r.evs else '—' }}</td><td class="r" data-v="{{ r.rs or 0 }}">{{ r.rs|pct(true) }}</td>
<td class="r">{{ '%.1f'|format(r.mcap/1e9) if r.mcap else '—' }}</td><td>{{ funnel_chips(r.funnels) }}</td><td class="muted" style="font-size:12px">{{ r.flags|join('; ') }}</td></tr>{% endfor %}
</tbody></table></div>
<script>
function gfilter(){const q=document.getElementById('gq').value.toLowerCase(),f=document.getElementById('gf').value,nf=document.getElementById('gnf').checked;
 document.querySelectorAll('#gres tbody tr').forEach(tr=>{const ok=(!q||tr.innerText.toLowerCase().includes(q))&&(!f||tr.dataset.f.split(' ').includes(f))&&(!nf||tr.dataset.flag==='0');tr.style.display=ok?'':'none';});}
let gdir={};function gsort(i){const tb=document.querySelector('#gres tbody');const rows=[...tb.rows];gdir[i]=!gdir[i];
 const val=td=>{const v=td.dataset.v!==undefined?td.dataset.v:td.innerText.replace(/[%,$+]/g,'');const n=parseFloat(v);return isNaN(n)?td.innerText.toLowerCase():n;};
 rows.sort((a,b)=>{const x=val(a.cells[i]),y=val(b.cells[i]);return (x>y?1:x<y?-1:0)*(gdir[i]?-1:1);});rows.forEach(r=>tb.appendChild(r));}
</script>
{% endif %}
"""


def screen_growth():
    run = q1("SELECT * FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    ctx = dict(run=None, rows=[], top10=[], st={}, mkt={})
    if run:
        run["date"] = run["run_date"].isoformat()
        info = {r["ticker"]: r for r in q("SELECT ticker, level, status, reason_card FROM v2_names")}
        scores = q("""SELECT ticker, growth_score, funnels, metrics FROM v2_screen_scores WHERE run_date = %s
                      AND growth_score IS NOT NULL ORDER BY growth_score DESC""", (run["run_date"],))
        rows, by_t = [], {}
        for sc in scores:
            f = sc["funnels"] or {}
            fun = [k for k in GROWTH_FUNNELS if f.get(k)]
            m = sc["metrics"] or {}
            by_t[sc["ticker"]] = m
            if not fun:
                continue
            g, fin = m.get("growth") or {}, m.get("fin") or {}
            rows.append({"ticker": sc["ticker"], "name": m.get("name"), "industry": m.get("industry"), "mcap": m.get("mcap"),
                         "score": num(sc["growth_score"]), "parts": g.get("parts") or {}, "g": g.get("g_now"),
                         "accel": g.get("accel"), "gm": fin.get("gross_margin"), "r40": g.get("rule_of_40"),
                         "evs": fin.get("ev_rev") or fin.get("ps"), "rs": g.get("rs_6m"), "flags": g.get("flags") or [],
                         "fun": fun, "funnels": {k: True for k in fun}})
        top10 = []
        for t in run.get("growth_top10") or []:
            n = info.get(t["ticker"], {})
            top10.append({**t, "m": by_t.get(t["ticker"], {}), "status": n.get("status"), "reason_card": n.get("reason_card")})
        ctx.update(run=run, rows=rows, top10=top10, st=run.get("stages") or {}, mkt=run.get("market") or {})
    return page("screen", "Screen · Growth", GROWTH_TPL, **ctx)


@app.post("/screen/bulk")
def bulk():
    tickers = [t.strip().upper() for t in (request.form.get("tickers") or "").split(",") if t.strip()]
    action = request.form.get("action")
    if not tickers:
        return redirect(url_for("screen"))
    if action == "deepdive":
        for t in tickers:
            ensure_name(t)
        x("UPDATE v2_names SET status = 'deep_dive_requested', updated_at = now() WHERE ticker = ANY(%s)", (tickers,))
        flash(f"Deep dive requested for {', '.join(tickers)}.")
    elif action == "watch":
        room = WATCH_CAP - q1("SELECT COUNT(*) AS c FROM v2_names WHERE level = 'watchlist'")["c"]
        take, skipped = tickers[:max(room, 0)], tickers[max(room, 0):]
        for t in take:
            m = (q1("SELECT metrics FROM v2_screen_scores WHERE ticker = %s ORDER BY run_date DESC LIMIT 1", (t,))
                 or {}).get("metrics") or {}
            x("""INSERT INTO v2_names (ticker, name, sector, industry, level, status, first_seen, last_screened, level_since)
                 VALUES (%s, %s, %s, %s, 'watchlist', 'deep_dive_requested', CURRENT_DATE, CURRENT_DATE, CURRENT_DATE)
                 ON CONFLICT (ticker) DO UPDATE SET level = 'watchlist', level_since = CURRENT_DATE,
                   status = CASE WHEN EXISTS (SELECT 1 FROM v2_theses th WHERE th.ticker = v2_names.ticker)
                            THEN 'watching' ELSE 'deep_dive_requested' END, updated_at = now()""",
              (t, m.get("name"), m.get("sector"), m.get("industry")))
            x("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', 'Moved screen → watchlist (bulk)')", (t,))
        msg = f"Added {len(take)} to the Watchlist."
        if skipped:
            msg += f" Not added (Watchlist cap {WATCH_CAP} reached): {', '.join(skipped)}."
        flash(msg)
    return redirect(url_for("screen") + "#results")


# ------------------------------------------------------------------ Watchlist
WATCH_TPL = r"""
<h1>Watchlist</h1>
<p class="sub">{{ rows|length }} of {{ cap }} names · {{ core }} of {{ core_cap }} core pinned · every name carries a thesis and is checked daily for the buy signal.</p>
{% if rows|length >= cap %}<div class="flash">The Watchlist is full. Remove names before new ones can be added. Suggestions below.</div>{% endif %}
<div class="tablewrap"><table id="wl"><thead><tr><th>Ticker</th><th class="r">Price</th><th class="r">Entry target</th>
<th class="r">Gap to target</th><th>Buy signal</th><th class="r">Conviction</th><th>Verdict</th><th>Latest news</th>
<th class="r">Alerts</th><th></th></tr></thead><tbody>
{% for r in rows %}<tr>
<td>{% if r.is_core %}<span title="Core" class="chip acc">core</span>{% endif %}<a href="{{ url_for('name', ticker=r.ticker) }}"><b>{{ r.ticker }}</b></a><br><span class="muted">{{ r.name or '' }}</span></td>
<td class="r">{{ r.price|money }}</td><td class="r">{{ r.target|money }}{% if r.target_src == 'model' %}<br><span class="muted" style="font-size:11px">model, no thesis</span>{% endif %}</td>
<td class="r {{ 'good' if r.gap is not none and r.gap <= 0.05 }}">{{ r.gap|pct(true) }}</td>
<td>{% if r.health and r.health != 'green' %}<span class="dot {{ r.health }}" title="{{ r.health_why or '' }}"></span>{% endif %}{% if r.signal == 'buy' %}<span class="chip good">Buy signal</span>{% elif r.signal == 'near' %}<span class="chip warn">Approaching</span>{% elif r.signal == 'paused' %}<span class="chip bad">Paused</span>{% else %}<span class="muted">—</span>{% endif %}</td>
<td class="r">{{ r.conviction|n1 }}</td><td>{{ r.verdict or '—' }}</td>
<td style="max-width:260px">{% if r.news %}<span class="chip {{ TAG_CLASS.get(r.news.tag,'') }}">{{ r.news.tag or 'untagged' }}</span> {{ r.news.note or r.news.headline }}{% else %}<span class="muted">—</span>{% endif %}</td>
<td class="r">{{ r.alerts or '' }}</td>
<td style="white-space:nowrap">
<form class="inline" method="post" action="{{ url_for('pin', ticker=r.ticker) }}"><button>{{ 'Unpin' if r.is_core else 'Pin core' }}</button></form>
<form class="inline" method="post" action="{{ url_for('move', ticker=r.ticker) }}"><input type="hidden" name="to" value="book"><button>To Book</button></form>
</td></tr>{% else %}<tr><td colspan="10" class="empty">No names yet. Names arrive here after a deep dive, or with + Watch on the Screen.</td></tr>{% endfor %}
</tbody></table></div>

{% if swaps %}<h2>Swap suggestions</h2>
<p class="sub">Screen names now scoring above your weakest non-core Watchlist name. A prompt to compare, not a recommendation.</p>
<div class="tablewrap"><table><thead><tr><th>Screen name</th><th class="r">Score</th><th>Scores above</th><th></th></tr></thead><tbody>
{% for s in swaps %}<tr><td><a href="{{ url_for('name', ticker=s.ticker) }}"><b>{{ s.ticker }}</b></a> <span class="muted">{{ (s.name or '')|cname }}</span></td>
<td class="r">{{ s.score|n1 }}</td><td>{{ s.vs }} ({{ s.vs_score|n1 }})</td>
<td><form class="inline" method="post" action="{{ url_for('deepdive', ticker=s.ticker) }}"><button>Request deep dive</button></form></td></tr>{% endfor %}
</tbody></table></div>{% endif %}

<h2>Removal suggestions</h2>
<p class="sub">The five weakest non-core names: low conviction, far from target, weakening news, long without progress. You decide.</p>
<div class="tablewrap"><table><thead><tr><th>Ticker</th><th>Why</th><th></th></tr></thead><tbody>
{% for r in weakest %}<tr><td><a href="{{ url_for('name', ticker=r.ticker) }}"><b>{{ r.ticker }}</b></a></td><td>{{ r.why }}</td>
<td><form class="inline" method="post" action="{{ url_for('move', ticker=r.ticker) }}"><input type="hidden" name="to" value="screen">
<input name="reason" placeholder="Reason" required> <button>Remove</button></form></td></tr>
{% else %}<tr><td colspan="3" class="empty">No non-core names to suggest.</td></tr>{% endfor %}</tbody></table></div>
"""


def watch_rows(level):
    names = q("SELECT * FROM v2_names WHERE level = %s ORDER BY is_core DESC, ticker", (level,))
    t = [n["ticker"] for n in names]
    px, th = latest_prices(t), latest_by("v2_theses", "version DESC", t)
    try:
        model = {r["ticker"]: (r["data"].get("valuation") or {}) for r in q(
            "SELECT DISTINCT ON (ticker) ticker, data FROM v2_research WHERE ticker = ANY(%s) ORDER BY ticker, as_of DESC", (t,))}
    except psycopg2.Error:
        model = {}
    news, al = latest_by("v2_news", "published DESC NULLS LAST, id DESC", t), open_alerts(t)
    an = latest_by("v2_analyst", "date DESC", t)
    weak = {r["ticker"]: r["n"] for r in q(
        "SELECT ticker, COUNT(*) AS n FROM v2_news WHERE tag IN ('weakens','breaks') AND ticker = ANY(%s) "
        "AND published > now() - interval '8 weeks' GROUP BY ticker", (t,))}
    rows = []
    for n in names:
        tk = n["ticker"]
        thesis = th.get(tk) or {}
        price, target = px.get(tk), num(thesis.get("entry_target"))
        target_src = "thesis" if target else None
        if not target and num((model.get(tk) or {}).get("entry_target")):
            target, target_src = num(model[tk]["entry_target"]), "model"
        gap = (price / target - 1) if price and target else None
        broke = (news.get(tk) or {}).get("tag") == "breaks" or n.get("health") == "red"
        signal = None
        if gap is not None:
            signal = "paused" if broke and gap <= 0.05 else ("buy" if gap <= 0 else ("near" if gap <= 0.05 else None))
        days = (dt.date.today() - n["level_since"]).days if n.get("level_since") else 0
        val = thesis.get("valuation") or {}
        rows.append({**n, "price": price, "target": target, "target_src": target_src, "gap": gap, "signal": signal,
                     "conviction": thesis.get("conviction"), "verdict": thesis.get("verdict"),
                     "news": news.get(tk), "alerts": al.get(tk), "weakens": weak.get(tk, 0), "days": days,
                     "iv_base": num(val.get("iv_base")), "analyst": an.get(tk)})
    return rows


@app.route("/watchlist")
def watchlist():
    rows = watch_rows("watchlist")
    pool = [r for r in rows if not r["is_core"]]
    book_themes = {t for r in q("SELECT themes FROM v2_names WHERE level = 'book'") for t in (r["themes"] or [])}
    for r in pool:
        conv = num(r["conviction"]) or 0
        gap = max(r["gap"] or 0, 0)
        r["penalty"] = ((100 - conv) * 0.4 + min(gap, 1) * 100 * 0.3
                        + min(r["weakens"], 5) * 20 * 0.2 + min(r["days"] / 180, 1) * 100 * 0.1)
        why = [f"conviction {conv:.0f}" if r["conviction"] is not None else "no conviction score"]
        if r["gap"] is not None and r["gap"] > 0.2:
            why.append(f"price {r['gap']:.0%} above entry target")
        if r["weakens"]:
            why.append(f"{r['weakens']} weakening news item(s) in 8 weeks")
        if r["days"] >= 180:
            why.append(f"{r['days']} days on the list")
        overlap = set(r.get("themes") or []) & book_themes
        if overlap:
            r["penalty"] += 10
            why.append(f"theme already held in the Book ({', '.join(sorted(overlap))})")
        r["why"] = "; ".join(why)
    weakest = sorted(pool, key=lambda r: -r["penalty"])[:5]
    # swap suggestions: this week's screen names scoring above the weakest non-core Watchlist name
    swaps = []
    best = lambda r: max(num(r.get("score")) or 0, num(r.get("growth_score")) or 0)   # value or growth score, whichever is higher
    scored = [r for r in pool if num(r.get("score")) is not None or num(r.get("growth_score")) is not None]
    if scored:
        low = min(scored, key=best)
        swaps = [dict(s, vs=low["ticker"], vs_score=best(low)) for s in q(
            """SELECT s.ticker, GREATEST(COALESCE(s.score, 0), COALESCE(s.growth_score, 0)) AS score,
                      CASE WHEN COALESCE(s.growth_score, 0) > COALESCE(s.score, 0) THEN 'growth' ELSE 'value' END AS track,
                      s.metrics->>'name' AS name FROM v2_screen_scores s
               JOIN v2_names n USING (ticker)
               WHERE s.run_date = (SELECT MAX(run_date) FROM v2_screen_scores) AND n.level = 'screen'
                 AND n.status <> 'rejected' AND GREATEST(COALESCE(s.score, 0), COALESCE(s.growth_score, 0)) > %s
               ORDER BY 2 DESC LIMIT 5""", (best(low),))]
    return page("watchlist", "Watchlist", WATCH_TPL, rows=rows, weakest=weakest, cap=WATCH_CAP, swaps=swaps,
                core=sum(1 for r in rows if r["is_core"]), core_cap=CORE_CAP)


# ------------------------------------------------------------------ Book
BOOK_TPL = r"""
<h1>Book</h1>
<p class="sub">{{ rows|length }} name{{ "" if rows|length == 1 else "s" }} owned (target about {{ target }}) · monitored daily: company releases, price, themes, analysts, kill criteria.</p>
<p class="muted" style="font-size:12px;margin-top:-8px"><b>Value</b> holdings: add at the entry target, review at Claude's base value, trim above the high value; held 2-4 years.
<b>Growth</b> holdings: add where the 5-year market-size case returns 15%+ a year, trim only when the price is beyond that case (under 5% a year); held 5-10 years and ended mainly by a broken thesis, not by valuation.</p>
{% if rows|length > target %}<div class="flash">The Book holds {{ rows|length }} names, above your target of {{ target }}.</div>{% endif %}
<div class="tablewrap"><table><thead><tr><th>Health</th><th>Ticker</th><th class="r">Price</th><th class="r">Cost</th>
<th class="r">P&amp;L</th><th>Style</th><th>Now</th><th class="r">Add at</th><th class="r">Trim at</th><th class="r">Claude base value</th><th class="r">Analyst mean</th>
<th class="r">Open alerts</th><th></th></tr></thead><tbody>
{% for r in rows %}<tr>
<td title="{{ r.health_why or '' }}"><span class="dot {{ r.health or '' }}"></span>{{ r.health or '—' }}{% if r.health_why %}<br><span class="muted" style="font-size:11px">{{ r.health_why }}</span>{% endif %}</td>
<td><a href="{{ url_for('name', ticker=r.ticker) }}"><b>{{ r.ticker }}</b></a><br><span class="muted">{{ r.name or '' }}</span></td>
<td class="r">{{ r.price|money }}</td><td class="r">{{ r.cost|money }}</td>
<td class="r {{ 'good' if r.pnl and r.pnl > 0 else ('bad' if r.pnl and r.pnl < 0) }}">{{ r.pnl|pct(true) }}</td>
<td><span class="chip {{ 'acc' if r.style == 'growth' else '' }}" title="Hold for {{ r.hold }}">{{ r.style }}</span>{% if r.style_how == 'auto' %}<br><span class="muted" style="font-size:11px">auto</span>{% endif %}</td>
<td>{% if r.state == 'add' %}<span class="chip good">Add zone</span>{% elif r.state == 'trim' %}<span class="chip warn">Trim zone</span>{% elif r.state == 'fair' %}<span class="chip warn">At fair value</span>{% else %}<span class="chip">Hold</span>{% endif %}
{% if r.tam_irr is not none %}<br><span class="muted" style="font-size:11px">5y case {{ r.tam_irr|pct }}/yr</span>{% endif %}</td>
<td class="r">{{ r.add_at|money }}</td><td class="r">{{ r.trim_at|money }}</td>
<td class="r">{{ r.iv_base|money }}</td>
<td class="r">{{ (r.analyst or {}).get('mean')|money }}</td><td class="r">{{ r.alerts or '' }}</td>
<td><form class="inline" method="post" action="{{ url_for('move', ticker=r.ticker) }}"><input type="hidden" name="to" value="watchlist">
<button title="Sold, but still like the business">Back to Watchlist</button></form></td>
</tr>{% else %}<tr><td colspan="13" class="empty">Nothing in the Book yet. Move a Watchlist name here when you buy it.</td></tr>{% endfor %}
</tbody></table></div>

<h2>Alerts</h2>
<div class="tablewrap"><table><thead><tr><th>When</th><th>Ticker</th><th>Severity</th><th>What happened</th><th>Source</th><th>Your action</th></tr></thead><tbody>
{% for a in alerts %}<tr>
<td class="muted" style="white-space:nowrap">{{ a.fired_at|d }}</td>
<td><a href="{{ url_for('name', ticker=a.ticker) }}">{{ a.ticker }}</a></td>
<td><span class="chip {{ {'high':'bad','medium':'warn'}.get(a.severity,'') }}">{{ a.severity }}</span></td>
<td>{{ a.kind or '' }}{% if a.detail %}: {{ a.detail }}{% endif %}</td><td class="muted">{{ a.source or '' }}</td>
<td style="white-space:nowrap">{% if a.addressed %}<span class="muted">{{ a.action or 'done' }}{% if a.note %}: {{ a.note }}{% endif %}</span>{% else %}
<form class="inline" method="post" action="{{ url_for('ack', alert_id=a.id) }}">
<select name="action"><option>acknowledged</option><option>acted</option><option>dismissed</option></select>
<input name="note" placeholder="Note"> <button>Save</button></form>{% endif %}</td>
</tr>{% else %}<tr><td colspan="6" class="empty">No alerts.</td></tr>{% endfor %}</tbody></table></div>
"""

HOLD = {"value": "2-4 years, or until the price reaches Claude's value",
        "growth": "5-10 years, while the moat and the industry keep growing; valuation alone rarely ends it"}


def name_style(n, thesis):
    if n.get("style") in ("value", "growth"):
        return n["style"], "set"
    has_tam = bool(((thesis or {}).get("valuation") or {}).get("tam"))
    auto = "growth" if has_tam and (num(n.get("growth_score")) or 0) > (num(n.get("score")) or 0) else "value"
    return auto, "auto"


def book_zones(r):
    """Style-aware add / trim levels, mirrored from the daily monitor (tools/monitor.py book_zones)."""
    th = q1("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (r["ticker"],)) or {}
    style, how = name_style(r, th)
    val = th.get("valuation") or {}
    price = r.get("price")
    out = {"style": style, "style_how": how, "hold": HOLD[style], "add_at": None, "trim_at": None, "state": "hold", "tam_irr": None}
    if style == "growth":
        five = [t for t in tam_calc(val.get("tam"), price) if t["years"] == 5]
        if five:
            out["add_at"], out["trim_at"] = five[0]["zone"], five[0]["future"] / 1.05 ** 5
            out["tam_irr"] = five[0]["irr"]
    else:
        out["add_at"], out["trim_at"] = num(th.get("entry_target")) or r.get("target"), num(val.get("iv_high"))
        out["review_at"] = num(val.get("iv_base"))
    if price and out["add_at"] and price <= out["add_at"]:
        out["state"] = "add"
    elif price and out["trim_at"] and price >= out["trim_at"]:
        out["state"] = "trim"
    elif style == "value" and price and out.get("review_at") and price >= out["review_at"]:
        out["state"] = "fair"
    return out


@app.post("/name/<ticker>/style")
def set_style(ticker):
    st = request.form.get("style")
    if st in ("value", "growth"):
        x("UPDATE v2_names SET style = %s, updated_at = now() WHERE ticker = %s", (st, ticker.upper()))
        x("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', %s)", (ticker.upper(), f"Tagged as {st}"))
        flash(f"{ticker.upper()} tagged as {st}.")
    return back(url_for("name", ticker=ticker.upper()))



@app.route("/book")
def book():
    rows = watch_rows("book")
    pos = {r["ticker"]: r for r in q("SELECT DISTINCT ON (ticker) * FROM v2_positions WHERE exited IS NULL "
                                     "ORDER BY ticker, entered DESC")}
    for r in rows:
        r["cost"] = num((pos.get(r["ticker"]) or {}).get("cost"))
        r["pnl"] = (r["price"] / r["cost"] - 1) if r["price"] and r["cost"] else None
        r["p_iv"] = (r["price"] / r["iv_base"]) if r["price"] and r["iv_base"] else None
        r.update(book_zones(r))
    alerts = q("SELECT a.* FROM v2_alerts a JOIN v2_names n USING (ticker) WHERE n.level IN ('book','watchlist') "
               "ORDER BY a.addressed, a.fired_at DESC LIMIT 100")
    return page("book", "Book", BOOK_TPL, rows=rows, alerts=alerts, target=BOOK_TARGET)


# ------------------------------------------------------------------ Name / thesis page
NAME_TPL = r"""
<div class="row" style="justify-content:space-between">
<div><h1>{{ n.ticker }} <span class="muted" style="font-weight:400">{{ (n.name or '')|cname }}</span>
<span class="chip {{ 'acc' if style == 'growth' }}" style="font-size:12px;vertical-align:middle">{{ style }}{% if style_how == 'auto' %} (auto){% endif %}</span></h1>
<form class="inline" method="post" action="{{ url_for('set_style', ticker=n.ticker) }}"><span class="muted" style="font-size:12px">Tag as</span>
<button name="style" value="value">Value</button><button name="style" value="growth">Growth</button></form>
<p class="sub"><span class="chip acc">{{ n.level }}</span><span class="chip">{{ n.status }}</span>{% if n.is_core %}<span class="chip acc">core</span>{% endif %}
{{ n.sector or '' }}{% if n.industry %} · {{ n.industry }}{% endif %} · last screened {{ n.last_screened|d }}</p></div>
<div class="row">
{% if n.level == 'screen' %}<form class="inline" method="post" action="{{ url_for('move', ticker=n.ticker) }}"><input type="hidden" name="to" value="watchlist"><button class="primary">Add to Watchlist</button></form>{% endif %}
{% if n.level == 'watchlist' %}<form class="inline" method="post" action="{{ url_for('move', ticker=n.ticker) }}"><input type="hidden" name="to" value="book"><button class="primary">Move to Book</button></form>{% endif %}
{% if n.level == 'book' %}<form class="inline" method="post" action="{{ url_for('move', ticker=n.ticker) }}"><input type="hidden" name="to" value="watchlist"><button>Back to Watchlist</button></form>{% endif %}
{% if n.level != 'screen' %}<form class="inline" method="post" action="{{ url_for('move', ticker=n.ticker) }}"><input type="hidden" name="to" value="screen">
<input name="reason" placeholder="Reason for rejecting" required> <button>Reject</button></form>{% endif %}
</div></div>

<div class="grid g4">
<div class="card tile"><div class="k">Price</div><div class="v">{{ price|money }}</div></div>
<div class="card tile"><div class="k">Claude base value (locked)</div><div class="v">{{ val.iv_base|money }}</div>
<div class="k">range {{ val.iv_low|money }} to {{ val.iv_high|money }}</div></div>
<div class="card tile"><div class="k">Entry target</div><div class="v">{{ (thesis or {}).get('entry_target')|money }}</div></div>
<div class="card tile"><div class="k">Analyst mean ({{ (analyst or {}).get('n') or '—' }})</div><div class="v">{{ (analyst or {}).get('mean')|money }}</div>
<div class="k">{{ (analyst or {}).get('low')|money }} to {{ (analyst or {}).get('high')|money }}</div></div>
<div class="card tile"><div class="k">Conviction · verdict</div><div class="v">{{ (thesis or {}).get('conviction')|n1 }}</div><div class="k">{{ (thesis or {}).get('verdict') or '—' }}</div></div>
{% if r %}<div class="card tile"><div class="k">Model value (weekly)</div><div class="v">{{ r.valuation.base|money }}</div>
<div class="k">bear {{ r.valuation.low|money }} · bull {{ r.valuation.high|money }}</div></div>
<div class="card tile"><div class="k">Margin of safety</div><div class="v {{ 'good' if r.valuation.current_mos is not none and r.valuation.current_mos >= r.valuation.required_mos else 'bad' }}">{{ r.valuation.current_mos|pct }}</div>
<div class="k">needs {{ r.valuation.required_mos|pct }} · model entry {{ r.valuation.entry_target|money }}</div></div>
<div class="card tile"><div class="k">Moat · risk</div><div class="v" style="font-size:18px">{{ r.moat.rating }} · {{ r.risk.rating }}</div>
<div class="k">{{ r.red_flags|length }} red flag{{ '' if r.red_flags|length == 1 else 's' }}</div></div>{% endif %}
</div>
{% if val.refresh %}{% set rf = val.refresh %}<div class="card" style="margin-top:12px" id="refresh"><h3>Latest results refresh <span class="muted" style="font-weight:400;font-size:13px">{{ rf.date }}</span></h3>
<p style="margin-top:0">{{ rf.summary }}</p>
{% if rf.previous %}<p class="muted" style="font-size:12px">Before: conviction {{ rf.previous.conviction|n1 }}, {{ rf.previous.verdict }}, entry {{ rf.previous.entry_target|money }}, base value {{ rf.previous.iv_base|money }}</p>{% endif %}
{% if rf.pillars %}<table><thead><tr><th>Pillar</th><th>Status</th><th>What the results said</th></tr></thead><tbody>
{% for p in rf.pillars %}<tr><td>{{ p.pillar }}</td><td><span class="chip {{ {'intact':'good','watch':'warn','breached':'bad'}.get((p.status or '')|lower, '') }}">{{ p.status }}</span></td><td>{{ p.note }}</td></tr>{% endfor %}</tbody></table>{% endif %}
</div>{% endif %}
{% if tam_rows %}<div class="card" style="margin-top:12px" id="tam"><h3>Growth target (TAM) <span class="muted" style="font-weight:400;font-size:13px">separate from the value entry target</span></h3>
<p class="muted" style="margin-top:0">{{ val.tam.market }}{% if val.tam.tam_now_bn %} · market today ${{ '{:,.0f}'.format(val.tam.tam_now_bn|float) }}B{% endif %}{% if val.tam.source %} · {{ val.tam.source }}{% endif %}</p>
<table><thead><tr><th>Horizon</th><th>Segments: market × share × net margin</th><th class="r">Revenue</th><th class="r">Earnings</th><th class="r">Exit P/E</th><th class="r">Value then</th><th class="r">Value today</th><th class="r">vs price</th><th class="r">Return a year from today's price</th></tr></thead><tbody>
{% for t in tam_rows %}<tr><td><b>{{ t.years }} years</b><br><span class="muted">{{ t.dr }}% discount · {{ '{:,.0f}'.format(t.shares_m|float) }}M shares</span></td>
<td>{% for g in t.rows %}<div>{{ g.name }}: ${{ '{:,.0f}'.format(g.tam_bn|float) }}B × {{ g.share_pct }}% × {{ g.net_margin_pct }}%</div>{% endfor %}{% if t.other_rev_bn %}<div class="muted">Other: ${{ t.other_rev_bn }}B at {{ t.other_margin_pct or 0 }}%</div>{% endif %}</td>
<td class="r">${{ '{:,.0f}'.format(t.rev_bn) }}B</td><td class="r">${{ '{:,.1f}'.format(t.ni_bn) }}B</td><td class="r">{{ t.exit_pe }}x</td>
<td class="r">{{ t.future|money }}</td><td class="r"><b>{{ t.today|money }}</b></td><td class="r {{ 'good' if t.vs_price and t.vs_price > 0 else 'bad' }}">{{ t.vs_price|pct(true) }}</td><td class="r">{{ t.irr|pct }}</td></tr>
{% if t.note %}<tr><td></td><td colspan="8" class="muted">{{ t.note }}</td></tr>{% endif %}{% endfor %}</tbody></table>
{% for t in tam_rows if t.years == 5 %}<p><b>Growth buy zone:</b> at or below <b>{{ t.zone|money }}</b> the 5-year case returns 15%+ a year{% if price %} · today {{ price|money }} ({{ (price / t.zone - 1)|pct(true) }} vs the zone){% endif %}. Watchlist names alert here; Claude re-checks the case overnight before it is sent.</p>{% endfor %}
{% if val.tam.why_share %}<p><b>Why this share is reachable:</b> {{ val.tam.why_share }}</p>{% endif %}
{% if val.tam.what_must_go_right %}<p><b>What must go right:</b> {{ val.tam.what_must_go_right }}</p>{% endif %}
</div>{% endif %}
{% if spark %}<div class="card" style="margin-top:12px">{{ spark }}</div>{% endif %}

{{ research }}
<div id="screen">{{ detail }}</div>
<div class="grid g2" style="margin-top:12px" id="scenarios">
<div class="card"><h3>Your scenarios</h3>
<p class="muted" style="margin-top:0">Claude's value stays fixed; these are yours. 10-year, two-stage DCF on free cash flow per share.</p>
<form method="post" action="{{ url_for('scenario', ticker=n.ticker) }}" id="sc">
<div class="kv">
<span class="k">Name</span><input name="name" value="My base case" required>
<span class="k">FCF per share now ($)</span><input name="fcf_ps" type="number" step="0.01" value="{{ inp.fcf_ps or '' }}" required>
<span class="k">Growth years 1–5 (%)</span><input name="g1" type="number" step="0.1" value="{{ inp.g1 or 8 }}">
<span class="k">Growth years 6–10 (%)</span><input name="g2" type="number" step="0.1" value="{{ inp.g2 or 5 }}">
<span class="k">Terminal growth (%)</span><input name="tg" type="number" step="0.1" value="{{ inp.tg or 2.5 }}">
<span class="k">Discount rate (%) · cost of equity</span><input name="dr" type="number" step="0.1" value="{{ inp.dr or 10 }}">
<span class="k">Value per share</span><b id="live">—</b></div>
<p><button class="primary">Save scenario</button></p></form>
<table><thead><tr><th>Scenario</th><th class="r">Value</th><th class="r">vs Claude</th><th class="r">vs price</th><th></th></tr></thead><tbody>
{% for s in scenarios %}<tr><td>{{ s.name }}<br><span class="muted">{{ s.inputs.g1 }}% / {{ s.inputs.g2 }}% growth, {{ s.inputs.dr }}% discount</span></td>
<td class="r"><b>{{ s.iv|money }}</b></td>
<td class="r">{% if val.iv_base %}{{ (s.iv|float / val.iv_base|float - 1)|pct(true) }}{% else %}—{% endif %}</td>
<td class="r">{% if price %}{{ (s.iv|float / price - 1)|pct(true) }}{% else %}—{% endif %}</td>
<td><form class="inline" method="post" action="{{ url_for('scenario_delete', sid=s.id) }}"><button>Delete</button></form></td></tr>
{% else %}<tr><td colspan="5" class="empty">No scenarios yet.</td></tr>{% endfor %}</tbody></table>
</div>
<div class="card"><h3>Recent alerts, news and filings</h3>
{% for a in alerts %}<div><span class="chip {{ {'high':'bad','medium':'warn'}.get(a.severity,'') }}">{{ a.severity }}</span> {{ a.fired_at|d }} · {{ a.kind }}{% if a.detail %}: {{ a.detail }}{% endif %}</div>{% endfor %}
{% for x in news %}<div><span class="chip {{ TAG_CLASS.get(x.tag,'') }}">{{ x.tag or 'news' }}</span> {{ x.published|d }} · <a href="{{ x.url }}" target="_blank" rel="noopener">{{ x.headline }}</a>{% if x.note %}<br><span class="muted">{{ x.note }}</span>{% endif %}</div>{% endfor %}
{% for e in events %}<div><span class="chip">{{ e.form }}</span> {{ e.filed|d }} · <a href="{{ e.url }}" target="_blank" rel="noopener">{{ e.kind or e.form }}</a>{% if e.detail %}: {{ e.detail }}{% endif %}</div>{% endfor %}
{% if not (alerts or news or events) %}<p class="empty">Nothing yet. The daily and weekly jobs fill this.</p>{% endif %}
</div></div>

<h2 id="thesis">Investment thesis {% if thesis %}<span class="muted" style="font-weight:400">v{{ thesis.version }} · {{ thesis.created_at|d }} · {{ thesis.approval }}</span>{% endif %}</h2>
{% if thesis and thesis.approval != 'approved' %}<form method="post" action="{{ url_for('approve', ticker=n.ticker) }}"><button class="primary">Approve this thesis</button></form>{% endif %}
{% if not thesis %}<p class="empty">No v2 thesis yet. It is written by the deep-dive task when this name is promoted, or on request.</p>{% endif %}
{% for group, secs in SECTIONS %}<div class="card" style="margin-top:12px"><h3>{{ group }}</h3>
{% for key, title in secs %}<div class="sec"><b>{{ title }}</b>
{% set body = (thesis or {}).get('sections', {}).get(key) %}
<div class="body">{% if body %}{{ body }}{% else %}<span class="empty">Not written yet</span>{% endif %}</div>
{% for c in comments.get(key, []) %}<div class="cmt">{{ c.body }} <span class="muted">· {{ c.created_at|d }}</span></div>{% endfor %}
<details><summary>Comment</summary><form method="post" action="{{ url_for('comment', ticker=n.ticker) }}">
<input type="hidden" name="section" value="{{ key }}"><textarea name="body" required></textarea><button>Save comment</button></form></details>
</div>{% endfor %}</div>{% endfor %}

{% if legacy %}<h2>Legacy thesis <span class="muted" style="font-weight:400">last updated {{ legacy.updated_date|d }} · needs review</span></h2>
<div class="card"><div class="kv">
{% for k,label in [('conviction_tier','Conviction'),('verdict','Verdict'),('intrinsic_value','Intrinsic value'),('target_price','Target price'),
('bear_target','Bear'),('bull_target','Bull'),('moat_type','Moat'),('investment_case','Investment case'),('buy_trigger','Buy trigger'),
('sell_trigger','Sell trigger'),('risk_factors','Risks'),('key_90d_metric','Key 90-day metric')] %}
{% if legacy.get(k) %}<span class="k">{{ label }}</span><span style="white-space:pre-wrap">{{ legacy[k] }}</span>{% endif %}{% endfor %}
</div></div>{% endif %}

{% if moves %}<h2>History</h2>{% for c in moves %}<div class="cmt">{{ c.created_at|d }} · {{ c.body }}</div>{% endfor %}{% endif %}

<script>
(function(){var f=document.getElementById('sc');if(!f)return;function v(n){return parseFloat(f.elements[n].value)}
function calc(){var p=v('fcf_ps'),g1=v('g1')/100,g2=v('g2')/100,tg=v('tg')/100,dr=v('dr')/100;
if(!(p>0)||!(dr>tg)){document.getElementById('live').textContent='—';return}
var pv=0,c=p;for(var y=1;y<=10;y++){c*=1+(y<=5?g1:g2);pv+=c/Math.pow(1+dr,y)}
pv+=c*(1+tg)/(dr-tg)/Math.pow(1+dr,10);document.getElementById('live').textContent='$'+pv.toFixed(2)}
f.addEventListener('input',calc);calc()})();
</script>
"""


def sparkline(pts, lines):
    """Inline SVG price line with optional horizontal reference lines."""
    if len(pts) < 2:
        return None
    vals = [p for p in pts] + [v for _, v, _ in lines if v]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    W, H, pad = 1000, 160, 6
    X = lambda i: pad + i * (W - 2 * pad) / (len(pts) - 1)
    Y = lambda v: H - pad - (v - lo) / (hi - lo) * (H - 2 * pad)
    path = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(pts))
    ref = "".join(
        f'<line x1="0" x2="{W}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--{c})" stroke-dasharray="5 5"/>'
        f'<text x="{W - 4}" y="{max(Y(v) - 4, 14):.1f}" text-anchor="end" fill="var(--{c})" font-size="12">{label} {money(v)}</text>'
        for label, v, c in lines if v)
    return Markup(f'<div class="muted" style="font-size:12px">Price, last {len(pts)} trading days</div>'
                  f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="none" role="img" aria-label="Price history">'
                  f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="{path}"/>{ref}</svg>')


def ensure_name(ticker):
    """Return the v2_names row; create it from the latest screen score if the stock was scored but never recorded."""
    n = q1("SELECT * FROM v2_names WHERE ticker = %s", (ticker,))
    if n:
        return n
    sc = q1("SELECT * FROM v2_screen_scores WHERE ticker = %s ORDER BY run_date DESC LIMIT 1", (ticker,))
    if not sc:
        return None
    m = sc["metrics"] or {}
    x("""INSERT INTO v2_names (ticker, name, sector, industry, level, status, first_seen, last_screened,
                               times_screened, score, flow_score, funnels)
         VALUES (%s,%s,%s,%s,'screen','new',%s,%s,1,%s,%s,%s) ON CONFLICT (ticker) DO NOTHING""",
      (ticker, m.get("name"), m.get("sector"), m.get("industry"), sc["run_date"], sc["run_date"],
       sc["score"], sc["flow_score"], psycopg2.extras.Json(sc["funnels"] or {})))
    return q1("SELECT * FROM v2_names WHERE ticker = %s", (ticker,))


SCREEN_DETAIL_TPL = r"""
{% if sc %}{% set f = sc.metrics.fin or {} %}{% set p = sc.metrics.px or {} %}
<h2>Latest screen <span class="muted" style="font-weight:400">{{ sc.run_date|d }} · score {{ sc.score|n1 }} · flow {{ sc.flow_score|n1 }} · fiscal year {{ f.fy }}</span></h2>
<div class="grid g2">
<div class="card"><h3>Key numbers</h3><div class="kv">
<span class="k">Market cap</span><span>{{ big(sc.metrics.mcap) }}</span>
<span class="k">Revenue · net income</span><span>{{ big(f.revenue) }} · {{ big(f.net_income) }}</span>
<span class="k">Margins (gross · operating · net)</span><span>{{ f.gross_margin|pct }} · {{ f.op_margin|pct }} · {{ f.net_margin|pct }}</span>
<span class="k">Revenue growth (1y · 5y CAGR)</span><span>{{ f.rev_growth_1y|pct(true) }} · {{ f.rev_cagr|pct(true) }}</span>
<span class="k">P/E · P/B · P/S</span><span>{{ fx(f.pe) }} · {{ fx(f.pb) }} · {{ fx(f.ps) }}</span>
<span class="k">EV/EBIT · EV/EBITDA · EV/Rev</span><span>{{ fx(f.ev_ebit,'x') }} · {{ fx(f.ev_ebitda,'x') }} · {{ fx(f.ev_rev,'x') }}</span>
<span class="k">FCF yield · FCF / net income (3y)</span><span>{{ f.fcf_yield|pct }} · {{ f.fcf_to_ni|pct }}</span>
<span class="k">ROE · ROIC (latest · 5y avg · 5y min)</span><span>{{ f.roe|pct }} · {{ f.roc_latest|pct }} · {{ f.roc_avg|pct }} · {{ f.roc_min|pct }}</span>
<span class="k">Debt/equity · net debt/EBITDA · interest cover</span><span>{% if f.debt_found == false %}<span class="warn">debt not found in filings</span>{% else %}{{ fx(f.de) }} · {{ fx(f.nd_ebitda,'x') }}{% endif %} · {{ fx(f.interest_cover,'x') }}</span>
<span class="k">Current ratio · shares 5y · shareholder yield</span><span>{{ fx(f.current_ratio) }} · {{ f.share_change_5y|pct(true) }} · {{ f.shareholder_yield|pct }}</span>
<span class="k">Graham · P/E value (15x) · Lynch 5y</span><span>{{ f.graham|money }} · {{ f.pe_value|money }} · {{ f.lynch_5y|money }}</span>
<span class="k">Price · 52w range · off high</span><span>{{ p.price|money }} · {{ p.low_52w|money }} to {{ p.high_52w|money }} · {{ p.drawdown|pct }}</span>
<span class="k">Returns (3m · 1y) · vs 200-day avg</span><span>{{ p.ret_3m|pct(true) }} · {{ p.ret_1y|pct(true) }} · {{ p.vs_ma200|pct(true) }}</span>
</div></div>
<div class="card"><h3>Gates and tests</h3>
<p class="muted" style="margin-top:0">Balance-sheet gates must all pass; then at least 55% of the value tests for the quant-value funnel.</p>
{% for k,v in (f.gates or {}).items() %}<div>{{ tick(v) }} {{ TEST_LABELS.get(k, k) }} <span class="muted">(gate)</span></div>{% endfor %}
{% for k,v in (f.tests or {}).items() %}<div>{{ tick(v) }} {{ TEST_LABELS.get(k, k) }}{% if v is none %} <span class="muted">(no data)</span>{% endif %}</div>{% endfor %}
<div style="margin-top:10px"><b>Funnels:</b> {{ funnel_chips(sc.funnels) or 'none' }}</div>
{% if p.capitulation_faded %}<div class="muted">Heavy selling (3x volume) has faded in the last 10 days.</div>{% endif %}
{% if p.sharp_drop_30d %}<div class="muted">Fell 10%+ in one day on 2x volume within 30 days.</div>{% endif %}
</div></div>{% endif %}
"""


def big(v):
    v = num(v)
    if v is None:
        return "—"
    a = abs(v)
    return f"${v/1e12:.2f}T" if a >= 1e12 else f"${v/1e9:.1f}B" if a >= 1e9 else f"${v/1e6:.0f}M"


def fx(v, suffix=""):
    v = num(v)
    return "—" if v is None else f"{v:.1f}{suffix}"


TEST_LABELS = {
    "current_ratio_ge_1": "Current ratio at least 1.0", "net_debt_ebitda_le_3": "Net debt under 3x EBITDA",
    "interest_cover_ge_4": "Interest covered at least 4x", "fcf_positive_2_of_3": "Free cash flow positive in 2 of last 3 years",
    "equity_positive": "Positive shareholders' equity", "profitable_2_of_3": "Profitable in 2 of last 3 years",
    "pe_lt_20": "P/E below 20", "pb_lt_3": "P/B below 3", "roc_avg_gt_12": "ROIC (5y avg) above 12%",
    "rev_cagr_gt_5": "Revenue growth (5y) above 5% a year", "shares_not_rising": "Share count flat or falling",
    "ev_ebit_lt_15": "EV/EBIT below 15", "fcf_yield_gt_5": "FCF yield above 5%",
    "gross_margin_stable": "Gross margin stable (within 5 points)", "debt_equity_lt_1": "Debt/equity below 1",
}


def tick(v):
    return Markup('<span class="good">✓</span>' if v is True else '<span class="bad">✕</span>' if v is False
                  else '<span class="muted">–</span>')


app.jinja_env.globals.update(big=big, fx=fx, tick=tick, TEST_LABELS=TEST_LABELS)


@app.route("/name/<ticker>")
def name(ticker):
    ticker = ticker.upper()
    n = ensure_name(ticker)
    if not n:
        abort(404)
    thesis = q1("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (ticker,))
    val = (thesis or {}).get("valuation") or {}
    analyst = q1("SELECT * FROM v2_analyst WHERE ticker = %s ORDER BY date DESC LIMIT 1", (ticker,))
    price = latest_prices([ticker]).get(ticker)
    hist = [float(r["close"]) for r in q("SELECT close FROM (SELECT date, close FROM v2_prices WHERE ticker = %s "
                                          "ORDER BY date DESC LIMIT 260) s ORDER BY date", (ticker,))]
    spark = sparkline(hist, [("Entry", num((thesis or {}).get("entry_target")), "good"),
                             ("Claude value", num(val.get("iv_base")), "warn")])
    cm = {}
    moves = []
    for c in q("SELECT * FROM v2_comments WHERE ticker = %s ORDER BY created_at", (ticker,)):
        (moves.append(c) if c["section"] == "move" else cm.setdefault(c["section"], []).append(c))
    sc = q1("SELECT * FROM v2_screen_scores WHERE ticker = %s ORDER BY run_date DESC LIMIT 1", (ticker,))
    r = latest_research(ticker)
    inp = dict(val.get("inputs") or {})
    if not inp.get("fcf_ps") and r and (r.get("valuation") or {}).get("inputs_for_scenario", {}).get("fcf_ps"):
        inp = dict(r["valuation"]["inputs_for_scenario"])
    if not inp.get("fcf_ps") and sc:
        fin = (sc["metrics"] or {}).get("fin") or {}
        if fin.get("fcf") and fin.get("shares"):
            inp["fcf_ps"] = round(fin["fcf"] / fin["shares"], 2)
        if fin.get("rev_cagr") is not None:
            inp["g1"] = round(min(max(fin["rev_cagr"] * 100, 0), 12), 1)
    return page(n["level"], ticker, NAME_TPL, detail=Markup(render_template_string(SCREEN_DETAIL_TPL, sc=sc)),
                r=r, research=Markup(render_template_string(RESEARCH_TPL, r=r, ticker=ticker)), n=n, thesis=thesis, val=val, analyst=analyst, price=price,
                spark=spark, comments=cm, moves=moves, inp=inp, SECTIONS=SECTIONS,
                scenarios=q("SELECT * FROM v2_scenarios WHERE ticker = %s ORDER BY created_at DESC", (ticker,)),
                alerts=q("SELECT * FROM v2_alerts WHERE ticker = %s ORDER BY fired_at DESC LIMIT 10", (ticker,)),
                news=q("SELECT * FROM v2_news WHERE ticker = %s ORDER BY published DESC NULLS LAST LIMIT 8", (ticker,)),
                events=q("SELECT * FROM v2_events WHERE ticker = %s ORDER BY filed DESC LIMIT 8", (ticker,)),
                legacy=(n.get("legacy") or {}).get("thesis"), tam_rows=tam_calc(val.get("tam"), price),
                style=name_style(n, thesis)[0], style_how=name_style(n, thesis)[1])


# ------------------------------------------------------------------ actions
def back(default):
    return redirect(request.referrer or default)


@app.post("/name/<ticker>/move")
def move(ticker):
    ticker, to = ticker.upper(), request.form.get("to")
    reason = (request.form.get("reason") or "").strip()
    n = ensure_name(ticker)
    if not n or to not in ("screen", "watchlist", "book"):
        abort(400)
    if to == "watchlist" and n["level"] == "screen":
        cnt = q1("SELECT COUNT(*) AS c FROM v2_names WHERE level = 'watchlist'")["c"]
        if cnt >= WATCH_CAP:
            flash(f"The Watchlist already holds {cnt} names (cap {WATCH_CAP}). Remove names first; "
                  "suggestions are on the Watchlist page.")
            return back(url_for("screen"))
    if to == "screen" and n["level"] != "screen" and not reason:
        flash("Please give a reason when taking a name off the Watchlist or Book.")
        return back(url_for("watchlist"))
    status = "rejected" if to == "screen" and n["level"] != "screen" else (
        "holding" if to == "book" else ("exited" if n["level"] == "book" else "watching") if to == "watchlist" else n["status"])
    x("UPDATE v2_names SET level = %s, status = %s, level_since = CURRENT_DATE, is_core = (is_core AND %s <> 'screen'), "
      "reject_reason = CASE WHEN %s = 'rejected' THEN %s ELSE reject_reason END, updated_at = now() WHERE ticker = %s",
      (to, status, to, status, reason, ticker))
    x("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', %s)",
      (ticker, f"Moved {n['level']} → {to}" + (f": {reason}" if reason else "")))
    if status == "rejected":
        x("ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS reject_price NUMERIC")
        x("UPDATE v2_names SET reject_price = %s WHERE ticker = %s", (latest_prices([ticker]).get(ticker), ticker))
    if to == "book" and (q1("SELECT COUNT(*) AS c FROM v2_names WHERE level = 'book'")["c"] > BOOK_TARGET):
        flash(f"The Book is now above your target of {BOOK_TARGET} names.")
    if to == "watchlist" and n["level"] == "screen" and not q1("SELECT 1 AS x FROM v2_theses WHERE ticker = %s LIMIT 1", (ticker,)):
        flash(f"{ticker} is on the Watchlist without a v2 thesis yet. A deep dive has been requested.")
        x("UPDATE v2_names SET status = 'deep_dive_requested' WHERE ticker = %s", (ticker,))
    return back(url_for("name", ticker=ticker))


@app.post("/name/<ticker>/pin")
def pin(ticker):
    ticker = ticker.upper()
    n = q1("SELECT is_core FROM v2_names WHERE ticker = %s", (ticker,))
    if not n:
        abort(404)
    if not n["is_core"] and q1("SELECT COUNT(*) AS c FROM v2_names WHERE is_core")["c"] >= CORE_CAP:
        flash(f"You already have {CORE_CAP} core names. Unpin one first.")
        return back(url_for("watchlist"))
    x("UPDATE v2_names SET is_core = NOT is_core WHERE ticker = %s", (ticker,))
    return back(url_for("watchlist"))


@app.post("/name/<ticker>/deepdive")
def deepdive(ticker):
    if not ensure_name(ticker.upper()):
        abort(404)
    x("UPDATE v2_names SET status = 'deep_dive_requested', updated_at = now() WHERE ticker = %s", (ticker.upper(),))
    flash(f"Deep dive requested for {ticker.upper()}. The next deep-dive task picks it up.")
    return back(url_for("screen"))


@app.post("/name/<ticker>/approve")
def approve(ticker):
    x("UPDATE v2_theses SET approval = 'approved' WHERE id = (SELECT id FROM v2_theses WHERE ticker = %s "
      "ORDER BY version DESC LIMIT 1)", (ticker.upper(),))
    return back(url_for("name", ticker=ticker))


@app.post("/name/<ticker>/comment")
def comment(ticker):
    body = (request.form.get("body") or "").strip()
    if body:
        x("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, %s, %s)",
          (ticker.upper(), request.form.get("section") or "general", body))
    return back(url_for("name", ticker=ticker))


@app.post("/name/<ticker>/scenario")
def scenario(ticker):
    f = request.form
    inputs = {k: num(f.get(k)) for k in ("fcf_ps", "g1", "g2", "tg", "dr")}
    if None in inputs.values():
        flash("Fill in every scenario input.")
        return back(url_for("name", ticker=ticker))
    iv = dcf_iv(**inputs)
    if iv is None:
        flash("The discount rate must be higher than terminal growth.")
        return back(url_for("name", ticker=ticker))
    x("INSERT INTO v2_scenarios (ticker, name, inputs, iv) VALUES (%s, %s, %s, %s)",
      (ticker.upper(), (f.get("name") or "Scenario").strip(), psycopg2.extras.Json(inputs), round(iv, 2)))
    return back(url_for("name", ticker=ticker))


@app.post("/scenario/<int:sid>/delete")
def scenario_delete(sid):
    x("DELETE FROM v2_scenarios WHERE id = %s", (sid,))
    return back(url_for("screen"))


@app.post("/alert/<int:alert_id>/ack")
def ack(alert_id):
    action = request.form.get("action") if request.form.get("action") in ("acknowledged", "acted", "dismissed") else "acknowledged"
    x("UPDATE v2_alerts SET addressed = TRUE, action = %s, note = %s WHERE id = %s",
      (action, (request.form.get("note") or "").strip() or None, alert_id))
    return back(url_for("book"))


# ------------------------------------------------------------------ Decide: one list of everything that needs a decision
SIZING = {"value": {"starter": 0.03, "full": 0.05}, "growth": {"starter": 0.02, "full": 0.06}}
LIMITS = {"name": 0.10, "theme": 0.35, "style": 0.80}
ACTIONS = {  # alert kind -> (action, order)
    "Buy signal confirmed": ("BUY", 1), "Buy signal": ("BUY", 1), "Old idea at buy level": ("RE-CHECK", 6),
    "Add zone": ("ADD", 2), "Add zone, thesis flagged": ("REVIEW", 3), "Trim zone": ("TRIM", 2),
    "Fair value reached": ("REVIEW", 4), "Kill criterion hit": ("EXIT?", 0), "Buy signal, thesis weakened": ("REVIEW", 3),
    "Buy signal paused": ("REVIEW", 4), "Thesis refreshed after results": ("APPROVE", 5),
    "Screen idea on sale": ("LOOK", 7), "Earnings preview": ("PREPARE", 5),
}


def portfolio():
    """Positions with live weights by name, theme and style; cash from settings."""
    pos = q("""SELECT DISTINCT ON (p.ticker) p.*, n.themes, n.style, n.score, n.growth_score, n.name FROM v2_positions p
               JOIN v2_names n USING (ticker) WHERE p.exited IS NULL AND n.level = 'book' ORDER BY p.ticker, p.entered DESC""")
    px = latest_prices([p["ticker"] for p in pos])
    st = q1("SELECT value FROM v2_settings WHERE key = 'portfolio'") or {}
    cash = num((st.get("value") or {}).get("cash")) or 0
    rows, total = [], cash
    for p in pos:
        th = q1("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (p["ticker"],)) or {}
        mv = (num(p["shares"]) or 0) * (px.get(p["ticker"]) or 0)
        total += mv
        rows.append({**p, "mv": mv, "price": px.get(p["ticker"]), "style": name_style(p, th)[0], "conviction": num(th.get("conviction")),
                     "themes": p.get("themes") or []})
    by_theme, by_style = {}, {}
    for r in rows:
        r["w"] = r["mv"] / total if total else 0
        for t in r["themes"] or ["(no theme)"]:
            by_theme[t] = by_theme.get(t, 0) + r["w"] / max(len(r["themes"] or [1]), 1)
        by_style[r["style"]] = by_style.get(r["style"], 0) + r["w"]
    warn = [f"{r['ticker']} is {r['w']:.0%} of the portfolio (limit {LIMITS['name']:.0%})" for r in rows if r["w"] > LIMITS["name"]]
    warn += [f"{t} is {w:.0%} (theme limit {LIMITS['theme']:.0%})" for t, w in by_theme.items() if w > LIMITS["theme"]]
    warn += [f"{s.title()} holdings are {w:.0%} (limit {LIMITS['style']:.0%})" for s, w in by_style.items() if w > LIMITS["style"]]
    return {"rows": sorted(rows, key=lambda r: -r["mv"]), "total": total, "cash": cash, "by_theme": by_theme,
            "by_style": by_style, "warn": warn}


def size_for(ticker, style, conviction, pf, price):
    """Suggested amount to buy or add: target weight from style and conviction, capped by the name and theme limits."""
    s = SIZING.get(style, SIZING["value"])
    c = conviction or 0
    target = s["full"] if c >= 75 else (s["starter"] + s["full"]) / 2 if c >= 60 else s["starter"]
    cur_w = next((r["w"] for r in pf["rows"] if r["ticker"] == ticker), 0)
    n = q1("SELECT themes FROM v2_names WHERE ticker = %s", (ticker,)) or {}
    room = [LIMITS["name"] - cur_w]
    for t in n.get("themes") or []:
        room.append(LIMITS["theme"] - pf["by_theme"].get(t, 0))
    add_w = max(0.0, min(target - cur_w, *room))
    amt = add_w * pf["total"] if pf["total"] else None
    why = f"{style} · conviction {c:.0f} → target {target:.0%} of the portfolio, now {cur_w:.1%}"
    if add_w <= 0 and target > cur_w:
        why += " · no room: a name or theme limit is already reached (trim elsewhere first, or accept the extra concentration)"
    elif add_w < target - cur_w:
        why += " · reduced to stay within a portfolio limit"
    return {"target_w": target, "cur_w": cur_w, "add_w": add_w, "amount": amt,
            "shares": int(amt // price) if amt and price else None, "why": why}


DECIDE_TPL = r"""
<style>.dec{display:grid;gap:10px}.dec .card{display:grid;grid-template-columns:110px 1fr 320px;gap:14px;align-items:start}
.act{font-weight:700;font-size:13px;padding:6px 8px;border-radius:8px;text-align:center;background:var(--panel2)}
.act.BUY,.act.ADD{background:rgba(46,160,67,.18);color:var(--good)}.act.TRIM,.act.EXIT\?{background:rgba(248,81,73,.15);color:var(--bad)}
.act.REVIEW,.act.APPROVE,.act.PREPARE{background:rgba(210,153,34,.15);color:var(--warn)}
.kv{display:grid;grid-template-columns:repeat(4,auto);gap:2px 14px;font-size:12px;margin-top:6px}.kv b{font-size:13px}
.wbar{display:grid;grid-template-columns:200px 1fr 50px;gap:8px;align-items:center;font-size:13px;margin:3px 0}
.wbar .b{height:12px;border-radius:4px;background:var(--accent)}.wbar .b.over{background:var(--bad)}
@media (max-width:800px){.dec .card{grid-template-columns:1fr}}</style>
<h1>Decide</h1>
<p class="sub">Everything that needs a decision, most urgent first. Each item shows why, the numbers that matter, Claude's check and a suggested size. Done or Skip records it in the decision journal.</p>
{% if pf.warn %}<div class="flash">⚠ Portfolio limits: {{ pf.warn|join(' · ') }}</div>{% endif %}
<div class="dec">
{% for d in items %}<div class="card">
<div><div class="act {{ d.action }}">{{ d.action }}</div><div class="muted" style="font-size:11px;margin-top:6px">{{ d.when|d }}</div></div>
<div><b><a href="{{ url_for('name', ticker=d.ticker) }}">{{ d.ticker }}</a></b> <span class="muted">{{ (d.name or '')|cname }}</span>
{% if d.style %}<span class="chip {{ 'acc' if d.style == 'growth' }}">{{ d.style }}</span>{% endif %} <span class="chip">{{ d.level }}</span>
<div style="margin-top:4px">{{ d.kind }}: {{ d.detail }}</div>
<div class="kv"><span>Price<br><b>{{ d.price|money }}</b></span><span>{{ 'Growth zone' if d.style == 'growth' else 'Entry target' }}<br><b>{{ d.buy_at|money }}</b></span>
<span>Claude base value<br><b>{{ d.iv_base|money }}</b></span><span>Conviction<br><b>{{ d.conviction|n1 }}</b></span></div>
{% if d.check %}<div style="font-size:12px;margin-top:6px"><b>Claude's check:</b> {{ d.check }}</div>{% endif %}
{% if d.size %}<div style="font-size:12px;margin-top:6px"><b>Suggested size:</b> {% if d.size.amount %}${{ '{:,.0f}'.format(d.size.amount) }}{% if d.size.shares %} (~{{ d.size.shares }} shares){% endif %}{% elif d.size.add_w <= 0 %}none{% else %}add positions to see amounts{% endif %}
<span class="muted">· {{ d.size.why }}</span></div>{% endif %}
{% if d.trim_note %}<div style="font-size:12px;margin-top:6px"><b>Trim guide:</b> {{ d.trim_note }}</div>{% endif %}
</div>
<form method="post" action="{{ url_for('decide_record') }}"><input type="hidden" name="alert_id" value="{{ d.id or '' }}"><input type="hidden" name="ticker" value="{{ d.ticker }}"><input type="hidden" name="action" value="{{ d.action }}"><input type="hidden" name="price" value="{{ d.price or '' }}">
<div class="row"><input name="shares" placeholder="Shares (if traded)" style="width:130px"><input name="note" placeholder="Why (for the journal)" style="flex:1;min-width:150px"></div>
<div class="row" style="margin-top:6px"><button class="primary" name="decision" value="done">Done</button><button name="decision" value="skipped">Skip</button><button name="decision" value="later">Later</button></div></form>
</div>{% else %}<p class="empty">Nothing needs a decision right now.</p>{% endfor %}
</div>

<h2>Portfolio</h2>
<div class="grid g2"><div class="card"><h3>By name</h3>
{% for r in pf.rows %}<div class="wbar"><span><a href="{{ url_for('name', ticker=r.ticker) }}">{{ r.ticker }}</a> <span class="muted">{{ r.style }}</span></span><div><div class="b {{ 'over' if r.w > limits.name }}" style="width:{{ (r.w / 0.15 * 100)|round(1) if r.w < 0.15 else 100 }}%"></div></div><b>{{ r.w|pct }}</b></div>
{% else %}<p class="empty">Add shares and costs to your Book to see weights.</p>{% endfor %}
<p class="muted" style="font-size:12px">Total ${{ '{:,.0f}'.format(pf.total) }} incl. cash ${{ '{:,.0f}'.format(pf.cash) }}.</p>
<form method="post" action="{{ url_for('set_cash') }}" class="row"><input name="cash" placeholder="Cash available (USD)" value="{{ pf.cash|int if pf.cash else '' }}"><button>Save cash</button></form></div>
<div class="card"><h3>By theme and style</h3>
{% for t, w in pf.by_theme.items()|sort(attribute='1', reverse=True) %}<div class="wbar"><span>{{ t }}</span><div><div class="b {{ 'over' if w > limits.theme }}" style="width:{{ (w * 100)|round(1) }}%"></div></div><b>{{ w|pct }}</b></div>{% endfor %}
<hr>{% for s, w in pf.by_style.items() %}<div class="wbar"><span>{{ s|title }}</span><div><div class="b {{ 'over' if w > limits.style }}" style="width:{{ (w * 100)|round(1) }}%"></div></div><b>{{ w|pct }}</b></div>{% endfor %}
<p class="muted" style="font-size:12px">Sizing: value starter {{ sizing.value.starter|pct }}, full {{ sizing.value.full|pct }}; growth starter {{ sizing.growth.starter|pct }}, full {{ sizing.growth.full|pct }} (full at conviction 75+, half-way at 60-74). Limits: {{ limits.name|pct }} a name, {{ limits.theme|pct }} a theme, {{ limits.style|pct }} a style.</p></div></div>

<h2>Decision journal</h2>
<div class="tablewrap"><table><thead><tr><th>Date</th><th>Ticker</th><th>Action</th><th>Decision</th><th class="r">Price then</th><th class="r">Shares</th><th class="r">Since</th><th class="r">vs S&amp;P</th><th>Note</th></tr></thead><tbody>
{% for j in journal %}<tr><td class="muted">{{ j.decided_at|d }}</td><td><a href="{{ url_for('name', ticker=j.ticker) }}">{{ j.ticker }}</a></td><td>{{ j.action }}</td><td>{{ j.decision }}</td>
<td class="r">{{ j.price|money }}</td><td class="r">{{ j.shares or '' }}</td><td class="r {{ 'good' if j.ret and j.ret > 0 else 'bad' if j.ret }}">{{ j.ret|pct(true) }}</td><td class="r {{ 'good' if j.rel and j.rel > 0 else 'bad' if j.rel }}">{{ j.rel|pct(true) }}</td><td class="muted">{{ j.note or '' }}</td></tr>
{% else %}<tr><td colspan="9" class="empty">Decisions you record appear here, scored against the S&amp;P over time.</td></tr>{% endfor %}</tbody></table></div>
<p class="muted" style="font-size:12px">"Since" is the stock's move since the decision; for a skip, a good skip is a negative number. Reviewed in the Sunday digest at 6 and 12 months.</p>
"""


@app.route("/decide")
def decide():
    pf = portfolio()
    al = q("""SELECT a.*, n.name, n.level, n.style, n.score, n.growth_score FROM v2_alerts a JOIN v2_names n USING (ticker)
              WHERE NOT a.addressed AND a.kind = ANY(%s) AND a.fired_at > now() - interval '21 days'
                AND COALESCE(a.sent_via, '') <> 'hold' ORDER BY a.fired_at DESC""", (list(ACTIONS),))
    seen, items = set(), []
    px = latest_prices(list({a["ticker"] for a in al}))
    for a in al:
        act, order = ACTIONS[a["kind"]]
        if (a["ticker"], act) in seen:
            continue
        seen.add((a["ticker"], act))
        th = q1("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (a["ticker"],)) or {}
        val = th.get("valuation") or {}
        style = name_style(a, th)[0]
        price = px.get(a["ticker"])
        five = [t for t in tam_calc(val.get("tam"), price) if t["years"] == 5]
        buy_at = five[0]["zone"] if style == "growth" and five else num(th.get("entry_target"))
        chk = q1("""SELECT kind, detail FROM v2_alerts WHERE ticker = %s AND kind IN ('Buy signal confirmed','Buy signal, thesis weakened')
                    ORDER BY fired_at DESC LIMIT 1""", (a["ticker"],))
        d = {**a, "action": act, "order": order, "when": a["fired_at"], "price": price, "style": style, "buy_at": buy_at,
             "iv_base": num(val.get("iv_base")), "conviction": num(th.get("conviction")),
             "check": (chk["detail"] if chk else None)}
        if act in ("BUY", "ADD"):
            d["size"] = size_for(a["ticker"], style, d["conviction"], pf, price)
        if act == "TRIM":
            cur_w = next((r["w"] for r in pf["rows"] if r["ticker"] == a["ticker"]), 0)
            d["trim_note"] = (f"now {cur_w:.1%} of the portfolio; " +
                              ("growth holding: trim a third, keep a core position while the thesis holds" if style == "growth"
                               else "value holding: trim half above the high value, sell the rest if the value has not grown"))
        items.append(d)
    pend = q("""SELECT DISTINCT ON (t.ticker) t.ticker, t.created_at, t.verdict, t.conviction, n.name, n.level, n.style
                FROM v2_theses t JOIN v2_names n USING (ticker)
                WHERE t.approval = 'pending' AND n.level IN ('watchlist','book') ORDER BY t.ticker, t.version DESC""")
    ppx = latest_prices([p["ticker"] for p in pend])
    for p in pend:
        if (p["ticker"], "APPROVE") in seen:
            continue
        th = q1("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (p["ticker"],)) or {}
        val = th.get("valuation") or {}
        sty = name_style(p, th)[0]
        five = [t for t in tam_calc(val.get("tam"), ppx.get(p["ticker"])) if t["years"] == 5]
        items.append({"ticker": p["ticker"], "name": p["name"], "level": p["level"], "style": p["style"], "action": "APPROVE",
                      "order": 5, "when": p["created_at"], "kind": "Thesis waiting for approval",
                      "detail": f"{p['verdict']} · conviction {num(p['conviction']) or 0:.0f}. Read it, comment, then approve",
                      "price": ppx.get(p["ticker"]), "style": sty,
                      "buy_at": five[0]["zone"] if sty == "growth" and five else num(th.get("entry_target")),
                      "iv_base": num(val.get("iv_base")), "conviction": num(p["conviction"])})
    items.sort(key=lambda d: (d["order"], -(d["when"].timestamp() if hasattr(d["when"], "timestamp") else 0)))
    journal = q("SELECT * FROM v2_decisions ORDER BY decided_at DESC LIMIT 60")
    jpx = latest_prices(list({j["ticker"] for j in journal}) + ["SPY"])
    for j in journal:
        p0, p1 = num(j["price"]), jpx.get(j["ticker"])
        j["ret"] = (p1 / p0 - 1) if p0 and p1 else None
        s0, s1 = num(j.get("spy_price")), jpx.get("SPY")
        j["rel"] = (j["ret"] - (s1 / s0 - 1)) if j["ret"] is not None and s0 and s1 else None
    return page("decide", "Decide", DECIDE_TPL, items=items, pf=pf, journal=journal, limits=LIMITS, sizing=SIZING)


@app.post("/decide/record")
def decide_record():
    f = request.form
    t, act, dec = f.get("ticker", "").upper(), f.get("action"), f.get("decision")
    if dec == "later":
        return back(url_for("decide"))
    shares = num(f.get("shares"))
    price = num(f.get("price"))
    spy = latest_prices(["SPY"]).get("SPY")
    x("""INSERT INTO v2_decisions (ticker, action, decision, price, shares, note, alert_id, spy_price)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (t, act, dec, price, shares, (f.get("note") or "").strip() or None,
                                              num(f.get("alert_id")), spy))
    if f.get("alert_id"):
        x("UPDATE v2_alerts SET addressed = TRUE, action = %s, note = %s WHERE id = %s",
          ("acted" if dec == "done" else "dismissed", f.get("note") or None, int(f["alert_id"])))
    if dec == "done" and act == "APPROVE":
        x("""UPDATE v2_theses SET approval = 'approved' WHERE ticker = %s
             AND version = (SELECT MAX(version) FROM v2_theses WHERE ticker = %s)""", (t, t))
    if dec == "done" and shares and price and act in ("BUY", "ADD", "TRIM", "EXIT?"):
        p = q1("SELECT * FROM v2_positions WHERE ticker = %s AND exited IS NULL ORDER BY entered DESC LIMIT 1", (t,))
        if act in ("BUY", "ADD"):
            if p:
                old_sh, old_c = num(p["shares"]) or 0, num(p["cost"]) or price
                new_sh = old_sh + shares
                x("UPDATE v2_positions SET shares = %s, cost = %s WHERE id = %s",
                  (new_sh, (old_sh * old_c + shares * price) / new_sh, p["id"]))
            else:
                x("INSERT INTO v2_positions (ticker, entered, shares, cost, notes) VALUES (%s, CURRENT_DATE, %s, %s, 'Decide page')",
                  (t, shares, price))
            if (q1("SELECT level FROM v2_names WHERE ticker = %s", (t,)) or {}).get("level") != "book":
                x("UPDATE v2_names SET level = 'book', status = 'holding', level_since = CURRENT_DATE WHERE ticker = %s", (t,))
                x("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', 'Moved → book (bought, Decide page)')", (t,))
        elif p:
            left = (num(p["shares"]) or 0) - shares
            if left > 0:
                x("UPDATE v2_positions SET shares = %s WHERE id = %s", (left, p["id"]))
            else:
                x("UPDATE v2_positions SET exited = CURRENT_DATE WHERE id = %s", (p["id"],))
                x("UPDATE v2_names SET level = 'watchlist', status = 'exited', level_since = CURRENT_DATE WHERE ticker = %s", (t,))
                x("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', 'Moved book → watchlist (sold, Decide page)')", (t,))
    flash(f"{t}: {act} recorded as {dec}.")
    return back(url_for("decide"))


@app.post("/decide/cash")
def set_cash():
    cash = num((request.form.get("cash") or "").replace(",", "")) or 0
    x("""INSERT INTO v2_settings (key, value) VALUES ('portfolio', %s)
         ON CONFLICT (key) DO UPDATE SET value = v2_settings.value || EXCLUDED.value""", (psycopg2.extras.Json({"cash": cash}),))
    return back(url_for("decide"))


# ------------------------------------------------------------------ Outlook: macro regime, what-if, calendar, week ahead
OUTLOOK_TPL = r"""
<style>.tiles{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.tile span{font-size:12px;color:var(--mute)}
.tile b{display:block;font-size:18px}.tile i{font-style:normal;font-size:12px}
.cal td{vertical-align:top}.day{white-space:nowrap;font-weight:600}.k{font-size:11px;padding:1px 6px;border-radius:6px;background:var(--chip)}
.up{color:var(--good)}.dn{color:var(--bad)}</style>
<h1>Outlook</h1>
<p class="sub">What could move your stocks next: the macro backdrop, how your names have reacted to rates, the dollar, oil and the market, and the event calendar.{% if m %} Updated {{ m.as_of }}.{% endif %}</p>
{% if wa and wa.summary %}<h2>Week ahead <span class="muted" style="font-weight:400;font-size:13px">by Claude, {{ wa.as_of }}</span></h2>
<div class="card"><p style="margin-top:0">{{ wa.summary }}</p>
{% for e in wa.events or [] %}<div style="margin:6px 0"><b>{{ e.date }} · {{ e.title }}</b>{% if e.names %} <span class="muted">({{ e.names|join(', ') }})</span>{% endif %}<br>
<span class="muted">{{ e.why }}</span>{% if e.playbook %}<br>→ {{ e.playbook }}{% endif %}</div>{% endfor %}</div>{% endif %}
{% if m %}{% set r = m.regime %}
<h2>Macro backdrop</h2>
{% if m.flags %}<div class="flash">⚠ {{ m.flags|join(' · ') }}</div>{% endif %}
<div class="tiles">
<div class="tile"><span>S&amp;P 500</span><b>{{ '{:,.0f}'.format(r.spx) if r.spx }}</b><i class="{{ 'up' if (r.spx_vs_200d or 0) > 0 else 'dn' }}">{{ r.spx_vs_200d|pct(true) }} vs 200-day</i></div>
<div class="tile"><span>VIX (fear gauge)</span><b>{{ '%.1f'|format(r.vix) if r.vix }}</b><i>{{ 'stressed' if r.vix and r.vix > 25 else 'calm' if r.vix and r.vix < 15 else 'normal' }}</i></div>
<div class="tile"><span>US 10-year yield</span><b>{{ '%.2f%%'|format(r.y10) if r.y10 }}</b><i class="{{ 'dn' if (r.y10_chg_1m or 0) > 0 else 'up' }}">{{ ('%+.2fpt'|format(r.y10_chg_1m)) if r.y10_chg_1m is not none }} in a month</i></div>
<div class="tile"><span>3-month bill</span><b>{{ '%.2f%%'|format(r.y3m) if r.y3m }}</b><i>proxy for the Fed rate</i></div>
<div class="tile"><span>Curve (10y − 3m)</span><b>{{ ('%+.2fpt'|format(r.curve_10y_3m)) if r.curve_10y_3m is not none }}</b><i>{{ 'inverted: recession signal' if r.curve_10y_3m is not none and r.curve_10y_3m < 0 else 'normal' }}</i></div>
<div class="tile"><span>Dollar index</span><b>{{ '%.1f'|format(r.usd) if r.usd }}</b><i>{{ r.usd_chg_1m|pct(true) }} in a month</i></div>
<div class="tile"><span>Oil (WTI)</span><b>{{ r.oil|money }}</b><i>{{ r.oil_chg_1m|pct(true) }} in a month</i></div>
<div class="tile"><span>High-yield bonds</span><b>{{ r.hyg_chg_1m|pct(true) }}</b><i>1 month; falling = credit stress</i></div>
</div>

<h2>What if…</h2>
<p class="muted" style="margin-top:-6px">Estimated move for your Book from each shock, using how each name reacted over the past two years (weekly). A guide to exposure, not a forecast.</p>
<div class="tablewrap"><table><thead><tr><th>Scenario</th><th class="r">Book</th><th>Hit hardest</th><th>Holds up best</th></tr></thead><tbody>
{% for name, s in m.scenarios.items() %}<tr><td>{{ name }}</td><td class="r {{ 'good' if s.book and s.book > 0 else 'bad' if s.book }}"><b>{{ s.book|pct(true) }}</b></td>
<td>{% for t, v in s.worst %}<a href="{{ url_for('name', ticker=t) }}">{{ t }}</a> {{ v|pct(true) }}{% if not loop.last %} · {% endif %}{% endfor %}</td>
<td>{% for t, v in s.best %}<a href="{{ url_for('name', ticker=t) }}">{{ t }}</a> {{ v|pct(true) }}{% if not loop.last %} · {% endif %}{% endfor %}</td></tr>{% endfor %}</tbody></table></div>

<h2>Sensitivity by name</h2>
<div class="tablewrap"><table><thead><tr><th>Ticker</th><th></th><th class="r">Market beta</th><th class="r">10-year +1pt</th><th class="r">Dollar +5%</th><th class="r">Oil +20%</th><th class="r">Explained (R²)</th><th>Reads as</th></tr></thead><tbody>
{% for t, s in sens %}<tr><td><a href="{{ url_for('name', ticker=t) }}"><b>{{ t }}</b></a></td><td class="muted">{{ s.level }}</td>
<td class="r">{{ '%.2f'|format(s.beta_spx) }}</td><td class="r {{ 'bad' if s.per_y10_pt < -0.03 else 'good' if s.per_y10_pt > 0.03 }}">{{ s.per_y10_pt|pct(true) }}</td>
<td class="r">{{ (s.per_usd * 0.05)|pct(true) }}</td><td class="r">{{ (s.per_oil * 0.2)|pct(true) }}</td><td class="r muted">{{ s.r2|pct }}</td>
<td class="muted" style="font-size:12px">{{ 'High beta' if s.beta_spx > 1.5 else 'Defensive' if s.beta_spx < 0.7 else 'Market-like' }}{% if s.per_y10_pt < -0.05 %} · hurt by higher rates{% elif s.per_y10_pt > 0.05 %} · helped by higher rates{% endif %}</td></tr>{% endfor %}</tbody></table></div>
{% else %}<p class="empty">The macro view fills after the next daily run.</p>{% endif %}

<h2>Calendar · next 30 days</h2>
<div class="tablewrap"><table class="cal"><thead><tr><th>Date</th><th>Time (ET)</th><th>What</th><th>Why it matters</th></tr></thead><tbody>
{% for e in cal %}<tr><td class="day">{{ e.date.strftime('%a %d %b') }}</td><td class="muted">{{ e.time_et or '' }}</td>
<td><span class="k">{{ e.kind }}</span> {% if e.ticker %}<a href="{{ url_for('name', ticker=e.ticker) }}"><b>{{ e.ticker }}</b></a> results <span class="muted">({{ e.level }})</span>{% else %}{{ e.title }}{% endif %}</td>
<td class="muted" style="font-size:12px">{% set d = e.detail or {} %}{% if d.implied_move %}options imply ±{{ d.implied_move|pct }} · {% endif %}{% if d.eps_est %}EPS est {{ '%.2f'|format(d.eps_est) }} · {% endif %}{{ d.why or '' }}</td></tr>
{% else %}<tr><td colspan="4" class="empty">The calendar fills after the next daily run.</td></tr>{% endfor %}</tbody></table></div>
"""


@app.route("/outlook")
def outlook():
    m = (q1("SELECT value FROM v2_settings WHERE key = 'macro'") or {}).get("value")
    wa = (q1("SELECT value FROM v2_settings WHERE key = 'week_ahead'") or {}).get("value")
    try:
        cal = q("""SELECT c.*, n.level FROM v2_calendar c LEFT JOIN v2_names n USING (ticker)
                   WHERE c.date BETWEEN CURRENT_DATE AND CURRENT_DATE + 30
                     AND (c.kind <> 'earnings' OR n.level IN ('watchlist','book')) ORDER BY c.date, c.time_et NULLS LAST""")
    except psycopg2.Error:
        cal = []
    sens = sorted(((m or {}).get("sens") or {}).items(), key=lambda kv: (kv[1].get("level") != "book", kv[0]))
    return page("outlook", "Outlook", OUTLOOK_TPL, m=m, wa=wa, cal=cal, sens=sens)


RESEARCH_TPL = r"""
<style>
.subnav{position:sticky;top:53px;z-index:3;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);margin:14px 0 4px;display:flex;gap:6px;flex-wrap:wrap}
.subnav a{font-size:12px;padding:3px 10px;border-radius:999px;background:var(--chip);color:var(--mute)}.subnav a:hover{color:var(--ink);text-decoration:none}
.rbar{position:relative;height:56px;margin:8px 6px 4px}
.rbar .track{position:absolute;top:24px;left:0;right:0;height:6px;border-radius:3px;background:var(--panel2)}
.rbar .band{position:absolute;top:22px;height:10px;border-radius:5px;background:var(--accent);opacity:.35}
.rbar .mk{position:absolute;top:14px;width:2px;height:26px;transform:translateX(-1px)}
.rbar .lb{position:absolute;font-size:11px;transform:translateX(-50%);white-space:nowrap}
.rbar .lb.t{top:-2px}.rbar .lb.b{top:42px}
.sens td{text-align:right}.sens td.hi{color:var(--good)}.sens td.lo{color:var(--bad)}.sens td.mid{font-weight:700;outline:1px solid var(--accent)}
.fin td,.fin th{text-align:right;white-space:nowrap;font-size:12.5px}.fin td:first-child,.fin th:first-child{text-align:left;position:sticky;left:0;background:var(--panel)}
.peer tr.me td{background:var(--panel2);font-weight:600}.peer tr.med td{color:var(--mute);font-style:italic}
.flag{padding:6px 10px;border-radius:8px;margin:4px 0;background:var(--panel2);border-left:3px solid var(--line)}
.flag.high{border-color:var(--bad)}.flag.medium{border-color:var(--warn)}.flag.low{border-color:var(--mute)}
.rating{font-size:20px;font-weight:700}.summary{max-height:7.5em;overflow:hidden;position:relative}
details[open] .summary{max-height:none}
</style>
<nav class="subnav">
<a href="#valuation">Valuation</a><a href="#financials">10-year financials</a><a href="#peers">Competitors</a>
<a href="#quality">Moat and risk</a><a href="#flags">Red flags</a><a href="#insiders">Insiders and owners</a>
<a href="#analysts">Analysts</a><a href="#catalysts">Catalysts and news</a><a href="#business">Business and management</a>
<a href="#screen">Screen detail</a><a href="#scenarios">Your scenarios</a><a href="#thesis">Thesis</a>
</nav>
{% if not r %}
<div class="card" style="margin-top:12px"><b>No research yet for {{ ticker }}.</b>
<p class="muted">The research job fills valuation, 10-year financials, competitors, insiders, analysts, catalysts and red flags.
It runs for every Watchlist and Book name, candidates and the weekly top 10, or on request.</p>
<form method="post" action="{{ url_for('request_research', ticker=ticker) }}"><button class="primary">Request research</button></form></div>
{% else %}{% set v = r.valuation %}

<h2 id="valuation">Valuation <span class="muted" style="font-weight:400">model as of {{ r.as_of|d }} · updates weekly · Claude's locked value changes only at review</span></h2>
<div class="grid g2">
<div class="card"><h3>Bear, base and bull vs today's price</h3>
{{ range_bar(r) }}
<div class="kv" style="margin-top:8px">
<span class="k">Company type (for margin of safety)</span><span>{{ v.company_type.replace('_',' ') }} <span class="muted">· auto-classified, deep dive confirms</span></span>
<span class="k">Required margin of safety</span><span>{{ v.required_mos|pct }}</span>
<span class="k">Margin of safety today</span><span class="{{ 'good' if v.current_mos is not none and v.current_mos >= v.required_mos else 'bad' }}">{{ v.current_mos|pct }}</span>
<span class="k">Reverse DCF: growth the price implies</span><span>{% if v.reverse_dcf and v.reverse_dcf.implied_growth is not none %}<b>{{ v.reverse_dcf.implied_growth|pct }}</b> a year for 5 years <span class="muted">· history {{ v.reverse_dcf.historic_growth|pct }}</span>{% else %}—{% endif %}</span>
<span class="k">Entry target (base × (1 − required MoS))</span><span><b>{{ v.entry_target|money }}</b>{% if r.price and v.entry_target %} <span class="muted">· price is {{ (r.price / v.entry_target - 1)|pct(true) }} vs target</span>{% endif %}</span>
</div>
{% for f in v.flags %}<div class="flag medium">{{ f }}</div>{% endfor %}{% for nte in v.notes %}<div class="flag low">{{ nte }}</div>{% endfor %}
</div>
<div class="card"><h3>Methods</h3>
<table><thead><tr><th>Method</th><th class="r">Value / share</th><th class="r">Weight</th></tr></thead><tbody>
{% for m in v.methods %}<tr><td>{{ m.label }}<br><span class="muted" style="font-size:12px">{% for k,val in m.inputs.items() %}{{ k }} {{ fmt_input(k, val) }}{% if not loop.last %} · {% endif %}{% endfor %}</span></td>
<td class="r"><b>{{ m.value|money }}</b></td><td class="r">{{ m.weight|pct }}</td></tr>{% endfor %}
<tr><td><b>Weighted base value</b></td><td class="r"><b>{{ v.base|money }}</b></td><td></td></tr></tbody></table>
<details style="margin-top:10px"><summary>Assumptions</summary><div class="kv" style="margin-top:6px">
{% for k,val in v.assumptions.items() %}<span class="k">{{ k }}</span><span>{{ fmt_input(k, val) }}</span>{% endfor %}</div></details>
</div></div>
{% if v.sensitivity %}<div class="card" style="margin-top:12px"><h3>DCF sensitivity: value per share</h3>
<p class="muted" style="margin-top:0">Rows: discount rate (WACC). Columns: terminal growth. Green is above today's price, red below.</p>
<table class="sens"><thead><tr><th>WACC \ growth</th>{% for g in v.sensitivity.g %}<th class="r">{{ '%.1f'|format(g*100) }}%</th>{% endfor %}</tr></thead><tbody>
{% for row in v.sensitivity.grid %}{% set i = loop.index0 %}<tr><td style="text-align:left">{{ '%.1f'|format(v.sensitivity.wacc[i]*100) }}%</td>
{% for cell in row %}<td class="{{ 'mid' if i == 2 and loop.index0 == 1 }} {{ 'hi' if r.price and cell > r.price else 'lo' }}">{{ cell|money }}</td>{% endfor %}</tr>{% endfor %}
</tbody></table></div>{% endif %}

<h2 id="financials">10-year financials <span class="muted" style="font-weight:400">from SEC annual reports · fiscal years</span></h2>
{{ fin_chart(r.history) }}
<div class="tablewrap"><table class="fin"><thead><tr><th>Metric</th>{% for h in r.history %}<th>{{ h.year }}</th>{% endfor %}</tr></thead><tbody>
{% for key, label, kind in FIN_ROWS %}<tr><td>{{ label }}</td>{% for h in r.history %}<td class="{{ 'bad' if h[key] is not none and h[key] < 0 }}">{{ fmt_kind(h[key], kind) }}</td>{% endfor %}</tr>{% endfor %}
</tbody></table></div>

<h2 id="peers">Competitors <span class="muted" style="font-weight:400">same industry, nearest in size, from this week's screen</span></h2>
<div class="tablewrap"><table class="peer"><thead><tr><th>Company</th><th class="r">Mkt cap</th><th class="r">Rev CAGR 5y</th><th class="r">Net margin</th>
<th class="r">ROIC 5y avg</th><th class="r">P/E</th><th class="r">EV/EBITDA</th><th class="r">FCF yield</th><th class="r">Off high</th></tr></thead><tbody>
{% for p in r.peers %}<tr class="{{ 'me' if loop.first }}"><td><a href="{{ url_for('name', ticker=p.ticker) }}">{{ p.ticker }}</a> <span class="muted">{{ (p.name or '')|cname }}</span></td>
<td class="r">{{ big(p.mcap) }}</td><td class="r">{{ p.rev_cagr|pct }}</td><td class="r">{{ p.net_margin|pct }}</td><td class="r">{{ p.roc_avg|pct }}</td>
<td class="r">{{ fx(p.pe) }}</td><td class="r">{{ fx(p.ev_ebitda,'x') }}</td><td class="r">{{ p.fcf_yield|pct }}</td><td class="r">{{ p.drawdown|pct }}</td></tr>{% endfor %}
{% set pm = peer_medians(r.peers[1:]) %}<tr class="med"><td>Peer median</td><td class="r">{{ big(pm.mcap) }}</td><td class="r">{{ pm.rev_cagr|pct }}</td><td class="r">{{ pm.net_margin|pct }}</td>
<td class="r">{{ pm.roc_avg|pct }}</td><td class="r">{{ fx(pm.pe) }}</td><td class="r">{{ fx(pm.ev_ebitda,'x') }}</td><td class="r">{{ pm.fcf_yield|pct }}</td><td class="r">{{ pm.drawdown|pct }}</td></tr>
</tbody></table></div>

<div class="grid g2" id="quality" style="margin-top:12px">
<div class="card"><h3>Moat rating</h3><div class="rating {{ {'Wide':'good','Narrow':'warn'}.get(r.moat.rating,'muted') }}">{{ r.moat.rating }} <span class="muted" style="font-size:13px;font-weight:400">{{ r.moat.points }}/{{ r.moat.of }} signals</span></div>
<ul>{% for x in r.moat.reasons %}<li>{{ x }}</li>{% else %}<li class="muted">No quantitative moat signals.</li>{% endfor %}</ul><p class="muted" style="font-size:12px">{{ r.moat.note }}</p></div>
<div class="card"><h3>Risk rating</h3><div class="rating {{ {'Low':'good','Medium':'warn','High':'bad'}.get(r.risk.rating) }}">{{ r.risk.rating }}</div>
<ul>{% for x in r.risk.reasons %}<li>{{ x }}</li>{% else %}<li class="muted">No elevated risk signals.</li>{% endfor %}</ul>
<p class="muted" style="font-size:12px">Beta {{ fx(r.beta) }} · 1-year volatility {{ r.volatility_1y|pct }}</p></div>
</div>

<h2 id="flags">Red flags <span class="muted" style="font-weight:400">{{ r.red_flags|length }}</span></h2>
{% for f in r.red_flags|sort(attribute='severity') %}<div class="flag {{ f.severity }}"><b>{{ f.severity|capitalize }}</b> · {{ f.text }}</div>
{% else %}<p class="muted">No red flags found in the filings, ownership data or recent headlines.</p>{% endfor %}

<h2 id="insiders">Insiders and owners</h2>
<div class="grid g2">
<div class="card"><h3>Insider trades, last 6 months · <span class="{{ 'good' if 'buying' in r.insiders.signal.lower() else ('warn' if 'selling' in r.insiders.signal.lower() else 'muted') }}">{{ r.insiders.signal }}</span></h3>
<p class="muted" style="margin-top:0">{{ r.insiders.buys }} buys ({{ big(r.insiders.buy_value) }}) by {{ r.insiders.buyers }} insider{{ '' if r.insiders.buyers == 1 else 's' }} · {{ r.insiders.sells }} sales ({{ big(r.insiders.sell_value) }})</p>
<div class="tablewrap" style="max-height:320px;overflow:auto"><table><thead><tr><th>Date</th><th>Insider</th><th>Type</th><th class="r">Value</th></tr></thead><tbody>
{% for t in r.insiders.transactions[:20] %}<tr><td>{{ t.date|d }}</td><td>{{ t.insider }}<br><span class="muted" style="font-size:12px">{{ t.position or '' }}</span></td>
<td class="{{ 'good' if t.kind=='buy' else ('warn' if t.kind=='sell') }}">{{ t.kind }}</td><td class="r">{{ big(t.value) if t.value else '—' }}</td></tr>
{% else %}<tr><td colspan="4" class="empty">No insider trades reported.</td></tr>{% endfor %}</tbody></table></div></div>
<div class="card"><h3>Ownership</h3><div class="kv">
<span class="k">Insiders</span><span>{{ r.ownership.insiders|pct }}</span><span class="k">Institutions</span><span>{% if r.ownership.institutions and r.ownership.institutions > 1 %}over 100% <span class="muted">(Yahoo counts some shares twice)</span>{% else %}{{ r.ownership.institutions|pct }}{% endif %}</span>
<span class="k">Short interest</span><span class="{{ 'warn' if r.ownership.short_float and r.ownership.short_float > 0.1 }}">{{ r.ownership.short_float|pct }} of float · {{ fx(r.ownership.short_ratio) }} days to cover</span></div>
<table style="margin-top:10px"><thead><tr><th>Top institutional holders</th><th class="r">Held</th></tr></thead><tbody>
{% for h in r.ownership.top_holders %}<tr><td>{{ h.holder }}</td><td class="r">{{ h.pct|pct }}</td></tr>{% else %}<tr><td colspan="2" class="empty">Not available.</td></tr>{% endfor %}</tbody></table></div>
</div>

<h2 id="analysts">Analysts</h2>
<div class="grid g2">
<div class="card"><h3>Price targets · {{ r.analysts.n or 0 }} analysts · {{ (r.analysts.recommendation or '—').replace('_',' ') }}</h3>
{{ target_bar(r) }}
<div class="kv"><span class="k">Mean · median target</span><span>{{ r.analysts.mean|money }} · {{ r.analysts.median|money }}{% if r.price and r.analysts.mean %} <span class="muted">({{ (r.analysts.mean / r.price - 1)|pct(true) }} vs price)</span>{% endif %}</span>
<span class="k">Our model base vs street mean</span><span>{% if v.base and r.analysts.mean %}{{ (v.base / r.analysts.mean - 1)|pct(true) }} <span class="muted">· a large gap is either our edge or our error; the thesis must say which</span>{% else %}—{% endif %}</span>
<span class="k">Forward P/E · forward EPS</span><span>{{ fx(r.analysts.forward_pe) }} · {{ r.analysts.forward_eps|money }}</span></div></div>
<div class="card"><h3>Recent rating changes (120 days)</h3>
<table><thead><tr><th>Date</th><th>Firm</th><th>Change</th><th class="r">Target</th></tr></thead><tbody>
{% for a in r.analysts.actions %}<tr><td>{{ a.date|d }}</td><td>{{ a.firm }}</td><td class="{{ 'good' if a.action=='up' else ('bad' if a.action=='down') }}">{{ a.action }}{% if a.to %} · {{ a.to }}{% endif %}</td>
<td class="r">{{ a.target|money if a.target else '—' }}</td></tr>{% else %}<tr><td colspan="4" class="empty">No rating changes.</td></tr>{% endfor %}</tbody></table></div>
</div>

<h2 id="catalysts">Catalysts and news</h2>
<div class="grid g2">
<div class="card"><h3>Upcoming</h3><div class="kv">
<span class="k">Next earnings</span><span>{% if r.catalysts.next_earnings is string %}{{ r.catalysts.next_earnings|d }}{% elif r.catalysts.next_earnings %}{{ r.catalysts.next_earnings[0]|d }}{% else %}—{% endif %}</span>
<span class="k">Ex-dividend date</span><span>{{ r.catalysts.ex_dividend|d }}</span>
<span class="k">Dividend yield</span><span>{{ fx(r.catalysts.dividend_yield) }}{% if r.catalysts.dividend_yield %}%{% endif %}</span></div>
<p class="muted" style="font-size:12px">Dated thesis catalysts (investor days, rulings, launches) are set in the thesis.</p></div>
<div class="card"><h3>Recent headlines</h3>
{% set own = r.catalysts.news|selectattr('about_company')|list %}{% set rel = r.catalysts.news|rejectattr('about_company')|list %}
{% for n in own %}<div style="margin:6px 0">{% if n.negative %}<span class="chip bad">watch</span>{% endif %}<a href="{{ n.url }}" target="_blank" rel="noopener">{{ n.title }}</a><br><span class="muted" style="font-size:12px">{{ n.publisher or '' }} · {{ n.date|d }}</span></div>
{% else %}<p class="empty">No recent headlines naming the company.</p>{% endfor %}
{% if rel %}<details><summary>{{ rel|length }} related headlines (peers and sector)</summary>{% for n in rel %}<div style="margin:6px 0"><a href="{{ n.url }}" target="_blank" rel="noopener">{{ n.title }}</a> <span class="muted" style="font-size:12px">· {{ n.publisher or '' }} · {{ n.date|d }}</span></div>{% endfor %}</details>{% endif %}</div>
</div>

<h2 id="business">Business and management</h2>
<div class="grid g2">
<div class="card"><h3>What the company does</h3>
<p class="muted" style="margin-top:0">{{ r.business.employees and '{:,}'.format(r.business.employees) ~ ' employees' or '' }} {{ r.business.city or '' }}{% if r.business.website %} · <a href="{{ r.business.website }}" target="_blank" rel="noopener">website</a>{% endif %}</p>
<details><summary>Show full description</summary><div style="margin-top:6px">{{ r.business.summary or 'No description available.' }}</div></details>
<div class="summary" style="margin-top:6px">{{ (r.business.summary or '')[:420] }}{% if (r.business.summary or '')|length > 420 %}…{% endif %}</div></div>
<div class="card"><h3>Leadership</h3><table><thead><tr><th>Name</th><th>Role</th><th class="r">Age</th><th class="r">Pay</th></tr></thead><tbody>
{% for o in r.business.officers %}<tr><td>{{ o.name }}</td><td>{{ o.title }}</td><td class="r">{{ o.age or '—' }}</td><td class="r">{{ big(o.pay) if o.pay else '—' }}</td></tr>
{% else %}<tr><td colspan="4" class="empty">Not available.</td></tr>{% endfor %}</tbody></table>
<p class="muted" style="font-size:12px">Fit for the job, track record and incentives are judged in the thesis (Management fit).</p></div>
</div>
<form method="post" action="{{ url_for('request_research', ticker=ticker) }}" style="margin-top:12px"><button>Refresh research on the next run</button></form>
{% endif %}
"""

FIN_ROWS = [("revenue", "Revenue", "big"), ("rev_growth", "Revenue growth", "pct"), ("gross_margin", "Gross margin", "pct"),
            ("op_margin", "Operating margin", "pct"), ("net_margin", "Net margin", "pct"), ("net_income", "Net income", "big"),
            ("fcf", "Free cash flow", "big"), ("fcf_margin", "FCF margin", "pct"), ("roic", "ROIC (ROE for financials)", "pct"),
            ("eps", "EPS (diluted)", "money"), ("shares", "Diluted shares", "count"), ("buybacks", "Buybacks", "big"),
            ("dividends", "Dividends paid", "big"), ("sbc", "Stock-based pay", "big")]


def fmt_kind(v, kind):
    v = num(v)
    if v is None:
        return "—"
    if kind == "big":
        return big(v)
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "money":
        return f"${v:,.2f}"
    return f"{v / 1e6:,.0f}M"


def fmt_input(k, v):
    n = num(v)
    if n is None:
        return v if v is not None else "—"
    kl = k.lower()
    if any(w in kl for w in ("growth", "rate", "wacc", "margin", "premium", "cost of", "share")) and abs(n) < 1:
        return f"{n:.1%}"
    if abs(n) >= 1e6:
        return big(n)
    return f"{n:,.2f}"


def _bar(points, lo_hi=None):
    """HTML range bar. points: [(label, value, css colour var, top|bottom)]; lo_hi: (low, high) band."""
    vals = [p[1] for p in points if p[1] is not None] + [x for x in (lo_hi or ()) if x is not None]
    if len(vals) < 2:
        return Markup('<p class="muted">Not enough data for a range.</p>')
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    pos = lambda v: (v - lo) / (hi - lo) * 100
    html = '<div class="rbar"><div class="track"></div>'
    if lo_hi and None not in lo_hi:
        html += f'<div class="band" style="left:{pos(lo_hi[0]):.1f}%;width:{pos(lo_hi[1]) - pos(lo_hi[0]):.1f}%"></div>'
    for label, v, col, side in points:
        if v is None:
            continue
        p = pos(v)
        html += (f'<div class="mk" style="left:{p:.1f}%;background:var(--{col})"></div>'
                 f'<div class="lb {"t" if side == "top" else "b"}" style="left:{min(max(p, 6), 94):.1f}%;color:var(--{col})">{label} {money(v)}</div>')
    return Markup(html + "</div>")


def range_bar(r):
    v = r["valuation"]
    return _bar([("Bear", v.get("low"), "bad", "bottom"), ("Base", v.get("base"), "ink", "top"),
                 ("Bull", v.get("high"), "good", "bottom"), ("Price", r.get("price"), "accent", "top"),
                 ("Entry", v.get("entry_target"), "warn", "top")], (v.get("low"), v.get("high")))


def target_bar(r):
    a = r["analysts"]
    return _bar([("Low", a.get("low"), "bad", "bottom"), ("Mean", a.get("mean"), "ink", "top"),
                 ("High", a.get("high"), "good", "bottom"), ("Price", r.get("price"), "accent", "top")],
                (a.get("low"), a.get("high")))


def fin_chart(hist):
    """Revenue and free-cash-flow bars by year with net margin as a line."""
    rows = [h for h in hist if h.get("revenue")]
    if len(rows) < 2:
        return ""
    W, H, top, bottom = 1000, 190, 16, 28
    mx = max(max(h["revenue"] for h in rows), max((h.get("fcf") or 0) for h in rows))
    mn = min(0, min((h.get("fcf") or 0) for h in rows))
    scale = lambda v: top + (mx - v) / (mx - mn) * (H - top - bottom)
    zero = scale(0)
    slot = W / len(rows)
    bw = slot * 0.3
    svg = [f'<line x1="0" x2="{W}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="var(--line)"/>']
    margins = []
    for i, h in enumerate(rows):
        x = i * slot + slot / 2
        y = scale(h["revenue"])
        svg.append(f'<rect x="{x - bw - 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{zero - y:.1f}" rx="2" fill="var(--accent)" opacity=".8"><title>{h["year"]} revenue {big(h["revenue"])}</title></rect>')
        f = h.get("fcf")
        if f is not None:
            yf_ = scale(f)
            svg.append(f'<rect x="{x + 2:.1f}" y="{min(yf_, zero):.1f}" width="{bw:.1f}" height="{abs(zero - yf_):.1f}" rx="2" fill="var(--{"good" if f >= 0 else "bad"})" opacity=".8"><title>{h["year"]} free cash flow {big(f)}</title></rect>')
        svg.append(f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle" font-size="12" fill="var(--mute)">{h["year"]}</text>')
        if h.get("net_margin") is not None:
            margins.append((x, h["net_margin"]))
    if len(margins) >= 2:
        mlo, mhi = min(m for _, m in margins), max(m for _, m in margins)
        mhi = mhi if mhi > mlo else mlo + 0.01
        my = lambda m: top + (mhi - m) / (mhi - mlo) * (H - top - bottom) * 0.9
        svg.append('<polyline fill="none" stroke="var(--warn)" stroke-width="2" points="' +
                   " ".join(f"{x:.1f},{my(m):.1f}" for x, m in margins) + '"/>')
        for x, m in margins:
            svg.append(f'<circle cx="{x:.1f}" cy="{my(m):.1f}" r="3" fill="var(--warn)"><title>net margin {m:.1%}</title></circle>')
    legend = ('<div class="row" style="font-size:12px;color:var(--mute);margin-bottom:4px">'
              '<span><span class="dot" style="background:var(--accent)"></span>Revenue</span>'
              '<span><span class="dot green"></span>Free cash flow</span>'
              '<span><span class="dot amber"></span>Net margin (own scale)</span></div>')
    return Markup(f'<div class="card">{legend}<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Revenue, free cash flow and net margin by year">{"".join(svg)}</svg></div>')


def peer_medians(peers):
    keys = ("mcap", "rev_cagr", "net_margin", "roc_avg", "pe", "ev_ebitda", "fcf_yield", "drawdown")
    return {k: median(num(p.get(k)) for p in peers) for k in keys}


app.jinja_env.globals.update(FIN_ROWS=FIN_ROWS, fmt_kind=fmt_kind, fmt_input=fmt_input, range_bar=range_bar,
                             target_bar=target_bar, fin_chart=fin_chart, peer_medians=peer_medians)


@app.post("/name/<ticker>/research")
def request_research(ticker):
    x("ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS research_requested BOOLEAN DEFAULT FALSE")
    x("UPDATE v2_names SET research_requested = TRUE WHERE ticker = %s", (ticker.upper(),))
    flash(f"Research requested for {ticker.upper()}. It is filled on the next research run.")
    return back(url_for("name", ticker=ticker))


def latest_research(ticker):
    try:
        row = q1("SELECT data FROM v2_research WHERE ticker = %s ORDER BY as_of DESC LIMIT 1", (ticker,))
    except psycopg2.Error:
        return None
    return row["data"] if row else None


@app.route("/health")
def health():
    return {"ok": True, "names": q1("SELECT COUNT(*) AS c FROM v2_names")["c"]}


if __name__ == "__main__":
    app.run(debug=True)
