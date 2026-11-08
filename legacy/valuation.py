"""
Valuation models:
  1. DCF (Discounted Cash Flow) — primary method
  2. Comparable valuation (P/E and EV/EBITDA multiples)
  3. Quick intrinsic value (Graham formula + EPS x reasonable P/E)
"""
import math


def dcf_valuation(
    current_fcf: float,
    growth_rate_1_5: float,   # annual growth % for years 1-5
    growth_rate_6_10: float,  # annual growth % for years 6-10
    wacc: float,              # discount rate %
    terminal_growth: float,   # perpetual growth rate %
    shares_outstanding: float,
    net_debt: float = 0,      # total_debt - cash (can be negative = net cash)
) -> dict:
    """
    Two-stage DCF model.
    Returns intrinsic value per share and sensitivity table.
    """
    if shares_outstanding <= 0:
        return {"error": "Shares outstanding must be > 0"}

    r = wacc / 100
    g = terminal_growth / 100
    g1 = growth_rate_1_5 / 100
    g2 = growth_rate_6_10 / 100

    if r <= g:
        return {"error": "WACC must be greater than terminal growth rate"}

    # Project FCF for 10 years
    fcf_projections = []
    fcf = current_fcf

    pv_fcfs = 0
    for year in range(1, 11):
        growth = g1 if year <= 5 else g2
        fcf = fcf * (1 + growth)
        pv = fcf / ((1 + r) ** year)
        pv_fcfs += pv
        fcf_projections.append({
            "year": year,
            "fcf": round(fcf),
            "pv": round(pv),
        })

    # Terminal value (Gordon Growth Model)
    terminal_fcf = fcf * (1 + g)
    terminal_value = terminal_fcf / (r - g)
    pv_terminal = terminal_value / ((1 + r) ** 10)

    # Enterprise value
    enterprise_value = pv_fcfs + pv_terminal

    # Equity value = EV - net debt
    equity_value = enterprise_value - net_debt

    # Intrinsic value per share
    intrinsic_value = equity_value / shares_outstanding

    # Sensitivity table: wacc ± 2%, terminal growth ± 1%
    sensitivity = []
    for dw in [-2, -1, 0, 1, 2]:
        row = {"wacc": round(wacc + dw, 1), "values": {}}
        for dg in [-1, -0.5, 0, 0.5, 1]:
            alt_r = (wacc + dw) / 100
            alt_g = (terminal_growth + dg) / 100
            if alt_r <= alt_g:
                row["values"][terminal_growth + dg] = "N/A"
                continue
            pv_f = sum(
                (current_fcf * ((1 + g1) ** min(y, 5)) * ((1 + g2) ** max(0, y - 5))) /
                ((1 + alt_r) ** y)
                for y in range(1, 11)
            )
            # recalculate year-10 FCF
            fcf10 = current_fcf
            for y in range(1, 11):
                g_yr = g1 if y <= 5 else g2
                fcf10 *= (1 + g_yr)
            tv = (fcf10 * (1 + alt_g)) / (alt_r - alt_g)
            pv_tv = tv / ((1 + alt_r) ** 10)
            ev_alt = pv_f + pv_tv
            eq_alt = (ev_alt - net_debt) / shares_outstanding
            row["values"][round(terminal_growth + dg, 1)] = round(eq_alt, 2)
        sensitivity.append(row)

    return {
        "intrinsic_value": round(intrinsic_value, 2),
        "enterprise_value": round(enterprise_value),
        "equity_value": round(equity_value),
        "pv_fcfs": round(pv_fcfs),
        "pv_terminal": round(pv_terminal),
        "terminal_value": round(terminal_value),
        "tv_pct_of_ev": round(pv_terminal / enterprise_value * 100, 1) if enterprise_value else 0,
        "projections": fcf_projections,
        "sensitivity": sensitivity,
        "inputs": {
            "current_fcf": current_fcf,
            "growth_1_5": growth_rate_1_5,
            "growth_6_10": growth_rate_6_10,
            "wacc": wacc,
            "terminal_growth": terminal_growth,
            "net_debt": net_debt,
            "shares": shares_outstanding,
        }
    }


def graham_formula(eps: float, growth_rate: float, aaa_yield: float = 4.4) -> float:
    """
    Benjamin Graham's revised intrinsic value formula:
    V = EPS * (8.5 + 2g) * 4.4 / Y
    where Y = current AAA corporate bond yield (default 4.4%)
    """
    if eps <= 0:
        return 0
    return round(eps * (8.5 + 2 * growth_rate) * 4.4 / aaa_yield, 2)


def quick_valuation(eps: float, reasonable_pe: float = 15, growth_rate: float = 0) -> dict:
    """
    Quick intrinsic value:
    - Graham formula
    - EPS × reasonable P/E
    - Peter Lynch: EPS × (1 + growth_rate)^5 × 15
    """
    graham = graham_formula(eps, growth_rate)
    pe_method = round(eps * reasonable_pe, 2) if eps > 0 else 0

    # Peter Lynch 5-year forward
    forward_eps = eps * ((1 + growth_rate / 100) ** 5)
    lynch_method = round(forward_eps * 15, 2) if eps > 0 else 0

    return {
        "graham": graham,
        "pe_method": pe_method,
        "lynch_5yr": lynch_method,
        "average": round((graham + pe_method + lynch_method) / 3, 2) if eps > 0 else 0,
    }


def margin_of_safety(current_price: float, intrinsic_value: float) -> dict:
    """Calculate margin of safety and buy/sell signal."""
    if intrinsic_value <= 0 or current_price <= 0:
        return {"margin_of_safety": None, "signal": "unknown", "upside": None}

    mos = (intrinsic_value - current_price) / intrinsic_value * 100
    upside = (intrinsic_value - current_price) / current_price * 100

    if mos >= 30:
        signal = "strong_buy"
    elif mos >= 15:
        signal = "buy"
    elif mos >= 0:
        signal = "fairly_valued"
    elif mos >= -20:
        signal = "overvalued"
    else:
        signal = "significantly_overvalued"

    return {
        "margin_of_safety": round(mos, 1),
        "upside_pct": round(upside, 1),
        "signal": signal,
        "buy_price": round(intrinsic_value * 0.7, 2),   # 30% MOS
        "fair_value": round(intrinsic_value, 2),
        "current_price": current_price,
    }


def comparable_valuation(
    eps: float,
    ebitda: float,
    sector_pe: float,
    sector_ev_ebitda: float,
    net_debt: float,
    shares_outstanding: float
) -> dict:
    """
    Value based on industry multiples.
    """
    pe_value = round(eps * sector_pe, 2) if eps > 0 else 0

    ev_value = 0
    if ebitda > 0 and shares_outstanding > 0:
        ev = ebitda * sector_ev_ebitda
        equity = ev - net_debt
        ev_value = round(equity / shares_outstanding, 2)

    average = 0
    count = sum(1 for v in [pe_value, ev_value] if v > 0)
    if count:
        average = round((pe_value + ev_value) / count, 2)

    return {
        "pe_based": pe_value,
        "ev_ebitda_based": ev_value,
        "average": average,
        "sector_pe_used": sector_pe,
        "sector_ev_ebitda_used": sector_ev_ebitda,
    }
