"""One quick summary of everything (Telegram daily brief; also shown at the top of the app's Decide page).

  build(cur, html=True) -> text      sections: portfolio, decisions, Book zones and health, macro, next 7 days,
                                     new from Claude, screen
Short by design: one line per item, the five most important items per section, links into the app.
"""
import os, datetime as dt

APP_URL = os.environ.get("APP_URL", "https://value-investor-weld.vercel.app").rstrip("/")
DECIDE_KINDS = {"Kill criterion hit": "EXIT?", "Buy signal confirmed": "BUY", "Buy signal": "BUY", "Add zone": "ADD",
                "Trim zone": "TRIM", "Buy signal, thesis weakened": "REVIEW", "Add zone, thesis flagged": "REVIEW",
                "Fair value reached": "REVIEW", "Thesis refreshed after results": "APPROVE", "Earnings preview": "PREPARE",
                "Old idea at buy level": "RE-CHECK", "Screen idea on sale": "LOOK"}
ORDER = ["EXIT?", "BUY", "ADD", "TRIM", "REVIEW", "PREPARE", "APPROVE", "RE-CHECK", "LOOK"]


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build(cur, html=True):
    e = (lambda s: str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if html else str
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    a = (lambda t: f'<a href="{APP_URL}/name/{t}">{e(t)}</a>') if html else (lambda t: t)
    today = dt.date.today()
    out = [b(f"ValueInvestor · {today:%a %d %b}")]

    # ---- portfolio value and day move
    cur.execute("""SELECT p.ticker, p.shares, p.cost,
                     (SELECT close FROM v2_prices WHERE ticker = p.ticker ORDER BY date DESC LIMIT 1) AS px,
                     (SELECT close FROM v2_prices WHERE ticker = p.ticker ORDER BY date DESC OFFSET 1 LIMIT 1) AS px1
                   FROM (SELECT DISTINCT ON (ticker) * FROM v2_positions WHERE exited IS NULL ORDER BY ticker, entered DESC) p""")
    pos = cur.fetchall()
    mv = sum((_f(r["shares"]) or 0) * (_f(r["px"]) or 0) for r in pos)
    mv1 = sum((_f(r["shares"]) or 0) * (_f(r["px1"]) or _f(r["px"]) or 0) for r in pos)
    cost = sum((_f(r["shares"]) or 0) * (_f(r["cost"]) or 0) for r in pos if r["cost"])
    cur.execute("SELECT close FROM v2_prices WHERE ticker = 'SPY' ORDER BY date DESC LIMIT 2")
    spy = [_f(r["close"]) for r in cur.fetchall()]
    if mv:
        day = mv / mv1 - 1 if mv1 else None
        line = f"Book ${mv:,.0f}" + (f" · day {day:+.1%}" if day is not None else "")
        line += f" (S&P {spy[0] / spy[1] - 1:+.1%})" if len(spy) == 2 and spy[1] else ""
        line += f" · P&L {mv / cost - 1:+.0%}" if cost else ""
        movers = sorted(((r["ticker"], _f(r["px"]) / _f(r["px1"]) - 1) for r in pos if _f(r["px"]) and _f(r["px1"])),
                        key=lambda kv: -abs(kv[1]))[:3]
        if movers:
            line += "\nBiggest moves: " + ", ".join(f"{a(t)} {m:+.1%}" for t, m in movers)
        out.append(line)

    # ---- decisions
    cur.execute("""SELECT a.ticker, a.kind, a.detail FROM v2_alerts a JOIN v2_names n USING (ticker)
                   WHERE NOT a.addressed AND a.kind = ANY(%s) AND a.fired_at > now() - interval '21 days'
                     AND COALESCE(a.sent_via, '') <> 'hold' ORDER BY a.fired_at DESC""", (list(DECIDE_KINDS),))
    seen, dec = set(), []
    for r in cur.fetchall():
        act = DECIDE_KINDS[r["kind"]]
        if (r["ticker"], act) in seen:
            continue
        seen.add((r["ticker"], act))
        dec.append((ORDER.index(act), act, r["ticker"], r["detail"] or ""))
    cur.execute("""SELECT COUNT(DISTINCT t.ticker) AS c FROM v2_theses t JOIN v2_names n USING (ticker)
                   WHERE t.approval = 'pending' AND n.level IN ('watchlist','book')""")
    pend = cur.fetchone()["c"]
    dec.sort()
    if dec or pend:
        lines = [f"{act} {a(t)}: {e(d[:90])}" for _, act, t, d in dec[:6]]
        more = len(dec) - 6
        tail = (f"\n…{more} more" if more > 0 else "") + (f"\n{pend} thesis(es) waiting for approval" if pend else "")
        link = f' <a href="{APP_URL}/decide">Decide</a>' if html else ""
        out.append(b(f"To decide ({len(dec)})") + link + "\n" + "\n".join(lines) + tail)

    # ---- Claude's call on every holding (from each thesis's executive summary)
    cur.execute("""SELECT DISTINCT ON (t.ticker) t.ticker, t.sections->'exec_summary'->>'recommendation' AS call
                   FROM v2_theses t JOIN v2_names n USING (ticker) WHERE n.level = 'book' ORDER BY t.ticker, t.version DESC""")
    calls = {}
    for r in cur.fetchall():
        if r["call"]:
            calls.setdefault(r["call"], []).append(r["ticker"])
    if calls:
        out.append(b("Claude's calls on your Book") + "\n" + " · ".join(
            f"{c} {', '.join(a(t) for t in calls[c])}" for c in ("CUT", "TRIM", "ADD", "HOLD") if c in calls))

    # ---- Book health
    cur.execute("SELECT ticker, health, health_why FROM v2_names WHERE level = 'book' AND health IN ('red','amber') ORDER BY health DESC, ticker")
    hl = cur.fetchall()
    if hl:
        dot = {"red": "🔴", "amber": "🟠"} if html else {"red": "red", "amber": "amber"}
        out.append(b("Book health") + "\n" + "\n".join(f"{dot[r['health']]} {a(r['ticker'])} {e((r['health_why'] or '')[:80])}" for r in hl[:6]))

    # ---- macro
    cur.execute("SELECT value FROM v2_settings WHERE key = 'macro'")
    m = (cur.fetchone() or {}).get("value") or {}
    if m:
        r = m.get("regime") or {}
        bits = []
        if r.get("y10") is not None:
            bits.append(f"10y {r['y10']:.2f}%" + (f" ({r['y10_chg_1m']:+.2f} 1m)" if r.get("y10_chg_1m") is not None else ""))
        if r.get("vix") is not None:
            bits.append(f"VIX {r['vix']:.0f}")
        if r.get("usd_chg_1m") is not None:
            bits.append(f"USD {r['usd_chg_1m']:+.1%} 1m")
        if r.get("oil") is not None:
            bits.append(f"oil ${r['oil']:.0f}")
        sc = (m.get("scenarios") or {})
        worst = min(((k, v.get("book")) for k, v in sc.items() if v.get("book") is not None), key=lambda kv: kv[1], default=None)
        txt = " · ".join(bits)
        if m.get("flags"):
            txt += "\n⚠ " + e("; ".join(m["flags"][:3]))
        if worst:
            txt += f"\nBiggest exposure: {e(worst[0])} → Book {worst[1]:+.0%}"
        out.append(b("Macro") + "\n" + txt)

    # ---- next 7 days
    cur.execute("""SELECT c.date, c.time_et, c.kind, c.ticker, c.title, c.detail, n.level FROM v2_calendar c
                   LEFT JOIN v2_names n USING (ticker)
                   WHERE c.date BETWEEN CURRENT_DATE AND CURRENT_DATE + 7
                     AND (c.kind <> 'earnings' OR n.level IN ('watchlist','book')) AND c.kind <> 'market'
                   ORDER BY c.date, c.time_et NULLS LAST LIMIT 8""")
    ev = cur.fetchall()
    cur.execute("SELECT value FROM v2_settings WHERE key = 'week_ahead'")
    wa = (cur.fetchone() or {}).get("value") or {}
    if ev or wa.get("summary"):
        lines = []
        for x in ev:
            im = (x["detail"] or {}).get("implied_move")
            what = f"{a(x['ticker'])} results" + (f" (±{im:.0%})" if im else "") if x["ticker"] else e(x["title"][:60])
            lines.append(f"{x['date']:%a %d}: {what}")
        head = e(wa["summary"].split(". ")[0][:160]) + "\n" if wa.get("summary") and str(wa.get("as_of", "")) >= str(today - dt.timedelta(days=7)) else ""
        out.append(b("Next 7 days") + "\n" + head + "\n".join(lines))

    # ---- new from Claude (last 24h) and the screen
    cur.execute("""SELECT kind, COUNT(*) AS c FROM v2_inbox_log WHERE loaded_at > now() - interval '24 hours'
                   AND kind IS NOT NULL AND kind <> 'error' GROUP BY kind""")
    new = {r["kind"]: r["c"] for r in cur.fetchall()}
    if new:
        label = {"thesis": "new theses", "refresh": "results refreshes", "reason_cards": "reason/growth cards",
                 "news_tags": "news tags", "filing_tags": "filing reads", "buy_checks": "buy checks",
                 "week_ahead": "week ahead", "outlook": "theme outlooks", "book": "Book update", "tam": "TAM updates"}
        out.append(b("New from Claude (24h)") + "\n" + ", ".join(f"{c} {label.get(k, k)}" for k, c in new.items()))
    cur.execute("SELECT run_date, top10, growth_top10 FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    run = cur.fetchone()
    if run and run["run_date"] >= today - dt.timedelta(days=7):
        v = [t["ticker"] for t in (run["top10"] or [])[:3]]
        g = [t["ticker"] for t in (run["growth_top10"] or [])[:3]]
        out.append(b(f"Screen ({run['run_date']:%d %b})") + f"\nValue: {', '.join(a(t) for t in v)} · Growth: {', '.join(a(t) for t in g)}")

    if html:
        out.append(f'<a href="{APP_URL}/decide">Decide</a> · <a href="{APP_URL}/book">Book</a> · '
                   f'<a href="{APP_URL}/outlook">Outlook</a> · <a href="{APP_URL}/screen">Screen</a>')
    return "\n\n".join(out)
