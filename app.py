"""
ValueInvestor app — a thin, read-mostly view of the weekly research cycle stored in Google Drive.

Weekly cycle (Singapore time, run by cloud scheduled tasks, see research/RUNBOOK.md):
  Sat 08:52 screen (Lists A/B/C) -> Sat 13:52 deep dives + draft theses -> you approve/reject
  -> Sun 08:52 health check of approved theses -> Sun 17:52 digest (Telegram x2 + email)

Pages: This week · Screen · Theses (deep dive + thesis + approve/reject) · Tracker (kill criteria).
Secrets only in env vars: GOOGLE_SERVICE_ACCOUNT_JSON, DRIVE_ROOT_FOLDER_ID, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, DATABASE_URL (legacy; only used by the one-time backup-and-wipe admin route).
"""
import os, sys, json, math, datetime as dt

sys.path.insert(0, os.path.dirname(__file__))
from flask import Flask, jsonify, request, send_from_directory, Response
from flask.json.provider import DefaultJSONProvider
import drive_store as ds

SGT = dt.timezone(dt.timedelta(hours=8))
APPROVED_STATUSES = ("Watch", "Active", "Paused")


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


class _JSON(DefaultJSONProvider):
    def dumps(self, obj, **kw):
        return super().dumps(_clean(obj), **kw)


app = Flask(__name__, static_folder="static")
app.json_provider_class = _JSON
app.json = _JSON(app)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def gv(x):
    return x.get("value") if isinstance(x, dict) else x


def num(x):
    x = gv(x)
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def yahoo(t):
    return f"https://finance.yahoo.com/quote/{t}"


def drive_link(fid):
    return f"https://drive.google.com/file/d/{fid}/view" if fid and not str(fid).startswith("/") else None


def err(e, code=502):
    return jsonify({"ok": False, "error": str(e)[:300]}), code


@app.errorhandler(ds.DriveError)
def _drive_error(e):
    return err(e, 503)


@app.errorhandler(Exception)
def _any_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("unhandled")
    return err(f"{type(e).__name__}: {e}", 500)


# ── pages ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "drive": "local" if os.environ.get("VI_LOCAL_DATA") else "google"})


# ── week anchor ──────────────────────────────────────────────────────
def week_bounds(now=None):
    now = now or dt.datetime.now(SGT)
    sat = (now - dt.timedelta(days=(now.weekday() - 5) % 7)).date()     # most recent Saturday (today if Sat)
    return now, sat, sat + dt.timedelta(days=1)


def _sgt_date(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(SGT).date() if iso else None


# ── shared builders ──────────────────────────────────────────────────
def thesis_rows():
    _, idx = ds.theses_index()
    rows = []
    for t in idx.get("theses", []):
        r = dict(t)
        p, base, entry = num(t.get("price")), num(t.get("iv_base")), num(t.get("entry_price_target"))
        r["mos"] = round((base - p) / base, 4) if (p and base) else None
        r["below_entry"] = bool(p and entry and p <= entry)
        r["yahoo"] = yahoo(t["ticker"])
        r["deep_dive_link"] = drive_link(t.get("deep_dive_file_id"))
        r["tracked"] = t.get("approval") == "approved" and t.get("status") in APPROVED_STATUSES
        rows.append(r)
    return rows


def screen_lists(sc):
    """Normalise screen JSON (old and new schemas) into lists A/B/C."""
    if not sc:
        return {}
    def pick(*keys):
        for k in keys:
            if isinstance(sc.get(k), list):
                return sc[k]
        return []
    def row(x, lst):
        m = x.get("metrics", {}) if isinstance(x.get("metrics"), dict) else {}
        snap = x.get("snapshot") or x.get("round1") or {}
        return {"list": lst, "ticker": x.get("ticker"), "name": x.get("name") or x.get("company"),
                "sector": x.get("verified_sector") or x.get("sector"), "focus": x.get("focus_sector"),
                "price": num(x.get("price")), "market_cap": num(x.get("market_cap")),
                "tests_passed": x.get("other_tests_passed"), "tests": x.get("tests"),
                "pe": m.get("pe_ttm"), "fcf_yield": m.get("fcf_yield"), "roic": m.get("roic"),
                "net_margin": m.get("net_margin"), "rev_cagr_3y": m.get("rev_cagr_3y"),
                "gross_margin": m.get("gross_margin"), "rule_of_40": x.get("rule_of_40"),
                "drawdown": x.get("drawdown_from_52w_high"), "flags": x.get("flags", []),
                "gate": x.get("quality_gate"), "investable": x.get("investable"),
                "go": snap.get("go_no_go") if isinstance(snap, dict) else None,
                "why": x.get("why_it_passes") or (snap.get("why_cheap") if isinstance(snap, dict) else None) or x.get("snapshot_thesis"),
                "yahoo": yahoo(x.get("ticker", ""))}
    out = {"A": [row(x, "A") for x in pick("list_a", "shortlist", "shortlist_candidates")][:10],
           "B": [row(x, "B") for x in pick("list_b", "drawdown_shortlist", "drawdown_candidates")][:10],
           "C": [row(x, "C") for x in pick("list_c", "growth_shortlist", "growth_candidates")][:10]}
    return out


def build_week():
    now, sat, sun = week_bounds()
    sf, sc = ds.latest_screen()
    _, alerts = ds.latest_alerts()
    af = ds.latest_alerts()[0]
    dives = ds.all_deep_dives()
    rows = thesis_rows()
    by_t = {r["ticker"]: r for r in rows}

    screen_date = _sgt_date(sf["modifiedTime"]) if sf else None
    week_dives = [d for d in dives if d["date"] >= sat.isoformat()]
    alerts_date = _sgt_date(af["modifiedTime"]) if af else None

    def status(done, due):
        if done:
            return "done"
        return "due" if now < due else "missed"
    at = lambda d, h, m: dt.datetime.combine(d, dt.time(h, m), SGT)
    steps = [
        {"key": "screen", "name": "Screen", "when": "Sat 08:52", "status": status(screen_date and screen_date >= sat, at(sat, 10, 30)),
         "detail": f"Lists A/B/C · {sf['name']}" if sf else "no screen yet"},
        {"key": "deepdive", "name": "Deep dives", "when": "Sat 13:52",
         "status": status(len(week_dives) > 0, at(sat, 16, 30)),
         "detail": f"{len(week_dives)} this week: " + ", ".join(d["ticker"] for d in week_dives) if week_dives else "1 per list (3)"},
        {"key": "health", "name": "Health check", "when": "Sun 08:52",
         "status": status(alerts_date and alerts_date >= sun, at(sun, 10, 30)),
         "detail": f"{sum(r['tracked'] for r in rows)} tracked theses"},
        {"key": "digest", "name": "Digest", "when": "Sun 17:52",
         "status": "due" if now < at(sun, 17, 52) else "sent",
         "detail": "Telegram ×2 + 1 email"},
    ]

    # Companies to watch: this week's deep dives + tracked theses at/below entry
    watch, seen = [], set()
    for d in week_dives:
        r = by_t.get(d["ticker"], {"ticker": d["ticker"], "yahoo": yahoo(d["ticker"])})
        watch.append({**r, "why_watch": "new deep dive " + d["date"]}); seen.add(d["ticker"])
    for r in rows:
        if r["tracked"] and r["below_entry"] and r["ticker"] not in seen:
            watch.append({**r, "why_watch": "price at or below entry"}); seen.add(r["ticker"])
    if not watch:   # first weeks: show the latest drafts so the page is never empty
        for r in rows[:3]:
            watch.append({**r, "why_watch": "latest deep dive (" + str(r.get("last_review")) + ")"})

    # Thesis changes: alerts + any breached kill criteria on tracked theses
    changes = []
    for a in (alerts if isinstance(alerts, list) else alerts.get("alerts", [])):
        if a.get("health") != a.get("health_prev") or a.get("issues_new"):
            changes.append({"ticker": a.get("ticker"), "health": a.get("health"), "health_prev": a.get("health_prev"),
                            "issues": a.get("issues_new", []), "action": a.get("recommended_action"),
                            "yahoo": yahoo(a.get("ticker", ""))})
    for r in rows:
        if not r["tracked"]:
            continue
        _, th = ds.thesis(r["ticker"])
        for k in ((th or {}).get("monitor_state") or {}).get("kill_status", []) or []:
            if k.get("breached"):
                changes.append({"ticker": r["ticker"], "health": "Red", "kill": k, "action": "exit review",
                                "yahoo": r["yahoo"]})
    return {"now": now.isoformat(), "week_of": sat.isoformat(), "steps": steps, "watch": watch, "changes": changes,
            "awaiting": [r for r in rows if r.get("approval") == "pending"],
            "counts": {"tracked": sum(r["tracked"] for r in rows),
                       "green": sum(r["tracked"] and r.get("health") == "Green" for r in rows),
                       "amber": sum(r["tracked"] and r.get("health") == "Amber" for r in rows),
                       "red": sum(r["tracked"] and r.get("health") == "Red" for r in rows),
                       "drafts": sum(r.get("approval") == "pending" for r in rows)},
            "alerts_file": af["name"] if af else None}


# ── API ──────────────────────────────────────────────────────────────
@app.route("/api/week")
def api_week():
    return jsonify(build_week())


@app.route("/api/screen")
def api_screen():
    f, sc = ds.latest_screen()
    if not sc:
        return jsonify({"file": None, "lists": {}, "funnel": {}})
    h = ds.latest_screen_html()
    return jsonify({"file": f["name"], "date": sc.get("screen_date"), "funnel": sc.get("funnel", {}),
                    "rules": {"A": sc.get("rule"), "B": sc.get("drawdown_rule"), "C": sc.get("growth_rule")},
                    "queue": sc.get("deep_dive_queue"), "lists": screen_lists(sc),
                    "html_link": drive_link(h["id"]) if h else None})


@app.route("/api/theses")
def api_theses():
    return jsonify({"theses": thesis_rows()})


@app.route("/api/thesis/<ticker>")
def api_thesis(ticker):
    ticker = ticker.upper()
    f, th = ds.thesis(ticker)
    dives = ds.deep_dives(ticker)
    row = next((r for r in thesis_rows() if r["ticker"] == ticker), None)
    return jsonify({"ticker": ticker, "index": row, "thesis": th, "thesis_link": drive_link(f["id"]) if f else None,
                    "deep_dives": [{"date": d["date"], "has_html": "html" in d,
                                    "html_link": drive_link((d.get("html") or {}).get("id"))} for d in dives],
                    "yahoo": yahoo(ticker)})


@app.route("/deepdive/<ticker>")
@app.route("/deepdive/<ticker>/<date>")
def deepdive_html(ticker, date=None):
    dives = ds.deep_dives(ticker.upper())
    d = next((x for x in dives if (date is None or x["date"] == date) and "html" in x), None)
    if not d:
        return Response("Deep dive not found", 404, mimetype="text/plain")
    html = ds.backend().read(d["html"]["id"])
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups"   # our own file, still isolated
    return resp


@app.route("/api/thesis/<ticker>/decision", methods=["POST"])
def api_decision(ticker):
    ticker = ticker.upper()
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("approve", "reject"):
        return err("action must be approve or reject", 400)
    status = body.get("status") or ("Watch" if action == "approve" else "Closed")
    if action == "approve" and status not in ("Watch", "Active"):
        return err("status must be Watch or Active", 400)
    reason = (body.get("reason") or "").strip()[:500]
    today = dt.datetime.now(SGT).date().isoformat()

    tf, th = ds.thesis(ticker)
    if not th:
        return err(f"thesis.json for {ticker} not found", 404)
    idx_f, idx = ds.theses_index()
    entry = next((t for t in idx.get("theses", []) if t.get("ticker") == ticker), None)
    if not idx_f or entry is None:
        return err(f"{ticker} not in theses/index.json", 404)

    approval = "approved" if action == "approve" else "rejected"
    hist = th.setdefault("history", [])
    hist.append({"version": len(hist) + 1, "date": today, "changed_by": "Ming (app)",
                 "what_changed": f"approval {th.get('approval')} -> {approval}; status {th.get('status')} -> {status}",
                 "why": reason or ("approved for weekly tracking" if action == "approve" else "rejected")})
    th["approval"], th["status"] = approval, status
    if action == "reject":
        th["closed_reason"] = reason or "rejected by Ming"
    entry.update({"approval": approval, "status": status, "last_review": today})
    idx["updated"] = today
    ds.write_json(tf, th)
    ds.write_json(idx_f, idx)
    return jsonify({"ok": True, "ticker": ticker, "approval": approval, "status": status})


@app.route("/api/tracker")
def api_tracker():
    out = []
    for r in thesis_rows():
        if not r["tracked"]:
            continue
        _, th = ds.thesis(r["ticker"])
        th = th or {}
        ms = th.get("monitor_state") or {}
        kills = ms.get("kill_status") or [
            {"criterion": k.get("criterion"), "metric_key": k.get("metric_key"), "threshold": k.get("threshold"),
             "direction": k.get("direction"), "current": None, "breached": None,
             "check": "machine" if k.get("metric_key") else "judgement"} for k in th.get("kill_criteria", [])]
        out.append({**r, "kill_status": kills, "pillar_status": ms.get("pillar_status", []),
                    "open_issues": [i for i in th.get("issues", []) if not i.get("resolved")],
                    "last_checked": ms.get("last_checked"), "catalysts": th.get("catalysts", [])})
    return jsonify({"tracked": out})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    ds.clear_cache()
    return jsonify({"ok": True})


# ── Telegram (unchanged contract) ────────────────────────────────────
_notify_log = []


def _tg_call(method, payload=None):
    import urllib.request, urllib.parse
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    data = urllib.parse.urlencode(payload).encode() if payload else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=15) as r:
        return json.loads(r.read().decode())


@app.route("/api/notify", methods=["GET", "POST"])
def notify():
    import time as _t
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured"}), 503
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or request.args.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    now = _t.time()
    _notify_log[:] = [x for x in _notify_log if now - x < 3600]
    if len(_notify_log) >= 20:
        return jsonify({"ok": False, "error": "rate limit (20/hour)"}), 429
    chunks = [text[i:i + 3900] for i in range(0, min(len(text), 3900 * 4), 3900)]
    sent = 0
    try:
        for c in chunks:
            res = _tg_call("sendMessage", {"chat_id": chat_id, "text": c, "disable_web_page_preview": "true"})
            if not res.get("ok"):
                return jsonify({"ok": False, "error": res.get("description"), "sent": sent}), 502
            sent += 1
            _notify_log.append(now)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200], "sent": sent}), 502
    resp = jsonify({"ok": True, "messages_sent": sent, "chars": len(text)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/notify/setup", methods=["GET"])
def notify_setup():
    try:
        res = _tg_call("getUpdates")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 503
    chats = {}
    for u in res.get("result", []):
        c = (u.get("message") or {}).get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = {"chat_id": c["id"], "name": c.get("first_name") or c.get("title"), "type": c.get("type")}
    resp = jsonify({"ok": True, "chats": list(chats.values()),
                    "configured_chat_id": os.environ.get("TELEGRAM_CHAT_ID") or None})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── One-time legacy database backup + wipe (Ming 2026-10-02) ─────────
WIPE_PHRASE = "BACKUP-AND-WIPE-LEGACY-DB"


def _db():
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url)


def _tables(cur):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


@app.route("/api/admin/legacy-db", methods=["GET"])
def legacy_db_status():
    try:
        with _db() as conn, conn.cursor() as cur:
            counts = {}
            for t in _tables(cur):
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                counts[t] = cur.fetchone()[0]
        return jsonify({"ok": True, "tables": counts, "total_rows": sum(counts.values())})
    except Exception as e:
        return err(e, 503)


@app.route("/api/admin/legacy-db/backup-and-wipe", methods=["POST"])
def legacy_db_backup_and_wipe():
    """Exports every public table to Drive archive/legacy as one JSON file, re-reads it to verify
    the row counts, and only then truncates the tables. Requires the confirm phrase."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != WIPE_PHRASE:
        return err(f'send {{"confirm": "{WIPE_PHRASE}"}} to run', 400)
    stamp = dt.datetime.now(SGT).strftime("%Y-%m-%d_%H%M")
    try:
        conn = _db()
        cur = conn.cursor()
        dump, counts = {}, {}
        for t in _tables(cur):
            cur.execute(f'SELECT * FROM "{t}"')
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            dump[t], counts[t] = rows, len(rows)
        payload = json.dumps({"exported": stamp, "source": "Neon Postgres (DATABASE_URL)", "row_counts": counts,
                              "tables": dump}, default=str, ensure_ascii=False).encode("utf-8")
        legacy = ds.folder("archive", "legacy")
        made = ds.backend().create(legacy, f"app-db-backup_{stamp}.json", payload)
        back = json.loads(ds.backend().read(made["id"]).decode("utf-8"))
        if back.get("row_counts") != counts or any(len(back["tables"][t]) != n for t, n in counts.items()):
            conn.close()
            return err("backup verification failed; nothing was deleted", 500)
        if counts:
            cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in counts) + " RESTART IDENTITY CASCADE")
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "backup_file": made.get("name"), "backup_id": made.get("id"),
                        "backup_bytes": len(payload), "rows_deleted": counts})
    except Exception as e:
        return err(e, 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
