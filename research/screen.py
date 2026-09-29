"""
screen.py — Weekly quant screen (Round 0) per framework.json (profile: moderate).

Usage:  python screen.py --framework framework.json --out screen_YYYY-MM-DD.json [--top 25]

Pipeline
  1. Universe: Nasdaq screener (all US listings) -> US-domiciled common stock,
     market cap >= min, rough ADV >= min, excludes SPACs/warrants/units/preferreds/ADRs.
  2. Fundamentals: SEC XBRL *frames* (one call per concept-period covers every filer).
  3. Framework tests (13; dividend record = checklist only): balance-sheet tests must all pass
     (sector exceptions), plus >= N of the valuation/profitability tests.
  4. Value-trap flags. Rank: tests passed desc, then FCF yield desc.
  5. Top-N: Yahoo cross-check (price within 2%, mcap within 5%, 3-mo ADV, consensus,
     EPS-trend revisions) -> quality gate status.
Outputs JSON with every metric's inputs, source and as-of.
"""
import argparse, json, datetime as dt, re, statistics as st
import vi_data as D

YEAR = dt.date.today().year
LATEST_FY = f"CY{YEAR-1}"                                       # last full calendar year
BS_PERIODS = [f"CY{YEAR}Q2I", f"CY{YEAR}Q1I", f"CY{YEAR-1}Q4I"]  # latest balance sheet first

T = {
  "rev":   ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
  "ni":    ["NetIncomeLoss"],
  "opinc": ["OperatingIncomeLoss"],
  "da":    ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
            "DepreciationAmortizationAndAccretionNet", "Depreciation"],
  "ocf":   ["NetCashProvidedByUsedInOperatingActivities"],
  "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
  "pretax":["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
  "tax":   ["IncomeTaxExpenseBenefit"],
  "int":   ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
  "eq":    ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
  "ltd":   ["LongTermDebtNoncurrent", "LongTermDebt"],
  "std":   ["LongTermDebtCurrent", "DebtCurrent"],
  "stb":   ["ShortTermBorrowings", "CommercialPaper"],
  "cash":  ["CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
  "sti":   ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
  "ca":    ["AssetsCurrent"],
  "cl":    ["LiabilitiesCurrent"],
  "liab":  ["Liabilities"],
  "gw":    ["Goodwill"],
  "intang":["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
}
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
FIN_SECTORS = {"Finance", "Real Estate"}
UTIL_SECTORS = {"Utilities"}
BAD_NAME = re.compile(r"acquisition corp|warrant|\bunits?\b|preferred|depositary|\bnotes? due\b|\btrust\b|\bfund\b|\betf\b", re.I)


def gv(x):
    return x["value"] if isinstance(x, dict) else x


def pick_bs(tags):
    """Latest available balance-sheet instant per CIK."""
    out = {}
    for per in BS_PERIODS:
        for cik, v in D.sec_frames_first(tags, per).items():
            out.setdefault(cik, v + (per,))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--framework", default="framework.json")
    ap.add_argument("--out", default=f"screen_{D.TODAY}.json")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--exclude", default="", help="comma tickers already tracked/archived (novelty gate)")
    a = ap.parse_args()
    fw = json.load(open(a.framework))
    th = fw["screen"]["thresholds"]; prof = "moderate"
    min_mcap = gv(fw["universe"]["min_market_cap_usd"])
    min_adv = gv(fw["universe"]["min_avg_daily_dollar_volume_usd"])
    min_rev = gv(fw["universe"]["min_revenue_usd"])
    min_other = 5
    exclude = {t.strip().upper() for t in a.exclude.split(",") if t.strip()}

    # 1. universe
    uni = D.nasdaq_universe()
    sec = D.sec_tickers()
    funnel = {"nasdaq_listings": len(uni)}
    cands = []
    for r in uni:
        tk = r["ticker"]
        if not re.fullmatch(r"[A-Z]{1,5}", tk):          # drops ^ / . W U R suffix classes
            continue
        if r["country"] != "United States" or BAD_NAME.search(r["name"] or ""):
            continue
        if not r["market_cap"] or r["market_cap"] < min_mcap:
            continue
        if (r["price"] or 0) * (r["volume"] or 0) < min_adv * 0.5:   # 1-day proxy; confirmed w/ 3-mo ADV later
            continue
        if tk in exclude or tk not in sec:
            continue
        r["cik"] = sec[tk]["cik"]; r["exchange"] = sec[tk]["exchange"]
        cands.append(r)
    funnel["us_common_mcap_liquidity"] = len(cands)

    # 2. fundamentals via frames
    F = {k: D.sec_frames_first(v, LATEST_FY) for k, v in T.items()
         if k in ("rev", "ni", "opinc", "da", "ocf", "capex", "pretax", "tax", "int")}
    F["ocf_prev"] = D.sec_frames_first(T["ocf"], f"CY{YEAR-2}")
    F["capex_prev"] = D.sec_frames_first(T["capex"], f"CY{YEAR-2}")
    F["ni_prev"] = D.sec_frames_first(T["ni"], f"CY{YEAR-2}")
    for k in ("eq", "ltd", "std", "stb", "cash", "sti", "ca", "cl", "liab", "gw", "intang"):
        F[k] = pick_bs(T[k])
    ni_hist = {y: D.sec_frames_first(T["ni"], f"CY{y}") for y in range(YEAR-5, YEAR)}
    eq_hist = {y: D.sec_frames_first(T["eq"], f"CY{y}Q4I") for y in range(YEAR-6, YEAR)}
    eps_hist = {y: D.sec_frames_first(EPS_TAGS, f"CY{y}", unit="USD-per-shares") for y in range(YEAR-12, YEAR)}

    def g(k, cik):
        v = F[k].get(cik)
        return v[0] if v else None

    results = []
    for r in cands:
        c = r["cik"]; m = {}; src = {}
        rev, ni, opinc = g("rev", c), g("ni", c), g("opinc", c)
        if rev is None or ni is None:
            continue
        if rev < min_rev:
            continue
        da = g("da", c) or 0
        ocf, capex = g("ocf", c), g("capex", c) or 0
        eq = g("eq", c)
        debt = sum(x for x in (g("ltd", c), g("std", c), g("stb", c)) if x)
        cash = (g("cash", c) or 0) + (g("sti", c) or 0)
        mcap, price = r["market_cap"], r["price"]
        ev = mcap + debt - cash
        ebitda = (opinc + da) if opinc is not None else None
        pretax, tax = g("pretax", c), g("tax", c)
        t_rate = min(max(tax / pretax, 0), 0.35) if (pretax and tax and pretax > 0) else 0.21
        fcf = (ocf - capex) if ocf is not None else None
        # multi-year
        roes = []
        for y in range(YEAR-5, YEAR):
            n = ni_hist[y].get(c); e1 = eq_hist[y].get(c); e0 = eq_hist[y-1].get(c)
            if n and e1:
                avg_e = (e1[0] + e0[0]) / 2 if e0 else e1[0]
                if avg_e > 0:
                    roes.append(n[0] / avg_e)
        def eps_avg(years):
            vals = [eps_hist[y].get(c)[0] for y in years if eps_hist[y].get(c)]
            return st.mean(vals) if len(vals) == len(years) else None
        eps3 = eps_avg(range(YEAR-3, YEAR)); eps_old = eps_avg(range(YEAR-12, YEAR-9))
        eps_last = eps_hist[YEAR-1].get(c, (None,))[0]
        shares_implied = mcap / price if price else None
        bvps = eq / shares_implied if (eq and shares_implied) else None
        fin = r["sector"] in FIN_SECTORS; util = r["sector"] in UTIL_SECTORS

        m["pe_3yr_avg"] = price / eps3 if (eps3 and eps3 > 0) else None
        m["pe_ttm"] = price / eps_last if (eps_last and eps_last > 0) else None
        m["pb"] = price / bvps if (bvps and bvps > 0) else None
        m["pe_x_pb"] = m["pe_ttm"] * m["pb"] if (m["pe_ttm"] and m["pb"]) else None
        m["ev_ebitda"] = ev / ebitda if (ebitda and ebitda > 0) else None
        m["roe_5yr_avg"] = st.mean(roes) if len(roes) >= 4 else None
        ic = (eq or 0) + debt - cash
        m["roic"] = opinc * (1 - t_rate) / ic if (opinc is not None and ic > 0) else None
        m["net_margin"] = ni / rev if rev else None
        m["debt_to_equity"] = debt / eq if (eq and eq > 0) else None
        m["net_debt_ebitda"] = (debt - cash) / ebitda if (ebitda and ebitda > 0) else None
        ca, cl = g("ca", c), g("cl", c)
        m["current_ratio"] = ca / cl if (ca and cl) else None
        m["fcf_yield"] = fcf / mcap if fcf is not None else None
        m["eps_growth_10yr_total"] = (eps3 / eps_old - 1) if (eps3 and eps_old and eps_old > 0) else None
        m["interest_cover"] = opinc / g("int", c) if (opinc and g("int", c)) else None

        tests = {}
        def test(name, val, cond_ok, na=False):
            tests[name] = "N/A" if na else ("NO DATA" if val is None else ("PASS" if cond_ok(val) else "FAIL"))
        thr = lambda k: th[k][prof]
        test("pe_3yr_avg", m["pe_3yr_avg"], lambda v: v < thr("pe_3yr_avg"))
        test("pb", m["pb"], lambda v: v < thr("pb") and (m["roe_5yr_avg"] or 0) > 0.15 or v < 1.5)
        test("pe_x_pb", m["pe_x_pb"], lambda v: v < thr("pe_x_pb"))
        test("ev_ebitda", m["ev_ebitda"], lambda v: v < thr("ev_ebitda"), na=fin)
        test("roe_5yr_avg", m["roe_5yr_avg"], lambda v: v > thr("roe_5yr_avg"))
        test("roic", m["roic"], lambda v: v > thr("roic"), na=fin)
        test("net_margin", m["net_margin"], lambda v: v > thr("net_margin"))
        test("fcf_yield", m["fcf_yield"], lambda v: v > thr("fcf_yield"), na=fin)
        test("eps_growth_10yr_total", m["eps_growth_10yr_total"], lambda v: v > thr("eps_growth_10yr_total"))
        # balance sheet (must pass)
        test("debt_to_equity", m["debt_to_equity"], lambda v: v < (2.0 if util else thr("debt_to_equity")), na=fin)
        test("net_debt_ebitda", m["net_debt_ebitda"] if (debt - cash) > 0 else -1,
             lambda v: v < thr("net_debt_ebitda"), na=fin)
        test("current_ratio", m["current_ratio"], lambda v: v > thr("current_ratio"), na=(fin or util))
        bs = ["debt_to_equity", "net_debt_ebitda", "current_ratio"]
        bs_ok = all(tests[k] in ("PASS", "N/A") for k in bs)
        other = [k for k in tests if k not in bs]
        n_pass = sum(tests[k] == "PASS" for k in other)
        passed = bs_ok and n_pass >= min_other

        flags = []
        o2, cx2, n2 = g("ocf_prev", c), g("capex_prev", c) or 0, g("ni_prev", c)
        if fcf is not None and fcf < 0 and ni > 0 and o2 is not None and (o2 - cx2) < 0 and (n2 or 0) > 0:
            flags.append("VALUE_TRAP: positive NI but negative FCF 2 yrs (framework p.7)")
        if len(roes) >= 4 and roes[-1] < roes[0] - 0.05:
            flags.append("VALUE_TRAP: ROE down >5pts over 4 yrs (moat narrowing, framework p.10)")
        if m["interest_cover"] is not None and m["interest_cover"] < 3:
            flags.append("RED_FLAG: interest cover < 3x (framework p.22)")

        as_of = {k: (F[k].get(c) or (None, None))[1] for k in ("rev", "ni", "eq", "ca")}
        results.append({"ticker": r["ticker"], "name": r["name"], "cik": c, "exchange": r["exchange"],
                        "sector": r["sector"], "industry": r["industry"],
                        "price": D.fact(price, "Nasdaq.com screener"),
                        "market_cap": D.fact(mcap, "Nasdaq.com screener"),
                        "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
                        "tests": tests, "balance_sheet_ok": bs_ok, "other_tests_passed": n_pass,
                        "passed_screen": passed, "flags": flags,
                        "fundamentals_source": f"SEC XBRL frames {LATEST_FY} (flows) / latest of {BS_PERIODS} (balance sheet)",
                        "fundamentals_period_end": as_of})

    funnel["with_sec_fundamentals"] = len(results)
    passed = [x for x in results if x["passed_screen"]]
    funnel["passed_framework_screen"] = len(passed)
    passed.sort(key=lambda x: (x["other_tests_passed"], x["metrics"].get("fcf_yield") or 0), reverse=True)

    # 5. Yahoo cross-check on top N
    top = passed[: a.top]
    for x in top:
        y = D.yahoo_snapshot(x["ticker"])
        gate = []
        p1, p2 = x["price"]["value"], y.get("price")
        if p2:
            diff = abs(p1 - p2) / p2
            x["price_crosscheck"] = D.fact(round(p2, 2), "Yahoo Finance", diff=round(diff, 4))
            if diff > gv(fw["data_quality_gates"]["price_cross_check_max_diff"]):
                gate.append(f"CONFLICT price Nasdaq {p1} vs Yahoo {p2:.2f}")
        else:
            gate.append("NO DATA Yahoo price")
        m1, m2 = x["market_cap"]["value"], y.get("market_cap")
        if m2 and abs(m1 - m2) / m2 > gv(fw["data_quality_gates"]["mcap_cross_check_max_diff"]):
            gate.append(f"CONFLICT mcap Nasdaq {m1:,.0f} vs Yahoo {m2:,.0f}")
        adv = (y.get("avg_volume_3m") or 0) * (p2 or p1)
        x["adv_3m_usd"] = D.fact(round(adv), "Yahoo Finance 3-mo avg volume x price")
        if adv < min_adv:
            gate.append(f"FAIL liquidity ADV ${adv:,.0f} < ${min_adv:,.0f}")
        x["consensus"] = {k: y.get(k) for k in ("forwardEps", "targetMeanPrice", "numberOfAnalystOpinions",
                                                 "recommendationKey", "eps_trend_+1y")}
        x["consensus_source"] = D.fact(None, "Yahoo Finance (single source, confidence=Medium)")
        tr = y.get("eps_trend_+1y") or {}
        if tr.get("current") and tr.get("30daysAgo"):
            rev30 = tr["current"] / tr["30daysAgo"] - 1
            x["eps_revision_30d"] = round(rev30, 4)
            if abs(rev30) >= 0.05:
                x["flags"].append(f"ESTIMATE_REVISION {rev30:+.1%} NTM EPS in 30d")
        x["quality_gate"] = "PASS" if not gate else "FAIL"
        x["quality_issues"] = gate
    funnel["top_crosschecked"] = len(top)
    funnel["top_quality_pass"] = sum(1 for x in top if x["quality_gate"] == "PASS")

    out = {"screen_date": D.TODAY, "framework_version": fw["_meta"]["version"], "profile": prof,
           "rule": f"all balance-sheet tests pass (sector exceptions) + >= {min_other} of 9 other tests",
           "funnel": funnel, "shortlist_candidates": top,
           "also_passed": [{"ticker": x["ticker"], "other_tests_passed": x["other_tests_passed"],
                            "fcf_yield": x["metrics"].get("fcf_yield")} for x in passed[a.top:]],
           "sources": ["Nasdaq.com screener (universe, price, mcap)", "SEC EDGAR XBRL frames (fundamentals)",
                       "Yahoo Finance (cross-check, ADV, consensus)"]}
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    print(json.dumps(funnel, indent=1))
    for x in top:
        print(f'{x["ticker"]:6} {x["sector"][:18]:18} pass={x["other_tests_passed"]} '
              f'fcfy={x["metrics"].get("fcf_yield")} gate={x["quality_gate"]} {x["flags"][:1]}')


if __name__ == "__main__":
    main()
