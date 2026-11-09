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
    rev, flows, ebits = float(ctx["revenue0"]), [], []
    for t in range(YEARS):
        rev *= 1 + g[t]
        ebit = rev * m[t]
        ebits.append(ebit)
        flows.append(ebit * (1 - tax) - rev * rein)
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
