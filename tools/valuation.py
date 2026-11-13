"""Deterministic valuation engine (ValueInvestor 2.0 revisions, 9 Nov 2026).

AI writes assumptions; this code does every calculation. Same inputs always give the same outputs.

Assumptions file (inside a thesis, refresh or second_opinion inbox file, key "assumptions"):
{
  "revenue0": 245e9, "shares0": 7.43e9, "net_debt": -60e9,      # optional; taken from the weekly research data if missing
  "profile": {"model": "standard", "cyclical": false, "nd_ebitda": 0.4, "pre_profit": false,
              "moat": "widening|stable|narrowing|unproven", "roic_10y_above_15": true, "stable_revenue": true},
  "scenarios": {
    "bear": {"growth": [0.08, 0.07, 0.06, 0.05, 0.05], "margin": [0.42, 0.40], "reinvest_pct": 0.10, "tax": 0.18,
             "share_change": -0.005, "terminal_growth": 0.025, "exit_multiple": null, "grade": "E", "why": "..."},
    "base": {...}, "bull": {...}},
  "weights": {"bear": 0.25, "base": 0.5, "bull": 0.25}
}
growth: 1-10 yearly revenue growth rates; fewer than 10 fade linearly to terminal_growth by year 10.
margin: [start, end] operating margin, moved linearly over 10 years (or a list of 10).
reinvest_pct: capex plus working capital less depreciation, as a share of revenue.
exit_multiple: EV/EBIT at year 10; when null the terminal value uses terminal_growth.
"""
import hashlib, json

YEARS = 10
RETURN_YEARS = 5
DEFAULT_WEIGHTS = {"bear": 0.25, "base": 0.50, "bull": 0.25}
GROWTH_HURDLE = 0.15          # growth buy zone: base case returns 15%+ a year over 5 years
BEAR_LOSS_CAP = 0.35          # ... and the bear case loses no more than 35% from the buy price
NON_DCF_MODELS = {"bank", "insurer", "reit", "commodity", "pre_profit"}
MODEL_KEYWORDS = [("bank", ["bank", "savings institution", "major banks", "regional banks", "commercial bank"]),
                  ("insurer", ["insurance", "reinsurance"]),
                  ("reit", ["reit", "real estate investment trust"]),
                  ("commodity", ["oil & gas", "oil and gas", "integrated oil", "metal mining", "precious metals",
                                 "coal", "steel", "copper", "gold", "uranium", "aluminum"])]


def model_type(sector=None, industry=None, pre_profit=False):
    s = f"{sector or ''} {industry or ''}".lower()
    for model, words in MODEL_KEYWORDS:
        if any(w in s for w in words):
            return model
    return "pre_profit" if pre_profit else "standard"


def discount_rate(p):
    """Fixed method, not chosen per thesis: 10% base, +1pt cyclical or net debt/EBITDA above 3x, +2pt pre-profit."""
    r = 0.10
    if p.get("cyclical") or (p.get("nd_ebitda") or 0) > 3:
        r += 0.01
    if p.get("pre_profit"):
        r += 0.02
    return r


def margin_of_safety(p):
    """20% start; +10 cyclical; +5 leverage above 2.5x; +5 moat narrowing or unproven; -5 proven compounder. 15%-40%."""
    m = 0.20
    if p.get("cyclical"):
        m += 0.10
    if (p.get("nd_ebitda") or 0) > 2.5:
        m += 0.05
    if (p.get("moat") or "unproven").lower() in ("narrowing", "unproven"):
        m += 0.05
    if p.get("roic_10y_above_15") and p.get("stable_revenue"):
        m -= 0.05
    return round(min(max(m, 0.15), 0.40), 4)


def _path(v, start_default=None, n=YEARS, end=None):
    """Expand a growth list (fading to end) or a [start, end] margin pair into n yearly values."""
    if v is None:
        v = [start_default]
    v = [float(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
    if len(v) >= n:
        return v[:n]
    if end is not None:                       # growth: hold the given years, then fade to `end`
        last, k = v[-1], len(v)
        return v + [last + (end - last) * (i - k + 1) / (n - k) for i in range(k, n)]
    if len(v) == 2:                           # margin: straight line from start to end
        return [v[0] + (v[1] - v[0]) * i / (n - 1) for i in range(n)]
    return v + [v[-1]] * (n - len(v))


def scenario(s, ctx, dr):
    """Value one scenario. Returns value per share today, value per share at year 5 and the year-5 return from price."""
    tg = float(s.get("terminal_growth", 0.025))
    if dr <= tg:
        raise ValueError("discount rate must exceed terminal growth")
    g = _path(s.get("growth"), 0.05, end=tg)
    m = _path(s.get("margin"), 0.15)
    tax, rein = float(s.get("tax", 0.21)), float(s.get("reinvest_pct", 0.05))
    shocks = s.get("revenue_shocks") or []
    rev, flows, ebits = float(ctx["revenue0"]), [], []
    for t in range(YEARS):
        rev *= 1 + g[t]
        lvl = 1.0
        for sh in shocks:                      # level shifts: pct of revenue lost or gained from `year`, phased in
            y0, ph = int(sh.get("year", 1)), max(1, int(sh.get("phase_years", 1)))
            if t + 1 >= y0:
                lvl *= 1 + float(sh.get("pct", 0)) * min(1.0, (t + 2 - y0) / ph)
        ebit = rev * lvl * m[t]
        ebits.append(ebit)
        flows.append(ebit * (1 - tax) - rev * lvl * rein)
    if s.get("exit_multiple"):
        tv = ebits[-1] * float(s["exit_multiple"])
    else:
        tv = flows[-1] * (1 + tg) / (dr - tg)
    pv = sum(f / (1 + dr) ** (t + 1) for t, f in enumerate(flows)) + tv / (1 + dr) ** YEARS
    sc = float(s.get("share_change", 0.0))
    shares5 = ctx["shares0"] * (1 + sc) ** RETURN_YEARS
    value_today = (pv - ctx["net_debt"]) / shares5
    # year-5 view: value of the remaining flows at year 5 plus cash built in years 1-5
    ev5 = sum(f / (1 + dr) ** (t - RETURN_YEARS + 1) for t, f in enumerate(flows) if t >= RETURN_YEARS) \
        + tv / (1 + dr) ** (YEARS - RETURN_YEARS)
    cash5 = sum(flows[:RETURN_YEARS])
    value5 = (ev5 + cash5 - ctx["net_debt"]) / shares5
    price = ctx.get("price")
    ret = (value5 / price) ** (1 / RETURN_YEARS) - 1 if price and value5 > 0 else None
    return {"value": round(value_today, 2), "value_y5": round(value5, 2), "return_5y": round(ret, 4) if ret is not None else None,
            "growth_path": [round(x, 4) for x in g], "margin_path": [round(x, 4) for x in m], "discount_rate": dr}


def reverse_dcf(base, ctx, dr):
    """Uniform revenue growth for years 1-5 (then fading) that today's price implies, at base-case margins."""
    price = ctx.get("price")
    if not price:
        return None
    def v(gr):
        s = dict(base, growth=[gr] * 5, exit_multiple=None)
        return scenario(s, dict(ctx, price=None), dr)["value"]
    lo, hi = -0.30, 0.80
    try:
        if not (v(lo) <= price <= v(hi)):
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if v(mid) < price else (lo, mid)
    except (ValueError, ZeroDivisionError):
        return None
    return round((lo + hi) / 2, 4)


def context_from(assumptions, research=None, price=None):
    """Fill revenue0, shares0, net_debt and price from the assumptions file, else from the weekly research data."""
    r = research or {}
    hist = r.get("history") or []
    last_rev = next((h.get("revenue") for h in reversed(hist) if h.get("revenue")), None)
    nd = ((r.get("valuation") or {}).get("assumptions") or {}).get("Net debt")
    ctx = {"revenue0": assumptions.get("revenue0") or last_rev, "shares0": assumptions.get("shares0") or r.get("shares"),
           "net_debt": assumptions.get("net_debt") if assumptions.get("net_debt") is not None else (nd or 0),
           "price": price or r.get("price")}
    missing = [k for k in ("revenue0", "shares0") if not ctx[k]]
    if missing:
        raise ValueError("engine needs " + ", ".join(missing))
    ctx = {k: (float(v) if v is not None else None) for k, v in ctx.items()}
    return ctx


def fingerprint(assumptions, ctx):
    raw = json.dumps({"a": assumptions, "c": ctx}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def run(assumptions, research=None, price=None, style="value", sector=None, industry=None):
    """Value all scenarios and derive the entry target or growth buy zone. Raises ValueError on bad input."""
    p = dict(assumptions.get("profile") or {})
    model = p.get("model") or model_type(sector or (research or {}).get("sector"),
                                         industry or (research or {}).get("industry"), p.get("pre_profit"))
    ctx = context_from(assumptions, research, price)
    out = {"engine": "v1", "inputs_hash": fingerprint(assumptions, ctx), "context": ctx, "model": model,
           "dcf_valid": model not in NON_DCF_MODELS, "profile": p}
    if not out["dcf_valid"]:
        out["note"] = (f"DCF not valid for a {model.replace('_', '-')} business; use peer multiples, the reverse case"
                       + (" and the TAM case" if model == "pre_profit" else "") + ".")
        return out
    dr = discount_rate(p)
    sc = assumptions.get("scenarios") or {}
    if "base" not in sc:
        raise ValueError("assumptions need at least a base scenario")
    res = {k: scenario(v, ctx, dr) for k, v in sc.items() if k in ("bear", "base", "bull")}
    w = dict(DEFAULT_WEIGHTS, **(assumptions.get("weights") or {}))
    used = {k: w[k] for k in res}
    tw = sum(used.values()) or 1
    pw = sum(res[k]["value"] * used[k] for k in res) / tw
    mos = margin_of_safety(p)
    base = res["base"]["value"]
    out.update(discount_rate=dr, scenarios=res, weights=used, prob_weighted=round(pw, 2), margin_of_safety=mos,
               iv_low=res.get("bear", res["base"])["value"], iv_base=base, iv_high=res.get("bull", res["base"])["value"],
               entry_target=round(base * (1 - mos), 2) if base > 0 else None,
               reverse_dcf_growth=reverse_dcf(sc["base"], ctx, dr))
    # growth buy zone: base case returns 15%+ a year and the bear case loses no more than 35%
    v5 = res["base"]["value_y5"]
    zone_base = v5 / (1 + GROWTH_HURDLE) ** RETURN_YEARS if v5 > 0 else None
    bear = res.get("bear", {}).get("value")
    zone_bear = bear / (1 - BEAR_LOSS_CAP) if bear and bear > 0 else None
    zones = [z for z in (zone_base, zone_bear) if z]
    out["growth_zone"] = round(min(zones), 2) if zones else None
    out["growth_zone_why"] = ("bear-case loss cap" if zone_bear and zone_base and zone_bear < zone_base
                              else "base case 15% a year")
    if ctx.get("price") and bear:
        out["bear_loss_from_price"] = round(bear / ctx["price"] - 1, 4)
    out["buy_level"] = out["growth_zone"] if (style or "value") == "growth" else out["entry_target"]
    return out


def attribute_gap(a, b, research=None, price=None):
    """Explain why two analysts' base values differ: swap each input of B's base case into A's, one at a time.
    Returns [{input, effect, share}] sorted by size, plus both base values. Values are never averaged."""
    ra, rb = run(a, research, price), run(b, research, price)
    if not (ra.get("dcf_valid") and rb.get("dcf_valid")):
        return {"a": ra.get("iv_base"), "b": rb.get("iv_base"), "parts": []}
    ctx, dr = ra["context"], ra["discount_rate"]
    sa, sb = dict(a["scenarios"]["base"]), dict(b["scenarios"]["base"])
    base_a = scenario(sa, ctx, dr)["value"]
    parts = []
    for key in ("growth", "margin", "reinvest_pct", "tax", "share_change", "terminal_growth", "exit_multiple"):
        if sa.get(key) == sb.get(key):
            continue
        s = dict(sa, **{key: sb.get(key)})
        try:
            parts.append({"input": key, "effect": round(scenario(s, ctx, dr)["value"] - base_a, 2)})
        except (ValueError, ZeroDivisionError):
            continue
    if rb["discount_rate"] != dr:
        parts.append({"input": "discount_rate", "effect": round(scenario(sa, ctx, rb["discount_rate"])["value"] - base_a, 2)})
    tot = sum(abs(p["effect"]) for p in parts) or 1
    for p in parts:
        p["share"] = round(abs(p["effect"]) / tot, 3)
    parts.sort(key=lambda p: -abs(p["effect"]))
    gap = (rb["iv_base"] / ra["iv_base"] - 1) if ra.get("iv_base") else None
    return {"a": ra["iv_base"], "b": rb["iv_base"], "gap": round(gap, 4) if gap is not None else None, "parts": parts}


EVIDENCE_GRADES = {"A": "Verified primary source", "B": "Reliable third party", "C": "Management statement",
                   "D": "Analyst estimate", "E": "AI assumption"}


def evidence_summary(claims):
    """claims: [{claim, grade, source, date, pillar?}] -> counts by grade and the weak-support flag (>40% D/E on pillars)."""
    counts = {g: 0 for g in EVIDENCE_GRADES}
    pillar = [c for c in claims or [] if c.get("pillar")]
    for c in claims or []:
        g = str(c.get("grade") or "E").upper()[:1]
        counts[g if g in counts else "E"] += 1
    weak = [c for c in pillar if str(c.get("grade") or "E").upper()[:1] in ("D", "E")]
    share = len(weak) / len(pillar) if pillar else None
    unsourced = [c for c in claims or [] if str(c.get("grade") or "E").upper()[:1] in "ABC" and not c.get("source")]
    return {"counts": counts, "total": sum(counts.values()), "pillar_claims": len(pillar),
            "pillar_weak_share": round(share, 3) if share is not None else None,
            "weak_flag": bool(share is not None and share > 0.40), "unsourced": len(unsourced)}


# ------------------------------------------------------------------ simulator (stock page)
def apply_changes(base, ch):
    """Return a copy of a scenario with changes applied. Changes (all optional):
    growth (list, years 1-5) | growth_delta (pts added to years 1-5) | margin_end | margin_delta (added to year-10 margin)
    | margin_start_delta | reinvest_pct | share_change | share_change_delta | terminal_growth | exit_multiple
    | revenue_shocks [{year, pct, phase_years}] (added to any existing)"""
    import copy
    s = copy.deepcopy(base)
    ch = ch or {}
    tg = float(s.get("terminal_growth", 0.025))
    if ch.get("growth") is not None:
        s["growth"] = [float(x) for x in ch["growth"]]
    if ch.get("growth_delta"):
        g = _path(s.get("growth"), 0.05, end=tg)[:5]
        d = ch["growth_delta"]
        d = d if isinstance(d, (list, tuple)) else [d] * 5
        s["growth"] = [g[i] + float(d[i] if i < len(d) else d[-1]) for i in range(5)]
    m = _path(s.get("margin"), 0.15)
    m0, m1 = m[0], m[-1]
    if ch.get("margin_end") is not None:
        m1 = float(ch["margin_end"])
    m1 += float(ch.get("margin_delta") or 0)
    m0 += float(ch.get("margin_start_delta") or 0)
    s["margin"] = [m0, m1]
    for k in ("reinvest_pct", "share_change", "terminal_growth", "exit_multiple", "tax"):
        if ch.get(k) is not None:
            s[k] = ch[k]
    if ch.get("share_change_delta"):
        s["share_change"] = float(s.get("share_change", 0)) + float(ch["share_change_delta"])
    if ch.get("revenue_shocks"):
        s["revenue_shocks"] = list(s.get("revenue_shocks") or []) + list(ch["revenue_shocks"])
    return s


def simulate(assumptions, research=None, price=None, changes=None, case="base", discount_rate=None):
    """Value one case of the thesis assumptions after applying changes. Same engine as the thesis."""
    ctx = context_from(assumptions, research, price)
    dr = float(discount_rate) if discount_rate else discount_rate_of(assumptions)
    base = (assumptions.get("scenarios") or {}).get(case) or assumptions["scenarios"]["base"]
    out = scenario(apply_changes(base, changes), ctx, dr)
    ref = scenario(base, ctx, dr)
    out.update(base_value=ref["value"], change_vs_base=(out["value"] / ref["value"] - 1) if ref["value"] else None,
               vs_price=(out["value"] / ctx["price"] - 1) if ctx.get("price") else None, price=ctx.get("price"))
    return out


def discount_rate_of(assumptions):
    return discount_rate(assumptions.get("profile") or {})


def reverse(assumptions, research=None, price=None, target=None, changes=None, case="base"):
    """What it takes to be worth `target` (default: today's price): the uniform revenue growth for years 1-5 at the
    case's margins, and the year-10 operating margin needed at the case's growth."""
    ctx = context_from(assumptions, research, price)
    target = float(target or ctx.get("price") or 0)
    if not target:
        return None
    dr = discount_rate_of(assumptions)
    base = apply_changes((assumptions.get("scenarios") or {}).get(case) or assumptions["scenarios"]["base"], changes)

    def solve(fn, lo, hi):
        try:
            if not (fn(lo) <= target <= fn(hi)):
                return None
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if fn(mid) < target else (lo, mid)
            return round((lo + hi) / 2, 4)
        except (ValueError, ZeroDivisionError):
            return None
    g = solve(lambda x: scenario(dict(base, growth=[x] * 5), ctx, dr)["value"], -0.30, 0.80)
    m0 = _path(base.get("margin"), 0.15)[0]
    mm = solve(lambda x: scenario(dict(base, margin=[m0, x]), ctx, dr)["value"], -0.20, 0.80)
    return {"target": target, "growth_needed": g, "margin_needed": mm,
            "thesis_growth": round(sum(_path(base.get("growth"), 0.05, end=float(base.get("terminal_growth", 0.025)))[:5]) / 5, 4),
            "thesis_margin_end": round(_path(base.get("margin"), 0.15)[-1], 4)}


def default_assumptions(research):
    """A neutral starting point from the company's own history when no thesis assumptions exist yet
    (3-year revenue CAGR capped at 30%, latest operating margin, reinvestment from cash flow). Labelled 'from history'."""
    hist = [h for h in (research or {}).get("history") or [] if h.get("revenue")]
    if len(hist) < 3:
        return None
    r = [h["revenue"] for h in hist[-4:]]
    cagr = (r[-1] / r[0]) ** (1 / (len(r) - 1)) - 1 if r[0] and r[0] > 0 else 0.05
    g = max(min(cagr, 0.30), -0.05)
    om = next((h.get("op_margin") for h in reversed(hist) if h.get("op_margin") is not None), 0.15) or 0.15
    fcfm = next((h.get("fcf_margin") for h in reversed(hist) if h.get("fcf_margin") is not None), None)
    rein = max(0.0, min(0.30, om * 0.79 - fcfm)) if fcfm is not None else 0.05
    sh = [h.get("shares") for h in hist[-4:] if h.get("shares")]
    sc = ((sh[-1] / sh[0]) ** (1 / (len(sh) - 1)) - 1) if len(sh) >= 2 else 0.0
    path = [round(g - (g - 0.05) * i / 8, 4) for i in range(5)]
    base = {"growth": path, "margin": [round(om, 4), round(om, 4)], "reinvest_pct": round(rein, 4), "tax": 0.21,
            "share_change": round(sc, 4), "terminal_growth": 0.025, "grade": "A", "why": "company's own last 3 years"}
    return {"profile": {"moat": "unproven"}, "scenarios": {"base": base}, "source": "history"}
