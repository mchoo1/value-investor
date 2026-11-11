"""Telegram messages.

  python tools\\notify.py alerts    red alerts at once; up to 5 medium alerts a day; the rest wait for the digest
  python tools\\notify.py brief     daily brief: overnight alerts + one-screen summary of everything (also: daily)
  python tools\\notify.py screen    Saturday screen summary
  python tools\\notify.py digest    Sunday weekly digest
  python tools\\notify.py catchup   (hourly) send today's brief if the 07:07 run did not (Tue-Sat, 08:00-22:00 SGT)
  python tools\\notify.py jobfail NAME URL   tell Ming a scheduled job failed
Set TELEGRAM_DRY=1 to print instead of sending.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os, datetime as _dt
from common import db, ensure_schema, telegram, email, esc, heartbeat, APP_URL, TODAY
SGT = _dt.timezone(_dt.timedelta(hours=8))

import re
MODE = sys.argv[1] if len(sys.argv) > 1 else "alerts"
_SUFFIX = re.compile(r"\s+(Class [A-Z]\s+)?(Common|Ordinary|Capital)\s+(Stock|Shares)\b.*$|\s+Common Stock$|\s+Class [A-Z]$|\s+\(Holding.*$", re.I)
cname = lambda s: _SUFFIX.sub("", s or "").strip()
DAILY_CAP = 5
ICON = {"high": "🔴", "medium": "🟠", "info": "⚪"}


def link(t):
    if t in ("MARKET", "SYSTEM"):
        return f"<b>{t.title()}</b>"
    return f'<a href="{APP_URL}/name/{t}">{esc(t)}</a>'


def line(a):
    return f"{ICON.get(a['severity'], '')} {link(a['ticker'])} · <b>{esc(a['kind'])}</b>: {esc(a['detail'])}"


def lines(alerts_):
    """One line per alert, but alerts with the same kind and detail (e.g. a theme sell-off hitting six names) share a line."""
    groups, order = {}, []
    collapse = {"New thesis ready": "approve in the app", "New filing: 8-K": "Claude reads them against the thesis",
                "New filing: 10-Q": "quarterly reports filed", "New filing: 10-K": "annual reports filed"}
    for a in alerts_:
        k = (a["severity"], a["kind"], collapse.get(a["kind"], a["detail"]))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(a["ticker"])
    out = []
    for sev, kind, detail in order:
        ts = list(dict.fromkeys(groups[(sev, kind, detail)]))
        out.append(f"{ICON.get(sev, '')} {', '.join(link(t) for t in ts)} · <b>{esc(kind)}</b>: {esc(detail)}")
    return "\n".join(out)


def mark(cur, ids, via):
    if ids:
        cur.execute("UPDATE v2_alerts SET sent = TRUE, sent_via = %s, sent_at = now() WHERE id = ANY(%s)", (via, ids))


def quiet_now():
    """No alert pings between 23:00 and 07:00 Singapore time; alerts stay queued for the 07:07 run."""
    import datetime as dt
    h = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).hour
    return h >= 23 or h < 7


def alerts(conn, with_health=False):
    cur = conn.cursor()
    if quiet_now() and "--force" not in sys.argv:
        print("Quiet hours (23:00-07:00 SGT): alerts stay queued.")
        return
    cur.execute("""SELECT * FROM v2_alerts WHERE NOT sent AND sent_via IS NULL AND COALESCE(source, '') <> 'legacy'
                   ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, fired_at""")
    rows = cur.fetchall()
    high = [a for a in rows if a["severity"] == "high"]
    medium = [a for a in rows if a["severity"] == "medium"]
    info = [a for a in rows if a["severity"] == "info"]
    cur.execute("""SELECT COUNT(*) AS c FROM v2_alerts WHERE sent_via = 'telegram' AND severity = 'medium'
                   AND sent_at::date = CURRENT_DATE""")
    room = max(DAILY_CAP - cur.fetchone()["c"], 0)
    now_, later = medium[:room], medium[room:]
    parts = []
    if high:
        parts.append("<b>Needs attention</b>\n" + lines(high))
    if now_:
        parts.append("<b>Worth a look</b>\n" + lines(now_))
    if later:
        parts.append(f"<i>{len(later)} more alert(s) held for Sunday's digest.</i>")
    if with_health:
        cur.execute("SELECT value FROM v2_settings WHERE key = 'data_health'")
        h = cur.fetchone()
        fails = ((h or {}).get("value") or {}).get("failures") or {}
        if fails:
            parts.append("⚙️ <i>Data issues today: " + esc(", ".join(f"{k.replace('_', ' ')} {len(v)}" for k, v in fails.items()))
                         + ". Alerts may be incomplete.</i>")
    if parts and (high or now_ or (with_health and parts)):
        ok = telegram("\n\n".join(parts) + f'\n\n<a href="{APP_URL}/decide">Decide</a> · <a href="{APP_URL}/book">Book</a>')
        if not ok:
            print("Telegram not delivered; alerts stay queued.")
            return
    mark(cur, [a["id"] for a in high + now_], "telegram")
    mark(cur, [a["id"] for a in later + info], "digest")
    conn.commit()
    print(f"Alerts: {len(high)} high and {len(now_)} medium sent; {len(later) + len(info)} held for the digest.")


def brief(conn):
    """Daily 07:07 brief: overnight alerts first, then the one-screen summary of everything (tools/brief.py)."""
    from brief import build
    cur = conn.cursor()
    cur.execute("""SELECT * FROM v2_alerts WHERE NOT sent AND sent_via IS NULL AND COALESCE(source, '') <> 'legacy'
                   ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, fired_at""")
    rows = cur.fetchall()
    urgent = [a for a in rows if a["severity"] in ("high", "medium")]
    shown, rest = urgent[:20], urgent[20:] + [a for a in rows if a["severity"] == "info"]
    head = ""
    if shown:
        head = "<b>Overnight alerts</b>\n" + lines(shown) + (
            f"\n<i>+{len(rest)} more in the app</i>" if rest else "") + "\n\n"
    cur.execute("SELECT value FROM v2_settings WHERE key = 'data_health'")
    h = cur.fetchone()
    fails = ((h or {}).get("value") or {}).get("failures") or {}
    tail = ("\n\n⚙️ <i>Data issues today: " + esc(", ".join(f"{k.replace('_', ' ')} {len(v)}" for k, v in fails.items()))
            + "</i>") if fails else ""
    text = head + build(cur) + tail
    mailed = email(f"ValueInvestor daily brief · {_dt.datetime.now(SGT):%a %d %b}", text)
    tg_ready = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and not os.environ.get("TELEGRAM_DRY")
    sent = telegram(text)
    heartbeat(cur, "brief", ok=bool(sent and tg_ready), note={
        "sgt_date": str(_dt.datetime.now(SGT).date()), "telegram": bool(sent and tg_ready), "email": bool(mailed),
        "why": None if tg_ready else "TELEGRAM_BOT_TOKEN is not set for this job"})
    conn.commit()
    if not tg_ready:
        print("::warning::Telegram is not configured (TELEGRAM_BOT_TOKEN missing): the brief was not sent to Telegram.")
    if sent:
        mark(cur, [a["id"] for a in shown], "telegram")
        mark(cur, [a["id"] for a in rest], "digest")
        conn.commit()
        print(f"Brief sent ({len(shown)} alerts in it).")
    else:
        print("Brief not delivered; alerts stay queued.")


def catchup(conn):
    """Safety net, run every 2 hours: if today's brief has not gone out by 08:00 SGT (Tue-Sat), send it now."""
    now = _dt.datetime.now(SGT)
    if now.isoweekday() not in (2, 3, 4, 5, 6) or not (8 <= now.hour < 22):
        return
    cur = conn.cursor()
    cur.execute("SELECT value FROM v2_settings WHERE key = 'heartbeat:brief'")
    v = ((cur.fetchone() or {}).get("value") or {})
    if ((v.get("note") or {}) if isinstance(v.get("note"), dict) else {}).get("sgt_date") == str(now.date()):
        return
    print("Today's brief was not sent at 07:07: sending it now.")
    brief(conn)


def jobfail(name, url):
    text = (f"⚠️ <b>ValueInvestor job failed</b>: {esc(name)}\nThe rest of the system keeps running; this job's output is "
            f"missing until it runs again.\n<a href=\"{esc(url)}\">Open the run log</a>")
    email(f"ValueInvestor job failed: {name}", text)
    telegram(text)


def screen(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    run = cur.fetchone()
    if not run:
        print("No screen run yet.")
        return
    st, top, gtop = run.get("stages") or {}, run.get("top10") or [], run.get("growth_top10") or []
    names = {}
    allt = [t["ticker"] for t in top + gtop]
    if allt:
        cur.execute("SELECT ticker, name FROM v2_names WHERE ticker = ANY(%s)", (allt,))
        names = {r["ticker"]: r["name"] for r in cur.fetchall()}
    label = {"quant_value": "value", "fallen_quality": "fallen quality", "sharp_drop": "sharp drop",
             "fast_growth": "fast growth", "accelerating": "accelerating", "smart_money": "smart money",
             "share_taker": "share taker", "momentum_leader": "momentum",
             "estimates_rising": "estimates up", "insider_buying": "insiders buying", "activist": "activist 13D",
             "newsletter_buzz": "newsletter buzz"}

    def rows(lst, growth=False):
        out = []
        for i, t in enumerate(lst, 1):
            line_ = (f"{i}. {link(t['ticker'])} {esc(cname(names.get(t['ticker']))[:28])} · {t['score']:.0f} · "
                     + ", ".join(label.get(k, k) for k, v in (t.get("funnels") or {}).items() if v)
                     + (" ✅" if t.get("promote") else ""))
            if growth and t.get("parts"):
                line_ += f" · moat {t['parts'].get('moat', 0):.0f}"
            if growth and t.get("flags"):
                line_ += f"\n    ⚠ {esc(t['flags'][0])}"
            out.append(line_)
        return "\n".join(out)
    b = (run.get("market") or {}).get("buffett")
    ts = (run.get("market") or {}).get("theme_strength_6m") or {}
    tline = " · ".join(f"{k.split(' and ')[0]} {v:+.0%}" for k, v in sorted(ts.items(), key=lambda kv: -(kv[1] or -9)) if v is not None)
    msg = (f"<b>Weekly screen · {run['run_date']:%d %b}</b>\n"
           f"<b>Value</b> ($2B+): {st.get('us_2b', '?'):,} → {st.get('any_funnel', '?')} hit a funnel → "
           f"{st.get('cleared_bar', 0)} deep dive(s) ✅\n" + rows(top)
           + f"\n\n<b>Growth</b> ($1B+): {st.get('us_1b', '?'):,} → {st.get('growth_any_funnel', '?')} hit a funnel → "
           f"{st.get('growth_cleared_bar', 0)} deep dive(s) ✅\n" + (rows(gtop, True) or "none yet")
           + (f"\n\nThemes vs S&amp;P, 6 months: {esc(tline)}" if tline else "")
           + (f"\nBuffett indicator {b['ratio_est']:.0%} of GDP · {b['zone']}" if b else "")
           + f"\n\nReason and growth cards arrive Sunday morning, deep dives (2 value + 2 growth) Monday morning."
           + f'\n<a href="{APP_URL}/screen">Value screen</a> · <a href="{APP_URL}/screen?track=growth">Growth screen</a>')
    email(f"ValueInvestor weekly screen · {run['run_date']:%d %b}", msg)
    print("Screen summary sent." if telegram(msg) else "Screen summary not delivered.")


def digest(conn):
    cur = conn.cursor()
    out = [f"<b>Weekly digest · {TODAY:%d %b %Y}</b>"]
    cur.execute("SELECT level, COUNT(*) AS c FROM v2_names WHERE level IN ('watchlist','book') GROUP BY level")
    counts = {r["level"]: r["c"] for r in cur.fetchall()}
    b_ = counts.get('book', 0)
    out.append(f"Book {b_} name{'' if b_ == 1 else 's'} · Watchlist {counts.get('watchlist', 0)}/50")

    cur.execute("SELECT value FROM v2_settings WHERE key = 'week_ahead'")
    wa = (cur.fetchone() or {}).get("value") or {}
    cur.execute("""SELECT c.* FROM v2_calendar c LEFT JOIN v2_names n USING (ticker)
                   WHERE c.date BETWEEN CURRENT_DATE AND CURRENT_DATE + 7
                     AND (c.kind <> 'earnings' OR n.level IN ('watchlist','book')) ORDER BY c.date, c.time_et""")
    evs = cur.fetchall()
    if wa.get("summary") or evs:
        lines = [esc(wa["summary"])] if wa.get("summary") else []
        for e in evs[:14]:
            im = (e["detail"] or {}).get("implied_move")
            lines.append(f"• {e['date']:%a %d} {link(e['ticker']) if e['ticker'] else ''} {esc(e['title'] if not e['ticker'] else 'results')}"
                         + (f" (±{im:.0%} implied)" if im else ""))
        for ev in (wa.get("events") or [])[:5]:
            if ev.get("playbook"):
                lines.append(f"→ {esc(ev.get('title', ''))}: {esc(ev['playbook'])}")
        out.append("<b>Week ahead</b>\n" + "\n".join(lines) + f'\n<a href="{APP_URL}/outlook">Outlook</a>')
    cur.execute("SELECT ticker, health, health_why FROM v2_names WHERE level = 'book' ORDER BY ticker")
    book = cur.fetchall()
    if book:
        dot = {"green": "🟢", "amber": "🟠", "red": "🔴"}
        out.append("<b>Book health</b>\n" + "\n".join(
            f"{dot.get(r['health'], '⚪')} {link(r['ticker'])}" + (f" · {esc(r['health_why'])}" if r["health_why"] else "")
            for r in book))

    cur.execute("""SELECT * FROM v2_alerts WHERE kind IN ('Buy signal confirmed','Buy signal','Buy signal, thesis weakened',
                   'Approaching entry','Buy signal paused') AND fired_at > now() - interval '7 days' ORDER BY fired_at DESC""")
    sig = cur.fetchall()
    if sig:
        out.append("<b>Buy signals this week</b>\n" + "\n".join(line(a) for a in sig[:10]))

    cur.execute("""SELECT n.ticker, n.headline, n.tag, n.note FROM v2_news n JOIN v2_names m USING (ticker)
                   WHERE n.published > CURRENT_DATE - 8 AND m.level IN ('watchlist','book')
                   AND n.tag IN ('breaks','weakens','strengthens')
                   ORDER BY CASE n.tag WHEN 'breaks' THEN 0 WHEN 'weakens' THEN 1 ELSE 2 END, n.published DESC LIMIT 5""")
    news = cur.fetchall()
    if news:
        mark_ = {"breaks": "🔴", "weakens": "🟠", "strengthens": "🟢"}
        out.append("<b>Top news vs your theses</b>\n" + "\n".join(
            f"{mark_[r['tag']]} {link(r['ticker'])} {esc(r['note'] or r['headline'])}" for r in news))

    cur.execute("""SELECT * FROM v2_alerts WHERE fired_at > now() - interval '7 days' AND sent_via = 'digest'
                   ORDER BY CASE severity WHEN 'medium' THEN 0 ELSE 1 END, fired_at DESC""")
    held = cur.fetchall()
    if held:
        out.append(f"<b>Other alerts ({len(held)})</b>\n" + "\n".join(line(a) for a in held[:12])
                   + (f"\n…and {len(held) - 12} more in the app" if len(held) > 12 else ""))

    cur.execute("SELECT ticker FROM v2_names WHERE status = 'deep_dive_done' OR (level IN ('watchlist','book') AND ticker IN "
                "(SELECT ticker FROM v2_theses WHERE approval = 'pending' AND created_at > now() - interval '8 days'))")
    pend = [r["ticker"] for r in cur.fetchall()]
    if pend:
        out.append("<b>Waiting for your approval</b>\n" + ", ".join(link(t) for t in sorted(set(pend))))
    # decision journal: decisions reaching 6 or 12 months this week, scored vs the S&P
    cur.execute("""SELECT d.*, (SELECT close FROM v2_prices WHERE ticker = d.ticker ORDER BY date DESC LIMIT 1) AS now_px,
                          (SELECT close FROM v2_prices WHERE ticker = 'SPY' ORDER BY date DESC LIMIT 1) AS spy_now
                   FROM v2_decisions d
                   WHERE d.decided_at::date BETWEEN CURRENT_DATE - 189 AND CURRENT_DATE - 182
                      OR d.decided_at::date BETWEEN CURRENT_DATE - 372 AND CURRENT_DATE - 365""")
    rev = []
    for d in cur.fetchall():
        if d["price"] and d["now_px"]:
            r = float(d["now_px"]) / float(d["price"]) - 1
            rel = (r - (float(d["spy_now"]) / float(d["spy_price"]) - 1)) if d["spy_price"] and d["spy_now"] else None
            good = (rel or r) > 0 if d["decision"] == "done" and d["action"] in ("BUY", "ADD") else (rel or r) < 0
            rev.append(f"{'✅' if good else '❌'} {link(d['ticker'])} {esc(d['action'])} {esc(d['decision'])} "
                       f"{d['decided_at']:%d %b %Y}: {r:+.0%}" + (f" ({rel:+.0%} vs S&amp;P)" if rel is not None else ""))
    if rev:
        out.append("<b>Decision review (6 and 12 months)</b>\n" + "\n".join(rev))
    if counts.get("watchlist", 0) >= 50:
        out.append("⚠️ Watchlist is full. Removal suggestions are on the Watchlist page.")
    cur.execute("SELECT value FROM v2_settings WHERE key = 'data_health'")
    h = cur.fetchone()
    fails = ((h or {}).get("value") or {}).get("failures") or {}
    if fails:
        out.append("⚙️ <i>Data issues on the last run: " + esc(", ".join(f"{k.replace('_', ' ')} {len(v)}" for k, v in fails.items())) + "</i>")
    out.append(f'<a href="{APP_URL}/decide">Decide</a> · <a href="{APP_URL}/watchlist">Watchlist</a> · <a href="{APP_URL}/book">Book</a> · <a href="{APP_URL}/screen">Screen</a>')
    email(f"ValueInvestor weekly digest · {TODAY:%d %b %Y}", "\n\n".join(out))
    print("Digest sent." if telegram("\n\n".join(out)) else "Digest not delivered.")


if __name__ == "__main__":
    if MODE == "jobfail":                 # no database needed: the database may be what failed
        jobfail(sys.argv[2] if len(sys.argv) > 2 else "?", sys.argv[3] if len(sys.argv) > 3 else APP_URL)
        sys.exit(0)
    conn = db()
    c = conn.cursor()
    ensure_schema(c)
    conn.commit()
    if MODE == "alerts":
        alerts(conn)
    elif MODE in ("daily", "brief"):
        brief(conn)
    elif MODE == "screen":
        screen(conn)
    elif MODE == "digest":
        digest(conn)
    elif MODE == "catchup":
        catchup(conn)
    conn.close()
