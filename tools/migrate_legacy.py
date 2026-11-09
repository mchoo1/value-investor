"""Step 2: copy legacy ValueInvestor data into the v2 tables.

Reads the old tables only; never changes or deletes them.
  python tools\\migrate_legacy.py           -> dry run (prints, writes nothing)
  python tools\\migrate_legacy.py --apply   -> writes to the v2 tables
Run --apply once. Re-running is safe but resets status to the legacy mapping.
"""
import os, sys, re, json, datetime as dt
from collections import Counter
import psycopg2, psycopg2.extras

APPLY = "--apply" in sys.argv
conn = psycopg2.connect(os.environ["DATABASE_URL"],
                        cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

GOOD_TIERS = {"CORE", "HIGH", "HIGH WATCH"}
BAD_STATES = {"broken", "at_risk"}
BAD_STAGES = {"ARCHIVED", "POOL_DROPPED"}
EXCLUDE = {"HALO3", "TESTS_CONVICTI", "X2"}  # test junk found in the dry run
NORMAL = re.compile(r"^([A-Z]{1,5}([.-][A-Z]{1,2})?|\d{3,6}\.(T|TW|HK|SS|SZ|KS|KQ))$")
junk_seen = set()


def rows(table):
    try:
        cur.execute(f"SELECT * FROM {table}")
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        conn.rollback()
        print(f"  [skip] {table}: {e.__class__.__name__}")
        return []


def as_date(v):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


names = {}


def entry(ticker):
    t = (ticker or "").strip().upper()
    if not t:
        return None
    if t in EXCLUDE or not NORMAL.match(t):
        junk_seen.add(t)
        return None
    return names.setdefault(t, {"name": None, "dates": [], "themes": set(),
                                "stages": set(), "legacy": {}})


# 1. Theses (skip test artifacts; keep the latest row per ticker)
skipped = 0
for r in rows("thesis"):
    state = (r.get("thesis_status") or r.get("status") or "").strip()
    if state == "void_test_artifact":
        skipped += 1
        continue
    n = entry(r.get("ticker"))
    if not n:
        continue
    n["dates"] += [as_date(r.get("created_date")), as_date(r.get("updated_date"))]
    prev = n["legacy"].get("thesis")
    if prev is None or str(r.get("updated_date") or "") >= str(prev.get("updated_date") or ""):
        n["legacy"]["thesis"] = r

# 2. Shortlist
for r in rows("shortlist"):
    n = entry(r.get("ticker"))
    if not n:
        continue
    n["dates"] += [as_date(r.get(k)) for k in ("created_date", "shortlisted_date", "updated_date")]
    if r.get("stage"):
        n["stages"].add(r["stage"].strip().upper())
    for tag in re.split(r"[,;|/]", r.get("theme_tag") or ""):
        if tag.strip():
            n["themes"].add(tag.strip())
    n["legacy"]["shortlist"] = r

# 3. Watchlist
for r in rows("watchlist"):
    n = entry(r.get("ticker"))
    if not n:
        continue
    n["dates"].append(as_date(r.get("added_date")))
    n["name"] = n["name"] or r.get("name")
    n["legacy"]["watchlist"] = r

# 4. Research history
for r in rows("research_history"):
    n = entry(r.get("ticker"))
    if not n:
        continue
    n["dates"] += [as_date(r.get(k)) for k in ("first_researched_date", "archived_date", "created_date")]
    n["legacy"]["research_history"] = r

# Map each name to a v2 status
records = []
for ticker, n in sorted(names.items()):
    th = n["legacy"].get("thesis") or {}
    tier = (th.get("conviction_tier") or "").strip().upper()
    state = (th.get("thesis_status") or th.get("status") or "").strip().lower()
    rh = n["legacy"].get("research_history") or {}

    if tier in GOOD_TIERS:
        status, reason = "candidate", None
    elif tier == "AVOID" or state in BAD_STATES:
        status, reason = "rejected", f"Legacy thesis: {tier or state}"
    elif n["stages"] and n["stages"] <= BAD_STAGES and not th:
        status = "rejected"
        reason = rh.get("archive_reason") or f"Legacy shortlist: {'/'.join(sorted(n['stages']))}"
    else:
        status, reason = "bench", None

    dates = [d for d in n["dates"] if d]
    records.append({
        "ticker": ticker,
        "name": n["name"],
        "themes": sorted(n["themes"]),
        "status": status,
        "first_seen": min(dates) if dates else None,
        "last_screened": max(dates) if dates else None,
        "reject_reason": reason,
        "legacy": n["legacy"],
    })

# Legacy triggers -> alert history
SEV = {"critical": "high", "warning": "medium"}
alerts = [{
    "ticker": (r.get("ticker") or "").upper(),
    "fired_at": as_date(r.get("fired_date")) or as_date(r.get("created_date")),
    "kind": r.get("trigger_type"),
    "severity": SEV.get((r.get("severity") or "").lower(), "info"),
    "detail": r.get("details"),
    "action": r.get("suggested_action"),
    "addressed": bool(r.get("addressed")),
    "dedupe_key": f"legacy-trigger-{r.get('id')}",
} for r in rows("triggers") if (r.get("ticker") or "").upper() in names]

# Summary
counts = Counter(r["status"] for r in records)
all_dates = [r["last_screened"] for r in records if r["last_screened"]]
print(f"\nUnique tickers: {len(records)}")
print(f"Test artifacts skipped: {skipped}")
for s in ("candidate", "bench", "rejected"):
    print(f"  {s:<10} {counts.get(s, 0)}")
if all_dates:
    print(f"Last looked at: {min(all_dates)} to {max(all_dates)}")
print(f"Legacy alerts: {len(alerts)}")
cands = [r["ticker"] for r in records if r["status"] == "candidate"]
print(f"\nCandidates ({len(cands)}): {', '.join(cands)}")
print(f"\nTest tickers skipped ({len(junk_seen)}): {', '.join(sorted(junk_seen))}")

if not APPLY:
    print("\nDRY RUN: nothing written. Re-run with --apply to write.")
    conn.close()
    sys.exit(0)

as_json = lambda o: psycopg2.extras.Json(o, dumps=lambda x: json.dumps(x, default=str))
w = conn.cursor()
for r in records:
    w.execute("""
        INSERT INTO v2_names (ticker, name, themes, level, status, first_seen,
                              last_screened, reject_reason, legacy)
        VALUES (%s, %s, %s, 'screen', %s, %s, %s, %s, %s)
        ON CONFLICT (ticker) DO UPDATE SET
          name = COALESCE(EXCLUDED.name, v2_names.name), themes = EXCLUDED.themes,
          status = EXCLUDED.status, first_seen = EXCLUDED.first_seen,
          last_screened = EXCLUDED.last_screened, reject_reason = EXCLUDED.reject_reason,
          legacy = EXCLUDED.legacy, updated_at = now()""",
        (r["ticker"], r["name"], r["themes"], r["status"], r["first_seen"],
         r["last_screened"], r["reject_reason"], as_json(r["legacy"])))
for a in alerts:
    w.execute("""
        INSERT INTO v2_alerts (ticker, fired_at, kind, severity, detail, source,
                               action, dedupe_key, sent, addressed)
        VALUES (%s, %s, %s, %s, %s, 'legacy', %s, %s, TRUE, %s)
        ON CONFLICT (dedupe_key) DO NOTHING""",
        (a["ticker"], a["fired_at"], a["kind"], a["severity"], a["detail"],
         a["action"], a["dedupe_key"], a["addressed"]))
conn.commit()
w.execute("SELECT status, COUNT(*) AS n FROM v2_names GROUP BY status ORDER BY status")
print("\nWRITTEN. v2_names now holds:")
for row in w.fetchall():
    print(f"  {row['status']:<10} {row['n']}")
conn.close()
