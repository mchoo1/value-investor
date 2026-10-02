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
  "rev":   ["RevenuesNetOfInterestExpense", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
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
  "ltd":   ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebt"],
  "std":   ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
  "stb":   ["ShortTermBorrowings", "CommercialPaper"],
  "cash":  ["CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
  "sti":   ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
  "ca":    ["AssetsCurrent"],
  "cl":    ["LiabilitiesCurrent"],
  "liab":  ["Liabilities"],
  "gw":    ["Goodwill"],
  "intang":["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
  "gp":    ["GrossProfit"],
  "cogs":  ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"],
}
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
FIN_SECTORS = {"Finance", "Real Estate"}
UTIL_SECTORS = {"Utilities"}
BAD_NAME = re.compile(r"acquisition corp|warrant|\bunits?\b|preferred|depositary|\bnotes? due\b|\btrust\b|\bfund\b|\betf\b", re.I)

# Ming's focus sectors (2026-10-02): robotics, finance, AI data centres, technology.
# Mapping from Nasdaq sector/industry + seed tickers is PROPOSED; Round 1 verifies each label vs the 10-K.
FOCUS_SEED = {
  "ai_datacentre": {"EQIX", "DLR", "IRM", "VRT", "ETN", "PWR", "GEV", "NVT", "APH", "ANET", "CIEN", "COHR",
                    "CRDO", "SMCI", "DELL", "HPE", "CLS", "FIX", "EME", "MOD", "VST", "CEG", "TLN", "NRG",
                    "CORZ", "IREN", "ALAB", "MRVL", "AVGO", "NVDA", "AMD", "MU", "WDC", "STX", "LITE"},
  "robotics":      {"ISRG", "ROK", "TER", "ZBRA", "CGNX", "SYM", "EMR", "PRCT", "NOVT", "IPGP", "AVAV",
                    "KTOS", "SERV", "PATH", "JBT", "MKSI", "AZTA", "RR", "OUST", "LUNR"},
}
AI_DC_IND = {"Semiconductors", "Computer Manufacturing", "Computer peripheral equipment",
             "Computer Communications Equipment", "Electronic Components"}
ROBOT_NAME = re.compile(r"robot|automation|autonomous|machine vision", re.I)
FIN_EXCL_IND = {"Blank Checks", "Trusts Except Educational Religious and Charitable", "Real Estate"}
FOCUS_ORDER = ["ai_datacentre", "robotics", "technology", "finance"]   # high-growth first, finance last
NOT_FOCUS = {"FSLR", "ENPH", "SEDG", "ARRY", "NXT", "SHLS", "RUN", "CSIQ", "MAXN"}   # solar: Nasdaq files under Semiconductors


def focus_sector(r):
    tk, sec, ind, name = r["ticker"], r.get("sector") or "", r.get("industry") or "", r.get("name") or ""
    if tk in NOT_FOCUS or re.search(r"solar", name, re.I):
        return None
    if tk in FOCUS_SEED["ai_datacentre"] or ind in AI_DC_IND:
        return "ai_datacentre"
    if tk in FOCUS_SEED["robotics"] or ROBOT_NAME.search(name):
        return "robotics"
    if sec == "Technology" or ind in ("Telecommunications Equipment",):
        return "technology"
    if sec == "Finance" and ind not in FIN_EXCL_IND:
        return "finance"
    return None


def focus_rank(x):
    f = x.get("focus_sector")
    return len(FOCUS_ORDER) - FOCUS_ORDER.index(f) if f in FOCUS_ORDER else 0


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
    ap.add_argument("--top-drawdown", type=int, default=15)
    ap.add_argument("--top-growth", type=int, default=15)
    ap.add_argument("--exclude", nargs="?", const="", default="", help="comma tickers already tracked/archived (novelty gate)")
    a = ap.parse_args()
    fw = json.load(open(a.framework))
    th = fw["screen"]["thresholds"]; prof = "moderate"
    min_mcap = gv(fw["universe"]["min_market_cap_usd"])
    min_adv = gv(fw["universe"]["min_avg_daily_dollar_volume_usd"])
    min_rev = gv(fw["universe"]["min_revenue_usd"])
    min_other = 5
    dd_cfg = fw["screen"].get("drawdown_list", {})
    dd_min = gv(dd_cfg.get("min_drawdown_from_52w_high", 0.30))   # Ming 2026-10-02
    # List C (Ming 2026-10-02: growth at a sane price, losses allowed with a path). Thresholds PROPOSED.
    GC = {"rev_cagr_3y_min": 0.20, "rev_growth_latest_min": 0.15, "gross_margin_min": 0.40,
          "dilution_max": 0.05, "ev_sales_per_growth_pt_max": 0.5, "nd_ebitda_max": 3.0,
          "current_ratio_min": 1.2, "runway_years_min": 2.0}
    GC.update({k: gv(v) for k, v in fw["screen"].get("growth_list", {}).get("thresholds", {}).items()})
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
    F["rev_prev"] = D.sec_frames_first(T["rev"], f"CY{YEAR-2}")
    F["rev_3y"] = D.sec_frames_first(T["rev"], f"CY{YEAR-4}")
    # Banks/lenders often tag only fee revenue as "revenue"; use net interest income + noninterest income instead.
    for rk, per in (("rev", LATEST_FY), ("rev_prev", f"CY{YEAR-2}"), ("rev_3y", f"CY{YEAR-4}")):
        nii = D.sec_frames_first(["InterestIncomeExpenseNet", "InterestIncomeExpenseAfterProvisionForLoanLoss"], per)
        nonii = D.sec_frames_first(["NoninterestIncome"], per)
        for cik, v in nii.items():
            cur = F[rk].get(cik)
            if cur and cur[3] == "RevenuesNetOfInterestExpense":
                continue
            tot = v[0] + (nonii.get(cik, (0,))[0] or 0)
            if tot > 0 and (not cur or tot > cur[0]):
                F[rk][cik] = (tot, v[1], v[2], "InterestIncomeExpenseNet+NoninterestIncome")
    F["opinc_prev"] = D.sec_frames_first(T["opinc"], f"CY{YEAR-2}")
    F["gp"] = D.sec_frames_first(T["gp"], LATEST_FY)
    F["cogs"] = D.sec_frames_first(T["cogs"], LATEST_FY)
    F["dsh"] = D.sec_frames_first(["WeightedAverageNumberOfDilutedSharesOutstanding"], LATEST_FY, unit="shares")
    F["dsh_prev"] = D.sec_frames_first(["WeightedAverageNumberOfDilutedSharesOutstanding"], f"CY{YEAR-2}", unit="shares")
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
        if m["net_margin"] is not None and m["net_margin"] > 1:   # revenue tag incomplete (e.g. mortgage REITs): treat as NO DATA
            m["net_margin"] = None
        m["debt_to_equity"] = debt / eq if (eq and eq > 0) else None
        m["net_debt_ebitda"] = (debt - cash) / ebitda if (ebitda and ebitda > 0) else None
        ca, cl = g("ca", c), g("cl", c)
        m["current_ratio"] = ca / cl if (ca and cl) else None
        m["fcf_yield"] = fcf / mcap if fcf is not None else None
        m["eps_growth_10yr_total"] = (eps3 / eps_old - 1) if (eps3 and eps_old and eps_old > 0) else None
        m["interest_cover"] = opinc / g("int", c) if (opinc and g("int", c)) else None
        # growth metrics (List C)
        r_prev, r_3y = g("rev_prev", c), g("rev_3y", c)
        m["rev_growth_latest"] = rev / r_prev - 1 if (r_prev and r_prev > 0) else None
        m["rev_cagr_3y"] = (rev / r_3y) ** (1 / 3) - 1 if (r_3y and r_3y > 0 and rev > 0) else None
        gp = g("gp", c)
        if gp is None and g("cogs", c) is not None:
            gp = rev - g("cogs", c)
        m["gross_margin"] = gp / rev if (gp is not None and rev) else None
        d1, d0 = g("dsh", c), g("dsh_prev", c)
        m["dilution_yoy"] = d1 / d0 - 1 if (d1 and d0) else None
        m["ev_sales"] = (mcap if fin else ev) / rev if rev else None
        m["fcf_margin"] = fcf / rev if (fcf is not None and rev) else None
        op_prev = g("opinc_prev", c)
        m["op_margin"] = opinc / rev if (opinc is not None and rev) else None
        m["op_margin_prev"] = op_prev / r_prev if (op_prev is not None and r_prev) else None
        burn = -fcf if (fcf is not None and fcf < 0) else 0
        m["cash_runway_years"] = (cash / burn) if burn else None
        gt = {}
        def gtest(name, val, ok, na=False):
            gt[name] = "N/A" if na else ("NO DATA" if val is None else ("PASS" if ok(val) else "FAIL"))
        gtest("rev_cagr_3y", m["rev_cagr_3y"], lambda v: v >= GC["rev_cagr_3y_min"])
        gtest("rev_growth_latest", m["rev_growth_latest"], lambda v: v >= GC["rev_growth_latest_min"])
        gtest("gross_margin", m["gross_margin"], lambda v: v >= GC["gross_margin_min"], na=fin)
        if fin:   # banks/fintech: moat proxy = ROE improving, not gross margin
            gtest("roe_trend", (roes[-1] - roes[0]) if len(roes) >= 2 else None, lambda v: v > 0)
        gtest("dilution", m["dilution_yoy"], lambda v: v < GC["dilution_max"])
        g_pts = (m["rev_cagr_3y"] or 0) * 100
        gtest("valuation_vs_growth", (m["ev_sales"] / g_pts) if (m["ev_sales"] and g_pts > 0) else None,
              lambda v: v <= GC["ev_sales_per_growth_pt_max"])
        nd = debt - cash
        gtest("balance_sheet", nd if not fin else 0,
              lambda v: v <= 0 or (ebitda and ebitda > 0 and v / ebitda < GC["nd_ebitda_max"]), na=fin)
        gtest("liquidity", m["current_ratio"], lambda v: v > GC["current_ratio_min"], na=fin)
        profitable_now = ni > 0 or (fcf is not None and fcf > 0)
        path = (m["op_margin"] is not None and m["op_margin_prev"] is not None and m["op_margin"] > m["op_margin_prev"]
                and (m["cash_runway_years"] or 0) >= GC["runway_years_min"])
        gt["profit_or_path"] = "PASS" if (profitable_now or path) else "FAIL"
        growth_pass = all(v in ("PASS", "N/A") for v in gt.values())
        rule40 = ((m["rev_growth_latest"] or 0) + (m["fcf_margin"] or 0)) if not fin else (m["rev_growth_latest"] or 0)

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
                        "focus_sector": focus_sector(r), "is_financial": fin,
                        "profitable": (ni > 0) and (fin or (fcf is not None and fcf > 0)),
                        "growth_tests": gt, "passed_growth": growth_pass, "rule_of_40": round(rule40, 4),
                        "profitable_now": profitable_now,
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
    funnel["passed_in_focus_sectors"] = sum(1 for x in passed if x["focus_sector"])
    # List A: value screen, Ming's focus sectors first (Ming 2026-10-02)
    passed.sort(key=lambda x: (focus_rank(x), x["other_tests_passed"], x["metrics"].get("fcf_yield") or 0),
                reverse=True)

    # List B: >= dd_min below 52-week high, balance sheet passes, profitable (NI > 0 and FCF > 0; FCF n/a for financials)
    pool = [x for x in results if x["balance_sheet_ok"] and x["profitable"]]
    for x in passed + pool:
        x.setdefault("drawdown_from_52w_high", None)
    funnel["drawdown_pool_bs_ok_profitable"] = len(pool)
    hl = D.yahoo_52w([x["ticker"] for x in pool])
    dd = []
    for x in pool:
        h = hl.get(x["ticker"])
        if not h or not h["high_52w"] or not h["close"]:
            continue
        d = 1 - h["close"] / h["high_52w"]
        x["high_52w"] = D.fact(round(h["high_52w"], 2), "Yahoo Finance 1y daily highs", h["as_of"])
        x["drawdown_from_52w_high"] = round(d, 4)
        if d >= dd_min:
            dd.append(x)
    funnel["drawdown_ge_threshold"] = len(dd)
    funnel["drawdown_in_focus_sectors"] = sum(1 for x in dd if x["focus_sector"])
    dd.sort(key=lambda x: (focus_rank(x), x["other_tests_passed"], x["drawdown_from_52w_high"]), reverse=True)

    top = passed[: a.top]
    in_a = {x["ticker"] for x in top}
    for x in dd:
        x["also_in_list_a"] = x["ticker"] in in_a
    dd = [x for x in dd if not x["also_in_list_a"]]      # List B = names not already on List A
    top_dd = dd[: a.top_drawdown]
    # List C: high growth + moat proxy at a sane price, not on A or B
    in_ab = in_a | {x["ticker"] for x in top_dd}
    gr = [x for x in results if x["passed_growth"]]
    funnel["passed_growth_screen"] = len(gr)
    gr = [x for x in gr if x["ticker"] not in in_ab]
    funnel["growth_in_focus_sectors"] = sum(1 for x in gr if x["focus_sector"])
    gr.sort(key=lambda x: (focus_rank(x), x["rule_of_40"]), reverse=True)
    top_gr = gr[: a.top_growth]
    seen = set()
    for x in top + top_dd + top_gr:
        if x["ticker"] not in seen:
            seen.add(x["ticker"]); crosscheck(x, fw, min_adv)
    funnel["growth_top_crosschecked"] = len(top_gr)
    funnel["growth_top_quality_pass"] = sum(1 for x in top_gr if x["quality_gate"] == "PASS")
    funnel["top_crosschecked"] = len(top)
    funnel["top_quality_pass"] = sum(1 for x in top if x["quality_gate"] == "PASS")
    funnel["drawdown_top_crosschecked"] = len(top_dd)
    funnel["drawdown_top_quality_pass"] = sum(1 for x in top_dd if x["quality_gate"] == "PASS")

    out = {"screen_date": D.TODAY, "framework_version": fw["_meta"]["version"], "profile": prof,
           "rule": f"all balance-sheet tests pass (sector exceptions) + >= {min_other} of 9 other tests",
           "focus_sectors": {"order": FOCUS_ORDER, "mapping": "Nasdaq sector/industry + seed tickers (PROPOSED); verify vs 10-K SIC in Round 1"},
           "funnel": funnel,
           "shortlist_candidates": top,
           "drawdown_rule": f">= {dd_min:.0%} below 52-week high + balance-sheet tests pass + profitable (NI>0, FCF>0 ex-financials); focus sectors first (AI/data centre > robotics > tech > finance), then value tests passed, then drawdown; excludes names already on List A",
           "drawdown_candidates": top_dd,
           "growth_rule": ("PROPOSED: 3-yr revenue CAGR >= {rev_cagr_3y_min:.0%} and latest year >= {rev_growth_latest_min:.0%}; "
                           "gross margin >= {gross_margin_min:.0%} (financials: ROE improving); diluted shares < +{dilution_max:.0%}/yr; "
                           "EV/sales <= {ev_sales_per_growth_pt_max} x growth points; net cash or net debt/EBITDA < {nd_ebitda_max}x; "
                           "current ratio > {current_ratio_min}; profitable OR (operating margin improving and cash runway >= {runway_years_min} yrs). "
                           "Focus sectors first, then rule of 40; excludes names on A or B.").format(**GC),
           "growth_candidates": top_gr,
           "also_passed": [{"ticker": x["ticker"], "focus_sector": x["focus_sector"], "other_tests_passed": x["other_tests_passed"],
                            "fcf_yield": x["metrics"].get("fcf_yield")} for x in passed[a.top:]],
           "also_drawdown": [{"ticker": x["ticker"], "focus_sector": x["focus_sector"],
                              "drawdown_from_52w_high": x["drawdown_from_52w_high"]} for x in dd[a.top_drawdown:]],
           "sources": ["Nasdaq.com screener (universe, price, mcap, sector/industry)", "SEC EDGAR XBRL frames (fundamentals)",
                       "Yahoo Finance (52-week high, cross-check, ADV, consensus)"]}
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    print(json.dumps(funnel, indent=1))
    for lbl, lst in (("A value (focus first)", top), ("B drawdown >=%d%%" % round(dd_min * 100), top_dd),
                     ("C growth + moat (PROPOSED)", top_gr)):
        print("--", lbl)
        for x in lst:
            print(f'{x["ticker"]:6} {str(x["focus_sector"] or "-"):13} {x["sector"][:16]:16} pass={x["other_tests_passed"]} g3y={x["metrics"].get("rev_cagr_3y")} r40={x.get("rule_of_40")} '
                  f'dd={x.get("drawdown_from_52w_high")} fcfy={x["metrics"].get("fcf_yield")} gate={x["quality_gate"]} {x["flags"][:1]}')


def crosscheck(x, fw, min_adv):
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


if __name__ == "__main__":
    main()
