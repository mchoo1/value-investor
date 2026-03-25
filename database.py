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
                bull_target REAL
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
        # Migrations: add new columns if missing
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
        ]:
            if col not in existing_cols:
                c.execute(f"ALTER TABLE thesis ADD COLUMN {col} {col_type}")

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
