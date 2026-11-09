"""Telegram messages.

  python tools\\notify.py alerts    red alerts at once; up to 5 medium alerts a day; the rest wait for the digest
  python tools\\notify.py daily     same as alerts, plus a data-health line if a feed failed
  python tools\\notify.py screen    Saturday screen summary
  python tools\\notify.py digest    Sunday weekly digest
Set TELEGRAM_DRY=1 to print instead of sending.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import db, ensure_schema, telegram, esc, APP_URL, TODAY

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


def mark(cur, ids, via):
    if ids:
        cur.execute("UPDATE v2_alerts SET sent = TRUE, sent_via = %s, sent_at = now() WHERE id = ANY(%s)", (via, ids))


def alerts(conn, with_health=False):
    cur = conn.cursor()
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
        parts.append("<b>Needs attention</b>\n" + "\n".join(line(a) for a in high))
    if now_:
        parts.append("<b>Worth a look</b>\n" + "\n".join(line(a) for a in now_))
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
        ok = telegram("\n\n".join(parts) + f'\n\n<a href="{APP_URL}/book">Open the Book</a>')
        if not ok:
            print("Telegram not delivered; alerts stay queued.")
            return
    mark(cur, [a["id"] for a in high + now_], "telegram")
    mark(cur, [a["id"] for a in later + info], "digest")
    conn.commit()
    print(f"Alerts: {len(high)} high and {len(now_)} medium sent; {len(later) + len(info)} held for the digest.")


def screen(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    run = cur.fetchone()
    if not run:
        print("No screen run yet.")
        return
    st, top = run.get("stages") or {}, run.get("top10") or []
    names = {}
    if top:
        cur.execute("SELECT ticker, name FROM v2_names WHERE ticker = ANY(%s)", ([t["ticker"] for t in top],))
        names = {r["ticker"]: r["name"] for r in cur.fetchall()}
    label = {"quant_value": "value", "fallen_quality": "fallen quality", "sharp_drop": "sharp drop",
             "insider": "insiders", "smart_money": "smart money", "theme": "theme"}
    rows = [f"{i}. {link(t['ticker'])} {esc(cname(names.get(t['ticker']))[:30])} · {t['score']:.0f} · "
            + ", ".join(label.get(k, k) for k, v in (t.get("funnels") or {}).items() if v)
            + (" ✅" if t.get("promote") else "") for i, t in enumerate(top, 1)]
    b = (run.get("market") or {}).get("buffett")
    msg = (f"<b>Weekly screen · {run['run_date']:%d %b}</b>\n"
           f"{st.get('listings', '?'):,} listings → {st.get('with_fundamentals', '?'):,} scored → "
           f"{st.get('any_funnel', '?')} hit a funnel → {st.get('cleared_bar', 0)} cleared the deep-dive bar ✅\n\n"
           + "\n".join(rows)
           + (f"\n\nBuffett indicator {b['ratio_est']:.0%} of GDP · {b['zone']}" if b else "")
           + f"\n\nReason cards follow at 10:00, deep dives at 14:00.\n<a href=\"{APP_URL}/screen\">Open the Screen</a>")
    print("Screen summary sent." if telegram(msg) else "Screen summary not delivered.")


def digest(conn):
    cur = conn.cursor()
    out = [f"<b>Weekly digest · {TODAY:%d %b %Y}</b>"]
    cur.execute("SELECT level, COUNT(*) AS c FROM v2_names WHERE level IN ('watchlist','book') GROUP BY level")
    counts = {r["level"]: r["c"] for r in cur.fetchall()}
    b_ = counts.get('book', 0)
    out.append(f"Book {b_} name{'' if b_ == 1 else 's'} · Watchlist {counts.get('watchlist', 0)}/50")

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
    if counts.get("watchlist", 0) >= 50:
        out.append("⚠️ Watchlist is full. Removal suggestions are on the Watchlist page.")
    cur.execute("SELECT value FROM v2_settings WHERE key = 'data_health'")
    h = cur.fetchone()
    fails = ((h or {}).get("value") or {}).get("failures") or {}
    if fails:
        out.append("⚙️ <i>Data issues on the last run: " + esc(", ".join(f"{k.replace('_', ' ')} {len(v)}" for k, v in fails.items())) + "</i>")
    out.append(f'<a href="{APP_URL}/watchlist">Watchlist</a> · <a href="{APP_URL}/book">Book</a> · <a href="{APP_URL}/screen">Screen</a>')
    print("Digest sent." if telegram("\n\n".join(out)) else "Digest not delivered.")


if __name__ == "__main__":
    conn = db()
    c = conn.cursor()
    ensure_schema(c)
    conn.commit()
    if MODE in ("alerts", "daily"):
        alerts(conn, with_health=MODE == "daily")
    elif MODE == "screen":
        screen(conn)
    elif MODE == "digest":
        digest(conn)
    conn.close()
