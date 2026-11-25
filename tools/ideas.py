"""Stock ideas from outside the screen (a friend, a newsletter, a podcast, a client): one path from three doors.

  Telegram: send the bot   /add NVDA why or where you heard it   (also: add NVDA ..., /ideas to list pending)
            Answered instantly by the app's webhook (/telegram/webhook); the 2-hourly job is the fallback.
  App:      Watchlist tab → "Add an idea"

Every idea goes on the Watchlist at once, tagged "pending research", gets the weekly research data within ~2 hours,
and a full deep dive the next night (the same 02:13 task that writes every thesis). The thesis and its BUY/WAIT/PASS arrive in the next
morning's brief. A PASS is flagged on the Decide tab for you to remove.

  python tools/ideas.py telegram     read new Telegram messages and add ideas (run by the 2-hourly job)
"""
import os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import db, J, alert, ensure_schema, _tg, TODAY, APP_URL

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


# Telegram menu (shown when Ming types "/" in the chat); set by the app's "Connect Telegram commands" button and on /help
COMMANDS = [                         # menu order = the simple guide: find, read, check, then the rest
    ("deepdive", "TICKER [note]: full deep dive and investment thesis tonight"),
    ("thesis", "TICKER [section]: the thesis summary, or one section"),
    ("report", "TICKER: the full research report as a PDF"),
    ("book", "holdings: call, conviction and price vs value"),
    ("watchlist", "names closest to their buy level"),
    ("ask", "[TICKER] question: Claude replies within ~2 hours"),
    ("challenge", "TICKER [focus]: Claude tries to break the thesis"),
    ("check", "TICKER link: test a bear report or article"),
    ("ideas", "deep dives waiting and done"),
    ("cancel", "TICKER: cancel a requested deep dive"),
    ("feedback", "[TICKER] text: your view on a thesis or the app"),
    ("sections", "the thesis sections you can open with /thesis"),
    ("help", "how to use ValueInvestor and all commands"),
]
SECTION_ALIASES = {"tech": "technology", "quarter": "latest_quarter", "earnings": "latest_quarter", "q": "latest_quarter",
                   "gaap": "latest_quarter", "short": "controversy", "risks": "company_risks", "risk": "company_risks",
                   "strategy": "strategic_outlook", "backlog": "strategic_outlook", "bull": "bull_bear_tests",
                   "bear": "bull_bear_tests", "tests": "bull_bear_tests", "valuation": "valuation", "moat": "moat",
                   "variant": "variant_view", "pillars": "pillars", "kill": "pillars", "premortem": "premortem",
                   "catalysts": "catalysts_flow", "management": "management", "financials": "financials",
                   "business": "business", "tam": "tam_competitors", "industry": "industry_map", "outlook": "outlook"}


def set_commands():
    return _tg("setMyCommands", commands=[{"command": c, "description": d[:256]} for c, d in COMMANDS])


def _n(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _money(v):
    v = _n(v)
    return f"${v:,.2f}" if v is not None and v < 1000 else (f"${v:,.0f}" if v is not None else "n/a")


def _txt(v, limit=None):
    """A thesis field (string, list or dict) as plain text."""
    if v is None:
        return ""
    if isinstance(v, str):
        out = v
    elif isinstance(v, list):
        out = "\n".join("• " + _txt(x) for x in v)
    elif isinstance(v, dict):
        out = "; ".join(f"{k.replace('_', ' ')}: {_txt(x)}" for k, x in v.items() if x not in (None, "", []))
    else:
        out = str(v)
    return out if not limit or len(out) <= limit else out[:limit - 1].rstrip() + "…"


def _latest(cur, t):
    cur.execute("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (t,))
    return cur.fetchone()


def _price(cur, t):
    cur.execute("SELECT close FROM v2_prices WHERE ticker = %s ORDER BY date DESC LIMIT 1", (t,))
    r = cur.fetchone()
    return _n(r["close"]) if r else None


def thesis_text(cur, t, section=None):
    """/thesis TICKER: the executive summary in under a minute's read; /thesis TICKER controversy: one section in full."""
    from inbox import SECTION_KEYS
    th = _latest(cur, t)
    link = f"{APP_URL}/name/{t}#thesis"
    if not th:
        cur.execute("SELECT level, status FROM v2_names WHERE ticker = %s", (t,))
        n = cur.fetchone()
        if n and n["status"] == "deep_dive_requested":
            return f"{t}: no thesis yet; its deep dive is queued for tonight (02:13 SGT)."
        return f"{t}: no thesis yet. Send /deepdive {t} to get a full deep dive and investment thesis tonight."
    secs = th["sections"] or {}
    if section:
        key = SECTION_ALIASES.get(section.lower(), section.lower())
        if key not in SECTION_KEYS and key not in ("exec_summary", "change_my_mind"):
            return f"Unknown section '{section}'. Send /sections for the list."
        body = _txt(secs.get(key))
        if not body:
            return (f"{t}: the '{key.replace('_', ' ')}' section is not written yet"
                    + (" (it is being backfilled overnight)." if key in ("technology", "latest_quarter", "controversy",
                                                                          "strategic_outlook", "bull_bear_tests") else "."))
        head = f"{t} · {key.replace('_', ' ').capitalize()} (thesis v{th['version']}, {str(th['created_at'])[:10]})\n\n"
        tail = f"\n\nFull thesis: {link.replace('#thesis', '#sec-' + key)}"
        return head + _txt(body, 3900 - len(head) - len(tail)) + tail
    es = secs.get("exec_summary") if isinstance(secs.get("exec_summary"), dict) else {}
    val = th["valuation"] or {}
    px = _price(cur, t)
    base, entry = _n(val.get("iv_base")), _n(th["entry_target"])
    lines = [f"{t} · {th['verdict'] or '?'} · conviction {int(_n(th['conviction']) or 0)} · v{th['version']} "
             f"{str(th['created_at'])[:10]} ({th['approval']})"]
    if es.get("recommendation") or es.get("one_line"):
        lines.append(f"\nCall: {es.get('recommendation') or ''} {('— ' + _txt(es.get('one_line'), 400)) if es.get('one_line') else ''}".rstrip())
    pv = [f"Price {_money(px)}"]
    if base:
        pv.append(f"base value {_money(base)}" + (f" ({(base / px - 1):+.0%})" if px else ""))
    if entry:
        pv.append(f"buy below {_money(entry)}" + (" ✅ in range" if px and px <= entry else ""))
    if _n(val.get("growth_zone_engine")):
        pv.append(f"growth zone {_money(val['growth_zone_engine'])}")
    lines.append(" · ".join(pv))
    if es.get("why"):
        lines.append("\nWhy:\n" + _txt(es["why"][:3] if isinstance(es["why"], list) else es["why"], 700))
    if es.get("key_risks"):
        kr = es["key_risks"][:3] if isinstance(es["key_risks"], list) else es["key_risks"]
        if isinstance(kr, list):
            kr = [f"{r.get('risk')} (watch: {r.get('watch')})" if isinstance(r, dict) else r for r in kr]
        lines.append("\nKey risks:\n" + _txt(kr, 700))
    if secs.get("bull_bear_tests"):
        lines.append("\nWhat would prove it right or wrong:\n" + _txt(secs["bull_bear_tests"], 700))
    elif secs.get("variant_view"):
        lines.append("\nVariant view: " + _txt(secs["variant_view"], 400))
    plan = es.get("plan")
    if isinstance(plan, dict) and plan:
        lines.append("\nPlan: " + _txt(plan, 350))
    lines.append(f"\nMore: /thesis {t} quarter · controversy · tech · strategy · valuation · /report {t} (PDF)\nFull thesis: {link}")
    return _txt("\n".join(lines), 4000)


def watchlist_text(cur, level="watchlist"):
    cur.execute("SELECT ticker, status FROM v2_names WHERE level = %s ORDER BY ticker", (level,))
    names = cur.fetchall()
    rows = []
    for n in names:
        th = _latest(cur, n["ticker"]) or {}
        px = _price(cur, n["ticker"])
        entry, base = _n(th.get("entry_target")), _n((th.get("valuation") or {}).get("iv_base"))
        es = (th.get("sections") or {}).get("exec_summary") or {}
        rows.append({"t": n["ticker"], "px": px, "entry": entry, "base": base, "conv": _n(th.get("conviction")),
                     "call": (es.get("recommendation") if isinstance(es, dict) else None) or th.get("verdict"),
                     "gap": (px / entry - 1) if px and entry else None, "pending": n["status"] == "deep_dive_requested"})
    if not rows:
        return f"No names in the {level.capitalize()}."
    if level == "watchlist":
        rows.sort(key=lambda r: (r["gap"] is None, r["gap"] if r["gap"] is not None else 0))
        out = ["Watchlist, closest to the buy level first:"]
        for r in rows[:15]:
            g = ("✅ in buy range" if r["gap"] <= 0 else f"{r['gap']:.0%} above buy {_money(r['entry'])}") if r["gap"] is not None else "no buy level yet"
            out.append(f"{r['t']} {_money(r['px'])} · {g}" + (f" · {r['call']} {int(r['conv'])}" if r["call"] and r["conv"] else "")
                       + (" · deep dive queued" if r["pending"] else ""))
        if len(rows) > 15:
            out.append(f"…and {len(rows) - 15} more: {APP_URL}/watchlist")
    else:
        out = ["Book:"]
        for r in rows:
            vs = f" · value {_money(r['base'])} ({(r['base'] / r['px'] - 1):+.0%})" if r["base"] and r["px"] else ""
            out.append(f"{r['t']} {_money(r['px'])}{vs}" + (f" · {r['call']} {int(r['conv'])}" if r["call"] and r["conv"] else ""))
    out.append("Send /thesis TICKER for any name.")
    return "\n".join(out)


def cancel_text(cur, t):
    cur.execute("""UPDATE v2_names n SET status = CASE n.level WHEN 'book' THEN 'holding' WHEN 'watchlist' THEN 'watching'
                     ELSE CASE WHEN EXISTS (SELECT 1 FROM v2_theses x WHERE x.ticker = n.ticker) THEN 'deep_dive_done' ELSE 'new' END END,
                     updated_at = now()
                   WHERE n.ticker = %s AND n.status = 'deep_dive_requested' RETURNING n.ticker""", (t,))
    hit = cur.fetchone()
    cur.execute("UPDATE v2_ideas SET status = 'cancelled', done_at = now() WHERE ticker = %s AND status = 'pending' RETURNING id", (t,))
    hit = hit or cur.fetchone()
    return f"Deep dive for {t} cancelled." if hit else f"{t} has no deep dive waiting."


def send_report(cur, chat, t):
    """Build the PDF research report and send it to the chat as a document."""
    import requests, report, report_pdf
    rep = report.build(cur, t)
    data = report_pdf.build_pdf(rep)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or os.environ.get("TELEGRAM_DRY"):
        return f"(dry run) {t} report built, {len(data) // 1024} KB."
    st = (rep.get("stars") or {})
    cap = (f"{t} research report · fair value ${rep['fair_value']:,.2f}" if rep.get("fair_value") else f"{t} research report") + \
          (f" · {'★' * st['stars']} · {(rep.get('uncertainty') or {}).get('rating', '')} uncertainty" if st.get("stars") else "")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument", data={"chat_id": chat, "caption": cap[:1000]},
                      files={"document": (f"{t}-research-{TODAY}.pdf", data, "application/pdf")}, timeout=60)
    ok = r.ok and (r.json() or {}).get("ok")
    return None if ok else f"Could not send the PDF ({r.status_code}). Open it in the app: {APP_URL}/name/{t}/report.pdf"


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
    cmd0 = low.split()[0].split("@")[0] if low else ""
    arg = text.strip().split(None, 1)[1] if len(text.strip().split(None, 1)) > 1 else ""
    if cmd0 in ("/deepdive", "/dd", "/research"):
        tickers, note = parse_tickers(arg)
        if not tickers:
            return "Send: /deepdive TICKER [why or where you heard it], e.g. /deepdive ASTS satellite direct-to-phone"
        url = next((w for w in (note or "").split() if w.startswith("http")), None)
        msgs = []
        for t in tickers[:5]:
            ok, msg = add_idea(cur, t, "telegram", note=note or "Deep dive requested", url=url)
            conn.commit()
            msgs.append(msg)
        return ("\n".join(msgs) + "\nThe full investment thesis (executive summary, three layers, valuation, bull and bear tests) "
                "is pushed here when it is ready; or send /thesis " + tickers[0] + " tomorrow.")
    if cmd0 == "/thesis":
        bits = arg.split()
        if not bits:
            return "Send: /thesis TICKER, or /thesis TICKER section (e.g. /thesis NOW controversy). /sections lists them."
        return thesis_text(cur, bits[0].upper().lstrip("$"), bits[1] if len(bits) > 1 else None)
    if cmd0 == "/report":
        if not arg:
            return "Send: /report TICKER (the full research report as a PDF)"
        t = arg.split()[0].upper().lstrip("$")
        if not _latest(cur, t):
            return f"{t}: no thesis yet. Send /deepdive {t} first."
        return send_report(cur, chat, t)
    if cmd0 == "/sections":
        return ("Open one section with /thesis TICKER <name>:\ntech · quarter (GAAP vs non-GAAP) · controversy (short reports) · "
                "strategy (partnerships, backlog) · bull (bull/bear tests) · valuation · moat · variant · pillars · risks · "
                "catalysts · management · financials · business · tam · industry · outlook · premortem")
    if cmd0 == "/watchlist":
        return watchlist_text(cur, "watchlist")
    if cmd0 == "/book":
        return watchlist_text(cur, "book")
    if cmd0 == "/cancel":
        if not arg:
            return "Send: /cancel TICKER"
        msg = cancel_text(cur, arg.split()[0].upper().lstrip("$"))
        conn.commit()
        return msg
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
        try:
            set_commands()
        except Exception:
            pass
        return ("How it works: find (screen or /deepdive) → read (/thesis, /report) → check (/challenge, /check) → "
                "approve in the app → monitor (alerts and refreshes).\n\n"
                "Most used:\n/deepdive TICKER [note]: full deep dive and thesis tonight\n"
                "/thesis TICKER [section]: the summary, or one section (/sections lists them)\n"
                "/report TICKER: the full research report as a PDF\n"
                "/book · /watchlist: holdings, and names closest to their buy level\n"
                "/ask [TICKER] question: Claude replies within ~2 hours (09:00-21:00); reply to an answer to follow up\n"
                "/challenge TICKER [focus] · /check TICKER link: break the thesis, or test a source\n\n"
                "Also: /ideas (queue) · /cancel TICKER · /feedback text. Any other message is treated as a question.\n\n"
                f"Guide: {APP_URL}/help")
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
