"""Monitoring for Watchlist and Book names.

  python tools\\monitor.py daily    prices, filings, analyst moves, buy signals, price / theme / market alerts,
                                    kill criteria, health (Mon-Fri 07:00 SGT)
  python tools\\monitor.py weekly   3-5 headlines per name for Claude to tag (Sunday 08:00 SGT)
  python tools\\monitor.py export   write outbox/state.json (and Drive v2-outbox) for Claude's tasks
Data: Yahoo (yfinance), SEC EDGAR. All free.
"""
import os, sys, json, math, time, datetime as dt
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
from common import (db, J, alert, ensure_schema, week_key, TODAY, THEMES, APP_URL, DRIVE_STATE_FILE,
                    DRIVE_INBOX, drive_update)

MODE = sys.argv[1] if len(sys.argv) > 1 else "daily"
UA = os.environ.get("SEC_USER_AGENT", "")
SEC = requests.Session()
SEC.headers.update({"User-Agent": UA or "ValueInvestor research", "Accept-Encoding": "gzip, deflate"})
CACHE = Path(__file__).with_name(".cache")
CACHE.mkdir(exist_ok=True)
ITEMS_8K = {"1.01": "Material agreement", "1.02": "Agreement ended", "1.03": "Bankruptcy", "2.01": "Acquisition or sale",
            "2.02": "Earnings release", "2.05": "Restructuring costs", "2.06": "Impairment", "3.01": "Listing notice",
            "4.01": "Auditor change", "4.02": "Prior financials no longer reliable", "5.02": "Director or officer change",
            "5.07": "Shareholder vote", "7.01": "Reg FD disclosure", "8.01": "Other event"}
HIGH_8K = {"1.03", "4.01", "4.02", "3.01"}
THEME_WORDS = {
    "AI and energy infrastructure": ["ai", "data center", "datacenter", "semiconductor", "semis", "power", "grid",
                                     "electrification", "storage", "compute", "nuclear", "energy infra"],
    "Automation and robotics": ["robot", "automation"],
    "Healthcare": ["health", "pharma", "biotech", "medtech", "medical", "glp", "obesity"],
    "Space and satellites": ["space", "satellite", "launch", "defense", "defence"],
    "Digital finance": ["fintech", "payment", "digital finance", "brokerage", "crypto", "exchange"],
    "Quantum computing": ["quantum"],
    "Critical resources": ["copper", "uranium", "lithium", "rare earth", "mining", "critical resource", "materials"],
}
health_log = {"prices_missing": [], "info_failed": [], "sec_failed": [], "news_failed": []}


def themes_of(tags):
    out = set()
    for t in tags or []:
        tl = t.lower()
        for theme, words in THEME_WORDS.items():
            if tl == theme.lower() or any(w == tl or (len(w) > 3 and w in tl) for w in words):
                out.add(theme)
    return out


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def cik_map():
    f = CACHE / "company_tickers.json"
    if f.exists() and time.time() - f.stat().st_mtime < 6 * 86400:
        data = json.loads(f.read_text())
    else:
        data = SEC.get("https://www.sec.gov/files/company_tickers.json", timeout=60).json()
        f.write_text(json.dumps(data))
    return {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in data.values()}


def load(cur):
    cur.execute("SELECT * FROM v2_names WHERE level IN ('watchlist','book') ORDER BY ticker")
    names = [dict(r) for r in cur.fetchall()]
    t = [n["ticker"] for n in names]
    cur.execute("SELECT DISTINCT ON (ticker) * FROM v2_theses WHERE ticker = ANY(%s) ORDER BY ticker, version DESC", (t,))
    theses = {r["ticker"]: dict(r) for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT ON (ticker) ticker, data FROM v2_research WHERE ticker = ANY(%s) "
                "ORDER BY ticker, as_of DESC", (t,))
    research = {r["ticker"]: r["data"] for r in cur.fetchall()}
    return names, theses, research


def entry_of(tk, theses, research):
    th = theses.get(tk) or {}
    if num(th.get("entry_target")):
        return num(th["entry_target"]), "thesis"
    v = (research.get(tk) or {}).get("valuation") or {}
    if num(v.get("entry_target")):
        return num(v["entry_target"]), "model"
    return None, None


# ------------------------------------------------------------------ daily
def daily(conn):
    cur = conn.cursor()
    names, theses, research = load(cur)
    tickers = [n["ticker"] for n in names]
    etfs = sorted({e for v in THEMES.values() for e in v})
    print(f"Daily check: {len(names)} names ({sum(n['level'] == 'book' for n in names)} in Book), {len(etfs)} theme ETFs")
    created = 0
    rerun_screen = False

    # ---- prices
    syms = tickers + etfs + ["^GSPC", "^VIX", "^TNX"]
    df = yf.download(syms, period="1y", interval="1d", group_by="ticker", auto_adjust=True, threads=True, progress=False)
    closes = {}
    for s in syms:
        try:
            c = (df[s] if isinstance(df.columns, pd.MultiIndex) else df)["Close"].dropna()
            if len(c) >= 6:
                closes[s] = c
        except KeyError:
            pass
    health_log["prices_missing"] = [t for t in tickers if t not in closes]
    for t in tickers:
        c = closes.get(t)
        if c is None:
            continue
        rows = [(t, d.date(), round(float(v), 4)) for d, v in c.tail(260).items()]
        psycopg2_execute_values(cur, "INSERT INTO v2_prices (ticker, date, close) VALUES %s "
                                     "ON CONFLICT (ticker, date) DO UPDATE SET close = EXCLUDED.close", rows)
    chg = lambda s, n: float(s.iloc[-1] / s.iloc[-1 - n] - 1) if s is not None and len(s) > n else None
    spx5 = chg(closes.get("^GSPC"), 5)

    # ---- market regime
    vix = closes.get("^VIX")
    if vix is not None and float(vix.iloc[-1]) > 25:
        created += alert(cur, "MARKET", "Market stress", "medium",
                         f"VIX at {float(vix.iloc[-1]):.1f} (above 25)", f"vix-{week_key()}")
    tnx = closes.get("^TNX")
    if tnx is not None and len(tnx) > 5:
        bp = (float(tnx.iloc[-1]) - float(tnx.iloc[-6])) * 100
        if abs(bp) >= 25:
            created += alert(cur, "MARKET", "Rates move", "medium",
                             f"US 10-year yield {'up' if bp > 0 else 'down'} {abs(bp):.0f}bp in 5 days to "
                             f"{float(tnx.iloc[-1]):.2f}%", f"tnx-{TODAY}")

    # ---- theme sell-offs
    theme_moves = {}
    for theme, basket in THEMES.items():
        moves = [chg(closes.get(e), 5) for e in basket if closes.get(e) is not None]
        moves = [m for m in moves if m is not None]
        if moves:
            theme_moves[theme] = sum(moves) / len(moves)
    for theme, m in theme_moves.items():
        if m <= -0.05:
            held = [n for n in names if theme in themes_of(n["themes"])]
            sev = "high" if m <= -0.10 else "medium"
            detail = (f"{theme} basket down {abs(m):.1%} in 5 days. Exposed: "
                      + (", ".join(f"{n['ticker']} ({n['level']})" for n in held) or "none of your names"))
            created += alert(cur, "MARKET", "Theme sell-off", sev, detail, f"theme-{theme}-{week_key()}")
            for n in held:
                created += alert(cur, n["ticker"], "Theme sell-off", "high" if n["level"] == "book" else "medium",
                                 f"{theme} down {abs(m):.1%} in 5 days", f"theme-{theme}-{n['ticker']}-{week_key()}")
            if m <= -0.10:
                rerun_screen = True

    # ---- per name: price moves, analysts, kill criteria, buy signals
    cik_of = {}
    try:
        cik_of = cik_map()
    except Exception:
        health_log["sec_failed"].append("ticker map")
    kill_hit, news_break = set(), set()
    cur.execute("SELECT DISTINCT ticker FROM v2_news WHERE tag = 'breaks' AND published > CURRENT_DATE - 60")
    news_break = {r["ticker"] for r in cur.fetchall()}

    for n in names:
        tk, book = n["ticker"], n["level"] == "book"
        c = closes.get(tk)
        price = float(c.iloc[-1]) if c is not None else None
        r1, r5 = chg(c, 1), chg(c, 5)
        if r1 is not None and abs(r1) >= 0.07:
            created += alert(cur, tk, "Sharp move", "high" if book else "medium",
                             f"{'Up' if r1 > 0 else 'Down'} {abs(r1):.1%} in one day to ${price:,.2f}", f"move1-{tk}-{TODAY}")
        if r5 is not None and abs(r5) >= 0.12:
            created += alert(cur, tk, "Sharp move", "medium" if book else "info",
                             f"{'Up' if r5 > 0 else 'Down'} {abs(r5):.1%} in 5 days", f"move5-{tk}-{week_key()}")
        if book and r5 is not None and spx5 is not None and r5 - spx5 <= -0.10:
            created += alert(cur, tk, "Lagging the market", "medium",
                             f"{r5:+.1%} vs S&P 500 {spx5:+.1%} over 5 days", f"lag-{tk}-{week_key()}")

        # analysts
        info = {}
        try:
            yt = yf.Ticker(tk)
            info = yt.info or {}
            ud = yt.upgrades_downgrades
            if ud is not None and len(ud):
                ud = ud.reset_index()
                dcol = next((x for x in ud.columns if "date" in str(x).lower()), ud.columns[0])
                ud[dcol] = pd.to_datetime(ud[dcol], errors="coerce").dt.tz_localize(None)
                for _, a in ud[ud[dcol] >= pd.Timestamp(TODAY - dt.timedelta(days=3))].iterrows():
                    if a.get("Action") in ("up", "down"):
                        created += alert(cur, tk, f"Analyst {'upgrade' if a['Action'] == 'up' else 'downgrade'}",
                                         ("medium" if book else "info"),
                                         f"{a.get('Firm')}: {a.get('FromGrade') or '?'} to {a.get('ToGrade')}"
                                         + (f", target ${a['currentPriceTarget']:,.0f}" if num(a.get("currentPriceTarget")) else ""),
                                         f"rating-{tk}-{a.get('Firm')}-{str(a[dcol])[:10]}")
        except Exception:
            health_log["info_failed"].append(tk)
        mean = num(info.get("targetMeanPrice"))
        if mean:
            cur.execute("SELECT mean, date FROM v2_analyst WHERE ticker = %s AND date <= %s ORDER BY date DESC LIMIT 1",
                        (tk, TODAY - dt.timedelta(days=5)))
            prev = cur.fetchone()
            cur.execute("""INSERT INTO v2_analyst (ticker, date, mean, high, low, n) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (ticker, date) DO UPDATE SET mean=EXCLUDED.mean, high=EXCLUDED.high,
                             low=EXCLUDED.low, n=EXCLUDED.n""",
                        (tk, TODAY, mean, num(info.get("targetHighPrice")), num(info.get("targetLowPrice")),
                         info.get("numberOfAnalystOpinions")))
            if prev and num(prev["mean"]):
                m = mean / float(prev["mean"]) - 1
                if abs(m) >= 0.05:
                    created += alert(cur, tk, "Analyst targets moved", "medium" if book else "info",
                                     f"Mean target {'up' if m > 0 else 'down'} {abs(m):.0%} to ${mean:,.2f} "
                                     f"(from ${float(prev['mean']):,.2f} on {prev['date']})", f"tgt-{tk}-{week_key()}")
            if book and price and abs(price / mean - 1) <= 0.05:
                created += alert(cur, tk, "Near analyst target", "info",
                                 f"Price ${price:,.2f} is within 5% of the mean target ${mean:,.2f}", f"neartgt-{tk}-{week_key()}")

        # kill criteria (machine-checkable ones in thesis.valuation.kill)
        th = theses.get(tk) or {}
        for k in ((th.get("valuation") or {}).get("kill") or []):
            kind, val = k.get("type"), num(k.get("value"))
            hit = ((kind == "price_below" and price and val and price < val) or
                   (kind == "analyst_mean_below" and mean and val and mean < val))
            if hit:
                kill_hit.add(tk)
                created += alert(cur, tk, "Kill criterion hit", "high",
                                 k.get("text") or f"{kind} {val}", f"kill-{tk}-{kind}-{week_key()}", action="Review the thesis")

        # buy signal (Watchlist)
        entry, src = entry_of(tk, theses, research)
        if n["level"] == "watchlist" and price and entry:
            gap = price / entry - 1
            paused = tk in kill_hit or tk in news_break or n.get("health") == "red"
            if gap <= 0:
                if paused:
                    created += alert(cur, tk, "Buy signal paused", "medium",
                                     f"Price ${price:,.2f} is at the {src} entry target ${entry:,.2f}, but the thesis "
                                     "is flagged (kill criterion, breaking news or red health)", f"buyp-{tk}-{week_key()}")
                else:
                    new = alert(cur, tk, "Buy signal", "high" if src == "thesis" else "medium",
                                f"Price ${price:,.2f} is at or below the {src} entry target ${entry:,.2f} ({gap:+.1%})"
                                + ("" if src == "thesis" else " · no approved thesis yet, so treat as a lead"),
                                f"buy-{tk}-{week_key()}", action="Awaiting Claude's fresh check")
                    if new:
                        cur.execute("UPDATE v2_alerts SET sent_via = 'hold' WHERE dedupe_key = %s", (f"buy-{tk}-{week_key()}",))
                    created += new
            elif gap <= 0.05:
                created += alert(cur, tk, "Approaching entry", "medium",
                                 f"Price ${price:,.2f} is {gap:.1%} above the {src} entry target ${entry:,.2f}",
                                 f"near-{tk}-{week_key()}")

        # filings (EDGAR)
        cik = cik_of.get(tk)
        if cik:
            try:
                created += filings(cur, tk, cik, book)
            except Exception:
                health_log["sec_failed"].append(tk)
        time.sleep(0.15)

    # ---- buy signals not re-checked by Claude within 20 hours: release them anyway
    cur.execute("""UPDATE v2_alerts SET sent_via = NULL, detail = detail || ' (not yet re-checked by Claude)'
                   WHERE sent_via = 'hold' AND NOT sent AND fired_at < now() - interval '20 hours'""")

    # ---- health
    for n in names:
        tk = n["ticker"]
        why, level = [], "green"
        cur.execute("""SELECT kind, severity FROM v2_alerts WHERE ticker = %s AND NOT addressed
                       AND fired_at > now() - interval '7 days' AND kind NOT IN ('Buy signal','Approaching entry')""", (tk,))
        open_ = cur.fetchall()
        cur.execute("SELECT tag FROM v2_news WHERE ticker = %s AND published > CURRENT_DATE - 30 AND tag IN ('weakens','breaks')", (tk,))
        tags = [r["tag"] for r in cur.fetchall()]
        cur.execute("""SELECT (SELECT mean FROM v2_analyst WHERE ticker = %s ORDER BY date DESC LIMIT 1) AS now_,
                              (SELECT mean FROM v2_analyst WHERE ticker = %s AND date <= CURRENT_DATE - 30 ORDER BY date DESC LIMIT 1) AS then_""",
                    (tk, tk))
        a = cur.fetchone()
        rev = (float(a["now_"]) / float(a["then_"]) - 1) if a and a["now_"] and a["then_"] else None
        if tk in kill_hit:
            level = "red"; why.append("kill criterion hit")
        if "breaks" in tags:
            level = "red"; why.append("news breaks a pillar")
        if any(r["severity"] == "high" for r in open_) and level != "red":
            level = "amber"; why.append("open high alert")
        if "weakens" in tags and level == "green":
            level = "amber"; why.append("news weakens a pillar")
        if rev is not None and rev <= -0.10:
            level = "red" if level == "amber" else "amber" if level == "green" else level
            why.append(f"analyst targets {rev:+.0%} in 30 days")
        rs = research.get(tk) or {}
        si = num((rs.get("ownership") or {}).get("short_float"))
        if si is not None and si > 0.10 and level == "green":
            level = "amber"; why.append(f"short interest {si:.0%} of float")
        ins = rs.get("insiders") or {}
        if (ins.get("signal") == "Net selling" and num(ins.get("sell_value")) and num(rs.get("mcap"))
                and ins["sell_value"] / rs["mcap"] > 0.005 and level == "green"):
            level = "amber"; why.append("heavy insider selling")
        c = closes.get(tk)
        r5 = chg(c, 5)
        if r5 is not None and r5 <= -0.12 and level == "green":
            level = "amber"; why.append(f"price {r5:+.0%} in 5 days")
        cur.execute("UPDATE v2_names SET health = %s, health_why = %s WHERE ticker = %s", (level, "; ".join(why), tk))

    failures = {k: v for k, v in health_log.items() if v}
    cur.execute("""INSERT INTO v2_settings (key, value) VALUES ('data_health', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (J({"date": str(TODAY), "failures": failures, "theme_moves": theme_moves,
                    "names": len(names), "rerun_screen": rerun_screen}),))
    conn.commit()
    print(f"Created {created} new alerts. Theme 5-day moves: "
          + ", ".join(f"{k.split(' ')[0]} {v:+.1%}" for k, v in theme_moves.items()))
    if failures:
        print("Data issues:", {k: len(v) for k, v in failures.items()})
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"rerun_screen={'true' if rerun_screen else 'false'}\n")


def psycopg2_execute_values(cur, sql, rows):
    from psycopg2.extras import execute_values
    if rows:
        execute_values(cur, sql, rows)


def filings(cur, tk, cik, book):
    data = SEC.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", timeout=60).json()
    rec = data.get("filings", {}).get("recent", {})
    made = 0
    since = (TODAY - dt.timedelta(days=4)).isoformat()
    for i, form in enumerate(rec.get("form", [])):
        filed = rec["filingDate"][i]
        if filed < since:
            break
        if form not in ("8-K", "8-K/A", "10-Q", "10-K", "4", "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"):
            continue
        acc = rec["accessionNumber"][i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{rec['primaryDocument'][i]}"
        items = [x.strip() for x in (rec.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
        kind = ("; ".join(ITEMS_8K.get(x, f"Item {x}") for x in items) if form.startswith("8-K") else
                {"10-Q": "Quarterly report", "10-K": "Annual report", "4": "Insider transaction"}.get(form, "Activist / 5%+ stake"))
        cur.execute("""INSERT INTO v2_events (ticker, filed, form, kind, detail, url) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (url) DO NOTHING RETURNING id""", (tk, filed, form, kind, ", ".join(items), url))
        if not cur.fetchone():
            continue
        if form == "4":
            continue
        sev = "high" if set(items) & HIGH_8K else ("medium" if book or "2.02" in items or "13D" in form else "info")
        made += alert(cur, tk, f"New filing: {form}", sev, kind, f"filing-{acc}", source="sec",
                      action="Claude reads it against the thesis" if book else None)
    return made


# ------------------------------------------------------------------ weekly headlines
BAD = ("lawsuit", "investigation", "probe", "subpoena", "restat", "fraud", "downgrade", "recall", "guidance",
       "warns", "layoff", "bankrupt", "default", "short seller", "delist", "charges", "class action", "resigns")


def weekly(conn):
    cur = conn.cursor()
    names, _, _ = load(cur)
    added = 0
    for n in names:
        tk = n["ticker"]
        try:
            yt = yf.Ticker(tk)
            info = yt.info or {}
            news = yt.news or []
        except Exception:
            health_log["news_failed"].append(tk)
            continue
        short = (info.get("shortName") or n.get("name") or "").replace(",", "").split()
        keys = {tk.lower()} | ({short[0].lower()} if short and len(short[0]) > 3 else set())
        picked = []
        for item in news:
            c = item.get("content", item)
            title = c.get("title") or ""
            url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else None) or c.get("link")
            date = (c.get("pubDate") or "")[:10] or None
            tl = f" {title.lower()} "
            if not url or not any(f" {k} " in tl or f"{k}'" in tl or f"({k})" in tl or f" {k}:" in tl for k in keys):
                continue
            if date and date < (TODAY - dt.timedelta(days=8)).isoformat():
                continue
            pub = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
            picked.append((title, url, date, pub))
        for title, url, date, pub in picked[:5]:
            cur.execute("""INSERT INTO v2_news (ticker, published, headline, url, source) VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (url) DO NOTHING RETURNING id""", (tk, date or TODAY, title, url, pub or "Yahoo"))
            added += cur.fetchone() is not None
        time.sleep(0.2)
    conn.commit()
    print(f"Weekly headlines: {added} new for {len(names)} names.")


# ------------------------------------------------------------------ export for Claude's tasks
INBOX_FORMAT = {
    "where": f"Save one JSON file per task run into Drive folder ValueInvestor/v2-inbox (id {DRIVE_INBOX}). "
             "Name it <type>-<YYYY-MM-DD>-<short>.json. Never edit or delete other files.",
    "reason_cards": {"type": "reason_cards", "items": [{"ticker": "AMAT", "why_cheap": "...", "who_selling": "...",
                                                       "market_missing": "...", "buyers_back": "..."}]},
    "thesis": {"type": "thesis", "ticker": "AMAT", "add_to_watchlist": True, "themes": ["AI and energy infrastructure"],
               "conviction": 72, "verdict": "BUY|WATCH|PASS", "entry_target": 150.0,
               "valuation": {"iv_low": 140, "iv_base": 200, "iv_high": 260,
                             "inputs": {"fcf_ps": 7.5, "g1": 8, "g2": 5, "tg": 2.5, "dr": 9.5},
                             "kill": [{"type": "price_below|analyst_mean_below|manual", "value": 120.0, "text": "..."}]},
               "sections": {k: "text" for k in ["industry_map", "size_growth", "structure", "demand_supply",
                                                 "industry_risks", "outlook", "winners", "business", "right_to_win",
                                                 "moat", "tam_competitors", "management", "financials",
                                                 "company_risks", "variant_view", "valuation", "pillars",
                                                 "catalysts_flow", "premortem"]}},
    "news_tags": {"type": "news_tags", "items": [{"id": 123, "tag": "no change|strengthens|weakens|breaks",
                                                   "note": "one line on why"}]},
    "filing_tags": {"type": "filing_tags", "items": [{"id": 45, "tag": "no change|strengthens|weakens|breaks",
                                                       "note": "one line"}]},
    "buy_checks": {"type": "buy_checks", "items": [{"ticker": "VST", "verdict": "holds|weakened",
                                                     "note": "what changed since the last review"}]},
    "outlook": {"type": "outlook", "theme": "Critical resources", "kind": "light|full",
                "content": {"summary": "...", "demand_supply": "...", "risks": "...", "beneficiaries": ["..."]}},
}


def export(conn):
    cur = conn.cursor()
    ensure_schema(cur)
    names, theses, research = load(cur)
    tickers = [n["ticker"] for n in names]
    px = {}
    cur.execute("SELECT DISTINCT ON (ticker) ticker, close, date FROM v2_prices WHERE ticker = ANY(%s) ORDER BY ticker, date DESC", (tickers,))
    for r in cur.fetchall():
        px[r["ticker"]] = float(r["close"])
    cur.execute("SELECT * FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    run = cur.fetchone()
    screen = None
    if run:
        top = run["top10"] or []
        cur.execute("SELECT ticker, metrics FROM v2_screen_scores WHERE run_date = %s AND ticker = ANY(%s)",
                    (run["run_date"], [t["ticker"] for t in top]))
        met = {r["ticker"]: r["metrics"] for r in cur.fetchall()}
        cur.execute("SELECT ticker, reason_card IS NOT NULL AS has_card FROM v2_names WHERE ticker = ANY(%s)",
                    ([t["ticker"] for t in top],))
        cards = {r["ticker"]: r["has_card"] for r in cur.fetchall()}
        screen = {"date": str(run["run_date"]), "stages": run.get("stages"), "market": run.get("market"),
                  "top10": [{**t, "has_reason_card": cards.get(t["ticker"], False),
                             "name": (met.get(t["ticker"]) or {}).get("name"),
                             "sector": (met.get(t["ticker"]) or {}).get("sector"),
                             "industry": (met.get(t["ticker"]) or {}).get("industry"),
                             "fin": {k: ((met.get(t["ticker"]) or {}).get("fin") or {}).get(k) for k in
                                     ("pe", "pb", "ev_ebitda", "fcf_yield", "roc_avg", "rev_cagr", "net_margin",
                                      "nd_ebitda", "tests_passed", "tests_available")},
                             "px": {k: ((met.get(t["ticker"]) or {}).get("px") or {}).get(k) for k in
                                    ("price", "drawdown", "ret_3m", "high_52w")}} for t in top]}
    promoted = [t["ticker"] for t in (screen or {}).get("top10", []) if t.get("promote")]
    cur.execute("""SELECT n.ticker, n.name, n.sector, n.industry,
                     CASE WHEN n.status = 'deep_dive_requested' THEN 1
                          WHEN n.ticker = ANY(%s) THEN 2 ELSE 3 END AS priority,
                     CASE WHEN n.status = 'deep_dive_requested' THEN 'requested by Ming'
                          WHEN n.ticker = ANY(%s) THEN 'cleared this week''s deep-dive bar'
                          ELSE 'legacy candidate (CORE/HIGH) to re-underwrite' END AS why
                   FROM v2_names n
                   WHERE n.status = 'deep_dive_requested'
                      OR ((n.ticker = ANY(%s) OR n.status = 'candidate')
                          AND NOT EXISTS (SELECT 1 FROM v2_theses t WHERE t.ticker = n.ticker))
                   ORDER BY priority, n.ticker""", (promoted, promoted, promoted))
    dd = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT ON (ticker) ticker, data FROM v2_research WHERE ticker = ANY(%s) ORDER BY ticker, as_of DESC",
                ([d["ticker"] for d in dd],))
    rs = {r["ticker"]: r["data"] for r in cur.fetchall()}
    for i, d in enumerate(dd):
        r = (rs.get(d["ticker"]) or {}) if i < 6 else {}       # full research for the next 6 only, to keep the file small
        d["research"] = {k: r.get(k) for k in ("valuation", "moat", "risk", "red_flags", "peers", "analysts", "history",
                                                "business", "insiders", "ownership")} if r else None

    def summary(n):
        th = theses.get(n["ticker"]) or {}
        s = th.get("sections") or {}
        entry, src = entry_of(n["ticker"], theses, research)
        return {"ticker": n["ticker"], "name": n["name"], "level": n["level"], "core": n["is_core"],
                "themes": n["themes"], "health": n.get("health"), "price": px.get(n["ticker"]),
                "entry_target": entry, "entry_source": src, "thesis_version": th.get("version"),
                "thesis_date": str(th.get("created_at"))[:10] if th else None, "conviction": th.get("conviction"),
                "variant_view": s.get("variant_view"), "pillars": s.get("pillars"),
                "kill": (th.get("valuation") or {}).get("kill"),
                "legacy_thesis": None if th else ((n.get("legacy") or {}).get("thesis") or {}).get("investment_case")}

    cur.execute("SELECT id, ticker, published, headline, url, source FROM v2_news WHERE tag IS NULL "
                "AND published > CURRENT_DATE - 14 ORDER BY ticker, published DESC")
    news = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT e.id, e.ticker, e.filed, e.form, e.kind, e.url FROM v2_events e JOIN v2_names n USING (ticker)
                   WHERE n.level = 'book' AND e.form <> '4' AND e.filed > CURRENT_DATE - 7 AND e.tag IS NULL
                   ORDER BY e.filed DESC""")
    filings_ = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT ticker, detail, fired_at FROM v2_alerts WHERE kind = 'Buy signal' AND sent_via = 'hold' AND NOT sent")
    pending = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT ON (theme) theme, as_of, kind FROM v2_industry_outlooks ORDER BY theme, as_of DESC")
    outlooks = [dict(r) for r in cur.fetchall()]
    state = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "app_url": APP_URL, "inbox_folder_id": DRIVE_INBOX,
             "inbox_format": INBOX_FORMAT, "themes": list(THEMES), "screen": screen, "deep_dive_queue": dd,
             "watchlist_and_book": [summary(n) for n in names], "untagged_news": news, "new_book_filings": filings_,
             "pending_buy_checks": pending, "latest_outlooks": outlooks}
    text = json.dumps(state, default=str, indent=1)
    out = Path(__file__).parent.parent / "outbox"
    out.mkdir(exist_ok=True)
    (out / "state.json").write_text(text)
    try:
        sent = drive_update(DRIVE_STATE_FILE, text)
    except Exception as e:
        sent = False
        print("Drive update failed:", e.__class__.__name__, str(e)[:120])
    print(f"Exported state ({len(text) // 1024} KB): {len(names)} names, {len(dd)} deep-dive requests, "
          f"{len(news)} untagged headlines, {len(filings_)} new Book filings, {len(pending)} buy checks"
          + (" · saved to Drive" if sent else " · local only (no Drive credentials)"))


if __name__ == "__main__":
    conn = db()
    c = conn.cursor()
    ensure_schema(c)
    conn.commit()
    {"daily": daily, "weekly": weekly, "export": export}[MODE](conn)
    conn.close()
