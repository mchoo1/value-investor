"""Stock ideas from outside the screen (a friend, a newsletter, a podcast, a client): one path from three doors.

  Telegram: send the bot   /add NVDA why or where you heard it   (also: add NVDA ..., /ideas to list pending)
            Answered instantly by the app's webhook (/telegram/webhook); the 2-hourly job is the fallback.
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


KIND_REPLY = {
    "question": "Got it (#{id}{t}). Claude researches it and replies here, usually within 2 hours between 09:00 and 21:00.",
    "feedback": "Feedback noted (#{id}{t}). Claude reviews it within 2 hours and tells you what it changes.",
    "challenge": "Challenge queued (#{id}{t}). Claude will try hard to break the thesis: scorecard, the strongest counter-arguments "
                 "and fixes, usually within 2 hours (09:00-21:00).",
    "comment": "Comment saved (#{id}{t}). Claude reviews it against the evidence and replies under it within about 2 hours (09:00-21:00).",
    "check": "Source check queued (#{id}{t}). Claude will test each claim in it against filings and the thesis, within about 2 hours.",
}


def add_question(cur, text, via, ticker=None, kind="question", parent_id=None, url=None, section=None):
    """A question, feedback, challenge or source check for Claude, answered by the 2-hourly Q&A task (09:00-21:00 SGT).
    A follow-up (parent_id) carries the whole thread so Claude answers in context."""
    text = (text or "").strip()
    if not text and kind not in ("challenge", "check"):
        return False, "Write the question after the command, e.g. /ask NOW what happens if AI agents replace seats?"
    t = (ticker or "").upper().lstrip("$") or None
    if parent_id and (not t or not section):
        cur.execute("SELECT ticker, section FROM v2_questions WHERE id = %s", (parent_id,))
        pr = cur.fetchone() or {}
        t, section = t or pr.get("ticker"), section or pr.get("section")
    if t is None and text:                          # first word a known ticker? then it is about that stock
        first = text.split()[0].strip(",:;?").lstrip("$")
        if TICKER.match(first.upper()) and (first.isupper() or first.startswith("$")):
            cur.execute("SELECT 1 FROM v2_names WHERE ticker = %s", (first.upper(),))
            if cur.fetchone():
                t = first.upper()
    if kind in ("challenge", "check") and not t:
        return False, f"Say which stock, e.g. /{kind} NOW" + (" https://link-to-the-report" if kind == "check" else "")
    url = url or next((w for w in text.split() if w.startswith("http")), None)
    if kind == "check" and not url and len(text) < 40:
        return False, "Send the link (or paste the text) to check, e.g. /check HOOD https://example.com/bear-report"
    cur.execute("""INSERT INTO v2_questions (ticker, kind, text, via, parent_id, url, section)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (t, kind, (text or f"Challenge the {t} thesis")[:4000], via, parent_id, url, section))
    qid = cur.fetchone()["id"]
    msg = KIND_REPLY.get(kind, KIND_REPLY["question"]).format(id=qid, t=f", {t}" if t else "")
    return True, (msg + (f" (follow-up to #{parent_id})" if parent_id else ""))


def _qref(text):
    """'#12 and what about margins?' -> (12, 'and what about margins?')"""
    m = re.match(r"^#(\d+)\s*(.*)$", (text or "").strip(), re.S)
    return (int(m.group(1)), m.group(2)) if m else (None, text)


def handle_message(conn, chat, text, reply_to=None):
    """Act on one Telegram message from Ming's chat. Returns the reply text (or None to stay silent).
    reply_to: the Telegram message id Ming replied to; a reply to one of Claude's answers is a follow-up in that thread."""
    cur = conn.cursor()
    if reply_to and not (text or "").strip().startswith("/"):
        cur.execute("SELECT id FROM v2_questions WHERE tg_message_id = %s ORDER BY id DESC LIMIT 1", (reply_to,))
        r = cur.fetchone()
        if r:
            ok, msg = add_question(cur, text, "telegram", parent_id=r["id"])
            conn.commit()
            return msg
    low = (text or "").strip().lower()
    if low.startswith(("/add", "add ")):
        rest = text.strip().split(None, 1)
        tickers, note = parse_tickers(rest[1] if len(rest) > 1 else "")
        if not tickers:
            return "Send: /add TICKER [TICKER ...] why or where you heard it, e.g. /add CRWV ASTS podcast on AI cloud"
        url = next((w for w in (note or "").split() if w.startswith("http")), None)
        msgs = []
        for t in tickers[:5]:
            ok, msg = add_idea(cur, t, "telegram", note=note, url=url)
            conn.commit()
            msgs.append(msg)
        return "\n".join(msgs)
    if low.startswith("/ideas"):
        cur.execute("""SELECT ticker, added_at, via FROM v2_ideas WHERE status = 'pending' ORDER BY added_at""")
        rows = cur.fetchall()
        cur.execute("""SELECT ticker, verdict FROM v2_ideas WHERE status = 'researched' AND done_at > now() - interval '14 days'
                       ORDER BY done_at DESC LIMIT 8""")
        done = cur.fetchall()
        return ("Waiting for a deep dive: " + (", ".join(r["ticker"] for r in rows) if rows else "none")
                + ("\nResearched in the last 2 weeks: " + ", ".join(f"{r['ticker']} {r['verdict'] or ''}".strip() for r in done)
                   if done else ""))
    if low.startswith(("/help", "/start")):
        return ("Commands:\n/add TICKER [TICKER ...] note: research new ideas (full deep dive tonight)\n"
                "/ideas: ideas waiting and done\n/ask [TICKER] question: Claude researches and replies within ~2 hours\n"
                "/feedback [TICKER] text: tell Claude what you think of a thesis or the app\n"
                "/challenge TICKER [focus]: Claude tries hard to break the thesis (scorecard, counter-arguments, fixes)\n"
                "/check TICKER link [note]: Claude tests a bear report, analyst note or article against the facts\n"
                "Reply to any of Claude's answers to follow up in the same thread (or /ask #12 your follow-up).\n"
                "Any other message is treated as a question.")
    for cmd, kind in (("/ask", "question"), ("/feedback", "feedback"), ("/challenge", "challenge"), ("/check", "check")):
        if low.startswith(cmd):
            body = text.strip()[len(cmd):].strip()
            parent, body = _qref(body)
            tk = None
            if kind in ("challenge", "check") and body:
                w = body.split()[0].strip(",:;").lstrip("$")
                if TICKER.match(w.upper()):
                    tk, body = w.upper(), body[len(body.split()[0]):].strip()
            ok, msg = add_question(cur, body, "telegram", ticker=tk, kind=kind, parent_id=parent)
            conn.commit()
            return msg
    if not low.startswith("/"):
        ok, msg = add_question(cur, text, "telegram")
        conn.commit()
        return msg
    return None


def telegram_intake(conn, log=print):
    """Fallback when the instant webhook is not set up: read new messages every 2 hours. Only TELEGRAM_CHAT_ID is obeyed."""
    allowed = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not allowed:
        log("Telegram intake skipped: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both needed.")
        return 0
    cur = conn.cursor()
    cur.execute("SELECT value FROM v2_settings WHERE key = 'tg_offset'")
    off = ((cur.fetchone() or {}).get("value") or {}).get("offset")
    res = _tg("getUpdates", offset=off, timeout=0, allowed_updates=["message"]) or {}
    if not res.get("ok"):
        log(f"Telegram intake: {res.get('description') or 'no updates'} (the app's instant webhook handles messages when set).")
        return 0
    n, last = 0, off
    for u in res.get("result", []):
        last = u["update_id"] + 1
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id"))
        if chat != str(allowed):
            continue
        reply = handle_message(conn, chat, m.get("text") or "", (m.get("reply_to_message") or {}).get("message_id"))
        if reply:
            _reply(chat, reply)
            n += 1
    if last != off:
        cur.execute("""INSERT INTO v2_settings (key, value, updated_at) VALUES ('tg_offset', %s, now())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", (J({"offset": last}),))
    conn.commit()
    log(f"Telegram intake: {n} message(s) answered.")
    return n

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
        cur.execute("SELECT COUNT(*) AS c FROM v2_questions WHERE status = 'open'")
        openq = cur.fetchone()["c"]
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"new_ideas={'true' if need else 'false'}\nopen_questions={'true' if openq else 'false'}\n")
    conn.close()
