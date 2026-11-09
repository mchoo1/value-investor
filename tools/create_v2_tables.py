import os, psycopg2

SQL = """
CREATE TABLE IF NOT EXISTS v2_names (
  ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, industry TEXT,
  themes TEXT[] DEFAULT '{}', level TEXT DEFAULT 'screen'
    CHECK (level IN ('screen','watchlist','book')),
  status TEXT DEFAULT 'new', is_core BOOLEAN DEFAULT FALSE,
  first_seen DATE, last_screened DATE, times_screened INT DEFAULT 0,
  score NUMERIC, flow_score NUMERIC, funnels JSONB DEFAULT '{}',
  reason_card JSONB, reject_reason TEXT, legacy JSONB,
  updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS v2_screen_runs (
  run_date DATE PRIMARY KEY, funnel_counts JSONB, top10 JSONB,
  created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS v2_screen_scores (
  run_date DATE, ticker TEXT, score NUMERIC, flow_score NUMERIC,
  funnels JSONB, metrics JSONB, PRIMARY KEY (run_date, ticker));
CREATE TABLE IF NOT EXISTS v2_industry_outlooks (
  id SERIAL PRIMARY KEY, theme TEXT, as_of DATE, kind TEXT, content JSONB);
CREATE TABLE IF NOT EXISTS v2_theses (
  id SERIAL PRIMARY KEY, ticker TEXT, version INT, author TEXT DEFAULT 'claude',
  approval TEXT DEFAULT 'pending', sections JSONB, valuation JSONB,
  conviction INT, verdict TEXT, entry_target NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(), UNIQUE (ticker, version));
CREATE TABLE IF NOT EXISTS v2_scenarios (
  id SERIAL PRIMARY KEY, ticker TEXT, name TEXT, inputs JSONB, iv NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS v2_comments (
  id SERIAL PRIMARY KEY, ticker TEXT, section TEXT, body TEXT,
  created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS v2_positions (
  id SERIAL PRIMARY KEY, ticker TEXT, entered DATE, shares NUMERIC,
  cost NUMERIC, exited DATE, notes TEXT);
CREATE TABLE IF NOT EXISTS v2_alerts (
  id SERIAL PRIMARY KEY, ticker TEXT, fired_at TIMESTAMPTZ DEFAULT now(),
  kind TEXT, severity TEXT CHECK (severity IN ('info','medium','high')),
  detail TEXT, source TEXT, action TEXT, dedupe_key TEXT UNIQUE,
  sent BOOLEAN DEFAULT FALSE, addressed BOOLEAN DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS v2_news (
  id SERIAL PRIMARY KEY, ticker TEXT, published DATE, headline TEXT,
  url TEXT UNIQUE, source TEXT, tag TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS v2_events (
  id SERIAL PRIMARY KEY, ticker TEXT, filed DATE, form TEXT, kind TEXT,
  detail TEXT, url TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS v2_prices (
  ticker TEXT, date DATE, close NUMERIC, PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS v2_analyst (
  ticker TEXT, date DATE, mean NUMERIC, high NUMERIC, low NUMERIC, n INT,
  PRIMARY KEY (ticker, date));
"""

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute(SQL)
conn.commit()
cur.execute("SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'v2_%' ORDER BY 1")
names = [r[0] for r in cur.fetchall()]
print(f"OK: {len(names)} v2 tables ->", ", ".join(names))
conn.close()
