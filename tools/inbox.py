"""Load results from Claude's scheduled tasks (Drive folder ValueInvestor/v2-inbox) into the database.

  python tools\\inbox.py              read new files from Drive
  python tools\\inbox.py --dir PATH   read *.json from a local folder (e.g. inbox-seed/, loaded once per file)
Each file is loaded once (tracked by file id and modified time). Files are never edited or deleted.
"""
import sys, json, argparse, hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import db, J, alert, ensure_schema, heartbeat, drive_list, drive_read, DRIVE_INBOX, TODAY
import valuation as V


WATCH_CAP = 50
SECTION_KEYS = {"industry_map", "size_growth", "structure", "demand_supply", "industry_risks", "outlook", "winners",
                "business", "right_to_win", "moat", "tam_competitors", "management", "financials", "company_risks",
                "variant_view", "valuation", "pillars", "catalysts_flow", "premortem"}
TAGS = {"no change", "strengthens", "weakens", "breaks"}
SPLIT_GAP = 0.25          # second opinion: a base-value gap above 25% is a split
SPLIT_CAP = 74            # conviction cap while a split is unresolved (half size at most)
UNPROVEN_MOAT_CAP = 60    # conviction cap when the moat source has no grade A-C evidence

# minimum shape of each inbox file; anything else is rejected and logged, never half-loaded
REQUIRED = {"thesis": ["ticker"], "refresh": ["items"], "book": ["items"], "newsletter_tags": [],
            "exec_summary": ["items"], "tam": ["items"], "reason_cards": ["items"], "news_tags": ["items"],
            "filing_tags": ["items"], "buy_checks": ["items"], "outlook": ["theme"], "week_ahead": [],
            "second_opinion": ["ticker"], "idea": ["ticker"]}


class Later(Exception):
    """The file depends on something not loaded yet (e.g. a second opinion before its thesis): retry on the next run."""


def validate(d):
    if not isinstance(d, dict):
        raise ValueError("file is not a JSON object")
    kind = d.get("type")
    if kind not in REQUIRED:
        raise ValueError(f"unknown type {kind!r}")   # validate() stops this earlier
    missing = [k for k in REQUIRED[kind] if not d.get(k)]
    if missing:
        raise ValueError(f"{kind} file missing {', '.join(missing)}")
    if "items" in REQUIRED[kind] and not isinstance(d["items"], list):
        raise ValueError("items must be a list")
    for blk in [d] + (d.get("items") if isinstance(d.get("items"), list) else []):
        a = blk.get("assumptions") if isinstance(blk, dict) else None
        if a is not None and not (isinstance(a, dict) and isinstance(a.get("scenarios"), dict) and "base" in a["scenarios"]):
            raise ValueError("assumptions need a scenarios object with at least a base case")


def research_of(cur, t):
    cur.execute("SELECT data FROM v2_research WHERE ticker = %s ORDER BY as_of DESC LIMIT 1", (t,))
    r = cur.fetchone()
    return (r or {}).get("data") or {}


def price_of(cur, t):
    cur.execute("SELECT close FROM v2_prices WHERE ticker = %s ORDER BY date DESC LIMIT 1", (t,))
    r = cur.fetchone()
    return float(r["close"]) if r and r["close"] is not None else None


def apply_engine(cur, t, blk, val, style=None):
    """Run the valuation engine on the file's assumptions; the engine's numbers replace any typed-in values.
    Also grades evidence and applies conviction caps. Returns (entry_target or None, cap or None, notes)."""
    notes, cap, entry = [], None, None
    a = blk.get("assumptions")
    if a:
        try:
            rs = research_of(cur, t)
            out = V.run(a, rs, price_of(cur, t), style=style, sector=rs.get("sector"), industry=rs.get("industry"))
            out["assumptions"] = a
            val["engine"] = out
            if out.get("dcf_valid"):
                for k in ("iv_low", "iv_base", "iv_high"):
                    val[k] = out[k]
                entry = out.get("entry_target")
                if (style or "value") == "growth" and out.get("growth_zone"):
                    val["growth_zone_engine"] = out["growth_zone"]
            else:
                notes.append(out.get("note"))
        except (ValueError, ZeroDivisionError, TypeError, KeyError) as e:
            val["engine_error"] = str(e)[:200]
            notes.append(f"Valuation engine could not run: {str(e)[:120]}")
    else:
        notes.append("No assumptions file: values are typed in, not computed (unverified)")
    if blk.get("claims"):
        val["claims"] = blk["claims"]
        ev = V.evidence_summary(blk["claims"])
        val["evidence"] = ev
        if ev["weak_flag"]:
            notes.append(f"{ev['pillar_weak_share']:.0%} of pillar support is analyst estimates or AI assumptions (above 40%)")
    ms = blk.get("moat_source")
    if ms is not None or blk.get("claims") is not None:
        proven = isinstance(ms, dict) and ms.get("type") and str(ms.get("grade") or "E").upper()[:1] in "ABC"
        val["moat_source"] = ms
        if not proven:
            cap = UNPROVEN_MOAT_CAP
            notes.append(f"Moat source not shown with grade A-C evidence: conviction capped at {cap}")
    if notes:
        val["engine_notes"] = notes
    return entry, cap, notes


def capped(conv, cap):
    try:
        return min(int(conv), cap) if cap and conv is not None else conv
    except (TypeError, ValueError):
        return conv


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
    for k in ("change_my_mind", "expected"):
        if d.get(k):
            sections[k] = d[k]
    val = dict(d.get("valuation") or {})
    entry, cap, notes = apply_engine(cur, t, d, val, (d.get("style") or n.get("style")))
    entry = entry if entry is not None else d.get("entry_target")
    conv = capped(d.get("conviction"), cap)
    cur.execute("""INSERT INTO v2_theses (ticker, version, author, approval, sections, valuation, conviction, verdict, entry_target)
                   VALUES (%s,%s,'claude','pending',%s,%s,%s,%s,%s)""",
                (t, ver, J(sections), J(val), conv, d.get("verdict"), entry))
    if d.get("drivers"):
        cur.execute("UPDATE v2_names SET drivers = %s WHERE ticker = %s", ([str(x)[:40] for x in d["drivers"]][:6], t))
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
    from ideas import researched
    researched(cur, t, d.get("verdict") or (sections.get("exec_summary") or {}).get("recommendation"), ver)
    alert(cur, t, "New thesis ready", "medium",
          f"v{ver} · {d.get('verdict') or '?'} · conviction {conv or '?'} · entry target "
          f"{('$%.2f' % float(entry)) if entry else '—'}{' (engine)' if val.get('engine', {}).get('dcf_valid') else ''}.{moved} Approve it in the app."
          + (f" Missing sections: {', '.join(missing)}." if missing else "")
          + ("".join(f" {x}." for x in notes if x) if notes else ""),
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


def load_second_opinion(cur, d):
    """A second analyst's blind valuation and red-team attacks on the latest thesis.
    {"type":"second_opinion","ticker","model","thesis_version","assumptions":{...},"verdict":"BUY|WATCH|PASS",
     "attacks":[{"point","evidence","grade","pillar","category":"factual|financial|valuation|interpretation"}],
     "rebuttals":[{"attack":0,"response","resolved":true}]}   (rebuttals come from Claude in a later file)"""
    t = d["ticker"].upper()
    if (d.get("rebuttals") or d.get("improvements_applied")) and not d.get("attacks"):   # Claude answers the latest review
        cur.execute("SELECT * FROM v2_second_opinion WHERE ticker = %s AND kind = 'review' ORDER BY created_at DESC LIMIT 1", (t,))
        rv = cur.fetchone()
        if not rv:
            raise Later(f"no second-opinion review for {t} yet")
        res = dict(rv["result"] or {})
        res["rebuttals"] = (res.get("rebuttals") or []) + (d.get("rebuttals") or [])
        done = {int(x) for x in (d.get("improvements_applied") or []) if str(x).isdigit()}
        for i, imp in enumerate(res.get("improvements") or []):
            if i in done:
                imp["applied"] = True
        res["open_attacks"] = sum(1 for i, _ in enumerate(res.get("attacks") or [])
                                  if not any(r.get("attack") == i and r.get("resolved") for r in res["rebuttals"]))
        cur.execute("UPDATE v2_second_opinion SET result = %s WHERE id = %s", (J(res), rv["id"]))
        cur.execute("INSERT INTO v2_second_opinion (ticker, kind, model, thesis_version, result) VALUES (%s,'rebuttal',%s,%s,%s)",
                    (t, d.get("model") or "claude", rv["thesis_version"],
                     J({"rebuttals": d.get("rebuttals") or [], "improvements_applied": sorted(done)})))
        if res["open_attacks"] == 0:
            cur.execute("UPDATE v2_alerts SET addressed = TRUE, note = 'all red-team points answered' "
                        "WHERE ticker = %s AND kind IN ('Second opinion split','Open red-team points') AND NOT addressed", (t,))
        return f"rebuttal {t}: {res['open_attacks']} point(s) still open"
    cur.execute("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (t,))
    th = cur.fetchone()
    if not th:
        raise Later(f"no thesis for {t} yet")
    mine = ((th["valuation"] or {}).get("engine") or {}).get("assumptions")
    res = {"attacks": d.get("attacks") or [], "rebuttals": d.get("rebuttals") or [], "summary": d.get("summary"),
           "improvements": [i for i in (d.get("improvements") or []) if isinstance(i, dict) and i.get("suggestion")][:10],
           "scores": d.get("scores") if isinstance(d.get("scores"), dict) else None}
    if d.get("assumptions") and mine:
        try:
            res["gap"] = V.attribute_gap(mine, d["assumptions"], research_of(cur, t), price_of(cur, t))
        except (ValueError, ZeroDivisionError, TypeError, KeyError) as e:
            res["gap_error"] = str(e)[:200]
    elif d.get("assumptions"):
        try:
            o = V.run(d["assumptions"], research_of(cur, t), price_of(cur, t))
            res["gap"] = {"a": num_((th["valuation"] or {}).get("iv_base")), "b": o.get("iv_base"), "parts": [],
                          "note": "Claude's thesis has no assumptions file, so the gap cannot be attributed"}
            if res["gap"]["a"] and res["gap"]["b"]:
                res["gap"]["gap"] = round(res["gap"]["b"] / res["gap"]["a"] - 1, 4)
        except (ValueError, ZeroDivisionError, TypeError, KeyError) as e:
            res["gap_error"] = str(e)[:200]
    gap = (res.get("gap") or {}).get("gap")
    verdict_split = bool(d.get("verdict") and th["verdict"] and d["verdict"].upper()[:3] != str(th["verdict"]).upper()[:3])
    open_attacks = [i for i, _ in enumerate(res["attacks"])
                    if not any(r.get("attack") == i and r.get("resolved") for r in res["rebuttals"])]
    split = (gap is not None and abs(gap) > SPLIT_GAP) or verdict_split
    res.update(split=split, verdict_split=verdict_split, open_attacks=len(open_attacks), claude_verdict=th["verdict"])
    cur.execute("""INSERT INTO v2_second_opinion (ticker, kind, model, thesis_version, verdict, result)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (t, "rebuttal" if d.get("rebuttals") and not d.get("attacks") else "review", d.get("model") or "claude-second",
                 th["version"], d.get("verdict"), J(res)))
    hi = [i for i in res["improvements"] if str(i.get("priority") or "").lower() == "high"]
    if hi and not (split or open_attacks):
        alert(cur, t, "Thesis improvements suggested", "info",
              "; ".join(f"{i.get('area', '')}: {i['suggestion']}"[:140] for i in hi[:3]),
              f"soimp-{t}-v{th['version']}", source="claude", action="Applied at the next refresh")
    if split or open_attacks:
        if split and th["conviction"] and int(th["conviction"]) > SPLIT_CAP:
            cur.execute("UPDATE v2_theses SET conviction = %s WHERE id = %s", (SPLIT_CAP, th["id"]))
        cur.execute("SELECT level FROM v2_names WHERE ticker = %s", (t,))
        lvl = (cur.fetchone() or {}).get("level")
        top = ", ".join(f"{p['input']} {p['share']:.0%}" for p in ((res.get("gap") or {}).get("parts") or [])[:3])
        alert(cur, t, "Second opinion split" if split else "Open red-team points", "high" if (split and lvl == "book") else "medium",
              (f"Base value Claude {money_(res['gap'].get('a'))} vs second {money_(res['gap'].get('b'))} ({gap:+.0%})"
               + (f", driven by {top}" if top else "") + ". " if gap is not None else "")
              + (f"Verdict {th['verdict']} vs {d.get('verdict')}. " if verdict_split else "")
              + (f"{len(open_attacks)} attack(s) not yet answered." if open_attacks else "")
              + (f" Conviction capped at {SPLIT_CAP}." if split else ""),
              f"so-{t}-v{th['version']}-{len(res['rebuttals'])}", source="claude", action="Review both views")
    return f"second opinion {t}: gap {gap:+.0%}" if gap is not None else f"second opinion {t}"


def money_(v):
    return f"${float(v):,.2f}" if v is not None else "—"


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
            cur.execute("SELECT style FROM v2_names WHERE ticker = %s", (t,))
            st_ = (cur.fetchone() or {}).get("style")
            if not it.get("assumptions") and (last["valuation"] or {}).get("engine", {}).get("assumptions"):
                it = dict(it, assumptions=last["valuation"]["engine"]["assumptions"])   # re-run last assumptions at today's price
            e_entry, cap, _ = apply_engine(cur, t, it, val, st_)
            conv = capped(it.get("conviction", last["conviction"]), cap)
            verdict = it.get("verdict") or last["verdict"]
            entry = e_entry if e_entry is not None else it.get("entry_target", last["entry_target"])
            secs = dict(last["sections"] or {})
            for k in ("change_my_mind", "expected"):
                if it.get(k):
                    secs[k] = it[k]
            if isinstance(it.get("exec_summary"), dict):
                secs["exec_summary"] = it["exec_summary"]
            cur.execute("""INSERT INTO v2_theses (ticker, version, author, approval, sections, valuation, conviction, verdict, entry_target)
                           VALUES (%s, %s, 'claude-refresh', 'pending', %s, %s, %s, %s, %s)""",
                        (t, last["version"] + 1, J(secs), J(val), conv, verdict, entry))
            if it.get("reason") != "engine_backfill":
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
            backfill = it.get("reason") == "engine_backfill"
            alert(cur, t, "Engine values added" if backfill else "Thesis refreshed after results",
                  "info" if backfill and not changed else "high" if changed else "medium",
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
        if isinstance(d.get("digest"), dict) and d["digest"].get("top"):
            cur.execute("""INSERT INTO v2_settings (key, value, updated_at) VALUES ('news_digest', %s, now())
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                        (J({"as_of": str(TODAY), **d["digest"]}),))
        ni = 0
        for it in d.get("issues") or []:
            if not isinstance(it, dict) or not it.get("source"):
                continue
            url = it.get("url") if str(it.get("url") or "").startswith("https://mail.google.com/") else None
            cur.execute("""INSERT INTO v2_newsletter_issues (date, source, subject, headline, summary, url, tickers)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (date, source, subject) DO UPDATE SET headline = EXCLUDED.headline,
                             summary = EXCLUDED.summary, url = COALESCE(EXCLUDED.url, v2_newsletter_issues.url),
                             tickers = EXCLUDED.tickers, loaded_at = now()""",
                        (it.get("date") or str(TODAY), it["source"][:80], (it.get("subject") or "")[:200], (it.get("headline") or "")[:300],
                         (it.get("summary") or "")[:600], url, [str(x).upper()[:10] for x in (it.get("tickers") or [])][:10]))
            ni += 1
        return f"{ni} newsletter issues, {n} newsletter tags from {len({i.get('source') for i in d.get('items', [])})} sources" + (
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
    if kind == "second_opinion":
        return load_second_opinion(cur, d)
    if kind == "idea":                    # a stock idea Ming gave Claude in a chat
        from ideas import add_idea
        ok, msg = add_idea(cur, d["ticker"], "chat", note=d.get("note"), source=d.get("source"), url=d.get("url"))
        if not ok:
            raise ValueError(msg)
        return msg
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
    raise ValueError(f"unknown type {kind!r}")   # validate() stops this earlier


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
        chash, run_id = None, None
        try:
            text = f["path"].read_text() if "path" in f else drive_read(f["id"])
            chash = hashlib.sha256(text.encode()).hexdigest()[:24]
            d = json.loads(text)
            validate(d)
            kind, run_id = d.get("type"), d.get("run_id")
            cur.execute("""SELECT name FROM v2_inbox_log WHERE (content_hash = %s OR (run_id IS NOT NULL AND run_id = %s))
                           AND kind <> 'error' LIMIT 1""", (chash, run_id))
            dup = cur.fetchone()
            result = f"duplicate of {dup['name']}: skipped" if dup else load(cur, f["name"], d)
            if dup:
                kind = "duplicate"
        except Later as e:
            conn.rollback()
            log(f"  {f['name']}: waiting ({e})")
            continue
        except Exception as e:
            conn.rollback()
            result, kind = f"ERROR {e.__class__.__name__}: {str(e)[:150]}", "error"
            alert(cur, "SYSTEM", "Inbox file rejected", "info", f"{f['name']}: {result}", f"inbox-{f['id']}-{f['modifiedTime']}")
        cur.execute("INSERT INTO v2_inbox_log (file_id, modified, name, kind, result, content_hash, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (f["id"], f["modifiedTime"], f["name"], kind, result, chash if kind != "error" else None, run_id))
        conn.commit()
        done += 1
        loaded.append(f"{f['name']}: {result}")
        log(f"  {f['name']}: {result}")
    heartbeat(cur, "inbox", note=f"{done} loaded")
    conn.commit()
    log(f"Inbox: {done} new file(s) loaded, {len(files) - done} already done.")
    if own:
        conn.close()
    return {"loaded": done, "files": loaded}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    main(ap.parse_args().dir)
