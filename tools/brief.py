"""The daily brief (Telegram and email at 07:07 SGT; also shown at the top of the app's Decide page).

  build(cur, html=True) -> text

Thorough but short: written to be read in under 3 minutes (about 600 words at most). Every item links to where the
detail lives: the stock page, the Decide tab, the Outlook calendar, or the newsletter email itself.
Sections: market · your Book · to decide · holdings · risk check · Watchlist near buy · thesis activity · next 7 days ·
newsletters (one line per issue received) · screen.
"""
import os, re, datetime as dt

APP_URL = os.environ.get("APP_URL", "https://value-investor-weld.vercel.app").rstrip("/")
SGT = dt.timezone(dt.timedelta(hours=8))
WORD_BUDGET = 620                 # ~2.8 minutes at 220 words a minute
NAME_CAP, SECTOR_CAP, DRAWDOWN_ALERT = 0.10, 0.35, 0.15
DECIDE_KINDS = {"Kill criterion hit": "EXIT?", "Buy signal confirmed": "BUY", "Buy signal": "BUY", "Add zone": "ADD",
                "Trim zone": "TRIM", "Buy signal, thesis weakened": "REVIEW", "Add zone, thesis flagged": "REVIEW",
                "Fair value reached": "REVIEW", "Thesis refreshed after results": "APPROVE", "Earnings preview": "PREPARE",
                "Old idea at buy level": "RE-CHECK", "Screen idea on sale": "LOOK", "Newsletter flag": "REVIEW",
                "Second opinion split": "REVIEW", "Open red-team points": "REVIEW", "Engine values added": "APPROVE"}
ORDER = ["EXIT?", "BUY", "ADD", "TRIM", "REVIEW", "PREPARE", "APPROVE", "RE-CHECK", "LOOK"]
MARKET = [("^GSPC", "S&P 500"), ("QQQ", "Nasdaq 100"), ("^VIX", "VIX"), ("^TNX", "10y"), ("DX-Y.NYB", "USD"),
          ("CL=F", "Oil"), ("GC=F", "Gold")]


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _last2(cur, t):
    cur.execute("SELECT close FROM v2_prices WHERE ticker = %s ORDER BY date DESC LIMIT 2", (t,))
    r = [_f(x["close"]) for x in cur.fetchall()]
    return (r[0], r[1]) if len(r) == 2 else (r[0] if r else None, None)


def _safe(fn, *a):
    """A broken section must never stop the brief."""
    try:
        return fn(*a)
    except Exception as ex:                      # noqa: BLE001 - deliberately broad: the brief always goes out
        a[0].connection.rollback()
        return [f"<i>(section unavailable: {ex.__class__.__name__})</i>"]


def build(cur, html=True):
    e = (lambda s: str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if html else str
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    link = (lambda url, text: f'<a href="{url}">{text}</a>') if html else (lambda url, text: text)
    a = lambda t: link(f"{APP_URL}/name/{t}", e(t))
    today = dt.datetime.now(SGT).date()
    sections = []          # (title, lines, priority): lower priority is trimmed first when over the word budget

    # ---------------------------------------------------------------- market
    def market(cur):
        bits = []
        for sym, label in MARKET:
            p, p1 = _last2(cur, sym)
            if p is None:
                continue
            if sym == "^TNX":
                bits.append(f"{label} {p:.2f}%" + (f" ({(p - p1) * 100:+.0f}bp)" if p1 else ""))
            elif sym == "^VIX":
                bits.append(f"{label} {p:.1f}")
            else:
                bits.append(f"{label} " + (f"{p:,.0f}" if p > 100 else f"{p:,.2f}") + (f" ({p / p1 - 1:+.1%})" if p1 else ""))
        cur.execute("SELECT value FROM v2_settings WHERE key = 'macro'")
        m = (cur.fetchone() or {}).get("value") or {}
        out = [" · ".join(bits)] if bits else []
        for fl in (m.get("flags") or [])[:2]:
            out.append("⚠ " + e(fl))
        sc = m.get("scenarios") or {}
        worst = min(((k, v.get("book")) for k, v in sc.items() if v.get("book") is not None), key=lambda kv: kv[1], default=None)
        if worst:
            out.append(f"Biggest exposure: {e(worst[0])} → Book {worst[1]:+.0%} · " + link(f"{APP_URL}/outlook", "Outlook"))
        return out

    # ---------------------------------------------------------------- Book and holdings
    cur.execute("""SELECT p.ticker, p.shares, p.cost, n.sector, n.health, n.health_why, n.style,
                     (SELECT close FROM v2_prices WHERE ticker = p.ticker ORDER BY date DESC LIMIT 1) AS px,
                     (SELECT close FROM v2_prices WHERE ticker = p.ticker ORDER BY date DESC OFFSET 1 LIMIT 1) AS px1
                   FROM (SELECT DISTINCT ON (ticker) * FROM v2_positions WHERE exited IS NULL ORDER BY ticker, entered DESC) p
                   LEFT JOIN v2_names n USING (ticker) WHERE n.level = 'book'""")
    pos = [dict(r) for r in cur.fetchall()]
    for r in pos:
        r["mv"] = (_f(r["shares"]) or 0) * (_f(r["px"]) or 0)
        r["day"] = (_f(r["px"]) / _f(r["px1"]) - 1) if _f(r["px"]) and _f(r["px1"]) else None
    mv = sum(r["mv"] for r in pos)

    def book(cur):
        if not mv:
            return []
        mv1 = sum((_f(r["shares"]) or 0) * (_f(r["px1"]) or _f(r["px"]) or 0) for r in pos)
        cost = sum((_f(r["shares"]) or 0) * (_f(r["cost"]) or 0) for r in pos if r["cost"])
        s0, s1 = _last2(cur, "SPY")
        line = f"${mv:,.0f} · day {mv / mv1 - 1:+.1%}" if mv1 else f"${mv:,.0f}"
        line += f" (S&P {s0 / s1 - 1:+.1%})" if s0 and s1 else ""
        line += f" · P&L {mv / cost - 1:+.0%}" if cost else ""
        mov = sorted([r for r in pos if r["day"] is not None], key=lambda r: r["day"])
        out = [line]
        if mov:
            up = [r for r in mov[::-1] if r["day"] > 0][:3]
            dn = [r for r in mov if r["day"] < 0][:3]
            if up:
                out.append("Up: " + ", ".join(f"{a(r['ticker'])} {r['day']:+.1%}" for r in up))
            if dn:
                out.append("Down: " + ", ".join(f"{a(r['ticker'])} {r['day']:+.1%}" for r in dn))
        return out

    def holdings(cur):
        if not pos:
            return []
        cur.execute("""SELECT DISTINCT ON (ticker) ticker, sections->'exec_summary' AS es, entry_target
                       FROM v2_theses WHERE ticker = ANY(%s) ORDER BY ticker, version DESC""", ([r["ticker"] for r in pos],))
        th = {r["ticker"]: r for r in cur.fetchall()}
        dot = {"red": "🔴", "amber": "🟠", "green": "🟢", "grey": "⚪"} if html else {}
        out = []
        for r in sorted(pos, key=lambda r: -r["mv"]):
            es = (th.get(r["ticker"]) or {}).get("es") or {}
            plan = es.get("plan") or {}
            lv = []
            if _f(plan.get("add_below")):
                lv.append(f"add &lt;${_f(plan['add_below']):,.0f}" if html else f"add <${_f(plan['add_below']):,.0f}")
            if _f(plan.get("trim_above")):
                lv.append(f"trim &gt;${_f(plan['trim_above']):,.0f}" if html else f"trim >${_f(plan['trim_above']):,.0f}")
            w = r["mv"] / mv if mv else 0
            out.append(f"{dot.get(r['health'] or '', '•')} {a(r['ticker'])} ${_f(r['px']) or 0:,.2f}"
                       + (f" {r['day']:+.1%}" if r["day"] is not None else "") + f" · {w:.0%}"
                       + (f" · {e(es.get('recommendation'))}" if es.get("recommendation") else "")
                       + (f" · {' / '.join(lv)}" if lv else ""))
        return out

    def risk(cur):
        out = []
        if not mv:
            return out
        for r in pos:
            if r["mv"] / mv > NAME_CAP:
                out.append(f"{a(r['ticker'])} is {r['mv'] / mv:.0%} of the Book (limit {NAME_CAP:.0%})")
        sec = {}
        for r in pos:
            sec[r["sector"] or "Unknown"] = sec.get(r["sector"] or "Unknown", 0) + r["mv"] / mv
        out += [f"{e(k)} {v:.0%} (cap {SECTOR_CAP:.0%})" for k, v in sorted(sec.items(), key=lambda kv: -kv[1]) if v > SECTOR_CAP]
        cur.execute("""SELECT date, SUM(close * sh) AS v FROM v2_prices JOIN (VALUES {}) AS s(t, sh) ON s.t = ticker
                       WHERE date > CURRENT_DATE - 380 GROUP BY date HAVING COUNT(*) = %s ORDER BY date""".format(
            ",".join(["(%s, %s::numeric)"] * len(pos))), [x for r in pos for x in (r["ticker"], _f(r["shares"]) or 0)] + [len(pos)])
        ser = [_f(r["v"]) for r in cur.fetchall() if _f(r["v"])]
        if ser:
            dd = ser[-1] / max(ser) - 1
            if dd < -DRAWDOWN_ALERT:
                out.append(f"Book {dd:.0%} from its 1-year peak (alert past {DRAWDOWN_ALERT:.0%})")
        if out:
            out.append(link(f"{APP_URL}/book#risk", "Portfolio risk"))
        return out

    # ---------------------------------------------------------------- decisions
    def decide(cur):
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
        out = [f"{act} {a(t)}: {e(d[:110])}" for _, act, t, d in dec[:6]]
        if len(dec) > 6:
            out.append(f"…{len(dec) - 6} more")
        if pend:
            out.append(f"{pend} thesis update(s) waiting for approval")
        if out:
            out.append(link(f"{APP_URL}/decide", "Open the Decide tab"))
        return out

    def near_buy(cur):
        cur.execute("""SELECT n.ticker, t.entry_target, t.conviction,
                         (SELECT close FROM v2_prices WHERE ticker = n.ticker ORDER BY date DESC LIMIT 1) AS px
                       FROM v2_names n JOIN LATERAL (SELECT * FROM v2_theses WHERE ticker = n.ticker ORDER BY version DESC LIMIT 1) t ON TRUE
                       WHERE n.level = 'watchlist' AND t.entry_target IS NOT NULL""")
        rows = [(r["ticker"], _f(r["px"]) / _f(r["entry_target"]) - 1, r) for r in cur.fetchall()
                if _f(r["px"]) and _f(r["entry_target"])]
        rows = sorted([x for x in rows if x[1] <= 0.10], key=lambda x: x[1])[:5]
        return [f"{a(t)} ${_f(r['px']):,.2f} vs entry ${_f(r['entry_target']):,.2f} ({g:+.0%}) · conviction {r['conviction'] or '—'}"
                for t, g, r in rows]

    def activity(cur):
        cur.execute("""SELECT kind, COUNT(*) AS c FROM v2_inbox_log WHERE loaded_at > now() - interval '24 hours'
                       AND kind IS NOT NULL AND kind NOT IN ('error', 'duplicate') GROUP BY kind""")
        new = {r["kind"]: r["c"] for r in cur.fetchall()}
        label = {"thesis": "new theses", "refresh": "thesis refreshes", "reason_cards": "reason cards", "news_tags": "news tags",
                 "filing_tags": "filings read", "buy_checks": "buy checks", "week_ahead": "week ahead",
                 "newsletter_tags": "newsletter reads", "outlook": "theme outlooks", "book": "Book update",
                 "tam": "TAM updates", "second_opinion": "reviews", "exec_summary": "report updates"}
        out = [", ".join(f"{c} {label.get(k, k)}" for k, c in new.items())] if new else []
        cur.execute("""SELECT ticker, verdict, result FROM v2_second_opinion WHERE kind = 'review'
                       AND created_at > now() - interval '24 hours' ORDER BY created_at DESC LIMIT 4""")
        for r in cur.fetchall():
            res = r["result"] or {}
            imp = [i for i in res.get("improvements") or [] if str(i.get("priority")).lower() == "high"]
            gap = (res.get("gap") or {}).get("gap")
            out.append(f"Review {a(r['ticker'])}: " + ("split" if res.get("split") else "agrees")
                       + (f", value gap {gap:+.0%}" if gap is not None else "")
                       + (f"; top fix: {e(imp[0]['suggestion'][:90])}" if imp else ""))
        return out

    def week(cur):
        cur.execute("""SELECT c.date, c.time_et, c.kind, c.ticker, c.title, c.detail, n.level FROM v2_calendar c
                       LEFT JOIN v2_names n USING (ticker)
                       WHERE c.date BETWEEN CURRENT_DATE AND CURRENT_DATE + 7
                         AND (c.kind <> 'earnings' OR n.level IN ('watchlist','book')) AND c.kind <> 'market'
                       ORDER BY c.date, c.time_et NULLS LAST LIMIT 8""")
        ev = cur.fetchall()
        cur.execute("SELECT value FROM v2_settings WHERE key = 'week_ahead'")
        wa = (cur.fetchone() or {}).get("value") or {}
        out = []
        if wa.get("summary") and str(wa.get("as_of", "")) >= str(today - dt.timedelta(days=7)):
            out.append(e(wa["summary"].split(". ")[0][:180]))
        for x in ev:
            im = (x["detail"] or {}).get("implied_move")
            what = f"{a(x['ticker'])} results" + (f" (±{im:.0%} implied)" if im else "") if x["ticker"] else e(x["title"][:70])
            out.append(f"{x['date']:%a %d}{(' ' + x['time_et']) if x['time_et'] else ''}: {what}")
        if out:
            out.append(link(f"{APP_URL}/outlook", "Calendar"))
        return out

    # ---------------------------------------------------------------- newsletters: one line per issue received
    def newsletters(cur):
        out = []
        cur.execute("""SELECT date, source, subject, headline, url FROM v2_newsletter_issues
                       WHERE loaded_at > now() - interval '36 hours' ORDER BY date DESC, source LIMIT 14""")
        issues = cur.fetchall()
        for r in issues:
            text = e((r["headline"] or r["subject"] or "")[:150])
            out.append(f"• {b(e(r['source']))}: " + (link(r["url"], text) if r["url"] else text))
        if not issues:                                            # older loads: best point from each source
            cur.execute("""SELECT DISTINCT ON (source) source, point, ticker, topic FROM v2_newsletter_mentions
                           WHERE loaded_at > now() - interval '36 hours' ORDER BY source, thesis_relevant DESC, id""")
            out += [f"• {b(e(r['source']))}: {e((r['point'] or '')[:150])}" for r in cur.fetchall()]
        cur.execute("""SELECT m.ticker, m.stance, m.point, m.source FROM v2_newsletter_mentions m JOIN v2_names n USING (ticker)
                       WHERE m.loaded_at > now() - interval '36 hours' AND n.level IN ('book','watchlist')
                       ORDER BY (n.level = 'book') DESC, m.thesis_relevant DESC LIMIT 4""")
        mine = cur.fetchall()
        if mine:
            arrow = {"positive": "▲", "negative": "▼"}
            out.append("On your names: " + " · ".join(
                f"{arrow.get(r['stance'], '•')} {a(r['ticker'])} {e((r['point'] or '')[:80])} ({e(r['source'])})" for r in mine))
        return out

    def screen(cur):
        cur.execute("SELECT run_date, top10, growth_top10 FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
        run = cur.fetchone()
        if not run or run["run_date"] < today - dt.timedelta(days=7):
            return []
        v = [t["ticker"] for t in (run["top10"] or [])[:3]]
        g = [t["ticker"] for t in (run["growth_top10"] or [])[:3]]
        return [f"{run['run_date']:%d %b} · value: {', '.join(a(t) for t in v)} · growth: {', '.join(a(t) for t in g)} · "
                + link(f"{APP_URL}/screen", "Screen")]

    sections = [("Market", _safe(market, cur), 3), ("Your Book", _safe(book, cur), 1),
                ("To decide", _safe(decide, cur), 0), ("Holdings", _safe(holdings, cur), 2),
                ("Risk check", _safe(risk, cur), 2), ("Watchlist near buy", _safe(near_buy, cur), 4),
                ("Thesis activity", _safe(activity, cur), 5), ("Next 7 days", _safe(week, cur), 4),
                ("Newsletters", _safe(newsletters, cur), 3), ("Screen", _safe(screen, cur), 6)]

    def render(secs):
        body = [b(f"ValueInvestor · {today:%a %d %b}")]
        for title, lines, _ in secs:
            if lines:
                body.append(b(title) + "\n" + "\n".join(lines))
        if html:
            body.append(" · ".join(link(f"{APP_URL}/{p}", p.capitalize()) for p in ("decide", "book", "watchlist", "outlook", "screen")))
        return "\n\n".join(body)

    def words(t):
        return len(re.sub(r"<[^>]+>", " ", t).split())

    # trim the least important sections, then the longest lists, until it reads in under 3 minutes
    secs = [list(s) for s in sections]
    text = render(secs)
    for pr in (6, 5, 4, 3):
        if words(text) <= WORD_BUDGET:
            break
        for s in secs:
            if s[2] == pr and len(s[1]) > 2:
                s[1] = s[1][:2] + ([s[1][-1]] if "href" in s[1][-1] else [])
        text = render(secs)
    mins = max(1, round(words(text) / 220 + 0.4))
    return text.replace(b(f"ValueInvestor · {today:%a %d %b}"), b(f"ValueInvestor · {today:%a %d %b}") + f" · ~{mins} min read", 1)
