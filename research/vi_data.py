"""
vi_data.py — free data layer for ValueInvestor research tasks.

Sources (framework.json data_sources priority):
  1. SEC EDGAR  (company_tickers_exchange, XBRL frames, companyfacts)
  2. Nasdaq.com screener (universe, last price, market cap, sector, volume)
  3. Yahoo Finance via yfinance (price cross-check, avg volume, consensus, EPS trend)
  4. FRED (10Y UST)
Every value returned is a dict {value, source, as_of} via fact().
"""
import json, os, time, datetime as dt, urllib.request, csv, io

UA = os.environ.get("SEC_USER_AGENT", "ValueInvestor research mchoo1990@gmail.com")
CACHE = os.environ.get("VI_CACHE", "/tmp/vi_cache")
os.makedirs(CACHE, exist_ok=True)
TODAY = dt.date.today().isoformat()


def fact(value, source, as_of=None, **kw):
    d = {"value": value, "source": source, "as_of": as_of or TODAY}
    d.update(kw)
    return d


def _get(url, headers=None, cache_name=None, max_age_h=12, timeout=60):
    if cache_name:
        p = os.path.join(CACHE, cache_name)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
            return open(p, "rb").read()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    if cache_name:
        open(os.path.join(CACHE, cache_name), "wb").write(data)
    time.sleep(0.12)  # SEC fair-access: <10 req/s
    return data


# ── SEC ──────────────────────────────────────────────────────────────
def sec_tickers():
    """ticker -> {cik, name, exchange}"""
    d = json.loads(_get("https://www.sec.gov/files/company_tickers_exchange.json",
                        cache_name="sec_tickers.json", max_age_h=24))
    out = {}
    for cik, name, ticker, exch in d["data"]:
        if ticker:
            out[ticker.upper()] = {"cik": int(cik), "name": name, "exchange": exch}
    return out


def sec_frame(tag, period, unit="USD", taxonomy="us-gaap"):
    """All companies' value for one concept & calendar period. -> {cik: (val, end, accn)}"""
    url = f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
    raw = _get(url, cache_name=f"frame_{taxonomy}_{tag}_{unit}_{period}.json", max_age_h=72)
    if not raw:
        return {}
    d = json.loads(raw)
    return {int(x["cik"]): (x["val"], x.get("end"), x.get("accn")) for x in d.get("data", [])}


def sec_frames_first(tags, period, unit="USD"):
    """Coalesce several candidate tags (first non-missing wins per CIK)."""
    out = {}
    for t in tags:
        for cik, v in sec_frame(t, period, unit).items():
            out.setdefault(cik, v + (t,))
    return out


def sec_companyfacts(cik):
    raw = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
               cache_name=f"cf_{cik}.json", max_age_h=24)
    return json.loads(raw) if raw else None


def annual_series(cf, tags, unit="USD", instant=False):
    """From companyfacts: {fiscal_year_end: value} for 10-K FY data, first tag with data."""
    if not cf:
        return {}, None
    facts = cf.get("facts", {})
    cand = []
    for tag in tags:
        tax = "dei" if tag.startswith("dei:") else "us-gaap"
        t = tag.split(":")[-1]
        node = facts.get(tax, {}).get(t)
        if not node or unit not in node.get("units", {}):
            continue
        rows = {}
        for x in node["units"][unit]:
            if x.get("form") not in ("10-K", "10-K/A", "20-F", "40-F"):
                continue
            if not instant:
                if not x.get("start"):
                    continue
                days = (dt.date.fromisoformat(x["end"]) - dt.date.fromisoformat(x["start"])).days
                if days < 330 or days > 400:
                    continue
            end = x["end"]
            if end not in rows or x.get("filed", "") > rows[end][1]:
                rows[end] = (x["val"], x.get("filed", ""))
        if rows:
            cand.append(({k: v[0] for k, v in sorted(rows.items())}, tag))
    return _freshest(cand)


def _freshest(cand):
    """Pick the tag whose latest period is most recent (ties -> earlier in list).
    Companies switch XBRL tags over time (e.g. LDOS stopped LongTermDebtNoncurrent in 2015);
    taking the first tag with any data silently returned decade-old values."""
    if not cand:
        return {}, None
    best = max(range(len(cand)), key=lambda i: (max(cand[i][0]), -i))
    return cand[best]


def quarterly_series(cf, tags, unit="USD", instant=False):
    """10-Q/10-K quarterly (≈90d) values or instants, latest filed per end date."""
    if not cf:
        return {}, None
    facts = cf.get("facts", {})
    cand = []
    for tag in tags:
        tax = "dei" if tag.startswith("dei:") else "us-gaap"
        node = facts.get(tax, {}).get(tag.split(":")[-1])
        if not node or unit not in node.get("units", {}):
            continue
        rows = {}
        for x in node["units"][unit]:
            if x.get("form") not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
                continue
            if not instant:
                if not x.get("start"):
                    continue
                days = (dt.date.fromisoformat(x["end"]) - dt.date.fromisoformat(x["start"])).days
                if days < 80 or days > 100:
                    continue
            end = x["end"]
            if end not in rows or x.get("filed", "") > rows[end][1]:
                rows[end] = (x["val"], x.get("filed", ""))
        if rows:
            cand.append(({k: v[0] for k, v in sorted(rows.items())}, tag))
    return _freshest(cand)


# ── Nasdaq universe ─────────────────────────────────────────────────
def nasdaq_universe():
    raw = _get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
               headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
               cache_name="nasdaq_screener.json", max_age_h=12)
    rows = json.loads(raw)["data"]["rows"]

    def num(s):
        try:
            return float(str(s).replace("$", "").replace(",", "").replace("%", ""))
        except Exception:
            return None
    out = []
    for r in rows:
        out.append({"ticker": r["symbol"].strip().upper(), "name": r["name"],
                    "price": num(r["lastsale"]), "market_cap": num(r["marketCap"]),
                    "volume": num(r["volume"]), "country": r.get("country") or "",
                    "sector": r.get("sector") or "", "industry": r.get("industry") or ""})
    return out


# ── Yahoo ───────────────────────────────────────────────────────────
def yahoo_snapshot(ticker):
    import yfinance as yf
    t = yf.Ticker(ticker.replace(".", "-"))
    out = {"ticker": ticker}
    try:
        fi = t.fast_info
        out["price"] = fi["lastPrice"]
        out["market_cap"] = fi["marketCap"]
        out["avg_volume_3m"] = fi.get("threeMonthAverageVolume")
    except Exception as e:
        out["error_fast"] = str(e)[:120]
    try:
        i = t.info
        for k in ["forwardEps", "trailingEps", "targetMeanPrice", "numberOfAnalystOpinions",
                  "beta", "sector", "industry", "sharesOutstanding", "longBusinessSummary",
                  "recommendationKey", "heldPercentInsiders"]:
            out[k] = i.get(k)
    except Exception as e:
        out["error_info"] = str(e)[:120]
    try:
        et = t.eps_trend
        if et is not None and len(et):
            row = "+1y" if "+1y" in et.index else et.index[-1]
            out["eps_trend_+1y"] = {c: (None if et.loc[row, c] != et.loc[row, c] else float(et.loc[row, c]))
                                    for c in et.columns if c != "currency"}
    except Exception as e:
        out["error_trend"] = str(e)[:120]
    return out


# ── FRED ────────────────────────────────────────────────────────────
def fred_latest(series="DGS10"):
    """FRED series (percent -> decimal). Falls back to Yahoo ^TNX for DGS10 if FRED is unreachable."""
    try:
        txt = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}",
                   headers={"User-Agent": "curl/8.0"}, cache_name=f"fred_{series}.csv", max_age_h=12,
                   timeout=30).decode()
        rows = [l.split(",") for l in txt.strip().splitlines()[1:]]
        rows = [(d, v) for d, v in rows if v not in ("", ".")]
        d, v = rows[-1]
        return fact(float(v) / 100, f"FRED {series}", d)
    except Exception as e:
        if series != "DGS10":
            raise
        import yfinance as yf
        h = yf.Ticker("^TNX").history(period="5d")["Close"].dropna()
        return fact(float(h.iloc[-1]) / 100, "Yahoo ^TNX (FRED unreachable)", str(h.index[-1].date()),
                    warning=str(e)[:80])


def yahoo_52w(tickers, chunk=200):
    """Bulk 1-year daily history -> {ticker: {high_52w, close, as_of}} (Yahoo, unadjusted highs)."""
    import yfinance as yf
    out = {}
    tickers = [t.replace(".", "-") for t in tickers]
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            d = yf.download(part, period="1y", interval="1d", auto_adjust=False, progress=False,
                            threads=True, group_by="column")
        except Exception:
            continue
        if d is None or d.empty:
            continue
        hi, cl = d["High"], d["Close"].ffill()
        if not hasattr(hi, "columns"):                      # single ticker -> Series
            hi, cl = hi.to_frame(part[0]), cl.to_frame(part[0])
        last = str(d.index[-1].date())
        for t in part:
            if t in hi.columns:
                h, c = hi[t].max(), cl[t].iloc[-1]
                if h == h and c == c:
                    out[t.replace("-", ".")] = {"high_52w": float(h), "close": float(c), "as_of": last}
    return out
