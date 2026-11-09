"""Load results from Claude's scheduled tasks (Drive folder ValueInvestor/v2-inbox) into the database.

  python tools\\inbox.py              read new files from Drive
  python tools\\inbox.py --dir PATH   read *.json from a local folder (e.g. inbox-seed/, loaded once per file)
Each file is loaded once (tracked by file id and modified time). Files are never edited or deleted.
"""
import sys, json, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import db, J, alert, ensure_schema, drive_list, drive_read, DRIVE_INBOX, TODAY


WATCH_CAP = 50
SECTION_KEYS = {"industry_map", "size_growth", "structure", "demand_supply", "industry_risks", "outlook", "winners",
                "business", "right_to_win", "moat", "tam_competitors", "management", "financials", "company_risks",
                "variant_view", "valuation", "pillars", "catalysts_flow", "premortem"}
TAGS = {"no change", "strengthens", "weakens", "breaks"}


def known(cur, t):
    cur.execute("SELECT * FROM v2_names WHERE ticker = %s", (t,))
    return cur.fetchone()


def load_thesis(cur, d):
    t = d["ticker"].upper()
    n = known(cur, t)
    if not n:
        cur.execute("INSERT INTO v2_names (ticker, level, status, first_seen, last_screened) VALUES (%s,'screen','new',%s,%s)",
                    (t, TODAY, TODAY))
        n = known(cur, t)
    sections = {k: v for k, v in (d.get("sections") or {}).items() if k in SECTION_KEYS and v}
    missing = sorted(SECTION_KEYS - set(sections))
    es = d.get("exec_summary") or (d.get("sections") or {}).get("exec_summary")
    if isinstance(es, dict):
        sections["exec_summary"] = es
    cur.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM v2_theses WHERE ticker = %s", (t,))
    ver = cur.fetchone()["v"]
    cur.execute("""INSERT INTO v2_theses (ticker, version, author, approval, sections, valuation, conviction, verdict, entry_target)
                   VALUES (%s,%s,'claude','pending',%s,%s,%s,%s,%s)""",
                (t, ver, J(sections), J(d.get("valuation") or {}), d.get("conviction"), d.get("verdict"), d.get("entry_target")))
    if d.get("themes"):
        cur.execute("UPDATE v2_names SET themes = (SELECT ARRAY(SELECT DISTINCT unnest(themes || %s))) WHERE ticker = %s",
                    (d["themes"], t))
    moved = ""
    if d.get("add_to_watchlist") and n["level"] == "screen":
        cur.execute("SELECT COUNT(*) AS c FROM v2_names WHERE level = 'watchlist'")
        if cur.fetchone()["c"] < WATCH_CAP:
            cur.execute("""UPDATE v2_names SET level = 'watchlist', status = 'watching', level_since = CURRENT_DATE,
                           updated_at = now() WHERE ticker = %s""", (t,))
            cur.execute("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', 'Moved screen → watchlist after deep dive')", (t,))
            moved = " Added to the Watchlist."
        else:
            cur.execute("UPDATE v2_names SET status = 'deep_dive_done' WHERE ticker = %s", (t,))
            moved = " Watchlist is full (50): remove a name to add it."
    elif n["status"] == "deep_dive_requested":
        cur.execute("UPDATE v2_names SET status = %s WHERE ticker = %s",
                    ("watching" if n["level"] == "watchlist" else "holding" if n["level"] == "book" else "deep_dive_done", t))
    alert(cur, t, "New thesis ready", "medium",
          f"v{ver} · {d.get('verdict') or '?'} · conviction {d.get('conviction') or '?'} · entry target "
          f"{('$%.2f' % d['entry_target']) if d.get('entry_target') else '—'}.{moved} Approve it in the app."
          + (f" Missing sections: {', '.join(missing)}." if missing else ""),
          f"thesis-{t}-v{ver}", source="claude", action="Review and approve")
    return f"{t} v{ver}{' (' + str(len(missing)) + ' sections missing)' if missing else ''}"


def num_(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_book(cur, d):
    """Your holdings: {"type": "book", "replace": true, "items": [{"ticker", "shares", "cost"}]}.
    Each name moves to the Book with its position. With replace, Book names not listed are marked sold (back to Watchlist)."""
    held = []
    for it in d.get("items", []):
        t = (it.get("ticker") or "").upper()
        if not t:
            continue
        n = known(cur, t)
        if not n:
            cur.execute("INSERT INTO v2_names (ticker, level, status, first_seen) VALUES (%s,'screen','new',%s)", (t, TODAY))
            n = known(cur, t)
        cur.execute("SELECT 1 FROM v2_theses WHERE ticker = %s", (t,))
        has_thesis = cur.fetchone() is not None
        if n["level"] != "book":
            cur.execute("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', %s)",
                        (t, f"Moved {n['level']} → book (holdings import)"))
        cur.execute("""UPDATE v2_names SET level = 'book', level_since = COALESCE(CASE WHEN level = 'book' THEN level_since END, CURRENT_DATE),
                       status = %s, research_requested = TRUE, updated_at = now() WHERE ticker = %s""",
                    ("holding" if has_thesis else "deep_dive_requested", t))
        if (it.get("style") or "").lower() in ("value", "growth"):
            cur.execute("UPDATE v2_names SET style = %s WHERE ticker = %s", (it["style"].lower(), t))
        if it.get("shares"):
            cur.execute("UPDATE v2_positions SET exited = %s WHERE ticker = %s AND exited IS NULL", (TODAY, t))
            cur.execute("INSERT INTO v2_positions (ticker, entered, shares, cost, notes) VALUES (%s,%s,%s,%s,'holdings import')",
                        (t, TODAY, it["shares"], it.get("cost")))
        held.append(t)
    sold = []
    if d.get("replace") and held:
        cur.execute("SELECT ticker FROM v2_names WHERE level = 'book' AND NOT (ticker = ANY(%s))", (held,))
        sold = [r["ticker"] for r in cur.fetchall()]
        for t in sold:
            cur.execute("UPDATE v2_names SET level = 'watchlist', status = 'exited', level_since = CURRENT_DATE WHERE ticker = %s", (t,))
            cur.execute("UPDATE v2_positions SET exited = %s WHERE ticker = %s AND exited IS NULL", (TODAY, t))
            cur.execute("INSERT INTO v2_comments (ticker, section, body) VALUES (%s, 'move', 'Moved book → watchlist (not in holdings import)')", (t,))
    return f"Book: {len(held)} held ({', '.join(held)})" + (f"; {len(sold)} marked sold ({', '.join(sold)})" if sold else "")


def load(cur, name, d):
    kind = d.get("type")
    if kind == "book":
        return load_book(cur, d)
    if kind == "refresh":                 # thesis refresh after results: new version with updated numbers
        done = []
        for it in d.get("items", []):
            t = (it.get("ticker") or "").upper()
            cur.execute("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (t,))
            last = cur.fetchone()
            if not last:
                continue
            val = dict(last["valuation"] or {})
            for k, v in (it.get("valuation") or {}).items():
                if v not in (None, "", [], {}):
                    val[k] = v
            pillars = it.get("pillars") or []
            breached = [p for p in pillars if (p.get("status") or "").lower() == "breached"]
            val["refresh"] = {"date": str(TODAY), "summary": it.get("summary"), "pillars": pillars,
                              "previous": {"conviction": num_(last["conviction"]), "verdict": last["verdict"],
                                           "entry_target": num_(last["entry_target"]), "iv_base": (last["valuation"] or {}).get("iv_base")}}
            conv = it.get("conviction", last["conviction"])
            verdict = it.get("verdict") or last["verdict"]
            entry = it.get("entry_target", last["entry_target"])
            secs = dict(last["sections"] or {})
            if isinstance(it.get("exec_summary"), dict):
                secs["exec_summary"] = it["exec_summary"]
            cur.execute("""INSERT INTO v2_theses (ticker, version, author, approval, sections, valuation, conviction, verdict, entry_target)
                           VALUES (%s, %s, 'claude-refresh', 'pending', %s, %s, %s, %s, %s)""",
                        (t, last["version"] + 1, J(secs), J(val), conv, verdict, entry))
            cur.execute("UPDATE v2_names SET refresh_due = NULL, updated_at = now() WHERE ticker = %s", (t,))
            changed = verdict != last["verdict"] or bool(breached)
            if it.get("full_reunderwrite") or breached:
                cur.execute("UPDATE v2_names SET status = 'deep_dive_requested' WHERE ticker = %s", (t,))
            bits = [f"conviction {num_(last['conviction']) or 0:.0f} → {num_(conv) or 0:.0f}"]
            if verdict != last["verdict"]:
                bits.append(f"verdict {last['verdict']} → {verdict}")
            if entry and last["entry_target"] and float(entry) != float(last["entry_target"]):
                bits.append(f"entry ${float(last['entry_target']):,.2f} → ${float(entry):,.2f}")
            if breached:
                bits.append("pillar breached: " + "; ".join(p.get("pillar", "")[:60] for p in breached))
            alert(cur, t, "Thesis refreshed after results", "high" if changed else "medium",
                  (it.get("summary") or "")[:300] + " (" + ", ".join(bits) + ")", f"refresh-{t}-{TODAY}", source="claude",
                  action="Full re-underwrite queued" if (it.get("full_reunderwrite") or breached) else "Approve the update")
            done.append(t)
        return f"{len(done)} thesis refresh(es): {', '.join(done)}"
    if kind == "week_ahead":              # Claude's Sunday look-ahead: stored for the app and digest; adds calendar rows
        cur.execute("""INSERT INTO v2_settings (key, value, updated_at) VALUES ('week_ahead', %s, now())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                    (J({"as_of": str(TODAY), "summary": d.get("summary"), "events": d.get("events") or []}),))
        n = 0
        for c in d.get("calendar") or []:
            try:
                cur.execute("""INSERT INTO v2_calendar (date, time_et, kind, ticker, title, detail, source)
                               VALUES (%s,%s,%s,%s,%s,%s,'claude') ON CONFLICT (date, kind, title) DO UPDATE SET detail = EXCLUDED.detail""",
                            (c["date"], c.get("time_et"), c.get("kind") or "policy", c.get("ticker"), c["title"], J(c.get("detail") or {})))
                n += 1
            except Exception:
                continue
        return f"Week ahead saved; {n} calendar items"
    if kind == "newsletter_tags":         # Claude's read of the investing newsletters in agentchoo1990@gmail.com
        n, flagged = 0, []
        for it in d.get("items", []):
            k = (it.get("kind") or "stock").lower()
            t = (it.get("ticker") or "").upper() or None
            if k == "stock" and not t:
                continue
            stance = (it.get("stance") or "neutral").lower()
            cur.execute("""INSERT INTO v2_newsletter_mentions (date, source, subject, kind, ticker, topic, stance, point, thesis_relevant, hype)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (date, source, kind, ticker, topic) DO UPDATE SET stance = EXCLUDED.stance, point = EXCLUDED.point,
                             thesis_relevant = EXCLUDED.thesis_relevant, hype = EXCLUDED.hype""",
                        (it.get("date") or str(TODAY), (it.get("source") or "?")[:80], (it.get("subject") or "")[:200], k, t,
                         (it.get("topic") or "")[:120], stance, (it.get("point") or "")[:400],
                         bool(it.get("thesis_relevant")), bool(it.get("hype"))))
            n += 1
            lvl = (known(cur, t) or {}).get("level") if t else None
            if t and lvl in ("watchlist", "book") and it.get("thesis_relevant") and stance in ("negative", "positive"):
                alert(cur, t, "Newsletter flag", "medium" if (stance == "negative" and lvl == "book") else "info",
                      f"{it.get('source')}: {(it.get('point') or '')[:240]}", f"nl-{t}-{it.get('source')}-{it.get('date') or TODAY}",
                      source="claude", action="Check against the thesis pillars" if stance == "negative" else "Note for the thesis")
                flagged.append(t)
        return f"{n} newsletter tags from {len({i.get('source') for i in d.get('items', [])})} sources" + (
            f"; flagged {', '.join(sorted(set(flagged)))}" if flagged else "")
    if kind == "exec_summary":            # add or refresh the executive summary on the latest thesis (no new version)
        n = 0
        for it in d.get("items", []):
            t = (it.get("ticker") or "").upper()
            if t and isinstance(it.get("exec_summary"), dict):
                cur.execute("""UPDATE v2_theses SET sections = COALESCE(sections, '{}'::jsonb) || jsonb_build_object('exec_summary', %s::jsonb)
                               WHERE ticker = %s AND version = (SELECT MAX(version) FROM v2_theses WHERE ticker = %s)""",
                            (J(it["exec_summary"]), t, t))
                n += cur.rowcount
        return f"{n} executive summaries"
    if kind == "tam":                     # add or refresh the TAM target on the latest thesis (no new version)
        n = 0
        for it in d.get("items", []):
            t = (it.get("ticker") or "").upper()
            if t and isinstance(it.get("tam"), dict) and it["tam"].get("scenarios"):
                cur.execute("""UPDATE v2_theses SET valuation = COALESCE(valuation, '{}'::jsonb) || jsonb_build_object('tam', %s::jsonb)
                               WHERE ticker = %s AND version = (SELECT MAX(version) FROM v2_theses WHERE ticker = %s)""",
                            (J(it["tam"]), t, t))
                n += cur.rowcount
        return f"{n} TAM targets"
    if kind == "reason_cards":
        n = 0
        for it in d.get("items", []):
            t = (it.get("ticker") or "").upper()
            card = {k: it.get(k) for k in ("why_cheap", "who_selling", "market_missing", "buyers_back")}
            if t and all(card.values()):
                cur.execute("UPDATE v2_names SET reason_card = %s WHERE ticker = %s", (J(card), t))
                n += cur.rowcount
        return f"{n} reason cards"
    if kind == "thesis":
        return load_thesis(cur, d)
    if kind in ("news_tags", "filing_tags"):
        table = "v2_news" if kind == "news_tags" else "v2_events"
        n = 0
        for it in d.get("items", []):
            tag = (it.get("tag") or "").lower()
            if tag not in TAGS or not it.get("id"):
                continue
            cur.execute(f"UPDATE {table} SET tag = %s, note = %s WHERE id = %s RETURNING ticker, "
                        + ("headline" if table == "v2_news" else "kind") + " AS what", (tag, it.get("note"), it["id"]))
            row = cur.fetchone()
            if not row:
                continue
            n += 1
            book = (known(cur, row["ticker"]) or {}).get("level") == "book"
            if tag == "breaks":
                alert(cur, row["ticker"], "Thesis pillar broken", "high", f"{row['what']}: {it.get('note') or ''}",
                      f"{kind}-{it['id']}", source="claude", action="Full re-review; buy signal paused")
            elif tag == "weakens" and book:
                alert(cur, row["ticker"], "Thesis weakened", "medium", f"{row['what']}: {it.get('note') or ''}",
                      f"{kind}-{it['id']}", source="claude")
        return f"{n} tags"
    if kind == "buy_checks":
        n = 0
        for it in d.get("items", []):
            t = (it.get("ticker") or "").upper()
            ok = (it.get("verdict") or "").lower() == "holds"
            cur.execute("""UPDATE v2_alerts SET sent_via = NULL,
                             kind = %s, severity = %s,
                             detail = detail || %s, action = %s
                           WHERE ticker = %s AND kind = 'Buy signal' AND sent_via = 'hold' AND NOT sent""",
                        ("Buy signal confirmed" if ok else "Buy signal, thesis weakened", "high" if ok else "medium",
                         f" · Claude: {'thesis holds' if ok else 'thesis has weakened'} — {it.get('note') or ''}",
                         "Decide in the app" if ok else "Review before buying", t))
            n += cur.rowcount
        return f"{n} buy checks"
    if kind == "outlook":
        cur.execute("INSERT INTO v2_industry_outlooks (theme, as_of, kind, content) VALUES (%s,%s,%s,%s)",
                    (d.get("theme"), TODAY, d.get("kind") or "light", J(d.get("content") or {})))
        return f"outlook {d.get('theme')}"
    raise ValueError(f"unknown type {kind!r}")


def main(dir_=None, conn=None, log=print):
    """Load new files from a local folder (dir_) or the Drive inbox. Also used by the app's Sync button."""
    own = conn is None
    conn = conn or db()
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()
    args = argparse.Namespace(dir=dir_)
    if args.dir:
        import hashlib  # keyed by file name and content, so a checked-in folder loads once however often it is checked out
        files = [{"id": "dir:" + p.name, "name": p.name, "modifiedTime": hashlib.sha1(p.read_bytes()).hexdigest()[:16], "path": p}
                 for p in sorted(Path(args.dir).glob("*.json"))] if Path(args.dir).is_dir() else []
    else:
        files = drive_list(DRIVE_INBOX)
        if files is None:
            log("Inbox: no Drive credentials (GOOGLE_SERVICE_ACCOUNT_JSON); nothing loaded.")
            return {"loaded": 0, "files": [], "note": "no Drive credentials"}
        files = [f for f in files if f["name"].lower().endswith(".json")]
    done, loaded = 0, []
    for f in files:
        cur.execute("SELECT 1 FROM v2_inbox_log WHERE file_id = %s AND modified = %s", (f["id"], f["modifiedTime"]))
        if cur.fetchone():
            continue
        try:
            text = f["path"].read_text() if "path" in f else drive_read(f["id"])
            d = json.loads(text)
            result = load(cur, f["name"], d)
            kind = d.get("type")
        except Exception as e:
            conn.rollback()
            result, kind = f"ERROR {e.__class__.__name__}: {str(e)[:150]}", "error"
            alert(cur, "SYSTEM", "Inbox file rejected", "info", f"{f['name']}: {result}", f"inbox-{f['id']}-{f['modifiedTime']}")
        cur.execute("INSERT INTO v2_inbox_log (file_id, modified, name, kind, result) VALUES (%s,%s,%s,%s,%s)",
                    (f["id"], f["modifiedTime"], f["name"], kind, result))
        conn.commit()
        done += 1
        loaded.append(f"{f['name']}: {result}")
        log(f"  {f['name']}: {result}")
    log(f"Inbox: {done} new file(s) loaded, {len(files) - done} already done.")
    if own:
        conn.close()
    return {"loaded": done, "files": loaded}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    main(ap.parse_args().dir)
