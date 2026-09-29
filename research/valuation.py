"""
valuation.py — triangulated intrinsic value per framework.json (Phase 3 methods + Phase 4 MoS).

Usage:
  python valuation.py TICKER --framework framework.json --company-type average_quality \
         --peers MSFT,ORCL,... [--asset-heavy] [--growth 0.06] [--out val_TICKER.json]

Methods (framework p.15-19): DCF-FCFF, EPV, relative (peer median EV/EBITDA & P/E),
asset floor (tangible book / NCAV), DDM for dividend payers in financials/utilities.
Every input is recorded with source + as_of. Assumptions that are judgement calls
are listed under "assumptions" so the deep dive can show and challenge them.
"""
import argparse, json, statistics as st, datetime as dt
import vi_data as D

DEFAULT_ERP = 0.046   # PROPOSED fallback if Damodaran implied ERP unavailable (flagged in output)


def gv(x):
    return x["value"] if isinstance(x, dict) else x


def series(cf, key, instant=False, unit="USD"):
    from screen import T, EPS_TAGS
    tags = EPS_TAGS if key == "eps" else T[key]
    s, tag = D.annual_series(cf, tags, unit="USD-per-shares" if key == "eps" else unit, instant=instant)
    return s, tag


def last(s, n=1):
    v = list(s.values())
    return v[-n:] if n > 1 else (v[-1] if v else None)


def cagr(s, years=5):
    v = list(s.items())
    if len(v) < years + 1:
        years = len(v) - 1
    if years < 2:
        return None
    a, b = v[-years - 1][1], v[-1][1]
    return (b / a) ** (1 / years) - 1 if a and a > 0 and b > 0 else None


def dcf_fcff(fcff0, g1, g_term, wacc, years=10, fade_from=5):
    """Stage-1 growth g1 for `fade_from` yrs, linear fade to g_term by `years`, Gordon TV."""
    pv, f, gs = 0.0, fcff0, []
    for t in range(1, years + 1):
        g = g1 if t <= fade_from else g1 + (g_term - g1) * (t - fade_from) / (years - fade_from)
        f *= 1 + g
        gs.append(round(g, 4))
        pv += f / (1 + wacc) ** t
    tv = f * (1 + g_term) / (wacc - g_term)
    pv_tv = tv / (1 + wacc) ** years
    return pv + pv_tv, pv_tv / (pv + pv_tv), gs


def run(ticker, fw, company_type="average_quality", peers=None, asset_heavy=False,
        growth_override=None, extra_risk=False):
    ticker = ticker.upper()
    sec = D.sec_tickers()[ticker]
    cf = D.sec_companyfacts(sec["cik"])
    y = D.yahoo_snapshot(ticker)
    A, src = {}, {}
    notes, flags = [], []

    rev, _ = series(cf, "rev"); ni, _ = series(cf, "ni"); opinc, _ = series(cf, "opinc")
    da, _ = series(cf, "da"); ocf, _ = series(cf, "ocf"); capex, _ = series(cf, "capex")
    intr, _ = series(cf, "int"); tax, _ = series(cf, "tax"); pretax, _ = series(cf, "pretax")
    eq, _ = series(cf, "eq", True); ltd, _ = series(cf, "ltd", True); std_, _ = series(cf, "std", True)
    stb, _ = series(cf, "stb", True); cash, _ = series(cf, "cash", True); sti, _ = series(cf, "sti", True)
    ca, _ = series(cf, "ca", True); liab, _ = series(cf, "liab", True)
    gw, _ = series(cf, "gw", True); intang, _ = series(cf, "intang", True)

    # latest balance sheet from quarterly instants (fresher than FY)
    def q_last(key):
        from screen import T
        s, _ = D.quarterly_series(cf, T[key], instant=True)
        return (list(s.items())[-1] if s else (None, None))
    bs_date, eq_q = q_last("eq")
    debt = sum(v for v in (q_last("ltd")[1], q_last("std")[1], q_last("stb")[1]) if v)
    cash_q = (q_last("cash")[1] or 0) + (q_last("sti")[1] or 0)
    ca_q, liab_q = q_last("ca")[1], q_last("liab")[1]
    gw_q, int_q = q_last("gw")[1] or 0, q_last("intang")[1] or 0

    price = y.get("price"); shares = y.get("sharesOutstanding") or (y.get("market_cap") / price if price else None)
    mcap = price * shares if price and shares else None
    rf = D.fred_latest("DGS10")
    beta = y.get("beta")
    beta_used = min(max(beta, 0.6), 2.0) if beta else 1.0
    if not beta:
        flags.append("beta missing from Yahoo -> 1.0 used (check Damodaran industry beta)")
    erp = DEFAULT_ERP
    flags.append(f"ERP {erp:.1%} = PROPOSED default (Damodaran implied ERP; verify monthly)")
    ke = rf["value"] + beta_used * erp
    t_rates = [tax[k] / pretax[k] for k in list(pretax)[-3:] if k in tax and pretax[k] > 0]
    t = min(max(st.mean(t_rates), 0.10), 0.30) if t_rates else 0.21
    kd_pre = (last(intr) / debt) if (intr and debt) else 0.06
    kd_pre = min(max(kd_pre, 0.03), 0.10)
    E, Dv = mcap or 0, debt
    wacc = (E / (E + Dv)) * ke + (Dv / (E + Dv)) * kd_pre * (1 - t) if (E + Dv) else ke
    wacc = max(wacc, rf["value"] + 0.02)

    # normalized FCFF (3-yr avg): OCF + after-tax interest - capex
    yrs = list(ocf)[-3:]
    fcffs = [ocf[k] + (intr.get(k, 0) * (1 - t)) - capex.get(k, 0) for k in yrs]
    fcff0 = st.mean(fcffs) if fcffs else None
    g_hist = cagr(rev, 5)
    g1 = growth_override if growth_override is not None else (min(max(g_hist or 0.03, 0.0), 0.12))
    if (g_hist or 0) > 0.15:
        flags.append(f"historical revenue CAGR {g_hist:.1%} > 15%: capped at 12% stage-1 (framework p.12 skepticism)")
    g_term = min(0.025, rf["value"] - 0.01) if rf["value"] > 0.035 else 0.02
    g_term = min(g_term, gv(fw["valuation"]["dcf_rules"]["terminal_growth_max"]) if "dcf_rules" in fw["valuation"] else 0.03)

    methods = []
    if fcff0 and fcff0 > 0 and shares:
        ev, tv_share, gs = dcf_fcff(fcff0, g1, g_term, wacc)
        eqv = ev - debt + cash_q
        grid = []
        for dw in (-0.01, -0.005, 0, 0.005, 0.01):
            row = []
            for dg in (-0.005, 0, 0.005):
                ev2, _, _ = dcf_fcff(fcff0, g1, g_term + dg, wacc + dw)
                row.append(round((ev2 - debt + cash_q) / shares, 2))
            grid.append(row)
        methods.append({"name": "dcf_fcff", "value_per_share": round(eqv / shares, 2),
                        "key_inputs": {"fcff0_3yr_avg": round(fcff0), "stage1_growth": round(g1, 4),
                                       "terminal_growth": round(g_term, 4), "wacc": round(wacc, 4),
                                       "terminal_value_share": round(tv_share, 3), "growth_path": gs},
                        "sensitivity": {"wacc_deltas": [-0.01, -0.005, 0, 0.005, 0.01],
                                        "g_deltas": [-0.005, 0, 0.005], "grid": grid},
                        "source": "SEC XBRL 10-K (OCF, capex, interest); FRED DGS10; Yahoo beta"})
        if tv_share > 0.8:
            flags.append(f"DCF terminal value = {tv_share:.0%} of EV (>80%): valuation risk (framework p.16)")
    else:
        notes.append("DCF skipped: normalized FCFF <= 0 or missing")

    # EPV: normalized operating margin (5y avg) x latest revenue x (1-t) / WACC
    ks = [k for k in list(opinc)[-5:] if k in rev and rev[k]]
    if ks and shares:
        m_norm = st.mean(opinc[k] / rev[k] for k in ks)
        nopat = m_norm * last(rev) * (1 - t)
        epv_ev = nopat / wacc
        methods.append({"name": "epv", "value_per_share": round((epv_ev - debt + cash_q) / shares, 2),
                        "key_inputs": {"normalized_op_margin_5y": round(m_norm, 4), "latest_revenue": last(rev),
                                       "tax_rate": round(t, 3), "wacc": round(wacc, 4)},
                        "source": "SEC XBRL 10-K", "note": "assumes D&A ~= maintenance capex"})

    # Relative: peer median multiples (Yahoo)
    if peers:
        pe_l, eve_l, used = [], [], []
        import yfinance as yf
        for p in peers:
            try:
                i = yf.Ticker(p).info
                if i.get("trailingPE") and 0 < i["trailingPE"] < 80: pe_l.append(i["trailingPE"])
                if i.get("enterpriseToEbitda") and 0 < i["enterpriseToEbitda"] < 50: eve_l.append(i["enterpriseToEbitda"])
                used.append(p)
            except Exception:
                pass
        vals = []
        eps_ttm = y.get("trailingEps")
        if pe_l and eps_ttm and eps_ttm > 0:
            vals.append(("P/E", st.median(pe_l) * eps_ttm))
        ebitda = (last(opinc) or 0) + (last(da) or 0)
        if eve_l and ebitda > 0 and shares:
            vals.append(("EV/EBITDA", (st.median(eve_l) * ebitda - debt + cash_q) / shares))
        if vals:
            methods.append({"name": "multiples", "value_per_share": round(st.mean(v for _, v in vals), 2),
                            "key_inputs": {"peers": used, "median_pe": st.median(pe_l) if pe_l else None,
                                           "median_ev_ebitda": st.median(eve_l) if eve_l else None,
                                           "components": {k: round(v, 2) for k, v in vals}},
                            "source": "Yahoo Finance peer multiples (unadjusted medians - adjust in narrative)"})
    else:
        notes.append("Multiples skipped: no peers supplied (deep dive must choose 5-15 peers)")

    # Asset floor
    if shares and eq_q:
        tbv = (eq_q - gw_q - int_q) / shares
        ncav = ((ca_q or 0) - (liab_q or 0)) / shares
        methods.append({"name": "asset_floor", "value_per_share": round(max(tbv, ncav), 2),
                        "key_inputs": {"tangible_book_ps": round(tbv, 2), "ncav_ps": round(ncav, 2),
                                       "balance_sheet_date": bs_date},
                        "source": "SEC XBRL 10-Q/10-K balance sheet", "weighted": asset_heavy})

    # weights (framework p.19 midpoints; asset floor weighted only if asset-heavy)
    wmap = ({"dcf_fcff": .45, "multiples": .25, "epv": .15, "asset_floor": .15} if asset_heavy
            else {"dcf_fcff": .50, "multiples": .30, "epv": .20, "asset_floor": 0})
    used = [m for m in methods if wmap.get(m["name"], 0) > 0]
    tw = sum(wmap[m["name"]] for m in used)
    for m in methods:
        m["weight"] = round(wmap.get(m["name"], 0) / tw, 3) if tw and m in used else 0
    base = sum(m["value_per_share"] * m["weight"] for m in used) if used else None
    if len(used) < gv(fw["valuation"]["min_methods"]) and not asset_heavy:
        flags.append(f"only {len(used)} weighted methods (<3 required) - add peers or asset-heavy view")
    lows = [m["value_per_share"] for m in used]
    highs = list(lows)
    dcf = next((m for m in methods if m["name"] == "dcf_fcff"), None)
    if dcf:
        g = dcf["sensitivity"]["grid"]; lows.append(g[-1][0]); highs.append(g[0][-1])
    low, high = (min(lows), max(highs)) if lows else (None, None)
    spread = (high - low) / base if base else None
    if spread and spread > 1.0:
        flags.append(f"methods diverge widely (range = {spread:.0%} of base): investigate assumptions (framework p.19)")

    mos_req = gv(fw["margin_of_safety"]["required"][company_type]) if "required" in fw["margin_of_safety"] \
        else fw["margin_of_safety"]["by_company_type"][company_type]["use"]
    if extra_risk:
        mos_req += 0.10
    entry = base * (1 - mos_req) if base else None
    mos_now = (base - price) / base if base and price else None

    return {"ticker": ticker, "company": sec["name"], "as_of": D.TODAY,
            "price": D.fact(price, "Yahoo Finance"), "shares": D.fact(shares, "Yahoo Finance sharesOutstanding"),
            "risk_free": rf, "beta": D.fact(beta, "Yahoo Finance"), "erp": D.fact(erp, "PROPOSED default"),
            "cost_of_equity": round(ke, 4), "wacc": round(wacc, 4), "tax_rate": round(t, 3),
            "net_debt": D.fact(debt - cash_q, "SEC XBRL latest balance sheet", bs_date),
            "revenue_cagr_5y": g_hist, "methods": methods,
            "low": round(low, 2) if low else None, "base": round(base, 2) if base else None,
            "high": round(high, 2) if high else None,
            "company_type": company_type, "required_mos": round(mos_req, 3),
            "entry_price_target": round(entry, 2) if entry else None,
            "current_mos": round(mos_now, 4) if mos_now is not None else None,
            "flags": flags, "notes": notes}


def valuation_score(v):
    """Conviction component 'valuation_mos' 0-5 (PROPOSED mapping, approved via framework weights)."""
    if v["current_mos"] is None:
        return 0, "no valuation"
    gap = v["current_mos"] - v["required_mos"]
    s = 5 if gap >= 0.10 else 4 if gap >= 0 else 3 if gap >= -0.10 else 2 if v["current_mos"] >= 0 else 1 if v["current_mos"] >= -0.20 else 0
    if any("diverge" in f for f in v["flags"]):
        s = max(s - 1, 0)
    return s, f"MoS {v['current_mos']:.0%} vs required {v['required_mos']:.0%}"


def conviction(components, fw):
    """components: {name: (score_0_5, reason)} -> total 0-100"""
    w = fw["decision"]["conviction_score"]["components"]
    w = {k: (gv(x) if not isinstance(x, dict) else x.get("weight", 0)) for k, x in w.items()}
    return round(sum(w[k] * components[k][0] / 5 for k in w if k in components))


def verdict(score, v, catalyst_dated, red_flag):
    req, mos = v["required_mos"], v["current_mos"] or -1
    if red_flag or score < 50:
        return "PASS"
    if score >= 70 and mos >= req and catalyst_dated:
        return "BUY"
    return "WATCH"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker"); ap.add_argument("--framework", default="framework.json")
    ap.add_argument("--company-type", default="average_quality")
    ap.add_argument("--peers", default=""); ap.add_argument("--asset-heavy", action="store_true")
    ap.add_argument("--extra-risk", action="store_true"); ap.add_argument("--growth", type=float)
    ap.add_argument("--out")
    a = ap.parse_args()
    fw = json.load(open(a.framework))
    v = run(a.ticker, fw, a.company_type, [p for p in a.peers.split(",") if p], a.asset_heavy, a.growth, a.extra_risk)
    v["valuation_score"] = valuation_score(v)
    json.dump(v, open(a.out or f"val_{a.ticker.upper()}.json", "w"), indent=1, default=str)
    print(json.dumps({k: v[k] for k in ("ticker", "price", "wacc", "low", "base", "high", "required_mos",
                                        "entry_price_target", "current_mos", "flags", "notes")}, indent=1, default=str))
    for m in v["methods"]:
        print(m["name"], m["value_per_share"], "w=", m["weight"])
