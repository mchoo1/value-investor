"""
SQLite database setup and helpers.
All user data (thesis, portfolio, watchlist) is stored locally.
"""
import sqlite3
import json
from datetime import datetime

import os as _os
_BASE = _os.path.dirname(_os.path.abspath(__file__))
# DATA_DIR env var allows Railway / cloud to redirect to a persistent volume
_DATA_DIR = _os.environ.get("DATA_DIR", _os.path.join(_BASE, "data"))
_os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = _os.path.join(_DATA_DIR, "valueinvestor.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # Watchlist
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

    # Portfolio positions
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
            status TEXT DEFAULT 'open',  -- open / closed
            exit_price REAL,
            exit_date TEXT,
            notes TEXT
        )
    """)

    # Investment thesis
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
            status TEXT DEFAULT 'active'  -- active / closed / monitoring
        )
    """)

    # Weekly thesis reviews
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thesis_id INTEGER,
            ticker TEXT,
            review_date TEXT,
            current_price REAL,
            target_price REAL,
            price_change_pct REAL,
            thesis_intact INTEGER DEFAULT 1,  -- 1=yes, 0=no
            revenue_on_track INTEGER DEFAULT 1,
            margin_on_track INTEGER DEFAULT 1,
            new_developments TEXT,
            assumption_changes TEXT,
            action TEXT DEFAULT 'hold',  -- buy/hold/sell/watch
            confidence INTEGER DEFAULT 3,  -- 1-5
            notes TEXT,
            FOREIGN KEY (thesis_id) REFERENCES thesis(id)
        )
    """)

    # Screener saved results
    c.execute("""
        CREATE TABLE IF NOT EXISTS screener_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            market TEXT,
            filters TEXT,
            results TEXT
        )
    """)

    # ── Migrations: add snapshot columns if not present ──────────
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


# ── Watchlist CRUD ──────────────────────────────────────────────
def add_to_watchlist(ticker, name, market="US", notes=""):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (ticker, name, market, added_date, notes) VALUES (?,?,?,?,?)",
            (ticker.upper(), name, market, datetime.now().strftime("%Y-%m-%d"), notes)
        )
        conn.commit()
        return True
    except Exception as e:
        return str(e)
    finally:
        conn.close()


def get_watchlist():
    conn = get_db()
    rows = conn.execute("SELECT * FROM watchlist ORDER BY added_date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_from_watchlist(ticker):
    conn = get_db()
    conn.execute("DELETE FROM watchlist WHERE ticker=?", (ticker.upper(),))
    conn.commit()
    conn.close()


# ── Portfolio CRUD ───────────────────────────────────────────────
def add_position(ticker, name, entry_price, shares, entry_date, target_price, stop_loss, notes=""):
    conn = get_db()
    conn.execute(
        """INSERT INTO portfolio (ticker, name, entry_price, shares, entry_date, target_price, stop_loss, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (ticker.upper(), name, entry_price, shares, entry_date, target_price, stop_loss, notes)
    )
    conn.commit()
    conn.close()


def get_portfolio():
    conn = get_db()
    rows = conn.execute("SELECT * FROM portfolio ORDER BY entry_date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_position(pos_id, **kwargs):
    conn = get_db()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [pos_id]
    conn.execute(f"UPDATE portfolio SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def delete_position(pos_id):
    conn = get_db()
    conn.execute("DELETE FROM portfolio WHERE id=?", (pos_id,))
    conn.commit()
    conn.close()


# ── Thesis CRUD ──────────────────────────────────────────────────
def save_thesis(data: dict):
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d")

    existing = conn.execute("SELECT id FROM thesis WHERE ticker=?", (data["ticker"].upper(),)).fetchone()
    if existing:
        sets = ", ".join(f"{k}=?" for k in data if k != "ticker")
        vals = [data[k] for k in data if k != "ticker"] + [data["ticker"].upper()]
        conn.execute(f"UPDATE thesis SET updated_date=?, {sets} WHERE ticker=?",
                     [now] + vals)
        thesis_id = existing["id"]
    else:
        data["ticker"] = data["ticker"].upper()
        data["created_date"] = now
        data["updated_date"] = now
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        conn.execute(f"INSERT INTO thesis ({cols}) VALUES ({placeholders})", list(data.values()))
        thesis_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.commit()
    conn.close()
    return thesis_id


def get_thesis(ticker=None):
    conn = get_db()
    if ticker:
        rows = conn.execute("SELECT * FROM thesis WHERE ticker=?", (ticker.upper(),)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM thesis ORDER BY updated_date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_thesis(thesis_id):
    conn = get_db()
    conn.execute("DELETE FROM thesis WHERE id=?", (thesis_id,))
    conn.commit()
    conn.close()


# ── Weekly Reviews ───────────────────────────────────────────────
def save_weekly_review(data: dict):
    conn = get_db()
    data["review_date"] = data.get("review_date", datetime.now().strftime("%Y-%m-%d"))
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" * len(data))
    conn.execute(f"INSERT INTO weekly_review ({cols}) VALUES ({placeholders})", list(data.values()))
    conn.commit()
    conn.close()


def get_weekly_reviews(ticker=None, thesis_id=None):
    conn = get_db()
    if ticker:
        rows = conn.execute(
            "SELECT * FROM weekly_review WHERE ticker=? ORDER BY review_date DESC", (ticker.upper(),)
        ).fetchall()
    elif thesis_id:
        rows = conn.execute(
            "SELECT * FROM weekly_review WHERE thesis_id=? ORDER BY review_date DESC", (thesis_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM weekly_review ORDER BY review_date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
