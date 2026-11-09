"""Event calendar (free sources), stored in v2_calendar.

  Fed (FOMC) decision days          federalreserve.gov
  GDP, PCE inflation, trade         bea.gov release schedule
  CPI and jobs report               bls.gov schedule (often blocks bots; Claude's Sunday task fills gaps)
  Options expiry, quad witching, S&P quarterly rebalance   computed (third Fridays)
  Earnings for Watchlist and Book   Yahoo, with the options-implied move for the next 10 days
  Policy, elections, conferences    added by Claude's Sunday "week ahead" task (inbox type week_ahead)

  python tools\\events.py            refresh the calendar and print the next 21 days
"""
import re, sys, datetime as dt
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).parent))
from common import db, ensure_schema, J, TODAY

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}


def fomc():
    h = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers=UA, timeout=60).text
    out = []
    for y in (TODAY.year, TODAY.year + 1):
        i = h.find(f"{y} FOMC Meetings")
        if i < 0:
            continue
        j = h.find("FOMC Meetings", i + 20)
        seg = h[i:j if j > 0 else i + 20000]
        for mon, days in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([\w/]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', seg, re.S):
            mon = mon.split("/")[-1]
            last = re.findall(r"\d+", days)
            if mon not in MONTHS or not last:
                continue
            d = dt.date(y, MONTHS[mon], int(last[-1]))
            sep = "*" in days
            out.append((d, "14:00", "macro", None, "Fed rate decision (FOMC)" + (" + projections" if sep else ""),
                        {"why": "Rates path; moves long-duration growth names and banks most"}, "federalreserve.gov"))
    return out


def bea():
    h = requests.get("https://www.bea.gov/news/schedule", headers=UA, timeout=60).text
    out = []
    for r in re.findall(r"<tr[^>]*scheduled-releases-type-press[^>]*>(.*?)</tr>", h, re.S):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r)).strip()
        m = re.match(r"(\w+) (\d+) ([\d:]+ [AP]M) N ?ews (.*)", t)
        if not m or m.group(1) not in MONTHS:
            continue
        title = m.group(4)
        if not re.search(r"^GDP \(|Personal Income and Outlays|International Trade in Goods and Services", title):
            continue
        mon = MONTHS[m.group(1)]
        y = TODAY.year + (1 if mon < TODAY.month - 1 else 0)
        nice = ("PCE inflation and spending" if "Personal Income" in title else
                "GDP" if title.startswith("GDP") else "Trade balance") + " · " + title
        out.append((dt.date(y, mon, int(m.group(2))), m.group(3), "macro", None, nice, {"why": "Growth and inflation read"}, "bea.gov"))
    return out


def bls():
    out = []
    for code, label, why in (("cpi", "CPI inflation", "Inflation surprise moves rates and growth valuations"),
                             ("empsit", "Jobs report", "Labour market drives the Fed path")):
        try:
            r = requests.get(f"https://www.bls.gov/schedule/news_release/{code}.htm", headers=UA, timeout=60)
            if r.status_code != 200:
                continue
            for mon, day, year, tm in re.findall(r"(\w{3})\.? (\d{1,2}), (\d{4})</td>\s*<td[^>]*>([\d:]+ [AP]M)", r.text):
                full = next((k for k in MONTHS if k.startswith(mon)), None)
                if full:
                    out.append((dt.date(int(year), MONTHS[full], int(day)), tm, "macro", None, label, {"why": why}, "bls.gov"))
        except requests.RequestException:
            pass
    return out


def third_friday(y, m):
    d = dt.date(y, m, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def structure():
    out = []
    for k in range(0, 4):
        y, m = TODAY.year + (TODAY.month + k - 1) // 12, (TODAY.month + k - 1) % 12 + 1
        f = third_friday(y, m)
        if m in (3, 6, 9, 12):
            out.append((f, "16:00", "market", None, "Quad witching + S&P 500 quarterly rebalance",
                        {"why": "Heavy volume; index adds/deletes move prices around the close"}, "computed"))
        else:
            out.append((f, "16:00", "market", None, "Monthly options expiry", {"why": "Option-driven moves into the close"}, "computed"))
    return out


def earnings(tickers, implied_days=10):
    """Next earnings date per ticker (Yahoo) and, inside `implied_days`, the options-implied move (ATM straddle / price)."""
    import yfinance as yf
    out = []
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            cal = tk.calendar or {}
            ds = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if not ds:
                continue
            d = ds[0] if isinstance(ds, (list, tuple)) else ds
            d = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)[:10])
            if d < TODAY or d > TODAY + dt.timedelta(days=100):
                continue
            detail = {"eps_est": cal.get("Earnings Average"), "rev_est": cal.get("Revenue Average")}
            if (d - TODAY).days <= implied_days:
                exps = [e for e in tk.options if dt.date.fromisoformat(e) >= d]
                if exps:
                    ch = tk.option_chain(exps[0])
                    px = float(tk.fast_info["last_price"])
                    k = min(ch.calls["strike"], key=lambda s: abs(s - px))
                    mid = lambda df: float(((df[df["strike"] == k]["bid"] + df[df["strike"] == k]["ask"]) / 2).iloc[0])
                    straddle = mid(ch.calls) + mid(ch.puts)
                    if straddle > 0:
                        detail["implied_move"] = straddle / px
                        detail["expiry"] = exps[0]
            out.append((d, None, "earnings", t, f"{t} results", detail, "yahoo"))
        except Exception:
            continue
    return out


def refresh(conn, tickers, log=print):
    cur = conn.cursor()
    rows, srcs = [], {}
    for name, fn in (("Fed", fomc), ("BEA", bea), ("BLS", bls), ("market", structure), ("earnings", lambda: earnings(tickers))):
        try:
            got = fn()
            rows += got
            srcs[name] = len(got)
        except Exception as e:
            srcs[name] = f"failed ({e.__class__.__name__})"
    for d, tm, kind, tk, title, detail, src in rows:
        if d < TODAY - dt.timedelta(days=1):
            continue
        if kind == "earnings":                         # one row per name: drop an older date if Yahoo moved it
            cur.execute("DELETE FROM v2_calendar WHERE kind = 'earnings' AND ticker = %s AND date >= %s AND date <> %s",
                        (tk, TODAY, d))
        cur.execute("""INSERT INTO v2_calendar (date, time_et, kind, ticker, title, detail, source) VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (date, kind, title) DO UPDATE SET detail = EXCLUDED.detail, time_et = EXCLUDED.time_et""",
                    (d, tm, kind, tk, title, J(detail), src))
    conn.commit()
    log("Calendar: " + ", ".join(f"{k} {v}" for k, v in srcs.items()))
    return srcs


if __name__ == "__main__":
    conn = db()
    c = conn.cursor()
    ensure_schema(c)
    conn.commit()
    c.execute("SELECT ticker FROM v2_names WHERE level IN ('watchlist','book')")
    refresh(conn, [r["ticker"] for r in c.fetchall()])
    c.execute("SELECT * FROM v2_calendar WHERE date BETWEEN CURRENT_DATE AND CURRENT_DATE + 21 ORDER BY date, time_et")
    for r in c.fetchall():
        im = (r["detail"] or {}).get("implied_move")
        print(f"{r['date']} {r['time_et'] or '':8} {r['kind']:9} {r['title']}" + (f"  (implied ±{im:.1%})" if im else ""))
