"""Step 3: the Saturday screen (v2, comprehensive).

Universe : US-listed common stocks, market cap >= $2B, revenue >= $100M,
           20-day average traded value >= $10M (no ADRs, no SPACs).
Funnels  : quant_value, fallen_quality, sharp_drop (event-lite).
           Insider buying, 13D, 13F and theme funnels arrive in step 3b.
Saves    : every scored name with ~35 metrics, gate and test results; market
           context (indices, 10Y, VIX, Buffett indicator); funnel stage counts.
Data     : Nasdaq screener, SEC XBRL frames, Yahoo (yfinance), FRED. All free.

  python tools\\screen.py --limit 100 --dry   quick test, writes nothing
  python tools\\screen.py --dry               full run, writes nothing
  python tools\\screen.py                     full run, writes to the v2 tables
"""
import os, sys, json, time, math, argparse, datetime as dt
from io import StringIO
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf
import psycopg2, psycopg2.extras

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=0, help="only the N largest names (for testing)")
ap.add_argument("--dry", action="store_true", help="print results, write nothing")
args = ap.parse_args()

TODAY = dt.date.today()
YEARS = list(range(TODAY.year - 5, TODAY.year))          # last 5 full calendar years
UA = os.environ.get("SEC_USER_AGENT", "")
if "@" not in UA:
    sys.exit('Set your SEC contact first, e.g.:  set "SEC_USER_AGENT=ValueInvestor you@example.com"')

CACHE = Path(__file__).with_name(".cache")
CACHE.mkdir(exist_ok=True)
SEC = requests.Session()
SEC.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
PROMOTE_SCORE, PROMOTE_FUNNELS = 65, 2


# ---------------------------------------------------------------- helpers
def cached(name, fetch, max_age_days=6):
    f = CACHE / name
    if f.exists() and time.time() - f.stat().st_mtime < max_age_days * 86400:
        return json.loads(f.read_text())
    data = fetch()
    f.write_text(json.dumps(data))
    return data


def sec_get(url):
    time.sleep(0.12)                                   # SEC limit: 10 requests/second
    r = SEC.get(url, timeout=90)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def num(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return float("nan")


def div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def clean(o):
    """Make values JSON-safe (no NaN / numpy types), round floats."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if hasattr(o, "item"):
        o = o.item()
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return round(o, 6)
    return o


# ---------------------------------------------------------------- 0. market context
def market_context():
    out = {"as_of": str(TODAY), "indices": [], "buffett": None}
    idx = [("S&P 500", "^GSPC"), ("Nasdaq 100", "^NDX"), ("Dow Jones", "^DJI"),
           ("Russell 2000", "^RUT"), ("VIX", "^VIX"), ("US 10Y yield", "^TNX")]
    try:
        df = yf.download([s for _, s in idx], period="1y", interval="1d", auto_adjust=True,
                         progress=False, group_by="ticker")
        for label, s in idx:
            c = df[s]["Close"].dropna()
            if len(c) < 10:
                continue
            ytd = c[c.index >= pd.Timestamp(TODAY.year, 1, 1)]
            out["indices"].append({
                "label": label, "symbol": s, "last": float(c.iloc[-1]),
                "chg_1w": float(c.iloc[-1] / c.iloc[-6] - 1),
                "chg_ytd": float(c.iloc[-1] / ytd.iloc[0] - 1) if len(ytd) else None,
                "off_high": float(1 - c.iloc[-1] / c.max()),
                "spark": [round(float(v), 2) for v in c.iloc[::5]]})
    except Exception as e:
        print(f"    [warn] indices: {e.__class__.__name__}")

    # Buffett indicator, Z.1 basis: corporate equities market value / GDP, history since 1970
    try:
        def fred(series):
            r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            d = pd.read_csv(StringIO(r.text))
            d.columns = ["date", "v"]
            d["v"] = pd.to_numeric(d["v"], errors="coerce")
            return d.dropna().assign(date=lambda x: pd.to_datetime(x["date"])).set_index("date")["v"]
        eq, gdp = fred("NCBEILQ027S") / 1000, fred("GDP")          # both $bn
        ratio = (eq / gdp).dropna()
        ratio = ratio[ratio.index >= "1970-01-01"]
        last_q, last = ratio.index[-1], float(ratio.iloc[-1])
        est = last
        spx = next((i for i in out["indices"] if i["symbol"] == "^GSPC"), None)
        if spx:                                                   # roll forward with the S&P since quarter end
            s = yf.download("^GSPC", start=str((last_q + pd.offsets.QuarterEnd(0)).date()),
                            progress=False, auto_adjust=True)["Close"].dropna()
            if len(s) > 1:
                est = last * float(s.iloc[-1].item() if hasattr(s.iloc[-1], "item") else s.iloc[-1]) / float(
                    s.iloc[0].item() if hasattr(s.iloc[0], "item") else s.iloc[0])
        pctile = float((ratio < est).mean())
        zone = ("Undervalued" if pctile < .20 else "Fair value" if pctile < .50 else "Overvalued"
                if pctile < .80 else "Significantly overvalued" if pctile < .95 else "Strongly overvalued")
        out["buffett"] = {"ratio_est": est, "ratio_q": last, "quarter": str(last_q.date()),
                          "percentile": pctile, "zone": zone, "median": float(ratio.median()),
                          "zones_status": "proposed, awaiting approval"}
    except Exception as e:
        print(f"    [warn] Buffett indicator (FRED): {e.__class__.__name__}")
    return out


print("0/5 Market context ...")
MARKET = market_context()

# ---------------------------------------------------------------- 1. universe
print("1/5 Universe from Nasdaq screener ...")
rows = requests.get(
    "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true",
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"},
    timeout=90).json()["data"]["rows"]
STAGES = {"listings": len(rows)}

tick = cached("company_tickers.json", lambda: sec_get("https://www.sec.gov/files/company_tickers.json"))
cik_of = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in tick.values()}

universe = []
for r in rows:
    sym = (r.get("symbol") or "").strip().upper().replace("/", "-")
    if not sym or "^" in sym or " " in sym:
        continue
    if (r.get("country") or "") != "United States":
        continue                                       # excludes foreign ADRs
    if "blank check" in (r.get("industry") or "").lower():
        continue                                       # excludes SPACs
    mcap = num(r.get("marketCap"))
    if not mcap >= 2e9 or sym not in cik_of:
        continue
    universe.append({"ticker": sym, "name": r.get("name"), "sector": r.get("sector") or "Other",
                     "industry": r.get("industry"), "mcap": mcap, "cik": cik_of[sym]})
universe.sort(key=lambda u: -u["mcap"])
if args.limit:
    universe = universe[: args.limit]
STAGES["us_2b"] = len(universe)
print(f"    {len(universe)} US names with market cap >= $2B")

# ---------------------------------------------------------------- 2. fundamentals (SEC frames)
print("2/5 Fundamentals from SEC XBRL frames (first run takes a few minutes) ...")
DUR = {  # first tag that has a value wins
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "ebit": ["OperatingIncomeLoss"],
    "ni": ["NetIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "interest": ["InterestExpense", "InterestExpenseNonoperating"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"],
    "gp": ["GrossProfit"],
    "dil_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "buyback": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
INST = {
    "cur_assets": ["AssetsCurrent"],
    "cur_liab": ["LiabilitiesCurrent"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "debt_total": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "DebtLongtermAndShorttermCombinedAmount"],
    "debt_nc": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligationsNoncurrent"],
    "debt_cur": ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "st_borrow": ["ShortTermBorrowings", "CommercialPaper"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
}


def frame(tag, unit, period):
    data = cached(f"f_{tag}_{unit}_{period}.json",
                  lambda: sec_get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json"))
    return {d["cik"]: d["val"] for d in (data or {}).get("data", [])}


F = {}
for group, suffix in ((DUR, ""), (INST, "Q4I")):
    for key, tags in group.items():
        unit = "shares" if key == "dil_shares" else "USD"
        for y in YEARS:
            merged = {}
            for tag in reversed(tags):
                merged.update(frame(tag, unit, f"CY{y}{suffix}"))
            F[(key, y)] = merged


def fundamentals(cik, mcap, price, finance):
    G0 = lambda k, y: F[(k, y)].get(cik)

    def G(k, y):
        if k != "debt":
            return G0(k, y)
        parts = [G0("debt_total", y)] if G0("debt_total", y) is not None else [G0("debt_nc", y), G0("debt_cur", y)]
        parts = [p for p in parts if p is not None]
        if not parts:
            return None                                # debt not found in the filings
        return sum(parts) + (G0("st_borrow", y) or 0)
    yrs = [y for y in YEARS if G("rev", y)]
    if not yrs or yrs[-1] < YEARS[-2]:
        return None                                    # no recent annual data
    L = yrs[-1]
    rev, ebit, ni = G("rev", L), G("ebit", L), G("ni", L)
    debt_found = G("debt", L) is not None
    eq, debt, cash = G("equity", L), G("debt", L) or 0, G("cash", L) or 0
    if rev < 1e8:
        return None

    def fcf(y):
        cfo = G("cfo", y)
        return None if cfo is None else cfo - (G("capex", y) or 0)

    def ret_on_capital(y):
        if finance:                                    # banks/insurers: return on equity
            n, e = G("ni", y), G("equity", y)
            return n / e if n is not None and e and e > 0 else None
        e, q = G("ebit", y), G("equity", y)
        if e is None or q is None:
            return None
        ic = q + (G("debt", y) or 0) - (G("cash", y) or 0)
        return e * 0.79 / ic if ic > 0 else None

    rocs = [r for r in (ret_on_capital(y) for y in yrs) if r is not None]
    fcfs = [f for f in (fcf(y) for y in yrs[-3:]) if f is not None]
    nis = [G("ni", y) for y in yrs[-3:] if G("ni", y) is not None]
    gms = [G("gp", y) / G("rev", y) for y in yrs if G("gp", y) and G("rev", y)]
    sh = [G("dil_shares", y) for y in yrs if G("dil_shares", y)]
    da = G("da", L)
    ebitda = (ebit or 0) + (da or 0) if ebit is not None else None
    interest = G("interest", L)
    span = L - yrs[0]
    prev = G("rev", L - 1)
    cagr = (rev / G("rev", yrs[0])) ** (1 / span) - 1 if span >= 2 and G("rev", yrs[0]) > 0 else None
    shares = sh[-1] if sh else (mcap / price if price else None)
    eps = div(ni, shares)
    bvps = div(eq, shares)
    nd = debt - cash
    ev = mcap + nd if not finance else None
    f_last = fcf(L)
    g = min(max((cagr or 0) * 100, 2), 40) / 100

    metrics = {
        "fy": L, "revenue": rev, "net_income": ni, "ebit": ebit, "ebitda": ebitda, "fcf": f_last,
        "eps": eps, "bvps": bvps, "shares": shares,
        "net_margin": div(ni, rev), "op_margin": div(ebit, rev), "gross_margin": gms[-1] if gms else None,
        "rev_growth_1y": div(rev, prev) - 1 if prev else None, "rev_cagr": cagr,
        "pe": div(mcap, ni) if ni and ni > 0 else None, "pb": div(mcap, eq) if eq and eq > 0 else None,
        "ps": div(mcap, rev), "ev": ev, "ev_rev": div(ev, rev), "ev_ebit": div(ev, ebit) if ebit and ebit > 0 else None,
        "ev_ebitda": div(ev, ebitda) if ebitda and ebitda > 0 else None,
        "fcf_yield": div(f_last, mcap), "fcf_to_ni": div(sum(fcfs), sum(nis)) if fcfs and nis and sum(nis) > 0 else None,
        "roe": div(ni, eq) if eq and eq > 0 else None, "roc_latest": rocs[-1] if rocs else None,
        "roc_avg": sum(rocs) / len(rocs) if rocs else None, "roc_min": min(rocs) if rocs else None,
        "roc_years": len(rocs), "de": div(debt, eq) if debt_found and eq and eq > 0 else None,
        "net_debt": nd if debt_found else None, "debt_found": debt_found,
        "nd_ebitda": div(nd, ebitda) if debt_found and ebitda and ebitda > 0 else None,
        "interest_cover": div(ebit, interest) if interest else None,
        "current_ratio": div(G("cur_assets", L), G("cur_liab", L)),
        "share_change_5y": (sh[-1] / sh[0] - 1) if len(sh) >= 2 else None,
        "shareholder_yield": div((G("buyback", L) or 0) + (G("dividends", L) or 0), mcap),
        "graham": math.sqrt(22.5 * eps * bvps) if eps and bvps and eps > 0 and bvps > 0 else None,
        "pe_value": eps * 15 if eps and eps > 0 else None,
        "lynch_5y": eps * (1 + g) ** 5 * 15 / 1.1 ** 5 if eps and eps > 0 else None,
    }

    # Hard gates: True pass, False fail, None unknown
    if finance:
        gates = {"equity_positive": eq is not None and eq > 0,
                 "profitable_2_of_3": sum(1 for y in yrs[-3:] if (G("ni", y) or 0) > 0) >= 2}
    else:
        ca, cl = G("cur_assets", L), G("cur_liab", L)
        gates = {
            "current_ratio_ge_1": (ca / cl >= 1.0) if ca and cl else None,
            "net_debt_ebitda_le_3": None if not debt_found else True if nd <= 0 else ((nd / ebitda <= 3) if ebitda and ebitda > 0 else
                                                         (False if ebitda is not None else None)),
            "interest_cover_ge_4": True if not interest else ((ebit / interest >= 4) if ebit is not None else None),
            "fcf_positive_2_of_3": (sum(f > 0 for f in fcfs) >= 2) if len(fcfs) >= 2 else None,
        }
    gate_pass = all(v is not False for v in gates.values()) and sum(v is None for v in gates.values()) <= 1

    def test(ok, have):
        return bool(ok) if have else None

    tests = {
        "pe_lt_20": test(ni and ni > 0 and mcap / ni < 20, ni is not None),
        "pb_lt_3": test(eq and eq > 0 and mcap / eq < 3, eq is not None),
        "roc_avg_gt_12": test(rocs and sum(rocs) / len(rocs) > 0.12, len(rocs) >= 3),
        "rev_cagr_gt_5": test(cagr is not None and cagr > 0.05, cagr is not None),
        "shares_not_rising": test(sh and sh[-1] <= sh[0] * 1.02, len(sh) >= 3),
    }
    if not finance:
        tests.update({
            "ev_ebit_lt_15": test(ebit and ebit > 0 and ev / ebit < 15, ebit is not None),
            "fcf_yield_gt_5": test(f_last is not None and f_last / mcap > 0.05, f_last is not None),
            "gross_margin_stable": test(gms and pd.Series(gms).std() < 0.05, len(gms) >= 3),
            "debt_equity_lt_1": test(eq and eq > 0 and debt / eq < 1, eq is not None and debt_found),
        })
    have = [v for v in tests.values() if v is not None]
    return {**metrics, "gates": gates, "gate_pass": gate_pass, "tests": tests,
            "tests_passed": sum(have), "tests_available": len(have)}


# ---------------------------------------------------------------- 3. prices (Yahoo)
print("3/5 Prices from Yahoo Finance ...")
prices = {}
syms = [u["ticker"] for u in universe]
for i in range(0, len(syms), 200):
    chunk = syms[i:i + 200]
    df = yf.download(chunk, period="1y", interval="1d", group_by="ticker",
                     auto_adjust=True, threads=True, progress=False)
    for t in chunk:
        try:
            sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            sub = sub[["Close", "Volume"]].dropna()
            if len(sub) >= 120:
                prices[t] = sub
        except KeyError:
            pass
    print(f"    prices {min(i + 200, len(syms))}/{len(syms)}")
STAGES["priced"] = len(prices)


def price_stats(s):
    c, v = s["Close"], s["Volume"]
    v50 = v.rolling(50).mean()
    price, high, low = float(c.iloc[-1]), float(c.max()), float(c.min())
    ret = c.pct_change()
    back = lambda n: float(c.iloc[-1] / c.iloc[-n - 1] - 1) if len(c) > n else None
    return {
        "price": price, "high_52w": high, "low_52w": low, "drawdown": 1 - price / high,
        "ma200": float(c.tail(200).mean()), "vs_ma200": price / float(c.tail(200).mean()) - 1,
        "ret_1m": back(21), "ret_3m": back(63), "ret_1y": float(price / c.iloc[0] - 1),
        "dollar_vol_20d": float((c * v).tail(20).mean()),
        "sharp_drop_30d": bool(((ret.tail(30) <= -0.10) & (v.tail(30) >= 2 * v50.tail(30))).any()),
        "capitulation_faded": bool((v / v50).tail(60).max() >= 3 and v.tail(10).mean() <= 1.5 * float(v50.iloc[-1])),
        "bounced_off_low": price >= float(c.tail(20).min()) * 1.05,
        "spark": [round(float(x), 2) for x in c.iloc[::5]],
    }


# ---------------------------------------------------------------- 4. funnels and scores
print("4/5 Funnels and scores ...")
results, liquid = [], 0
for u in universe:
    if u["ticker"] not in prices:
        continue
    ps = price_stats(prices[u["ticker"]])
    if ps["dollar_vol_20d"] < 10e6:
        continue
    liquid += 1
    fin = fundamentals(u["cik"], u["mcap"], ps["price"], (u["sector"] or "") == "Finance")
    if not fin:
        continue
    funnels = {
        "quant_value": fin["gate_pass"] and fin["tests_available"] >= 5
                       and fin["tests_passed"] >= math.ceil(0.55 * fin["tests_available"]),
        "fallen_quality": fin["roc_years"] >= 4 and (fin["roc_min"] or 0) >= 0.15 and ps["drawdown"] >= 0.30,
        "sharp_drop": ps["sharp_drop_30d"],
    }
    flow = (min(ps["drawdown"] / 0.5, 1) * 40
            + (20 if ps["price"] < ps["ma200"] else 5)
            + (25 if ps["capitulation_faded"] else 0)
            + (15 if ps["bounced_off_low"] else 0))
    fund = 100 * fin["tests_passed"] / max(fin["tests_available"], 1)
    fund *= min(1.0, fin["tests_available"] / 7)       # fewer tests = less evidence (fixes bank bias)
    if not fin["gate_pass"]:
        fund *= 0.5
    hits = sum(funnels.values())
    score = min(100, 0.7 * fund + 0.3 * flow + 5 * max(hits - 1, 0))
    results.append({**u, "score": round(score, 1), "flow": round(flow, 1), "fund_score": round(fund, 1),
                    "funnels": funnels, "hits": hits, "fin": fin, "px": ps})

results.sort(key=lambda r: -r["score"])
STAGES.update({"liquid": liquid, "with_fundamentals": len(results),
               "gate_pass": sum(r["fin"]["gate_pass"] for r in results),
               "any_funnel": sum(1 for r in results if r["hits"])})
print(f"    scored {len(results)} names; "
      + ", ".join(f"{k} {sum(r['funnels'][k] for r in results)}"
                  for k in ("quant_value", "fallen_quality", "sharp_drop")))

# ---------------------------------------------------------------- 5. save
conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()
cur.execute("SELECT ticker, level, status FROM v2_names")
known = {r["ticker"]: r for r in cur.fetchall()}

eligible = [r for r in results if r["hits"] and r["fin"]["gate_pass"]
            and known.get(r["ticker"], {}).get("level", "screen") == "screen"
            and known.get(r["ticker"], {}).get("status") != "rejected"]
top10 = eligible[:10]
promote = [r for r in top10 if r["score"] >= PROMOTE_SCORE and r["hits"] >= PROMOTE_FUNNELS]
STAGES.update({"top10": len(top10), "cleared_bar": len(promote)})

print("\nFUNNEL  " + " -> ".join(f"{k} {v}" for k, v in STAGES.items()))
if MARKET.get("buffett"):
    b = MARKET["buffett"]
    print(f"BUFFETT {b['ratio_est']:.0%} of GDP, {b['percentile']:.0%} percentile since 1970: {b['zone']}")
print("\nTOP 10")
print(f"{'Ticker':<8}{'Score':>6}{'Flow':>6}  {'Funnels':<34}{'Price':>9}{'Off high':>9}{'P/E':>7}{'FCF yld':>8}  Tests")
for r in top10:
    fn = ", ".join(k for k, v in r["funnels"].items() if v)
    pe, fy = r["fin"]["pe"], r["fin"]["fcf_yield"]
    print(f"{r['ticker']:<8}{r['score']:>6}{r['flow']:>6}  {fn:<34}{r['px']['price']:>9.2f}"
          f"{r['px']['drawdown']:>8.0%}{(f'{pe:.1f}' if pe else '—'):>7}{(f'{fy:.1%}' if fy else '—'):>8}"
          f"  {r['fin']['tests_passed']}/{r['fin']['tests_available']}")
print(f"\nClear the deep-dive bar (score >= {PROMOTE_SCORE} and {PROMOTE_FUNNELS}+ funnels): "
      f"{', '.join(r['ticker'] for r in promote) or 'none this week'}")

if args.dry:
    print("\nDRY RUN: nothing written.")
    conn.close()
    sys.exit(0)

J = lambda o: psycopg2.extras.Json(clean(o))
w = conn.cursor()
w.execute("""ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS market JSONB;
             ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS stages JSONB;""")
for r in results:
    meta = {"fin": r["fin"], "px": r["px"], "name": r["name"], "sector": r["sector"], "industry": r["industry"],
            "mcap": r["mcap"], "fund_score": r["fund_score"]}
    w.execute("""INSERT INTO v2_screen_scores (run_date, ticker, score, flow_score, funnels, metrics)
                 VALUES (%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (run_date, ticker) DO UPDATE SET score=EXCLUDED.score,
                   flow_score=EXCLUDED.flow_score, funnels=EXCLUDED.funnels, metrics=EXCLUDED.metrics""",
              (TODAY, r["ticker"], r["score"], r["flow"], J(r["funnels"]), J(meta)))
    if r["hits"] or r["ticker"] in known:
        w.execute("""INSERT INTO v2_names (ticker, name, sector, industry, level, status, first_seen,
                                           last_screened, times_screened, score, flow_score, funnels)
                     VALUES (%s,%s,%s,%s,'screen','new',%s,%s,1,%s,%s,%s)
                     ON CONFLICT (ticker) DO UPDATE SET
                       name = COALESCE(v2_names.name, EXCLUDED.name), sector = EXCLUDED.sector,
                       industry = EXCLUDED.industry, last_screened = EXCLUDED.last_screened,
                       times_screened = v2_names.times_screened + 1, score = EXCLUDED.score,
                       flow_score = EXCLUDED.flow_score, funnels = EXCLUDED.funnels, updated_at = now()""",
                  (r["ticker"], r["name"], r["sector"], r["industry"], TODAY, TODAY,
                   r["score"], r["flow"], J(r["funnels"])))
# rejected names: baseline price on first sight, alert after a further 20% fall (anti-portfolio)
w.execute("ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS reject_price NUMERIC")
for r in results:
    k = known.get(r["ticker"])
    if not k or k.get("status") != "rejected":
        continue
    w.execute("UPDATE v2_names SET reject_price = %s WHERE ticker = %s AND reject_price IS NULL", (r["px"]["price"], r["ticker"]))
    w.execute("SELECT reject_price FROM v2_names WHERE ticker = %s", (r["ticker"],))
    base = w.fetchone()["reject_price"]
    if base and r["px"]["price"] <= float(base) * 0.8:
        w.execute("""INSERT INTO v2_alerts (ticker, kind, severity, detail, source, dedupe_key)
                     VALUES (%s, 'Rejected name fell 20%%', 'info', %s, 'screen', %s) ON CONFLICT (dedupe_key) DO NOTHING""",
                  (r["ticker"], f"Now ${r['px']['price']:,.2f}, {r['px']['price'] / float(base) - 1:+.0%} since rejected at "
                   f"${float(base):,.2f}. Score {r['score']:.0f}. Worth a second look?", f"rej20-{r['ticker']}-{TODAY}"))
        w.execute("UPDATE v2_names SET reject_price = %s WHERE ticker = %s", (r["px"]["price"], r["ticker"]))
counts = {k: sum(r["funnels"][k] for r in results) for k in ("quant_value", "fallen_quality", "sharp_drop")}
counts["scored"] = len(results)
w.execute("""INSERT INTO v2_screen_runs (run_date, funnel_counts, top10, market, stages) VALUES (%s,%s,%s,%s,%s)
             ON CONFLICT (run_date) DO UPDATE SET funnel_counts=EXCLUDED.funnel_counts, top10=EXCLUDED.top10,
               market=EXCLUDED.market, stages=EXCLUDED.stages""",
          (TODAY, J(counts), J([{"ticker": r["ticker"], "score": r["score"], "funnels": r["funnels"],
                                 "promote": r in promote} for r in top10]), J(MARKET), J(STAGES)))
conn.commit()
conn.close()
print(f"\nWRITTEN: {len(results)} scores saved; top 10 and market context saved for {TODAY}.")
