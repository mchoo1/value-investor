import os, psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"])
c = conn.cursor()
queries = {
    "thesis by status / stage / conviction":
        "SELECT COALESCE(thesis_status,status,'-'), COALESCE(stage,'-'), COALESCE(conviction_tier,'-'), COUNT(*) "
        "FROM thesis GROUP BY 1,2,3 ORDER BY 4 DESC",
    "shortlist by stage":
        "SELECT COALESCE(stage,'-'), COUNT(*) FROM shortlist GROUP BY 1 ORDER BY 2 DESC",
    "unique tickers across all tables":
        "SELECT COUNT(DISTINCT t) FROM (SELECT ticker t FROM thesis UNION SELECT ticker FROM shortlist "
        "UNION SELECT ticker FROM watchlist UNION SELECT ticker FROM research_history) x",
    "thesis date range":
        "SELECT MIN(created_date), MAX(updated_date) FROM thesis",
}
for title, q in queries.items():
    print(f"\n== {title} ==")
    c.execute(q)
    for row in c.fetchall():
        print(" | ".join(str(v) for v in row))
conn.close()