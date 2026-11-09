"""Research job: deep data for each name you follow, saved to v2_research.

For every Watchlist and Book name, candidate, requested name and this week's top 10:
  valuation engine (DCF-FCFF, EPV, peer multiples, asset floor; approved weights and
  margin-of-safety rules), 10-year financials, competitors, moat and risk ratings,
  red flags, insiders, ownership, analyst targets and actions, catalysts, news,
  business summary and management.
Data: SEC companyfacts, Yahoo (yfinance), FRED, and the latest screen. All free.

  python tools\\research.py                 the default set (Watchlist, Book, candidates, requests, top 10)
  python tools\\research.py --tickers MSFT,GPI
  python tools\\research.py --dry           print a summary, write nothing
"""
import os, sys, json, time, math, argparse, statistics as st, datetime as dt
from io import StringIO
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf
import psycopg2, psycopg2.extras

ap = argparse.ArgumentParser()
ap.add_argument("--tickers", default="")
ap.add_argument("--dry", action="store_true")
args = ap.parse_args()

TODAY = dt.date.today()
UA = os.environ.get("SEC_USER_AGENT", "")
if "@" not in UA:
    sys.exit('Set your SEC contact first, e.g.:  set "SEC_USER_AGENT=ValueInvestor you@example.com"')
SEC = requests.Session()
SEC.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
CACHE = Path(__file__).with_name(".cache")
CACHE.mkdir(exist_ok=True)

# Approved rules (framework.json, Ming 2026-09-29); ERP is a PROPOSED default
ERP = 0.046
REQUIRED_MOS = {"wide_moat_compounder": 0.25, "average_quality": 0.35,
                "cyclical_turnaround": 0.45, "high_growth_early": 0.50}
WEIGHTS = {"dcf_fcff": .50, "multiples": .30, "epv": .20, "asset_floor": 0}
WEIGHTS_ASSET_HEAVY = {"dcf_fcff": .45, "multiples": .25, "epv": .15, "asset_floor": .15}
ASSET_HEAVY_SECTORS = {"Finance", "Real Estate", "Utilities", "Energy", "Basic Materials"}
CYCLICAL_SECTORS = {"Energy", "Basic Materials", "Industrials", "Consumer Discretionary"}
BAD_NEWS = ("lawsuit", "investigation", "probe", "subpoena", "restat", "fraud", "downgrade", "recall",
            "cuts guidance", "cut guidance", "lowers guidance", "warns", "layoff", "bankrupt", "default",
            "short seller", "delist", "sec charges", "class action", "resigns")

T = {  # SEC tags, first one with a value wins
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gp": ["GrossProfit"], "opinc": ["OperatingIncomeLoss"], "ni": ["NetIncomeLoss"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"], "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "int": ["InterestExpense", "InterestExpenseNonoperating"], "tax": ["IncomeTaxExpenseBenefit"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "buyback": ["PaymentsForRepurchaseOfCommonStock"], "div": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    # balance sheet (instant)
    "eq": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "ltd": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebtNoncurrent"],
    "std": ["LongTermDebtCurrent", "DebtCurrent"], "stb": ["ShortTermBorrowings", "CommercialPaper"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "ca": ["AssetsCurrent"], "cl": ["LiabilitiesCurrent"], "liab": ["Liabilities"], "assets": ["Assets"],
    "gw": ["Goodwill"], "intang": ["IntangibleAssetsNetExcludingGoodwill"],
    "ar": ["AccountsReceivableNetCurrent"], "inv": ["InventoryNet"],
}
INSTANT = {"eq", "ltd", "std", "stb", "cash", "sti", "ca", "cl", "liab", "assets", "gw", "intang", "ar", "inv"}


# ------------------------------------------------------------------ helpers
def sec_get(url, cache=None, max_age_h=72):
    f = CACHE / cache if cache else None
    if f and f.exists() and time.time() - f.stat().st_mtime < max_age_h * 3600:
        return json.loads(f.read_text())
    time.sleep(0.12)
    r = SEC.get(url, timeout=90)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if f:
        f.write_text(json.dumps(data))
    return data


def div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (pd.Timestamp, dt.date, dt.datetime)):
        return str(o)[:10]
    if hasattr(o, "item"):
        try:
            o = o.item()
        except Exception:
            return str(o)
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else round(o, 6)
    return o


def annual(cf, key):
    """{fiscal-year-end year: value} from 10-K filings, first tag with data wins per year."""
    out = {}
    facts = (cf or {}).get("facts", {}).get("us-gaap", {})
    unit = "shares" if key == "shares" else "USD"
    for tag in T[key]:
        best = {}
        for f in facts.get(tag, {}).get("units", {}).get(unit, []):
            if f.get("form") not in ("10-K", "10-K/A") or "end" not in f:
                continue
            end = dt.date.fromisoformat(f["end"])
            if key not in INSTANT:
                if "start" not in f:
                    continue
                days = (end - dt.date.fromisoformat(f["start"])).days
                if not 330 <= days <= 400:
                    continue
            y = end.year
            if y not in best or f.get("filed", "") >= best[y][1]:
                best[y] = (f["val"], f.get("filed", ""))
        for y, (v, _) in best.items():
            out.setdefault(y, v)
    return dict(sorted(out.items()))


def latest_instant(cf, key):
    """Freshest balance-sheet value from 10-Q or 10-K."""
    facts = (cf or {}).get("facts", {}).get("us-gaap", {})
    for tag in T[key]:
        rows = [f for f in facts.get(tag, {}).get("units", {}).get("USD", [])
                if f.get("form") in ("10-Q", "10-K", "10-Q/A", "10-K/A") and "end" in f]
        if rows:
            r = max(rows, key=lambda f: (f["end"], f.get("filed", "")))
            return r["val"], r["end"]
    return None, None


def risk_free():
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        d = pd.read_csv(StringIO(r.text))
        v = pd.to_numeric(d.iloc[:, 1], errors="coerce").dropna()
        return float(v.iloc[-1]) / 100, "FRED DGS10"
    except Exception:
        try:
            c = yf.Ticker("^TNX").history(period="5d")["Close"].dropna()
            return float(c.iloc[-1]) / 100, "Yahoo ^TNX (FRED unavailable)"
        except Exception:
            return 0.043, "fallback 4.3% (FRED and Yahoo unavailable)"


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ------------------------------------------------------------------ valuation engine (approved method)
def dcf_fcff(fcff0, g1, g_term, wacc, years=10, fade_from=5):
    pv, f = 0.0, fcff0
    for t in range(1, years + 1):
        g = g1 if t <= fade_from else g1 + (g_term - g1) * (t - fade_from) / (years - fade_from)
        f *= 1 + g
        pv += f / (1 + wacc) ** t
    tv = f * (1 + g_term) / (wacc - g_term)
    pv_tv = tv / (1 + wacc) ** years
    return pv + pv_tv, pv_tv / (pv + pv_tv)


def valuation(S, info, price, shares, sector, peers, company_type, rf):
    flags, notes, methods = [], [], []
    finance = sector == "Finance"
    beta = info.get("beta")
    beta_used = min(max(beta, 0.6), 2.0) if beta else 1.0
    if not beta:
        flags.append("Beta missing from Yahoo; 1.0 used")
    ke = rf[0] + beta_used * ERP
    ys = list(S["pretax"])[-3:]
    rates = [S["tax"][y] / S["pretax"][y] for y in ys if y in S["tax"] and S["pretax"][y] and S["pretax"][y] > 0]
    t = min(max(st.mean(rates), 0.10), 0.30) if rates else 0.21
    debt, cash = S["debt_now"], S["cash_now"]
    kd = min(max(div(S["last"]("int"), debt) or 0.06, 0.03), 0.10)
    E = (price or 0) * (shares or 0)
    wacc = (E / (E + debt)) * ke + (debt / (E + debt)) * kd * (1 - t) if E + debt else ke
    wacc = max(wacc, rf[0] + 0.02)
    g_hist = S["cagr"]("rev")
    g1 = min(max(g_hist if g_hist is not None else 0.03, 0.0), 0.12)
    if (g_hist or 0) > 0.15:
        flags.append(f"Revenue grew {g_hist:.0%} a year; stage-1 growth capped at 12%")
    g_term = min(0.025, rf[0] - 0.01) if rf[0] > 0.035 else 0.02
    sens = None

    if not finance:
        yrs = list(S["ocf"])[-3:]
        fcffs = [S["ocf"][y] + S["int"].get(y, 0) * (1 - t) - S["capex"].get(y, 0) for y in yrs]
        fcff0 = st.mean(fcffs) if fcffs else None
        if fcff0 and fcff0 > 0 and shares:
            ev, tv_share = dcf_fcff(fcff0, g1, g_term, wacc)
            grid = [[round((dcf_fcff(fcff0, g1, g_term + dg, wacc + dw)[0] - debt + cash) / shares, 2)
                     for dg in (-0.005, 0, 0.005)] for dw in (-0.01, -0.005, 0, 0.005, 0.01)]
            sens = {"wacc": [round(wacc + d, 4) for d in (-0.01, -0.005, 0, 0.005, 0.01)],
                    "g": [round(g_term + d, 4) for d in (-0.005, 0, 0.005)], "grid": grid}
            methods.append({"name": "dcf_fcff", "label": "DCF (free cash flow to firm)",
                            "value": round((ev - debt + cash) / shares, 2),
                            "inputs": {"FCFF (3y avg)": round(fcff0), "Stage-1 growth": round(g1, 4),
                                       "Terminal growth": round(g_term, 4), "WACC": round(wacc, 4),
                                       "Terminal value share": round(tv_share, 3)}})
            if tv_share > 0.8:
                flags.append(f"Terminal value is {tv_share:.0%} of DCF value (above 80%)")
        else:
            notes.append("DCF skipped: 3-year average free cash flow is negative or missing")
        ks = [y for y in list(S["opinc"])[-5:] if S["rev"].get(y)]
        if ks and shares:
            m_norm = st.mean(S["opinc"][y] / S["rev"][y] for y in ks)
            epv_ev = m_norm * S["last"]("rev") * (1 - t) / wacc
            methods.append({"name": "epv", "label": "Earnings power value (no growth)",
                            "value": round((epv_ev - debt + cash) / shares, 2),
                            "inputs": {"Normalised operating margin (5y)": round(m_norm, 4), "Tax rate": round(t, 3)}})
    else:
        nis = [v for v in list(S["ni"].values())[-5:]]
        if nis and shares and st.mean(nis) > 0:
            methods.append({"name": "epv", "label": "Earnings power value (5y avg net income / cost of equity)",
                            "value": round(st.mean(nis) / ke / shares, 2),
                            "inputs": {"5y avg net income": round(st.mean(nis)), "Cost of equity": round(ke, 4)}})
        notes.append("Financial company: DCF on free cash flow not used; value from earnings power, peers and book")

    pe_l = [p["pe"] for p in peers if p.get("pe") and 0 < p["pe"] < 80]
    eve_l = [p["ev_ebitda"] for p in peers if p.get("ev_ebitda") and 0 < p["ev_ebitda"] < 50]
    vals = []
    eps = div(S["last"]("ni"), shares)
    if len(pe_l) >= 3 and eps and eps > 0:
        vals.append(("P/E", st.median(pe_l) * eps))
    ebitda = (S["last"]("opinc") or 0) + (S["last"]("da") or 0)
    if not finance and len(eve_l) >= 3 and ebitda > 0 and shares:
        vals.append(("EV/EBITDA", (st.median(eve_l) * ebitda - debt + cash) / shares))
    if vals:
        methods.append({"name": "multiples", "label": f"Peer multiples (median of {len(peers)} peers)",
                        "value": round(st.mean(v for _, v in vals), 2),
                        "inputs": {**({"Peer median P/E": round(st.median(pe_l), 1)} if pe_l else {}),
                                   **({"Peer median EV/EBITDA": round(st.median(eve_l), 1)} if eve_l else {}),
                                   **{f"Value at {k}": round(v, 2) for k, v in vals}}})
    else:
        notes.append("Peer multiples skipped: fewer than 3 usable peers")

    eq_now = S["eq_now"]
    if shares and eq_now:
        tbv = (eq_now - (S["gw_now"] or 0) - (S["intang_now"] or 0)) / shares
        ncav = ((S["ca_now"] - S["liab_now"]) / shares) if S["ca_now"] and S["liab_now"] else None
        floor = max(v for v in (tbv, ncav) if v is not None)
        methods.append({"name": "asset_floor", "label": "Asset floor (tangible book or net current assets)",
                        "value": round(floor, 2),
                        "inputs": {"Tangible book per share": round(tbv, 2),
                                   **({"Net current assets per share": round(ncav, 2)} if ncav is not None else {})}})

    heavy = sector in ASSET_HEAVY_SECTORS
    wmap = WEIGHTS_ASSET_HEAVY if heavy else WEIGHTS
    if finance:
        wmap = {"epv": .40, "multiples": .40, "asset_floor": .20}
    used = [m for m in methods if wmap.get(m["name"], 0) > 0 and m["value"] > 0]
    tw = sum(wmap[m["name"]] for m in used)
    for m in methods:
        m["weight"] = round(wmap.get(m["name"], 0) / tw, 3) if tw and m in used else 0
    base = sum(m["value"] * m["weight"] for m in used) if used else None
    if len(used) < 3:
        flags.append(f"Only {len(used)} weighted methods (3 required by the framework)")
    lows = [m["value"] for m in used]
    highs = list(lows)
    if sens:
        lows.append(sens["grid"][-1][0])
        highs.append(sens["grid"][0][-1])
    low, high = (min(lows), max(highs)) if lows else (None, None)
    if base and low is not None and (high - low) / base > 1.0:
        flags.append(f"Methods disagree widely (range is {(high - low) / base:.0%} of base value)")
    implied = None
    if not finance and price and shares:
        yrs_ = list(S["ocf"])[-3:]
        f0 = st.mean([S["ocf"][y] + S["int"].get(y, 0) * (1 - t) - S["capex"].get(y, 0) for y in yrs_]) if yrs_ else None
        target_ev = price * shares + debt - cash
        if f0 and f0 > 0 and target_ev > 0:
            lo_g, hi_g = -0.20, 0.60
            if (dcf_fcff(f0, hi_g, g_term, wacc)[0] >= target_ev) and (dcf_fcff(f0, lo_g, g_term, wacc)[0] <= target_ev):
                for _ in range(60):
                    mid = (lo_g + hi_g) / 2
                    lo_g, hi_g = (mid, hi_g) if dcf_fcff(f0, mid, g_term, wacc)[0] < target_ev else (lo_g, mid)
                implied = round((lo_g + hi_g) / 2, 4)
    req = REQUIRED_MOS[company_type]
    mos = (base - price) / base if base and price else None
    return {"base": round(base, 2) if base else None, "low": round(low, 2) if low else None,
            "high": round(high, 2) if high else None, "methods": methods, "sensitivity": sens,
            "company_type": company_type, "required_mos": req,
            "reverse_dcf": {"implied_growth": implied, "historic_growth": g_hist,
                            "note": "Annual free-cash-flow growth for 5 years (then fading) that today's price assumes"},
            "entry_target": round(base * (1 - req), 2) if base else None,
            "current_mos": round(mos, 4) if mos is not None else None,
            "assumptions": {"Risk-free rate": round(rf[0], 4), "Risk-free source": rf[1], "Beta used": round(beta_used, 2),
                            "Equity risk premium": ERP, "Cost of equity": round(ke, 4), "WACC": round(wacc, 4),
                            "Tax rate": round(t, 3), "Stage-1 growth": round(g1, 4), "Terminal growth": round(g_term, 4),
                            "Net debt": round(debt - cash)},
            "flags": flags, "notes": notes,
            "inputs_for_scenario": {"fcf_ps": round(div(st.mean(list(S["ni"].values())[-3:]) if finance and S["ni"] else S["fcf_avg3"], shares) or 0, 2), "g1": round(g1 * 100, 1),
                                    "g2": round((g1 + g_term) / 2 * 100, 1), "tg": round(g_term * 100, 1),
                                    "dr": round(max(ke, 0.08) * 100, 1)}}


# ------------------------------------------------------------------ one company
def research(ticker, cik, sector, industry, peers_all):
    cf = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", f"cf_{cik}.json")
    S = {k: annual(cf, k) for k in T}
    S["last"] = lambda k: list(S[k].values())[-1] if S[k] else None

    def cagr(k, n=5):
        v = list(S[k].items())
        n = min(n, len(v) - 1)
        if n < 2:
            return None
        a, b = v[-n - 1][1], v[-1][1]
        return (b / a) ** (1 / n) - 1 if a and a > 0 and b and b > 0 else None
    S["cagr"] = cagr
    (eq_now, bs_date) = latest_instant(cf, "eq")
    S["eq_now"] = eq_now
    for k in ("ltd", "std", "stb", "cash", "sti", "gw", "intang", "ca", "liab"):
        S[f"{k}_now"] = latest_instant(cf, k)[0]
    S["debt_now"] = sum(v for v in (S["ltd_now"], S["std_now"], S["stb_now"]) if v)
    S["cash_now"] = (S["cash_now"] or 0) + (S["sti_now"] or 0)
    S["last_fcf"] = (S["last"]("ocf") or 0) - (S["last"]("capex") or 0) if S["ocf"] else None
    f3y = [S["ocf"][y] - S["capex"].get(y, 0) for y in list(S["ocf"])[-3:]]
    S["fcf_avg3"] = st.mean(f3y) if f3y else None

    tk = yf.Ticker(ticker)
    info = safe(lambda: tk.info, {}) or {}
    hist = safe(lambda: tk.history(period="2y")["Close"].dropna(), pd.Series(dtype=float))
    price = info.get("currentPrice") or info.get("regularMarketPrice") or (float(hist.iloc[-1]) if len(hist) else None)
    shares = info.get("sharesOutstanding") or S["last"]("shares")

    # ---- 10-year history table
    years = sorted(S["rev"])[-10:]
    hist_rows = []
    for y in years:
        rev, ni, op, gp = S["rev"].get(y), S["ni"].get(y), S["opinc"].get(y), S["gp"].get(y)
        fcf = (S["ocf"].get(y) - S["capex"].get(y, 0)) if y in S["ocf"] else None
        ic = (S["eq"].get(y) or 0) + (S["ltd"].get(y) or 0) + (S["std"].get(y) or 0) - (S["cash"].get(y) or 0)
        prev = S["rev"].get(y - 1)
        hist_rows.append({"year": y, "revenue": rev, "rev_growth": div(rev, prev) - 1 if prev else None,
                          "gross_margin": div(gp, rev), "op_margin": div(op, rev), "net_margin": div(ni, rev),
                          "net_income": ni, "fcf": fcf, "fcf_margin": div(fcf, rev),
                          "roic": (div(ni, S["eq"].get(y)) if sector == "Finance" and (S["eq"].get(y) or 0) > 0 else
                                   div(op * 0.79, ic) if op is not None and ic > 0 else None),
                          "eps": div(ni, S["shares"].get(y)), "shares": S["shares"].get(y),
                          "buybacks": S["buyback"].get(y), "dividends": S["div"].get(y), "sbc": S["sbc"].get(y)})

    # ---- peers from the latest screen: same industry, nearest in size
    me = next((p for p in peers_all if p["ticker"] == ticker), None)
    mcap = (me or {}).get("mcap") or (price * shares if price and shares else None)
    same = [p for p in peers_all if p["ticker"] != ticker and p.get("industry") == industry]
    if len(same) < 4:
        same = [p for p in peers_all if p["ticker"] != ticker and p.get("sector") == sector]
    peers = sorted(same, key=lambda p: abs(math.log((p["mcap"] or 1) / (mcap or 1))))[:8]

    # ---- moat rating
    rocs = [r["roic"] for r in hist_rows[-5:] if r["roic"] is not None]
    gms = [r["gross_margin"] for r in hist_rows[-5:] if r["gross_margin"] is not None]
    oms = [r["op_margin"] for r in hist_rows[-5:] if r["op_margin"] is not None]
    pts, why = 0, []
    if rocs:
        avg = st.mean(rocs)
        if avg >= 0.15:
            pts += 2; why.append(f"{'ROE' if sector == 'Finance' else 'ROIC'} averaged {avg:.0%} over {len(rocs)} years")
        elif avg >= 0.10:
            pts += 1; why.append(f"ROIC averaged {avg:.0%}, above a typical cost of capital")
        if min(rocs) >= 0.10:
            pts += 1; why.append(f"ROIC never fell below {min(rocs):.0%}")
    if gms and st.mean(gms) >= 0.40:
        pts += 1; why.append(f"Gross margin averages {st.mean(gms):.0%}, a sign of pricing power")
    if len(gms) >= 3 and pd.Series(gms).std() < 0.03:
        pts += 1; why.append("Gross margin stable within 3 points")
    if len(oms) >= 3 and oms[-1] >= oms[0]:
        pts += 1; why.append("Operating margin flat or rising")
    if (cagr("rev") or 0) > 0.05:
        pts += 1; why.append(f"Revenue growing {cagr('rev'):.0%} a year")
    moat = {"rating": "Wide" if pts >= 5 else "Narrow" if pts >= 3 else "None evident", "points": pts, "of": 7,
            "reasons": why, "note": "Quantitative signals only; the deep dive names the moat type and checks durability"}

    # ---- company type for margin of safety (deep dive confirms)
    if moat["rating"] == "Wide" and (cagr("rev") or 0) <= 0.20:
        ctype = "wide_moat_compounder"
    elif (cagr("rev") or 0) > 0.20 and (not rocs or st.mean(rocs) < 0.10):
        ctype = "high_growth_early"
    elif sector in CYCLICAL_SECTORS and (len([r for r in hist_rows[-5:] if (r["net_income"] or 0) < 0]) or
                                         (len(oms) >= 3 and pd.Series(oms).std() > 0.05)):
        ctype = "cyclical_turnaround"
    else:
        ctype = "average_quality"

    rf = RF
    val = valuation(S, info, price, shares, sector, peers, ctype, rf)

    # ---- risk rating
    rpts, rwhy = 0, []
    ebitda = (S["last"]("opinc") or 0) + (S["last"]("da") or 0)
    nd_e = div(S["debt_now"] - S["cash_now"], ebitda) if ebitda > 0 else None
    if nd_e is not None and nd_e > 3:
        rpts += 2; rwhy.append(f"Net debt {nd_e:.1f}x EBITDA")
    elif nd_e is not None and nd_e > 2:
        rpts += 1; rwhy.append(f"Net debt {nd_e:.1f}x EBITDA")
    cover = div(S["last"]("opinc"), S["last"]("int"))
    if cover is not None and cover < 4:
        rpts += 1; rwhy.append(f"Interest covered only {cover:.1f}x")
    if (info.get("beta") or 1) > 1.5:
        rpts += 1; rwhy.append(f"Beta {info['beta']:.2f}")
    vol = float(hist.pct_change().tail(252).std() * math.sqrt(252)) if len(hist) > 60 else None
    if vol and vol > 0.45:
        rpts += 1; rwhy.append(f"Price volatility {vol:.0%} a year")
    losses = [r["year"] for r in hist_rows[-5:] if (r["net_income"] or 0) < 0]
    if losses:
        rpts += 1; rwhy.append(f"Net loss in {', '.join(map(str, losses))}")
    if S["last_fcf"] is not None and S["last_fcf"] < 0:
        rpts += 1; rwhy.append("Negative free cash flow last year")
    sh = [r["shares"] for r in hist_rows[-5:] if r["shares"]]
    dil = (sh[-1] / sh[0]) ** (1 / (len(sh) - 1)) - 1 if len(sh) >= 3 else None
    if dil is not None and dil > 0.03:
        rpts += 1; rwhy.append(f"Share count rising {dil:.1%} a year")
    si = info.get("shortPercentOfFloat")
    if si and si > 0.10:
        rpts += 1; rwhy.append(f"Short interest {si:.0%} of float")
    risk = {"rating": "High" if rpts >= 4 else "Medium" if rpts >= 2 else "Low", "points": rpts, "reasons": rwhy}

    # ---- red flags
    flags = []
    def flag(sev, text):
        flags.append({"severity": sev, "text": text})
    if dil is not None and dil > 0.03:
        flag("medium", f"Dilution: diluted shares up {dil:.1%} a year")
    if nd_e is not None and nd_e > 3:
        flag("high", f"Leverage: net debt {nd_e:.1f}x EBITDA (framework red line 3x)")
    if cover is not None and cover < 3:
        flag("high", f"Interest cover {cover:.1f}x (framework red line 3x)")
    f3 = [r["fcf"] for r in hist_rows[-3:] if r["fcf"] is not None]
    n3 = [r["net_income"] for r in hist_rows[-3:] if r["net_income"] is not None]
    if f3 and n3 and sum(n3) > 0 and sum(f3) / sum(n3) < 0.7:
        flag("medium", f"Weak cash conversion: free cash flow is {sum(f3) / sum(n3):.0%} of net income over 3 years")
    ar, inv = S["ar"], S["inv"]
    ly = years[-1] if years else None
    if ly and ly - 1 in ar and ly in ar and ly - 1 in S["rev"]:
        ar_g, rv_g = div(ar[ly], ar[ly - 1]) - 1, div(S["rev"][ly], S["rev"][ly - 1]) - 1
        if ar_g is not None and rv_g is not None and ar_g - rv_g > 0.15:
            flag("medium", f"Receivables grew {ar_g:.0%} vs revenue {rv_g:.0%}")
    if ly and ly - 1 in inv and ly in inv and ly - 1 in S["rev"]:
        in_g, rv_g = div(inv[ly], inv[ly - 1]) - 1, div(S["rev"][ly], S["rev"][ly - 1]) - 1
        if in_g is not None and rv_g is not None and in_g - rv_g > 0.20:
            flag("medium", f"Inventory grew {in_g:.0%} vs revenue {rv_g:.0%}")
    if len(gms) >= 3 and gms[-1] < gms[-2] < gms[-3]:
        flag("low", f"Gross margin down two years running ({gms[-3]:.0%} to {gms[-1]:.0%})")
    if eq_now is not None and eq_now < 0:
        flag("medium", "Negative shareholders' equity")
    if eq_now and S["gw_now"] and S["gw_now"] / eq_now > 0.5:
        flag("low", f"Goodwill is {S['gw_now'] / eq_now:.0%} of equity (acquisition-heavy)")
    if si and si > 0.10:
        flag("medium", f"Short interest {si:.0%} of float")

    # ---- insiders (last 6 months)
    ins = {"transactions": [], "buys": 0, "sells": 0, "buy_value": 0, "sell_value": 0, "buyers": 0, "signal": "No data"}
    df = safe(lambda: tk.insider_transactions)
    if df is not None and len(df):
        df = df.copy()
        dcol = next((c for c in df.columns if "date" in c.lower()), None)
        if dcol:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
            df = df[df[dcol] >= pd.Timestamp(TODAY - dt.timedelta(days=183))]
        for _, r in df.head(40).iterrows():
            text = str(r.get("Text") or r.get("Transaction") or "")
            kind = "buy" if "purchase" in text.lower() or "buy" in text.lower() else "sell" if "sale" in text.lower() else "other"
            value = float(r.get("Value") or 0) if pd.notna(r.get("Value")) else 0
            ins["transactions"].append({"date": str(r.get(dcol))[:10] if dcol else None, "insider": r.get("Insider"),
                                        "position": r.get("Position"), "kind": kind, "shares": r.get("Shares"),
                                        "value": value, "text": text[:120]})
            if kind == "buy":
                ins["buys"] += 1; ins["buy_value"] += value
            elif kind == "sell":
                ins["sells"] += 1; ins["sell_value"] += value
        ins["buyers"] = len({t["insider"] for t in ins["transactions"] if t["kind"] == "buy"})
        ins["signal"] = ("Cluster buying" if ins["buyers"] >= 3 else "Net buying" if ins["buy_value"] > ins["sell_value"]
                         else "Net selling" if ins["sell_value"] > 0 else "Quiet")
        if ins["sell_value"] > 0 and mcap and ins["sell_value"] / mcap > 0.005:
            flag("low", f"Insiders sold ${ins['sell_value'] / 1e6:,.0f}M in 6 months")

    # ---- analysts
    actions = []
    ud = safe(lambda: tk.upgrades_downgrades)
    if ud is not None and len(ud):
        ud = ud.reset_index()
        dcol = next((c for c in ud.columns if "date" in c.lower()), ud.columns[0])
        ud[dcol] = pd.to_datetime(ud[dcol], errors="coerce").dt.tz_localize(None)
        for _, r in ud[ud[dcol] >= pd.Timestamp(TODAY - dt.timedelta(days=120))].head(12).iterrows():
            actions.append({"date": str(r[dcol])[:10], "firm": r.get("Firm"), "action": r.get("Action"),
                            "from": r.get("FromGrade"), "to": r.get("ToGrade"),
                            "target": r.get("currentPriceTarget"), "prior_target": r.get("priorPriceTarget")})
    analysts = {"mean": info.get("targetMeanPrice"), "high": info.get("targetHighPrice"), "low": info.get("targetLowPrice"),
                "median": info.get("targetMedianPrice"), "n": info.get("numberOfAnalystOpinions"),
                "recommendation": info.get("recommendationKey"), "rec_mean": info.get("recommendationMean"),
                "forward_pe": info.get("forwardPE"), "forward_eps": info.get("forwardEps"),
                "actions": actions}

    # ---- catalysts and news
    cal = safe(lambda: tk.calendar, {}) or {}
    ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
    news = []
    for n in (safe(lambda: tk.news, []) or [])[:12]:
        c = n.get("content", n)
        title = c.get("title") or ""
        url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else None) or c.get("link")
        news.append({"title": title, "url": url, "publisher": (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher"),
                     "date": (c.get("pubDate") or "")[:10] or (str(dt.datetime.fromtimestamp(c["providerPublishTime"]))[:10] if c.get("providerPublishTime") else None),
                     "negative": any(w in title.lower() for w in BAD_NEWS)})
    short = (info.get("shortName") or info.get("longName") or "").replace(",", "").split()
    keys = {ticker.lower()} | ({short[0].lower()} if short and len(short[0]) > 3 else set()) | (
        {" ".join(short[:2]).lower()} if len(short) >= 2 else set())
    for n in news:
        tl = f" {n['title'].lower()} "
        n["about_company"] = any(f" {k} " in tl or f"{k}'" in tl or f" {k}:" in tl or f"({k})" in tl for k in keys)
        if n["negative"] and n["about_company"]:
            flag("low", f"News: {n['title'][:110]}")
    catalysts = {"next_earnings": [str(d)[:10] for d in ed] if isinstance(ed, (list, tuple)) else (str(ed)[:10] if ed else None),
                 "ex_dividend": str(cal.get("Ex-Dividend Date"))[:10] if isinstance(cal, dict) and cal.get("Ex-Dividend Date") else None,
                 "dividend_yield": info.get("dividendYield"), "news": news}

    # ---- business, management, ownership
    holders = safe(lambda: tk.institutional_holders)
    top_holders = []
    if holders is not None and len(holders):
        for _, r in holders.head(8).iterrows():
            top_holders.append({"holder": r.get("Holder"), "pct": r.get("pctHeld") if "pctHeld" in r else r.get("% Out"),
                                "shares": r.get("Shares"), "value": r.get("Value"), "pct_change": r.get("pctChange"),
                                "reported": str(r.get("Date Reported"))[:10] if r.get("Date Reported") is not None else None})
    business = {"summary": info.get("longBusinessSummary"), "website": info.get("website"),
                "employees": info.get("fullTimeEmployees"), "city": info.get("city"), "country": info.get("country"),
                "officers": [{"name": o.get("name"), "title": o.get("title"), "age": o.get("age"),
                              "pay": o.get("totalPay"), "since": o.get("yearBorn")}
                             for o in (info.get("companyOfficers") or [])[:8]]}
    ownership = {"insiders": info.get("heldPercentInsiders"), "institutions": info.get("heldPercentInstitutions"),
                 "short_float": si, "short_ratio": info.get("shortRatio"), "top_holders": top_holders}

    me_row = {"ticker": ticker, "mcap": mcap, **{k: (me or {}).get(k) for k in
              ("pe", "ev_ebitda", "rev_cagr", "net_margin", "roc_avg", "fcf_yield", "drawdown", "name")}}
    return {"as_of": str(TODAY), "price": price, "shares": shares, "mcap": mcap, "sector": sector, "industry": industry,
            "balance_sheet_date": bs_date, "valuation": val, "history": hist_rows,
            "peers": [me_row] + peers, "moat": moat, "risk": risk, "red_flags": flags, "insiders": ins,
            "analysts": analysts, "catalysts": catalysts, "business": business, "ownership": ownership,
            "beta": info.get("beta"), "volatility_1y": vol,
            "bull_base_bear": {"bear": val["low"], "base": val["base"], "bull": val["high"],
                               "analyst_low": analysts["low"], "analyst_high": analysts["high"]}}


# ------------------------------------------------------------------ run
conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS v2_research (
                 ticker TEXT, as_of DATE, data JSONB, PRIMARY KEY (ticker, as_of));
               ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS research_requested BOOLEAN DEFAULT FALSE;""")
conn.commit()
cur.execute("SELECT MAX(run_date) AS d FROM v2_screen_scores")
last_run = cur.fetchone()["d"]
cur.execute("""SELECT ticker, (metrics->>'mcap')::float AS mcap, metrics->>'sector' AS sector, metrics->>'industry' AS industry,
                      metrics->>'name' AS name, (metrics->'fin'->>'pe')::float AS pe, (metrics->'fin'->>'ev_ebitda')::float AS ev_ebitda,
                      (metrics->'fin'->>'rev_cagr')::float AS rev_cagr, (metrics->'fin'->>'net_margin')::float AS net_margin,
                      (metrics->'fin'->>'roc_avg')::float AS roc_avg, (metrics->'fin'->>'fcf_yield')::float AS fcf_yield,
                      (metrics->'px'->>'drawdown')::float AS drawdown
               FROM v2_screen_scores WHERE run_date = %s""", (last_run,))
peers_all = [dict(r) for r in cur.fetchall()]

if args.tickers:
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
else:
    cur.execute("""SELECT ticker FROM v2_names WHERE level IN ('watchlist','book') OR is_core
                     OR status IN ('candidate','deep_dive_requested') OR research_requested
                   UNION SELECT jsonb_array_elements(top10)->>'ticker' FROM v2_screen_runs
                     WHERE run_date = (SELECT MAX(run_date) FROM v2_screen_runs)
                   UNION SELECT jsonb_array_elements(COALESCE(growth_top10, '[]'::jsonb))->>'ticker' FROM v2_screen_runs
                     WHERE run_date = (SELECT MAX(run_date) FROM v2_screen_runs)""")
    tickers = sorted({r["ticker"] for r in cur.fetchall()})

tick = sec_get("https://www.sec.gov/files/company_tickers.json", "company_tickers.json", 144)
cik_of = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in tick.values()}
cur.execute("SELECT ticker, sector, industry FROM v2_names")
names_meta = {r["ticker"]: r for r in cur.fetchall()}
by_peer = {p["ticker"]: p for p in peers_all}
RF = risk_free()
print(f"Researching {len(tickers)} names · risk-free {RF[0]:.2%} ({RF[1]})")

ok, skipped = 0, []
w = conn.cursor()
for i, t in enumerate(tickers, 1):
    if t not in cik_of:
        skipped.append(f"{t} (not a US SEC filer)")
        continue
    meta = by_peer.get(t) or names_meta.get(t) or {}
    try:
        d = research(t, cik_of[t], meta.get("sector") or "Other", meta.get("industry"), peers_all)
    except Exception as e:
        skipped.append(f"{t} ({e.__class__.__name__}: {str(e)[:60]})")
        continue
    v = d["valuation"]
    print(f"{i:>3}/{len(tickers)} {t:<6} price {d['price'] or 0:>9.2f}  value {v['low'] or 0:>8.2f} / {v['base'] or 0:>8.2f} / "
          f"{v['high'] or 0:>8.2f}  MoS {(v['current_mos'] or 0):>6.0%} (needs {v['required_mos']:.0%})  "
          f"moat {d['moat']['rating']:<12} risk {d['risk']['rating']:<6} flags {len(d['red_flags'])}")
    if not args.dry:
        w.execute("""INSERT INTO v2_research (ticker, as_of, data) VALUES (%s, %s, %s)
                     ON CONFLICT (ticker, as_of) DO UPDATE SET data = EXCLUDED.data""",
                  (t, TODAY, psycopg2.extras.Json(clean(d))))
        a = d["analysts"]
        if a.get("mean"):
            w.execute("""INSERT INTO v2_analyst (ticker, date, mean, high, low, n) VALUES (%s,%s,%s,%s,%s,%s)
                         ON CONFLICT (ticker, date) DO UPDATE SET mean=EXCLUDED.mean, high=EXCLUDED.high,
                           low=EXCLUDED.low, n=EXCLUDED.n""", (t, TODAY, a["mean"], a["high"], a["low"], a["n"]))
        w.execute("UPDATE v2_names SET research_requested = FALSE WHERE ticker = %s", (t,))
        conn.commit()
    ok += 1

print(f"\nDone: {ok} researched." + (f" Skipped: {', '.join(skipped)}" if skipped else ""))
if args.dry:
    print("DRY RUN: nothing written.")
conn.close()
