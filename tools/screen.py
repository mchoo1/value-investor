"""Step 3: the Saturday screen (v2, comprehensive).

Universe : US-listed common stocks, market cap >= $2B, revenue >= $100M,
           20-day average traded value >= $10M (no ADRs, no SPACs).
Value    : quant_value, fallen_quality, sharp_drop ($2B+).
Growth   : fast_growth, accelerating, smart_money (13F), share_taker, momentum_leader ($1B+), scored on moat,
           growth, industry acceleration, smart money and valuation; risks flagged, never excluded.
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
sys.path.insert(0, str(Path(__file__).parent))
from common import theme_of

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
PROMOTE_SCORE, PROMOTE_FUNNELS = 65, 2          # value track deep-dive bar
VALUE_MIN_MCAP, GROWTH_MIN_MCAP = 2e9, 1e9
GROWTH_BAR, GROWTH_MOAT_BAR = 65, 55              # growth track deep-dive bar (score and moat score)
DEEP_DIVES_PER_TRACK = 2


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
    # foreign companies are included when they trade on a US exchange (ADRs, 20-F and 40-F filers)
    if "blank check" in (r.get("industry") or "").lower():
        continue                                       # excludes SPACs
    mcap = num(r.get("marketCap"))
    if not mcap >= GROWTH_MIN_MCAP or sym not in cik_of:
        continue
    country = (r.get("country") or "United States").strip() or "United States"
    universe.append({"ticker": sym, "name": r.get("name"), "sector": r.get("sector") or "Other",
                     "industry": r.get("industry"), "mcap": mcap, "cik": cik_of[sym],
                     "country": country, "foreign": country != "United States"})
universe.sort(key=lambda u: -u["mcap"])
if args.limit:
    universe = universe[: args.limit]
STAGES["us_1b"] = len(universe)
STAGES["us_2b"] = sum(u["mcap"] >= VALUE_MIN_MCAP for u in universe)
STAGES["foreign"] = sum(u["foreign"] for u in universe)
print(f"    {len(universe)} US-listed names >= $1B ({STAGES['foreign']} foreign; {STAGES['us_2b']} >= $2B for the value track)")

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
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
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


# Quarterly revenue (SEC frames CYyyyyQn): latest year-on-year growth and whether it is accelerating
REV_TAGS = DUR["rev"]
QTRS = []
_y, _q = TODAY.year, (TODAY.month - 1) // 3          # last completed quarter
for _ in range(9):
    if _q == 0:
        _y, _q = _y - 1, 4
    QTRS.append((_y, _q))
    _q -= 1
QTRS.reverse()
QF = {}
for (y, q) in QTRS:
    merged = {}
    for tag in reversed(REV_TAGS):
        merged.update(frame(tag, "USD", f"CY{y}Q{q}"))
    QF[(y, q)] = merged


def quarterly_growth(cik):
    """Latest quarter's year-on-year revenue growth, the two before it, and acceleration (latest minus their average)."""
    yoy = []
    for i in range(4, len(QTRS)):
        now, ago = QF[QTRS[i]].get(cik), QF[QTRS[i - 4]].get(cik)
        yoy.append(now / ago - 1 if now and ago and ago > 0 else None)
    have = [(i, g) for i, g in enumerate(yoy) if g is not None]
    if not have:
        return {}
    i_last, last = have[-1]
    prior = [g for i, g in have if i_last - 2 <= i < i_last]
    return {"q_growth": last, "q_label": "CY%dQ%d" % QTRS[i_last + 4],
            "q_growth_prior": sum(prior) / len(prior) if prior else None,
            "q_accel": (last - sum(prior) / len(prior)) if prior else None}


# Trailing 12 months for income-statement items (four latest quarters, US GAAP quarterly frames)
QKEYS = {"ni": DUR["ni"], "ebit": DUR["ebit"], "gp": DUR["gp"]}
QX = {}
for key, tags in QKEYS.items():
    for (y, q) in QTRS:
        merged = {}
        for tag in reversed(tags):
            merged.update(frame(tag, "USD", f"CY{y}Q{q}"))
        QX[(key, y, q)] = merged


def ttm(cik):
    """Sum of the latest four quarters (needs revenue and net income in all four), plus the prior four for growth."""
    for end in range(len(QTRS) - 1, 6, -1):
        last4 = QTRS[end - 3:end + 1]
        revs = [QF[k].get(cik) for k in last4]
        nis = [QX[("ni",) + k].get(cik) for k in last4]
        if all(v is not None for v in revs + nis):
            prev4 = QTRS[end - 7:end - 3]
            prevs = [QF[k].get(cik) for k in prev4]
            ebits = [QX[("ebit",) + k].get(cik) for k in last4]
            gps = [QX[("gp",) + k].get(cik) for k in last4]
            return {"rev": sum(revs), "ni": sum(nis), "end": "CY%dQ%d" % QTRS[end], "end_year": QTRS[end][0],
                    "ebit": sum(ebits) if all(e is not None for e in ebits) else None,
                    "gp": sum(gps) if all(g is not None for g in gps) else None,
                    "rev_prev": sum(prevs) if len(prev4) == 4 and all(v is not None for v in prevs) else None}
    return None


# Foreign filers (20-F IFRS, or 40-F/10-K not picked up by the frames): company facts, converted to USD
IFRS = {
    "rev": ["Revenue", "RevenueFromContractsWithCustomers"], "ebit": ["ProfitLossFromOperatingActivities"],
    "ni": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "cfo": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfPropertyPlantAndEquipment"],
    "interest": ["InterestExpense", "FinanceCosts"], "da": ["DepreciationAndAmortisationExpense",
                                                          "AdjustmentsForDepreciationAndAmortisationExpense"],
    "gp": ["GrossProfit"], "dil_shares": ["WeightedAverageNumberOfSharesOutstandingDiluted",
                                          "AdjustedWeightedAverageShares"],
    "buyback": ["PaymentsToAcquireOrRedeemEntitysShares"],
    "dividends": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"],
    "sbc": ["AdjustmentsForSharebasedPayments", "ExpenseFromSharebasedPaymentTransactionsWithEmployees"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    "cur_assets": ["CurrentAssets"], "cur_liab": ["CurrentLiabilities"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "debt_nc": ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings", "NoncurrentBorrowings"],
    "debt_cur": ["CurrentPortionOfNoncurrentBorrowings", "CurrentBorrowings", "ShorttermBorrowings"],
    "cash": ["CashAndCashEquivalents"],
}
FX = {}


def fx_usd(cur, year):
    """Average USD value of one unit of `cur` in `year` (Yahoo FX), cached."""
    if cur == "USD":
        return 1.0
    k = (cur, year)
    if k not in FX:
        try:
            pair = f"{cur}USD=X"
            c = yf.download(pair, start=f"{year}-01-01", end=f"{year}-12-31", progress=False, auto_adjust=True)["Close"]
            v = float(c.mean().iloc[0] if hasattr(c.mean(), "iloc") else c.mean())
            FX[k] = v if v == v and v > 0 else None
        except Exception:
            FX[k] = None
    return FX[k]


def ingest_foreign(cik):
    """Fill F[(key, year)][cik] from SEC company facts (ifrs-full, else us-gaap), annual 20-F/40-F/10-K values."""
    facts = cached(f"cf_{cik}.json", lambda: sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
    if not facts:
        return False
    tax = facts.get("facts", {})
    got = False
    for gaap, mapping in (("ifrs-full", IFRS), ("us-gaap", {**DUR, **INST})):
        if gaap not in tax:
            continue
        for key, tags in mapping.items():
            for tag in tags:
                units = (tax[gaap].get(tag) or {}).get("units") or {}
                done = False
                for unit, rows in units.items():
                    cur = "USD" if unit == "shares" else unit.split("/")[0]
                    for r in rows:
                        if r.get("form") not in ("20-F", "40-F", "10-K") or r.get("fp") not in ("FY", None):
                            continue
                        end = r.get("end", "")
                        if not end:
                            continue
                        y = int(end[:4]) - (1 if int(end[5:7]) <= 3 else 0)
                        if y not in YEARS or (key, y) not in F:
                            continue
                        if "start" in r and r["start"]:          # duration items: keep ~1-year periods only
                            days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(r["start"])).days
                            if not 330 <= days <= 400:
                                continue
                        rate = 1.0 if unit == "shares" else fx_usd(cur, y)
                        if rate is None:
                            continue
                        F[(key, y)].setdefault(cik, r["val"] * rate)
                        done = got = True
                if done:
                    break
        if got:
            break
    return got


def fundamentals(cik, mcap, price, finance, foreign=False):
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
    tt = ttm(cik) if not foreign else None
    basis = f"FY{L}"
    if tt and tt["end_year"] >= L:                     # fresher: trailing 12 months from the latest four quarters
        rev, ni = tt["rev"], tt["ni"]
        ebit = tt["ebit"] if tt["ebit"] is not None else ebit
        basis = f"TTM to {tt['end']}"
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
    shares = (mcap / price if price else None) if foreign else (sh[-1] if sh else (mcap / price if price else None))   # ADRs: per ADR
    eps = div(ni, shares)
    bvps = div(eq, shares)
    nd = debt - cash
    ev = mcap + nd if not finance else None
    f_last = fcf(L)
    g = min(max((cagr or 0) * 100, 2), 40) / 100

    metrics = {
        "fy": L, "revenue": rev, "net_income": ni, "ebit": ebit, "ebitda": ebitda, "fcf": f_last,
        "eps": eps, "bvps": bvps, "shares": shares,
        "net_margin": div(ni, rev), "op_margin": div(ebit, rev),
        "gross_margin": (tt["gp"] / tt["rev"]) if basis.startswith("TTM") and tt.get("gp") else (gms[-1] if gms else None),
        "rev_growth_1y": ((tt["rev"] / tt["rev_prev"] - 1) if basis.startswith("TTM") and tt.get("rev_prev")
                          else (div(G("rev", L), prev) - 1 if prev else None)), "rev_cagr": cagr, "basis": basis,
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
        "rev_growth_prev": (prev / G("rev", L - 2) - 1) if prev and G("rev", L - 2) else None,
        "gm_change_3y": (gms[-1] - gms[-4]) if len(gms) >= 4 else ((gms[-1] - gms[0]) if len(gms) >= 2 else None),
        "op_margin_prev2": div(G("ebit", L - 2), G("rev", L - 2)),
        "sbc_pct": div(G("sbc", L), rev), "rnd_pct": div(G("rnd", L), rev),
        "fcf_margin": div(f_last, rev), "cash": cash,
        "share_cagr": (sh[-1] / sh[-2] - 1) if len(sh) >= 2 and sh[-2] > 0 else None,   # last year only (IPO years distort longer spans)
        **quarterly_growth(cik),
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
from common import THEMES
ETFS = sorted({e for v in THEMES.values() for e in v} | {"SPY"})
syms = [u["ticker"] for u in universe] + ETFS
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
        "ret_1m": back(21), "ret_3m": back(63), "ret_6m": back(126), "ret_1y": float(price / c.iloc[0] - 1),
        "dollar_vol_20d": float((c * v).tail(20).mean()),
        "sharp_drop_30d": bool(((ret.tail(30) <= -0.10) & (v.tail(30) >= 2 * v50.tail(30))).any()),
        "capitulation_faded": bool((v / v50).tail(60).max() >= 3 and v.tail(10).mean() <= 1.5 * float(v50.iloc[-1])),
        "bounced_off_low": price >= float(c.tail(20).min()) * 1.05,
        "spark": [round(float(x), 2) for x in c.iloc[::5]],
    }


# ---------------------------------------------------------------- 4. two tracks: value and growth
# VALUE track ($2B+): cheap on today's numbers (quant_value, fallen_quality, sharp_drop), scored on value tests + flow.
# GROWTH track ($1B+): open entry - any one of five growth funnels gets a name in, with no price or profit filter.
#   Scored on moat 30, growth and acceleration 25, industry acceleration 25, smart money 10, valuation 10,
#   +5 when a strong moat sits in an accelerating industry. Risks are flagged, never used to exclude.
print("4/5 Funnels and scores (value and growth tracks) ...")
VALUE_FUNNELS = ("quant_value", "fallen_quality", "sharp_drop", "insider_buying", "activist", "newsletter_buzz")
GROWTH_FUNNELS = ("fast_growth", "accelerating", "smart_money", "share_taker", "momentum_leader", "estimates_rising",
                  "newsletter_buzz")
ALL_FUNNELS = tuple(dict.fromkeys(VALUE_FUNNELS + GROWTH_FUNNELS))
W = {"moat": 0.30, "growth": 0.25, "industry": 0.25, "smart": 0.10, "valuation": 0.10}
clip = lambda x, lo=0.0, hi=1.0: max(lo, min(hi, x))


def pick(*xs):
    for x in xs:
        if x is not None:
            return x
    return None


# smart money (13F): growth specialists and big institutions, last quarter vs the one before
try:
    from smartmoney import signals as sm_signals
    SM = sm_signals({u["ticker"] for u in universe}, log=print)
    print(f"    smart money: {len(SM)} names held or traded by tracked managers, {sum(r['hit'] for r in SM.values())} funnel hits")
except Exception as e:
    print(f"    [warn] smart money unavailable: {e.__class__.__name__}")
    SM = {}

# insider cluster buys (Form 4) and new activist stakes (Schedule 13D)
from signals import insider_buys, activists, estimates
INS = insider_buys({u["cik"] for u in universe}, log=print)
ACT = activists({u["cik"] for u in universe}, log=print)

spy = prices.get("SPY")
SPY6 = price_stats(spy)["ret_6m"] if spy is not None else None
THEME_RS = {}
for theme, etfs in THEMES.items():
    rs = [price_stats(prices[e])["ret_6m"] - SPY6 for e in etfs if e in prices and SPY6 is not None
          and price_stats(prices[e])["ret_6m"] is not None]
    THEME_RS[theme] = sum(rs) / len(rs) if rs else None
print("    theme strength vs S&P (6m): " + ", ".join(f"{k.split()[0]} {v:+.0%}" for k, v in THEME_RS.items() if v is not None))

# first pass: fundamentals and prices for everyone, so industry growth can be measured across peers
base, liquid = [], 0
for u in universe:
    if u["ticker"] not in prices:
        continue
    ps = price_stats(prices[u["ticker"]])
    if ps["dollar_vol_20d"] < 5e6:
        continue
    liquid += 1
    finance = (u["sector"] or "") == "Finance"
    fin = fundamentals(u["cik"], u["mcap"], ps["price"], finance, u["foreign"])
    if fin is None and u["foreign"] and ingest_foreign(u["cik"]):      # 20-F / IFRS filers: company facts in USD
        fin = fundamentals(u["cik"], u["mcap"], ps["price"], finance, True)
    if fin:
        fin["foreign"], fin["country"] = u["foreign"], u["country"]
    if fin:
        base.append((u, ps, fin))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


IND = {}
for key in ("industry", "sector"):
    groups = {}
    for u, ps, fin in base:
        groups.setdefault(u[key], []).append(fin)
    for k, fins in groups.items():
        if len(fins) >= 3:
            g_now = median([clip(f["rev_growth_1y"], -0.5, 1.5) if f["rev_growth_1y"] is not None else None for f in fins])
            g_prev = median([clip(f["rev_growth_prev"], -0.5, 1.5) if f["rev_growth_prev"] is not None else None for f in fins])
            IND[(key, k)] = {"g": g_now, "accel": (g_now - g_prev) if g_now is not None and g_prev is not None else None,
                             "n": len(fins)}


def peers(u):
    return IND.get(("industry", u["industry"])) or IND.get(("sector", u["sector"])) or {}


def growth_view(u, ps, fin):
    m = fin
    g_now = pick(m.get("q_growth"), m.get("rev_growth_1y"))
    accel = pick(m.get("q_accel"), (m["rev_growth_1y"] - m["rev_growth_prev"]) if m.get("rev_growth_1y") is not None
                 and m.get("rev_growth_prev") is not None else None)
    cagr = m.get("rev_cagr")
    themes = theme_of(u["industry"])
    pr = peers(u)
    rs6 = (ps["ret_6m"] - SPY6) if ps.get("ret_6m") is not None and SPY6 is not None else None
    sm = SM.get(u["ticker"]) or {}
    funnels = {
        "fast_growth": (g_now is not None and g_now >= 0.20) or (cagr is not None and cagr >= 0.15),
        "accelerating": g_now is not None and accel is not None and g_now >= 0.10 and accel >= 0.05,
        "smart_money": bool(sm.get("hit")),
        "share_taker": bool(themes) and g_now is not None and pr.get("g") is not None and g_now >= 0.10
                       and g_now - pr["g"] >= 0.10,
        "momentum_leader": rs6 is not None and rs6 >= 0.20 and ps["drawdown"] <= 0.15 and (g_now or 0) >= 0.10,
    }
    # --- moat (evidence of pricing power, returns, operating leverage, share gains, discipline)
    gm, roc, om, om2 = m.get("gross_margin"), m.get("roc_latest"), m.get("op_margin"), m.get("op_margin_prev2")
    moat = 0.0
    moat += 30 * clip(((gm or 0) - 0.30) / 0.40) if gm is not None else 10      # 30% -> 0, 70%+ -> full
    gmc = m.get("gm_change_3y")
    moat += 10 if gmc is not None and gmc >= 0.03 else 7 if gmc is not None and gmc >= -0.02 else 0 if gmc is not None else 4
    if roc is not None and roc > 0:
        moat += 20 * clip(roc / 0.25)
    elif (gm or 0) >= 0.60 and (g_now or 0) >= 0.25:
        moat += 8                                                                # reinvesting heavily: partial credit
    if om is not None and om2 is not None:
        moat += 15 * clip((om - om2) / 0.08 + 0.3)                               # widening margins = operating leverage
    if g_now is not None and pr.get("g") is not None:
        moat += 15 * clip((g_now - pr["g"]) / 0.20)                              # out-growing peers = taking share
    sc = m.get("share_cagr")
    moat += 10 if sc is not None and sc <= 0.02 else 5 if sc is not None and sc <= 0.05 else 4 if sc is not None and sc > 1.0 else 0 if sc is not None else 4
    # --- growth and acceleration
    growth = 50 * clip((g_now or 0) / 0.40) + 30 * clip(((accel or 0) + 0.10) / 0.15) * (1 if accel is not None else 0.5) \
        + 20 * clip((cagr or 0) / 0.25)
    # --- industry acceleration
    ind = 40 * clip((pr.get("g") or 0) / 0.20) + (30 * clip(((pr.get("accel") or 0) + 0.05) / 0.10) if pr.get("accel") is not None else 10)
    trs = [THEME_RS[t] for t in themes if THEME_RS.get(t) is not None]
    ind += 10 if themes else 0
    ind += 20 * clip(((max(trs) if trs else 0) + 0.10) / 0.20) if trs else 0
    # --- smart money
    buys = len(sm.get("new", [])) + len(sm.get("added", []))
    sells = len(sm.get("trimmed", [])) + len(sm.get("sold", []))
    ic = sm.get("inst_change")
    smart = 50 * clip(buys / 3) + 30 * clip(len(sm.get("holders", [])) / 6) + (20 * clip((ic + 0.05) / 0.20) if ic is not None and ic < 1 else 0)
    smart = max(0, smart - 15 * sells)
    ins = INS.get(u["cik"]) or {}
    if ins.get("hit"):
        smart = min(100, smart + 25)                                             # insiders buying with their own money
    # --- valuation sanity: price per point of growth (never a filter)
    gp = max(g_now or 0, cagr or 0) * 100
    pe, evs = m.get("pe"), pick(m.get("ev_rev"), m.get("ps"))
    if gp <= 0:
        val = 0
    elif pe:
        val = 100 * clip((3 - pe / gp) / 2)                                     # PEG 1 -> 100, 3 -> 0
    elif evs:
        val = 100 * clip((1.0 - evs / gp) / 0.8)                                # EV/sales per growth point 0.2 -> 100, 1.0 -> 0
    else:
        val = 30
    score = W["moat"] * moat + W["growth"] * growth + W["industry"] * ind + W["smart"] * smart + W["valuation"] * val
    if moat >= 70 and ind >= 70:
        score += 5                                                               # great moat in an accelerating industry
    # --- risks: shown, never used to exclude
    flags = []
    if sc is not None and sc > 1.0:
        flags.append("Recent IPO or big share issue (share count more than doubled)")
    elif sc is not None and sc > 0.05:
        flags.append(f"Dilution {sc:.0%} last year")
    if m.get("fcf") is not None and m["fcf"] < 0:
        runway = (m.get("cash") or 0) / -m["fcf"]
        flags.append(f"Burning cash (runway ~{runway:.1f} years)" if runway < 3 else "Free cash flow negative")
    if m.get("nd_ebitda") is not None and m["nd_ebitda"] > 3:
        flags.append(f"Net debt {m['nd_ebitda']:.1f}x EBITDA")
    if (m.get("sbc_pct") or 0) > 0.15:
        flags.append(f"Stock pay {m['sbc_pct']:.0%} of revenue")
    if evs and evs > 20:
        flags.append(f"Priced for perfection ({evs:.0f}x sales)")
    if gmc is not None and gmc <= -0.03:
        flags.append(f"Gross margin down {abs(gmc) * 100:.0f} pts in 3 years")
    if accel is not None and accel <= -0.10:
        flags.append(f"Growth slowing ({accel * 100:+.0f} pts)")
    if ps["drawdown"] >= 0.50:
        flags.append(f"{ps['drawdown']:.0%} below its 1-year high")
    if sells >= 2:
        flags.append(f"{sells} growth funds cut or sold")
    if u["mcap"] < VALUE_MIN_MCAP:
        flags.append("Small cap (under $2B)")
    if (m.get("interest_cover") or 99) < 3 and m.get("interest_cover") is not None:
        flags.append(f"Interest cover {m['interest_cover']:.1f}x")
    return {"funnels": funnels, "hits": sum(funnels.values()), "score": round(min(score, 100), 1),
            "parts": {"moat": round(moat, 1), "growth": round(growth, 1), "industry": round(ind, 1),
                      "smart": round(smart, 1), "valuation": round(val, 1)},
            "g_now": g_now, "accel": accel, "cagr": cagr, "rs_6m": rs6, "themes": themes,
            "peer_growth": pr.get("g"), "peer_accel": pr.get("accel"),
            "rule_of_40": ((g_now or 0) + (m.get("fcf_margin") or 0)) if g_now is not None else None,
            "smart_money": {k: sm.get(k) for k in ("holders", "new", "added", "trimmed", "sold", "inst_change", "inst_names", "period")} if sm else None,
            "insiders": ins or None, "activist": ACT.get(u["cik"]),
            "flags": flags}


results = []
for u, ps, fin in base:
    gv = growth_view(u, ps, fin)
    in_value = u["mcap"] >= VALUE_MIN_MCAP and ps["dollar_vol_20d"] >= 10e6
    funnels = {
        "quant_value": in_value and fin["gate_pass"] and fin["tests_available"] >= 5
                       and fin["tests_passed"] >= math.ceil(0.55 * fin["tests_available"]),
        "fallen_quality": in_value and fin["roc_years"] >= 4 and (fin["roc_min"] or 0) >= 0.15 and ps["drawdown"] >= 0.30,
        "sharp_drop": in_value and ps["sharp_drop_30d"],
        "insider_buying": in_value and bool((INS.get(u["cik"]) or {}).get("hit")),
        "activist": in_value and bool(ACT.get(u["cik"])),
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
                    "funnels": {**funnels, **gv["funnels"]}, "hits": hits, "growth": gv,
                    "g_score": gv["score"], "g_hits": gv["hits"], "fin": fin, "px": ps})

# analyst estimate revisions for a shortlist (Yahoo, one call per name): best 60 on each track
short = {r["ticker"] for r in sorted(results, key=lambda r: -r["score"])[:60]} | \
        {r["ticker"] for r in sorted(results, key=lambda r: -r["g_score"])[:60]}
EST = estimates(sorted(short), log=print)
for r in results:
    e = EST.get(r["ticker"])
    r["funnels"]["estimates_rising"] = bool(e and e.get("hit"))
    r["growth"]["estimates"] = e
    if r["funnels"]["estimates_rising"]:
        r["growth"]["funnels"]["estimates_rising"] = True
        r["g_hits"] += 1
        r["growth"]["parts"]["growth"] = min(100, r["growth"]["parts"]["growth"] + 10)
        r["g_score"] = round(min(100, r["g_score"] + W["growth"] * 10), 1)
        r["growth"]["score"] = r["g_score"]
        if r["hits"]:
            r["score"] = round(min(100, r["score"] + 3), 1)                     # value names: estimates turning up

# newsletter buzz (Claude's tags of the investing newsletters): 3+ different newsletters positive in 21 days,
# more positive than negative = a lead (it still has to pass the quality and valuation tests to rank).
# 6+ newsletters in 14 days, or one flagged as hype = "crowded trade" caution flag (never excludes).
NL = {}
try:
    _c = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
    _k = _c.cursor()
    _k.execute("""SELECT ticker,
                    COUNT(DISTINCT source) FILTER (WHERE stance = 'positive') AS pos,
                    COUNT(DISTINCT source) FILTER (WHERE stance = 'negative') AS neg,
                    COUNT(DISTINCT source) FILTER (WHERE date >= CURRENT_DATE - 14) AS n14,
                    BOOL_OR(hype) AS hype,
                    (ARRAY_AGG(source || ': ' || point ORDER BY date DESC))[1:3] AS last
                  FROM v2_newsletter_mentions WHERE kind = 'stock' AND ticker IS NOT NULL AND date >= CURRENT_DATE - 21
                  GROUP BY ticker""")
    NL = {r["ticker"]: dict(r) for r in _k.fetchall()}
    _c.close()
except Exception as e:
    print(f"    [warn] newsletter mentions unavailable: {e.__class__.__name__}")
for r in results:
    nl = NL.get(r["ticker"])
    hit = bool(nl and nl["pos"] >= 3 and nl["pos"] > nl["neg"])
    r["funnels"]["newsletter_buzz"] = hit
    r["growth"]["funnels"]["newsletter_buzz"] = hit
    r["growth"]["newsletters"] = nl
    if hit:
        r["g_hits"] += 1
        if r["mcap"] >= VALUE_MIN_MCAP:
            r["hits"] += 1
    if nl and (nl["n14"] >= 6 or nl["hype"]):
        r["growth"]["flags"].append(f"Crowded trade: {nl['n14']} newsletters in 2 weeks" + (" (hype)" if nl["hype"] else ""))
print(f"    newsletters: {len(NL)} names mentioned in 21 days, "
      f"{sum(r['funnels']['newsletter_buzz'] for r in results)} buzz hits")

results.sort(key=lambda r: -r["score"])
STAGES.update({"liquid": liquid, "with_fundamentals": len(results),
               "gate_pass": sum(r["fin"]["gate_pass"] for r in results),
               "any_funnel": sum(1 for r in results if r["hits"]),
               "growth_any_funnel": sum(1 for r in results if r["g_hits"])})
print(f"    scored {len(results)} names; value: "
      + ", ".join(f"{k} {sum(r['funnels'][k] for r in results)}" for k in VALUE_FUNNELS)
      + "; growth: " + ", ".join(f"{k} {sum(r['funnels'][k] for r in results)}" for k in GROWTH_FUNNELS))

# ---------------------------------------------------------------- 5. save
conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()
cur.execute("SELECT ticker, level, status FROM v2_names")
known = {r["ticker"]: r for r in cur.fetchall()}

def open_name(r):
    k = known.get(r["ticker"], {})
    return k.get("level", "screen") == "screen" and k.get("status") != "rejected"


eligible = [r for r in results if r["hits"] and r["fin"]["gate_pass"] and open_name(r)]
top10 = eligible[:10]
promote = [r for r in top10 if r["score"] >= PROMOTE_SCORE and r["hits"] >= PROMOTE_FUNNELS][:DEEP_DIVES_PER_TRACK]
g_eligible = sorted([r for r in results if r["g_hits"] and open_name(r)], key=lambda r: -r["g_score"])
g_top10 = g_eligible[:10]
g_promote = [r for r in g_top10 if r["g_score"] >= GROWTH_BAR
             and r["growth"]["parts"]["moat"] >= GROWTH_MOAT_BAR][:DEEP_DIVES_PER_TRACK]
STAGES.update({"top10": len(top10), "cleared_bar": len(promote),
               "growth_top10": len(g_top10), "growth_cleared_bar": len(g_promote)})

print("\nFUNNEL  " + " -> ".join(f"{k} {v}" for k, v in STAGES.items()))
if MARKET.get("buffett"):
    b = MARKET["buffett"]
    print(f"BUFFETT {b['ratio_est']:.0%} of GDP, {b['percentile']:.0%} percentile since 1970: {b['zone']}")
print("\nVALUE TOP 10")
print(f"{'Ticker':<8}{'Score':>6}{'Flow':>6}  {'Funnels':<34}{'Price':>9}{'Off high':>9}{'P/E':>7}{'FCF yld':>8}  Tests")
for r in top10:
    fn = ", ".join(k for k in VALUE_FUNNELS if r["funnels"][k])
    pe, fy = r["fin"]["pe"], r["fin"]["fcf_yield"]
    print(f"{r['ticker']:<8}{r['score']:>6}{r['flow']:>6}  {fn:<34}{r['px']['price']:>9.2f}"
          f"{r['px']['drawdown']:>8.0%}{(f'{pe:.1f}' if pe else '—'):>7}{(f'{fy:.1%}' if fy else '—'):>8}"
          f"  {r['fin']['tests_passed']}/{r['fin']['tests_available']}")
print(f"Value deep dives (score >= {PROMOTE_SCORE}, {PROMOTE_FUNNELS}+ funnels, best {DEEP_DIVES_PER_TRACK}): "
      f"{', '.join(r['ticker'] for r in promote) or 'none this week'}")
print("\nGROWTH TOP 10")
print(f"{'Ticker':<8}{'Score':>6} {'Moat':>5}{'Grow':>5}{'Ind':>5}{'Smart':>6}{'Val':>5}  {'Growth':>7}{'Accel':>7}  {'Funnels':<44} Risks")
for r in g_top10:
    g = r["growth"]
    pt = g["parts"]
    fn = ", ".join(k for k in GROWTH_FUNNELS if r["funnels"][k])
    print(f"{r['ticker']:<8}{r['g_score']:>6} {pt['moat']:>5.0f}{pt['growth']:>5.0f}{pt['industry']:>5.0f}{pt['smart']:>6.0f}{pt['valuation']:>5.0f}"
          f"  {(f'{g['g_now']:.0%}' if g['g_now'] is not None else '—'):>7}{(f'{g['accel'] * 100:+.0f}' if g['accel'] is not None else '—'):>7}"
          f"  {fn:<44} {'; '.join(g['flags'][:3])}")
print(f"Growth deep dives (score >= {GROWTH_BAR}, moat >= {GROWTH_MOAT_BAR}, best {DEEP_DIVES_PER_TRACK}): "
      f"{', '.join(r['ticker'] for r in g_promote) or 'none this week'}")

if args.dry:
    print("\nDRY RUN: nothing written.")
    conn.close()
    sys.exit(0)

J = lambda o: psycopg2.extras.Json(clean(o))
w = conn.cursor()
w.execute("""ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS market JSONB;
             ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS stages JSONB;
             ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS growth_top10 JSONB;
             ALTER TABLE v2_screen_scores ADD COLUMN IF NOT EXISTS growth_score NUMERIC;
             ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS growth_score NUMERIC;""")
for r in results:
    meta = {"fin": r["fin"], "px": r["px"], "name": r["name"], "sector": r["sector"], "industry": r["industry"],
            "mcap": r["mcap"], "fund_score": r["fund_score"], "growth": r["growth"]}
    w.execute("""INSERT INTO v2_screen_scores (run_date, ticker, score, flow_score, funnels, metrics, growth_score)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (run_date, ticker) DO UPDATE SET score=EXCLUDED.score, growth_score=EXCLUDED.growth_score,
                   flow_score=EXCLUDED.flow_score, funnels=EXCLUDED.funnels, metrics=EXCLUDED.metrics""",
              (TODAY, r["ticker"], r["score"], r["flow"], J(r["funnels"]), J(meta), r["g_score"]))
    if r["hits"] or r["g_hits"] or r["ticker"] in known:
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
        w.execute("UPDATE v2_names SET growth_score = %s WHERE ticker = %s", (r["g_score"], r["ticker"]))
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
counts = {k: sum(r["funnels"][k] for r in results) for k in ALL_FUNNELS}
counts["scored"] = len(results)
MARKET["theme_strength_6m"] = THEME_RS
w.execute("""INSERT INTO v2_screen_runs (run_date, funnel_counts, top10, growth_top10, market, stages) VALUES (%s,%s,%s,%s,%s,%s)
             ON CONFLICT (run_date) DO UPDATE SET funnel_counts=EXCLUDED.funnel_counts, top10=EXCLUDED.top10,
               growth_top10=EXCLUDED.growth_top10, market=EXCLUDED.market, stages=EXCLUDED.stages""",
          (TODAY, J(counts),
           J([{"ticker": r["ticker"], "score": r["score"], "track": "value",
               "funnels": {k: r["funnels"][k] for k in VALUE_FUNNELS}, "promote": r in promote} for r in top10]),
           J([{"ticker": r["ticker"], "score": r["g_score"], "track": "growth",
               "funnels": {k: r["funnels"][k] for k in GROWTH_FUNNELS}, "parts": r["growth"]["parts"],
               "flags": r["growth"]["flags"], "promote": r in g_promote} for r in g_top10]),
           J(MARKET), J(STAGES)))
conn.commit()
conn.close()
print(f"\nWRITTEN: {len(results)} scores saved; value and growth top 10s and market context saved for {TODAY}.")
