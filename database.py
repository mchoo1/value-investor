"""
Database helpers — works with SQLite locally and Postgres on Vercel/cloud.

Set DATABASE_URL env var (postgres://...) to use Postgres.
Otherwise falls back to local SQLite.
"""
import os
import json
import sqlite3
from datetime import datetime

# ── Backend detection ────────────────────────────────────────────
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_USE_PG = bool(_DATABASE_URL)

# SQLite path (used when DATABASE_URL is not set)
# Vercel filesystem is read-only — fall back to /tmp
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_BASE, "data"))
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    _DATA_DIR = "/tmp/valueinvestor_data"
    os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "valueinvestor.db")


# ── Connection helpers ───────────────────────────────────────────
def get_db():
    if _USE_PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def _sql(q):
    """Convert SQLite-style SQL to Postgres when needed."""
    if not _USE_PG:
        return q
    return (q
            .replace("?", "%s")
            .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            .replace("INSERT OR REPLACE", "INSERT")
            .replace("last_insert_rowid()", "lastval()"))


def _rows(cursor):
    """Fetch all rows as plain dicts."""
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):          # psycopg2 RealDictRow
        return [dict(r) for r in rows]
    if hasattr(rows[0], "keys"):           # sqlite3.Row
        return [dict(r) for r in rows]
    # Fallback: zip column names
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


# ── Schema ───────────────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = conn.cursor()

    if _USE_PG:
        # Postgres schema
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL UNIQUE,
                name TEXT,
                market TEXT DEFAULT 'US',
                added_date TEXT,
                notes TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                name TEXT,
                entry_price REAL,
                shares REAL,
                entry_date TEXT,
                target_price REAL,
                stop_loss REAL,
                status TEXT DEFAULT 'open',
                exit_price REAL,
                exit_date TEXT,
                notes TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS thesis (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                title TEXT,
                created_date TEXT,
                updated_date TEXT,
                investment_case TEXT,
                moat_type TEXT,
                moat_rating TEXT,
                revenue_growth_assumption REAL,
                margin_assumption REAL,
                wacc_assumption REAL,
                terminal_growth REAL,
                intrinsic_value REAL,
                margin_of_safety REAL,
                buy_trigger TEXT,
                sell_trigger TEXT,
                risk_factors TEXT,
                current_price REAL,
                status TEXT DEFAULT 'active',
                entry_pe REAL,
                entry_roe REAL,
                entry_revenue_growth REAL,
                entry_net_margin REAL,
                entry_market_cap REAL,
                target_price REAL,
                bear_target REAL,
                bull_target REAL,
                conviction_tier TEXT,
                verdict TEXT,
                key_90d_metric TEXT,
                strategy TEXT,
                probability_weighted_ev REAL,
                bear_probability REAL,
                base_probability REAL,
                bull_probability REAL,
                position_size_pct REAL,
                entry_price_low REAL,
                entry_price_high REAL,
                stop_loss REAL,
                risk_reward_ratio REAL,
                target_price_36m REAL,
                macro_sensitivity TEXT,
                report_date TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS weekly_review (
                id SERIAL PRIMARY KEY,
                thesis_id INTEGER,
                ticker TEXT,
                review_date TEXT,
                current_price REAL,
                target_price REAL,
                price_change_pct REAL,
                thesis_intact INTEGER DEFAULT 1,
                revenue_on_track INTEGER DEFAULT 1,
                margin_on_track INTEGER DEFAULT 1,
                new_developments TEXT,
                assumption_changes TEXT,
                action TEXT DEFAULT 'hold',
                confidence INTEGER DEFAULT 3,
                notes TEXT,
                FOREIGN KEY (thesis_id) REFERENCES thesis(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS screener_results (
                id SERIAL PRIMARY KEY,
                run_date TEXT,
                market TEXT,
                filters TEXT,
                results TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_queue (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL UNIQUE,
                added_date TEXT,
                notes TEXT
            )
        """)
        # ── Postgres migrations: add new thesis columns if missing ──
        c.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'thesis'
        """)
        pg_thesis_cols = {row[0] if isinstance(row, tuple) else row["column_name"]
                         for row in c.fetchall()}
        for col, col_type in [
            ("conviction_tier",       "TEXT"),
            ("verdict",               "TEXT"),
            ("key_90d_metric",        "TEXT"),
            ("strategy",              "TEXT"),
            ("probability_weighted_ev","REAL"),
            ("bear_probability",      "REAL"),
            ("base_probability",      "REAL"),
            ("bull_probability",      "REAL"),
            ("position_size_pct",     "REAL"),
            ("entry_price_low",       "REAL"),
            ("entry_price_high",      "REAL"),
            ("stop_loss",             "REAL"),
            ("risk_reward_ratio",     "REAL"),
            ("target_price_36m",      "REAL"),
            ("macro_sensitivity",     "TEXT"),
            ("report_date",           "TEXT"),
        ]:
            if col not in pg_thesis_cols:
                c.execute(f"ALTER TABLE thesis ADD COLUMN {col} {col_type}")
        # ── Postgres migrations: add Sprint 1 thesis columns ──
        for col, col_type in [
            ("stage",              "TEXT"),
            ("thesis_status",      "TEXT"),
            ("version",            "INTEGER"),
            ("next_review_date",   "TEXT"),
            ("kill_switch_config", "TEXT"),
            ("deep_dive_pdf_path", "TEXT"),
        ]:
            if col not in pg_thesis_cols:
                c.execute(f"ALTER TABLE thesis ADD COLUMN {col} {col_type}")

        # ── Sprint 1: shortlist table ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS shortlist (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL UNIQUE,
                stage TEXT NOT NULL DEFAULT 'CANDIDATE_POOL',
                strategy TEXT,
                tier TEXT,
                archetype TEXT,
                composite_score REAL,
                axis_scores TEXT,
                one_line_thesis TEXT,
                verdict TEXT,
                first_cut_summary TEXT,
                source TEXT DEFAULT 'task_a',
                weeks_in_pool INTEGER DEFAULT 0,
                expires_date TEXT,
                created_date TEXT,
                updated_date TEXT
            )
        """)

        # ── Sprint 1: triggers table ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_subtype TEXT,
                severity TEXT NOT NULL DEFAULT 'warning',
                fired_date TEXT,
                details TEXT,
                suggested_action TEXT,
                addressed BOOLEAN DEFAULT FALSE,
                addressed_date TEXT,
                resolution_notes TEXT,
                created_date TEXT
            )
        """)

        # ── Sprint 1: research_history (novelty gate, append-only) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_history (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                first_researched_date TEXT,
                archived_date TEXT,
                archive_reason TEXT,
                final_stage TEXT,
                thesis_status TEXT,
                created_date TEXT
            )
        """)

        # ── Sprint 1: valuation_runs ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS valuation_runs (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                run_date TEXT,
                run_type TEXT DEFAULT 'manual',
                assumptions TEXT,
                dcf_output TEXT,
                peer_comp_output TEXT,
                blended_iv REAL,
                created_by TEXT DEFAULT 'user',
                created_date TEXT
            )
        """)

        # ── Postgres migrations: research_queue ──
        c.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'research_queue'
        """)
        pg_queue_cols = {row[0] if isinstance(row, tuple) else row["column_name"]
                        for row in c.fetchall()}
        if not pg_queue_cols:
            pass  # table was just created above
    else:
        # SQLite schema
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                name TEXT,
                market TEXT DEFAULT 'US',
                added_date TEXT,
                notes TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT,
                entry_price REAL,
                shares REAL,
                entry_date TEXT,
                target_price REAL,
                stop_loss REAL,
                status TEXT DEFAULT 'open',
                exit_price REAL,
                exit_date TEXT,
                notes TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS thesis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                title TEXT,
                created_date TEXT,
                updated_date TEXT,
                investment_case TEXT,
                moat_type TEXT,
                moat_rating TEXT,
                revenue_growth_assumption REAL,
                margin_assumption REAL,
                wacc_assumption REAL,
                terminal_growth REAL,
                intrinsic_value REAL,
                margin_of_safety REAL,
                buy_trigger TEXT,
                sell_trigger TEXT,
                risk_factors TEXT,
                current_price REAL,
                status TEXT DEFAULT 'active'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS weekly_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thesis_id INTEGER,
                ticker TEXT,
                review_date TEXT,
                current_price REAL,
                target_price REAL,
                price_change_pct REAL,
                thesis_intact INTEGER DEFAULT 1,
                revenue_on_track INTEGER DEFAULT 1,
                margin_on_track INTEGER DEFAULT 1,
                new_developments TEXT,
                assumption_changes TEXT,
                action TEXT DEFAULT 'hold',
                confidence INTEGER DEFAULT 3,
                notes TEXT,
                FOREIGN KEY (thesis_id) REFERENCES thesis(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS screener_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                market TEXT,
                filters TEXT,
                results TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                added_date TEXT,
                notes TEXT
            )
        """)
        # Migrations: add new columns if missing (thesis table)
        existing_cols = {row[1] for row in c.execute("PRAGMA table_info(thesis)")}
        for col, col_type in [
            ("entry_pe",             "REAL"),
            ("entry_roe",            "REAL"),
            ("entry_revenue_growth", "REAL"),
            ("entry_net_margin",     "REAL"),
            ("entry_market_cap",     "REAL"),
            ("target_price",         "REAL"),
            ("bear_target",          "REAL"),
            ("bull_target",          "REAL"),
            ("conviction_tier",      "TEXT"),
            ("verdict",              "TEXT"),
            ("key_90d_metric",       "TEXT"),
            ("strategy",             "TEXT"),
            ("probability_weighted_ev", "REAL"),
            ("bear_probability",     "REAL"),
            ("base_probability",     "REAL"),
            ("bull_probability",     "REAL"),
            ("position_size_pct",    "REAL"),
            ("entry_price_low",      "REAL"),
            ("entry_price_high",     "REAL"),
            ("stop_loss",            "REAL"),
            ("risk_reward_ratio",    "REAL"),
            ("target_price_36m",     "REAL"),
            ("macro_sensitivity",    "TEXT"),
            ("report_date",          "TEXT"),
            # Sprint 1 — lifecycle fields
            ("stage",                "TEXT"),
            ("thesis_status",        "TEXT"),
            ("version",              "INTEGER"),
            ("next_review_date",     "TEXT"),
            ("kill_switch_config",   "TEXT"),
            ("deep_dive_pdf_path",   "TEXT"),
        ]:
            if col not in existing_cols:
                c.execute(f"ALTER TABLE thesis ADD COLUMN {col} {col_type}")

        # ── Sprint 1: shortlist table ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS shortlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                stage TEXT NOT NULL DEFAULT 'CANDIDATE_POOL',
                strategy TEXT,
                tier TEXT,
                archetype TEXT,
                composite_score REAL,
                axis_scores TEXT,
                one_line_thesis TEXT,
                verdict TEXT,
                first_cut_summary TEXT,
                source TEXT DEFAULT 'task_a',
                weeks_in_pool INTEGER DEFAULT 0,
                expires_date TEXT,
                created_date TEXT,
                updated_date TEXT
            )
        """)

        # ── Sprint 1: triggers table ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_subtype TEXT,
                severity TEXT NOT NULL DEFAULT 'warning',
                fired_date TEXT,
                details TEXT,
                suggested_action TEXT,
                addressed INTEGER DEFAULT 0,
                addressed_date TEXT,
                resolution_notes TEXT,
                created_date TEXT
            )
        """)

        # ── Sprint 1: research_history (novelty gate, append-only) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                first_researched_date TEXT,
                archived_date TEXT,
                archive_reason TEXT,
                final_stage TEXT,
                thesis_status TEXT,
                created_date TEXT
            )
        """)

        # ── Sprint 1: valuation_runs ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS valuation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                run_date TEXT,
                run_type TEXT DEFAULT 'manual',
                assumptions TEXT,
                dcf_output TEXT,
                peer_comp_output TEXT,
                blended_iv REAL,
                created_by TEXT DEFAULT 'user',
                created_date TEXT
            )
        """)

    conn.commit()
    conn.close()
    print("Database initialized.")


# ── Watchlist CRUD ───────────────────────────────────────────────
def add_to_watchlist(ticker, name, market="US", notes=""):
    conn = get_db()
    try:
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d")
        if _USE_PG:
            c.execute(
                """INSERT INTO watchlist (ticker, name, market, added_date, notes)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (ticker) DO UPDATE SET
                     name=EXCLUDED.name, market=EXCLUDED.market,
                     added_date=EXCLUDED.added_date, notes=EXCLUDED.notes""",
                (ticker.upper(), name, market, now, notes)
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO watchlist (ticker, name, market, added_date, notes) VALUES (?,?,?,?,?)",
                (ticker.upper(), name, market, now, notes)
            )
        conn.commit()
        return True
    except Exception as e:
        return str(e)
    finally:
        conn.close()


def get_watchlist():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM watchlist ORDER BY added_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


def remove_from_watchlist(ticker):
    conn = get_db()
    c = conn.cursor()
    c.execute(_sql("DELETE FROM watchlist WHERE ticker=?"), (ticker.upper(),))
    conn.commit()
    conn.close()


# ── Portfolio CRUD ───────────────────────────────────────────────
def add_position(ticker, name, entry_price, shares, entry_date, target_price, stop_loss, notes=""):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        _sql("""INSERT INTO portfolio (ticker, name, entry_price, shares, entry_date, target_price, stop_loss, notes)
           VALUES (?,?,?,?,?,?,?,?)"""),
        (ticker.upper(), name, entry_price, shares, entry_date, target_price, stop_loss, notes)
    )
    conn.commit()
    conn.close()


def get_portfolio():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM portfolio ORDER BY entry_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


def update_position(pos_id, **kwargs):
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    sets = ", ".join(f"{k}={ph}" for k in kwargs)
    vals = list(kwargs.values()) + [pos_id]
    c.execute(f"UPDATE portfolio SET {sets} WHERE id={ph}", vals)
    conn.commit()
    conn.close()


def delete_position(pos_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(_sql("DELETE FROM portfolio WHERE id=?"), (pos_id,))
    conn.commit()
    conn.close()


# ── Thesis CRUD ──────────────────────────────────────────────────
def save_thesis(data: dict):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d")
    ph = "%s" if _USE_PG else "?"

    c.execute(_sql(f"SELECT id FROM thesis WHERE ticker={ph}"), (data["ticker"].upper(),))
    existing = c.fetchone()

    if existing:
        sets = ", ".join(f"{k}={ph}" for k in data if k != "ticker")
        vals = [data[k] for k in data if k != "ticker"] + [data["ticker"].upper()]
        c.execute(f"UPDATE thesis SET updated_date={ph}, {sets} WHERE ticker={ph}", [now] + vals)
        thesis_id = existing["id"] if isinstance(existing, dict) else existing[0]
    else:
        data["ticker"] = data["ticker"].upper()
        data["created_date"] = now
        data["updated_date"] = now
        cols = ", ".join(data.keys())
        placeholders = ", ".join([ph] * len(data))
        if _USE_PG:
            c.execute(f"INSERT INTO thesis ({cols}) VALUES ({placeholders}) RETURNING id",
                      list(data.values()))
            thesis_id = c.fetchone()["id"]
        else:
            c.execute(f"INSERT INTO thesis ({cols}) VALUES ({placeholders})", list(data.values()))
            c.execute("SELECT last_insert_rowid()")
            thesis_id = c.fetchone()[0]

    conn.commit()
    conn.close()
    return thesis_id


def get_thesis(ticker=None):
    conn = get_db()
    c = conn.cursor()
    if ticker:
        c.execute(_sql("SELECT * FROM thesis WHERE ticker=?"), (ticker.upper(),))
    else:
        c.execute("SELECT * FROM thesis ORDER BY updated_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


def delete_thesis(thesis_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(_sql("DELETE FROM thesis WHERE id=?"), (thesis_id,))
    conn.commit()
    conn.close()


def get_thesis_tickers():
    """Return all unique tickers that have ever had a thesis entry."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT DISTINCT ticker FROM thesis ORDER BY ticker")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [r["ticker"] for r in rows]
    return [r[0] for r in rows]


# ── Research Queue ───────────────────────────────────────────────
def get_research_queue():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM research_queue ORDER BY added_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


def add_to_research_queue(ticker, notes=""):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d")
    if _USE_PG:
        c.execute(
            """INSERT INTO research_queue (ticker, added_date, notes)
               VALUES (%s,%s,%s)
               ON CONFLICT (ticker) DO UPDATE SET added_date=EXCLUDED.added_date, notes=EXCLUDED.notes""",
            (ticker.upper(), now, notes)
        )
    else:
        c.execute(
            "INSERT OR REPLACE INTO research_queue (ticker, added_date, notes) VALUES (?,?,?)",
            (ticker.upper(), now, notes)
        )
    conn.commit()
    conn.close()


def remove_from_research_queue(ticker):
    conn = get_db()
    c = conn.cursor()
    c.execute(_sql("DELETE FROM research_queue WHERE ticker=?"), (ticker.upper(),))
    conn.commit()
    conn.close()


# ── Weekly Reviews ───────────────────────────────────────────────
def save_weekly_review(data: dict):
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    data["review_date"] = data.get("review_date", datetime.now().strftime("%Y-%m-%d"))
    cols = ", ".join(data.keys())
    placeholders = ", ".join([ph] * len(data))
    c.execute(f"INSERT INTO weekly_review ({cols}) VALUES ({placeholders})", list(data.values()))
    conn.commit()
    conn.close()


def get_weekly_reviews(ticker=None, thesis_id=None):
    conn = get_db()
    c = conn.cursor()
    if ticker:
        c.execute(_sql("SELECT * FROM weekly_review WHERE ticker=? ORDER BY review_date DESC"),
                  (ticker.upper(),))
    elif thesis_id:
        c.execute(_sql("SELECT * FROM weekly_review WHERE thesis_id=? ORDER BY review_date DESC"),
                  (thesis_id,))
    else:
        c.execute("SELECT * FROM weekly_review ORDER BY review_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════════
# SPRINT 1 — Shortlist CRUD
# ══════════════════════════════════════════════════════════════════

def get_shortlist(stage=None):
    conn = get_db()
    c = conn.cursor()
    if stage:
        c.execute(_sql("SELECT * FROM shortlist WHERE stage=? ORDER BY composite_score DESC"),
                  (stage,))
    else:
        c.execute("SELECT * FROM shortlist ORDER BY composite_score DESC NULLS LAST")
    rows = _rows(c)
    conn.close()
    return rows


def save_shortlist_ticker(data: dict):
    """Upsert a shortlist row by ticker."""
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    ticker = data["ticker"].upper()
    data["ticker"] = ticker
    data.setdefault("created_date", now)
    data["updated_date"] = now

    c.execute(_sql(f"SELECT id FROM shortlist WHERE ticker={ph}"), (ticker,))
    existing = c.fetchone()
    if existing:
        sets = ", ".join(f"{k}={ph}" for k in data if k not in ("id", "ticker", "created_date"))
        vals = [data[k] for k in data if k not in ("id", "ticker", "created_date")] + [ticker]
        c.execute(f"UPDATE shortlist SET {sets} WHERE ticker={ph}", vals)
    else:
        cols = ", ".join(data.keys())
        placeholders = ", ".join([ph] * len(data))
        c.execute(f"INSERT INTO shortlist ({cols}) VALUES ({placeholders})", list(data.values()))

    conn.commit()
    conn.close()


def patch_thesis(ticker: str, fields: dict):
    """Partial update of thesis row by ticker (skips id, ticker, created_date)."""
    if not fields:
        return False
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    safe_keys = [k for k in fields if k not in ("id", "ticker", "created_date")]
    if not safe_keys:
        conn.close()
        return False
    sets = ", ".join(f"{k}={ph}" for k in safe_keys)
    vals = [fields[k] for k in safe_keys] + [now, ticker.upper()]
    c.execute(_sql(f"UPDATE thesis SET {sets}, updated_date={ph} WHERE ticker={ph}"), vals)
    updated = c.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def patch_shortlist(ticker: str, fields: dict):
    """Partial update of shortlist row by ticker."""
    if not fields:
        return False
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    safe_keys = [k for k in fields if k not in ("id", "ticker", "created_date")]
    sets = ", ".join(f"{k}={ph}" for k in safe_keys)
    vals = [fields[k] for k in safe_keys] + [now, ticker.upper()]
    c.execute(_sql(f"UPDATE shortlist SET {sets}, updated_date={ph} WHERE ticker={ph}"), vals)
    updated = c.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_shortlist_ticker(ticker: str):
    conn = get_db()
    c = conn.cursor()
    c.execute(_sql("DELETE FROM shortlist WHERE ticker=?"), (ticker.upper(),))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════
# SPRINT 1 — Triggers CRUD
# ══════════════════════════════════════════════════════════════════

def get_triggers(ticker=None, addressed=None, severity=None):
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    conditions = []
    params = []
    if ticker:
        conditions.append(f"ticker={ph}")
        params.append(ticker.upper())
    if addressed is not None:
        conditions.append(f"addressed={ph}")
        params.append(1 if addressed else 0)
    if severity:
        conditions.append(f"severity={ph}")
        params.append(severity)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    c.execute(f"SELECT * FROM triggers {where} ORDER BY "
              f"CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, fired_date DESC",
              params)
    rows = _rows(c)
    conn.close()
    return rows


def save_trigger(data: dict):
    """Insert a new trigger row."""
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    data = dict(data)
    data["ticker"] = data["ticker"].upper()
    data.setdefault("fired_date", now)
    data.setdefault("created_date", now)
    data.setdefault("addressed", 0)

    cols = ", ".join(data.keys())
    placeholders = ", ".join([ph] * len(data))
    if _USE_PG:
        c.execute(f"INSERT INTO triggers ({cols}) VALUES ({placeholders}) RETURNING id",
                  list(data.values()))
        trigger_id = c.fetchone()["id"]
    else:
        c.execute(f"INSERT INTO triggers ({cols}) VALUES ({placeholders})", list(data.values()))
        c.execute("SELECT last_insert_rowid()")
        trigger_id = c.fetchone()[0]

    conn.commit()
    conn.close()
    return trigger_id


def patch_trigger(trigger_id: int, fields: dict):
    """Mark addressed or update resolution notes."""
    if not fields:
        return False
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    safe_keys = [k for k in fields if k != "id"]
    sets = ", ".join(f"{k}={ph}" for k in safe_keys)
    vals = [fields[k] for k in safe_keys] + [trigger_id]
    # Auto-stamp addressed_date when marking addressed
    if fields.get("addressed") and "addressed_date" not in fields:
        sets += f", addressed_date={ph}"
        vals = [fields[k] for k in safe_keys] + [now, trigger_id]
    c.execute(_sql(f"UPDATE triggers SET {sets} WHERE id={ph}"), vals)
    updated = c.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_trigger_counts():
    """Return counts by severity for the nav badge."""
    conn = get_db()
    c = conn.cursor()
    c.execute(_sql(
        "SELECT severity, COUNT(*) as cnt FROM triggers WHERE addressed=? GROUP BY severity"
    ), (0,))
    rows = _rows(c)
    conn.close()
    counts = {"critical": 0, "warning": 0, "info": 0, "total": 0}
    for r in rows:
        s = r["severity"]
        n = r["cnt"]
        counts[s] = n
        counts["total"] += n
    return counts


# ══════════════════════════════════════════════════════════════════
# SPRINT 1 — Research History CRUD (novelty gate, append-only)
# ══════════════════════════════════════════════════════════════════

def get_research_history(ticker=None):
    conn = get_db()
    c = conn.cursor()
    if ticker:
        c.execute(_sql("SELECT * FROM research_history WHERE ticker=? ORDER BY created_date DESC"),
                  (ticker.upper(),))
    else:
        c.execute("SELECT * FROM research_history ORDER BY created_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


def save_research_history(data: dict):
    """Append a new record to research_history."""
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    data = dict(data)
    data["ticker"] = data["ticker"].upper()
    data.setdefault("first_researched_date", now)
    data.setdefault("created_date", now)

    cols = ", ".join(data.keys())
    placeholders = ", ".join([ph] * len(data))
    if _USE_PG:
        c.execute(f"INSERT INTO research_history ({cols}) VALUES ({placeholders}) RETURNING id",
                  list(data.values()))
        rec_id = c.fetchone()["id"]
    else:
        c.execute(f"INSERT INTO research_history ({cols}) VALUES ({placeholders})", list(data.values()))
        c.execute("SELECT last_insert_rowid()")
        rec_id = c.fetchone()[0]

    conn.commit()
    conn.close()
    return rec_id


def get_researched_tickers():
    """All tickers ever researched — used by Task A novelty gate."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT DISTINCT ticker FROM research_history")
    rows = c.fetchall()
    conn.close()
    return [r[0] if isinstance(r, tuple) else r["ticker"] for r in rows]


# ══════════════════════════════════════════════════════════════════
# SPRINT 1 — Valuation Runs CRUD
# ══════════════════════════════════════════════════════════════════

def get_valuation_runs(ticker=None):
    conn = get_db()
    c = conn.cursor()
    if ticker:
        c.execute(_sql("SELECT * FROM valuation_runs WHERE ticker=? ORDER BY run_date DESC"),
                  (ticker.upper(),))
    else:
        c.execute("SELECT * FROM valuation_runs ORDER BY run_date DESC")
    rows = _rows(c)
    conn.close()
    return rows


def save_valuation_run(data: dict):
    """Save one valuation run (manual or scheduled)."""
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if _USE_PG else "?"
    now = datetime.now().strftime("%Y-%m-%d")
    import json
    data = dict(data)
    data["ticker"] = data["ticker"].upper()
    data.setdefault("run_date", now)
    data.setdefault("created_date", now)
    # Serialise dict fields
    for key in ("assumptions", "dcf_output", "peer_comp_output"):
        if isinstance(data.get(key), dict):
            data[key] = json.dumps(data[key])

    cols = ", ".join(data.keys())
    placeholders = ", ".join([ph] * len(data))
    if _USE_PG:
        c.execute(f"INSERT INTO valuation_runs ({cols}) VALUES ({placeholders}) RETURNING id",
                  list(data.values()))
        run_id = c.fetchone()["id"]
    else:
        c.execute(f"INSERT INTO valuation_runs ({cols}) VALUES ({placeholders})", list(data.values()))
        c.execute("SELECT last_insert_rowid()")
        run_id = c.fetchone()[0]

    conn.commit()
    conn.close()
    return run_id
