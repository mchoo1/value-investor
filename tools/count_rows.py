import os, psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"])
c = conn.cursor()
for t in ["watchlist", "portfolio", "thesis", "weekly_review", "screener_results",
          "research_queue", "shortlist", "triggers", "research_history", "valuation_runs"]:
    try:
        c.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"{t}: {c.fetchone()[0]} rows")
    except Exception as e:
        conn.rollback()
        print(f"{t}: missing ({e.__class__.__name__})")
conn.close()