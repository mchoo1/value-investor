"""Stock ideas from outside the screen (a friend, a newsletter, a podcast, a client): one path from three doors.

  Telegram: send the bot   /add NVDA why or where you heard it   (also: add NVDA ..., /ideas to list pending)
  App:      Watchlist tab → "Add an idea"

Every idea goes on the Watchlist at once, tagged "pending research", gets the weekly research data within ~2 hours,
and a full deep dive (same standard as Monday's) the next night. The thesis and its BUY/WAIT/PASS arrive in the next
morning's brief. A PASS is flagged on the Decide tab for you to remove.

  python tools/ideas.py telegram     read new Telegram messages and add ideas (run by the 2-hourly job)
"""
import os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import db, J, alert, ensure_schema, _tg, TODAY

WATCH_CAP = 50
TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")


def parse_tickers(text):
    """'CRWV, ASTS $RKLB heard on a podcast' -> (['CRWV', 'ASTS', 'RKLB'], 'heard on a podcast').
    Leading tickers (capitals or $-prefixed, comma or space separated) up to the first other word; the rest is the note."""
    words = re.split(r"(\s+)", (text or "").strip())
    tickers, i, listed = [], 0, False
    for i, w in enumerate(words):
        if w.isspace() or not w.strip(","):
            listed = listed or "," in w
            continue
        tok = w.strip(",;")
        ok_case = tok.startswith("$") or tok.isupper() or not tickers or listed
        listed = w.endswith((",", ";"))
        if ok_case and TICKER.match(tok.lstrip("$").upper().replace(".", "-")):
            tickers.append(tok.lstrip("$").upper())
            continue
        return tickers, "".join(words[i:]).strip() or None
    return tickers, None


def add_idea(cur, ticker, via, note=None, source=None, url=None):
    """Put an outside idea on the Watchlist (pending research) and queue research + a deep dive. Returns a message."""
    t = (ticker or "").strip().upper().lstrip("$").replace(".", "-")
    if not TICKER.match(t):
        return False, f"'{ticker}' does not look like a US ticker."
    cur.execute("SELECT * FROM v2_names WHERE ticker = %s", (t,))
    n = cur.fetchone()
    if not n:
        cur.execute("""INSERT INTO v2_names (ticker, level, status, first_seen, last_screened)
                       VALUES (%s, 'screen', 'new', %s, %s)""", (t, TODAY, TODAY))
        cur.execute("SELECT * FROM v2_names WHERE ticker = %s", (t,))
        n = cur.fetchone()
    cur.execute("SELECT id FROM v2_ideas WHERE ticker = %s AND status = 'pending' LIMIT 1", (t,))
    if cur.fetchone():
        return True, f"{t} is already waiting for its deep dive."
    cur.execute("""INSERT INTO v2_ideas (ticker, via, source, note, url) VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                (t, via, (source or "")[:120] or None, (note or "")[:600] or None, (url or "")[:400] or None))
    where = n["level"]
    if n["level"] == "screen":
        cur.execute("SELECT COUNT(*) AS c FROM v2_names WHERE level = 'watchlist'")
        if cur.fetchone()["c"] < WATCH_CAP:
            cur.execute("UPDATE v2_names SET level = 'watchlist', level_since = CURRENT_DATE WHERE ticker = %s", (t,))
            where = "watchlist"
    cur.execute("""UPDATE v2_names SET status = 'deep_dive_requested', research_requested = TRUE, updated_at = now()
                   WHERE ticker = %s""", (t,))
    cur.execute("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', %s)",
                (t, f"Idea added via {via}" + (f" ({source})" if source else "") + (f": {note}" if note else "")))
    msg = (f"{t} added to the Watchlist, pending research. Deep dive tonight; the call arrives in tomorrow's brief."
           if where == "watchlist" else
           f"{t} is in the Book already: a fresh deep dive is queued." if where == "book" else
           f"{t} queued for a deep dive, but the Watchlist is full (50): it stays on the Screen until you remove a name.")
    return True, msg


def researched(cur, t, verdict, version):
    """Called when a thesis arrives: close the idea and flag a PASS for removal."""
    cur.execute("""UPDATE v2_ideas SET status = 'researched', verdict = %s, thesis_version = %s, done_at = now()
                   WHERE ticker = %s AND status = 'pending' RETURNING via, source""", (verdict, version, t))
    rows = cur.fetchall()
    if not rows:
        return
    v = (verdict or "?").upper()
    alert(cur, t, "Idea researched", "medium" if v.startswith("PASS") else "info",
          f"Your idea (via {rows[0]['via']}{', ' + rows[0]['source'] if rows[0]['source'] else ''}) is researched: {v}."
          + (" Consider removing it from the Watchlist." if v.startswith("PASS") else " Read the report on its page."),
          f"idea-{t}-v{version}", source="claude", action="Remove from the Watchlist?" if v.startswith("PASS") else "Read the report")


def _reply(chat, text):
    _tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True)


def telegram_intake(conn, log=print):
    """Read new messages sent to the bot. Only Ming's chat (TELEGRAM_CHAT_ID) is obeyed."""
    allowed = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not allowed:
        log("Telegram intake skipped: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both needed.")
        return 0
    cur = conn.cursor()
    cur.execute("SELECT value FROM v2_settings WHERE key = 'tg_offset'")
    off = ((cur.fetchone() or {}).get("value") or {}).get("offset")
    res = _tg("getUpdates", offset=off, timeout=0, allowed_updates=["message"]) or {}
    added, last = 0, off
    for u in res.get("result", []):
        last = u["update_id"] + 1
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id"))
        text = (m.get("text") or "").strip()
        if chat != str(allowed) or not text:
            continue
        low = text.lower()
        if low.startswith(("/add", "add ")):
            tickers, note = parse_tickers(text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else "")
            if not tickers:
                _reply(chat, "Send: /add TICKER [TICKER ...] why or where you heard it, e.g. /add CRWV ASTS podcast on AI cloud")
                continue
            url = next((w for w in (note or "").split() if w.startswith("http")), None)
            msgs = []
            for t in tickers[:5]:
                ok, msg = add_idea(cur, t, "telegram", note=note, url=url)
                conn.commit()
                msgs.append(msg)
                added += ok
            _reply(chat, "\n".join(msgs))
        elif low.startswith("/ideas"):
            cur.execute("SELECT ticker, added_at FROM v2_ideas WHERE status = 'pending' ORDER BY added_at")
            rows = cur.fetchall()
            _reply(chat, "Waiting for a deep dive: " + (", ".join(r["ticker"] for r in rows) if rows else "none"))
        elif low.startswith(("/help", "/start")):
            _reply(chat, "Add stock ideas: /add TICKER [TICKER ...] note (e.g. /add CRWV ASTS podcast on AI cloud). "
                         "It goes on your Watchlist and gets a full deep dive tonight. /ideas lists ideas waiting.")
    if last != off:
        cur.execute("""INSERT INTO v2_settings (key, value, updated_at) VALUES ('tg_offset', %s, now())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", (J({"offset": last}),))
    conn.commit()
    log(f"Telegram intake: {added} idea(s) added.")
    return added


if __name__ == "__main__":
    conn = db()
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()
    if (sys.argv[1:] or ["telegram"])[0] == "telegram":
        n = telegram_intake(conn)
        cur.execute("""SELECT COUNT(*) AS c FROM v2_ideas i JOIN v2_names n USING (ticker)
                       WHERE i.status = 'pending' AND n.research_requested""")
        need = cur.fetchone()["c"]
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"new_ideas={'true' if need else 'false'}\n")
    conn.close()
