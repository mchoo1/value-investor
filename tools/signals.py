"""Event signals for the screen (all free):

  insider_buys()   open-market purchases by insiders (Form 4, code P) from the SEC insider-transactions data sets
                   (quarterly bulk files; the latest two quarters), plus Yahoo's recent insider table for a shortlist
  activists()      new Schedule 13D filings (an investor crossing 5% with intent to influence) from the EDGAR form index
  estimates()      analyst EPS estimate revisions (Yahoo) for a shortlist: next-year estimate now vs 30 and 90 days ago

Every function fails soft: a source that is down returns {} and the screen carries on.
"""
import io, os, re, json, time, zipfile, datetime as dt
from pathlib import Path
import requests
import pandas as pd

UA = os.environ.get("SEC_USER_AGENT", "")
CACHE = Path(__file__).with_name(".cache") / "signals"
CACHE.mkdir(parents=True, exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
TODAY = dt.date.today()


def _get(url, binary=False):
    for i in range(3):
        try:
            r = S.get(url, timeout=180)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r.content if binary else r.text
        if r.status_code in (429, 503):
            time.sleep(3 + 3 * i)
            continue
        return None
    return None


# ------------------------------------------------------------------ insiders
def insider_buys(ciks, days=120, log=print):
    """{cik: {"buyers": [...], "value": $, "ceo_cfo": bool, "last": date, "hit": bool}} for open-market buys."""
    try:
        page = _get("https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets") or ""
        links = re.findall(r'href="(/files/[^"]*form345\.zip)"', page)[:2]
        ciks = set(ciks)
        since = TODAY - dt.timedelta(days=days)
        out = {}
        for link in links:
            p = CACHE / Path(link).name
            if not p.exists():
                raw = _get("https://www.sec.gov" + link, binary=True)
                if not raw:
                    continue
                p.write_bytes(raw)
            with zipfile.ZipFile(p) as z:
                rd = lambda n, cols: pd.read_csv(io.BytesIO(z.read(n)), sep="\t", dtype=str, usecols=cols, low_memory=False)
                sub = rd("SUBMISSION.tsv", ["ACCESSION_NUMBER", "ISSUERCIK"])
                own = rd("REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"])
                tr = rd("NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                                               "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
            tr = tr[(tr["TRANS_CODE"] == "P") & (tr["TRANS_ACQUIRED_DISP_CD"] == "A")].copy()
            tr["date"] = pd.to_datetime(tr["TRANS_DATE"], errors="coerce", format="mixed").dt.date
            tr = tr[tr["date"] >= since]
            tr["value"] = pd.to_numeric(tr["TRANS_SHARES"], errors="coerce") * pd.to_numeric(tr["TRANS_PRICEPERSHARE"], errors="coerce")
            df = tr.merge(sub, on="ACCESSION_NUMBER").merge(own.drop_duplicates("ACCESSION_NUMBER"), on="ACCESSION_NUMBER")
            # directors and officers only (10% holders and funds are a different signal); drop bad prices
            rel = df["RPTOWNER_RELATIONSHIP"].fillna("").str.lower()
            df = df[rel.str.contains("director|officer")]
            df = df[(df["value"] > 0) & (df["value"] < 5e7)]
            df["cik"] = pd.to_numeric(df["ISSUERCIK"], errors="coerce")
            df = df[df["cik"].isin(ciks)]
            for cik, g in df.groupby("cik"):
                r = out.setdefault(int(cik), {"buyers": set(), "value": 0.0, "ceo_cfo": False, "last": None})
                r["buyers"] |= set(g["RPTOWNERNAME"].dropna())
                r["value"] += float(g["value"].fillna(0).sum())
                titles = " ".join(g["RPTOWNER_TITLE"].fillna("").str.upper())
                if re.search(r"\b(CEO|CHIEF EXECUTIVE|CFO|CHIEF FINANCIAL|PRESIDENT|CHAIR)", titles):
                    r["ceo_cfo"] = True
                last = max(g["date"])
                r["last"] = max(r["last"], last) if r["last"] else last
        for r in out.values():
            r["buyers"] = sorted(r["buyers"])[:8]
            r["last"] = str(r["last"]) if r["last"] else None
            # cluster: 2+ insiders and $100k+, or a CEO/CFO/chair buying $250k+, or $1M+ by anyone
            r["hit"] = (len(r["buyers"]) >= 2 and r["value"] >= 1e5) or (r["ceo_cfo"] and r["value"] >= 2.5e5) or r["value"] >= 1e6
        log(f"    insiders: {len(out)} companies with open-market buys in {days} days, {sum(r['hit'] for r in out.values())} clusters")
        return out
    except Exception as e:
        log(f"    [warn] insider data unavailable: {e.__class__.__name__} {str(e)[:80]}")
        return {}


# ------------------------------------------------------------------ activists (13D)
def activists(ciks, days=120, log=print):
    """{cik: {"filers": [...], "last": date, "hit": True}} for new Schedule 13D filings on these companies."""
    try:
        ciks = set(ciks)
        since = TODAY - dt.timedelta(days=days)
        qs = {(TODAY.year, (TODAY.month - 1) // 3 + 1)}
        back = TODAY - dt.timedelta(days=days)
        qs.add((back.year, (back.month - 1) // 3 + 1))
        by_path = {}
        for (y, q) in sorted(qs):
            name = f"formidx_{y}Q{q}.txt"
            p = CACHE / name
            if not p.exists() or (y, q) == (TODAY.year, (TODAY.month - 1) // 3 + 1):
                txt = _get(f"https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/form.idx")
                if txt:
                    p.write_text(txt)
            if not p.exists():
                continue
            for line in p.read_text(errors="ignore").splitlines():
                if not (line.startswith("SCHEDULE 13D ") or line.startswith("SC 13D ")):
                    continue
                m = re.match(r"(SCHEDULE 13D|SC 13D)\s+(.*?)\s{2,}(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(\S+)", line)
                if not m:
                    continue
                _, company, cik, date, path = m.groups()
                if dt.date.fromisoformat(date) < since:
                    continue
                by_path.setdefault(path, []).append((int(cik), company.strip(), date))
        out = {}
        for path, rows in by_path.items():
            subj = [r for r in rows if r[0] in ciks]
            if not subj:
                continue
            filers = [r[1] for r in rows if r[0] not in ciks]
            for cik, _, date in subj:
                o = out.setdefault(cik, {"filers": [], "last": date, "hit": True})
                o["filers"] = sorted(set(o["filers"]) | set(filers))[:6]
                o["last"] = max(o["last"], date)
        log(f"    activists: {len(out)} companies with a new Schedule 13D in {days} days")
        return out
    except Exception as e:
        log(f"    [warn] 13D data unavailable: {e.__class__.__name__}")
        return {}


# ------------------------------------------------------------------ estimate revisions
def estimates(tickers, log=print):
    """{ticker: {"ny_now", "ny_30d", "ny_90d", "chg_30d", "chg_90d", "up_30d", "down_30d", "hit"}} from Yahoo."""
    import yfinance as yf
    out, fails = {}, 0
    for t in tickers:
        p = CACHE / f"est_{t}_{TODAY:%Y%m%d}.json"
        if p.exists():
            out[t] = json.loads(p.read_text())
            continue
        try:
            tk = yf.Ticker(t)
            tr, rv = tk.eps_trend, tk.eps_revisions
            if tr is None or tr.empty or "+1y" not in tr.index:
                fails += 1
                continue
            row = tr.loc["+1y"]
            now, d30, d90 = (float(row.get(k)) if pd.notna(row.get(k)) else None for k in ("current", "30daysAgo", "90daysAgo"))
            up = down = None
            if rv is not None and not rv.empty and "+1y" in rv.index:
                up = int(rv.loc["+1y"].get("upLast30days") or 0)
                down = int(rv.loc["+1y"].get("downLast30days") or rv.loc["+1y"].get("downLast30Days") or 0)
            ch = lambda a, b: (a / b - 1) if a is not None and b and b > 0 else None
            r = {"ny_now": now, "ny_30d": d30, "ny_90d": d90, "chg_30d": ch(now, d30), "chg_90d": ch(now, d90),
                 "up_30d": up, "down_30d": down}
            r["hit"] = (r["chg_90d"] or 0) >= 0.05 and (r["chg_30d"] or 0) >= 0 and (up or 0) >= (down or 0)
            out[t] = r
            p.write_text(json.dumps(r))
            time.sleep(0.4)
        except Exception:
            fails += 1
            if fails >= 8 and not out:
                log("    [warn] estimate revisions unavailable (Yahoo refused); skipped this week")
                return {}
    log(f"    estimates: {len(out)} names checked, {sum(r['hit'] for r in out.values())} with rising forward EPS")
    return out
