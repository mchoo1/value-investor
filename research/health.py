"""
health.py — mechanical thesis health checks (Sunday task). Claude adds judgement checks
(earnings miss vs guidance, guidance cuts, management/capital-allocation shifts from news).

Usage: python health.py thesis.json --framework framework.json [--out issues_TICKER.json]

Rules (Ming 2026-09-28 + framework.json monitoring):
  price_vs_value   price > iv_base (MoS gone) -> medium; price < entry target -> info (opportunity)
  estimate_rev     NTM EPS consensus moved >= 5% in 30d -> medium
  dilution         diluted shares +3% YoY -> medium
  leverage         NetDebt/EBITDA +1.0x vs last check or crosses 3x -> high
  kpi_breach       pillar KPI crosses breach_threshold -> high (if machine-checkable)
  kill_criterion   kill metric crosses threshold -> high (health RED)
  catalyst_missed  expected_date passed, status pending -> medium
  stale            next_review_date passed or valuation.as_of > 90 days -> medium
  filings          new 8-K since last check (item 5.02 officer change -> medium; 2.02 results -> info)
Health: RED if any high; AMBER if any medium; else GREEN. Never closes a thesis.
"""
import argparse, json, datetime as dt
import vi_data as D
from screen import T

KPI_MAP = {  # machine-checkable pillar KPIs -> function(companyfacts) -> latest value
    "roic": None, "gross_margin": None, "operating_margin": None, "net_margin": None,
    "fcf": None, "revenue_growth": None, "net_debt_ebitda": None,
}


def gv(x):
    return x["value"] if isinstance(x, dict) else x


def latest_metrics(cf):
    q = lambda k, inst=False: D.quarterly_series(cf, T[k], instant=inst)[0]
    a = lambda k, inst=False: D.annual_series(cf, T[k], instant=inst)[0]
    def ttm(k):
        s = list(q(k).items())
        return sum(v for _, v in s[-4:]) if len(s) >= 4 else (list(a(k).values()) or [None])[-1]
    rev, opinc, ni, ocf_a = ttm("rev"), ttm("opinc"), ttm("ni"), a("ocf")
    da = ttm("da") or 0
    capex_a = a("capex")
    m = {}
    if rev:
        m["operating_margin"] = opinc / rev if opinc is not None else None
        m["net_margin"] = ni / rev if ni is not None else None
    revs = list(a("rev").values())
    if len(revs) >= 2 and revs[-2]:
        m["revenue_growth"] = revs[-1] / revs[-2] - 1
    if ocf_a:
        k = list(ocf_a)[-1]
        m["fcf"] = ocf_a[k] - capex_a.get(k, 0)
    last_q = lambda k: (list(q(k, True).values()) or [0])[-1] or 0
    debt = last_q("ltd") + last_q("std") + last_q("stb")
    cash = last_q("cash") + last_q("sti")
    ebitda = (opinc or 0) + da
    m["net_debt_ebitda"] = (debt - cash) / ebitda if ebitda > 0 else None
    eq = last_q("eq")
    if opinc is not None and (eq + debt - cash) > 0:
        m["roic"] = opinc * 0.79 / (eq + debt - cash)
    sh, _ = D.quarterly_series(cf, ["dei:EntityCommonStockSharesOutstanding"], unit="shares", instant=True)
    sh = list(sh.items())
    if len(sh) >= 5:
        m["shares_yoy"] = sh[-1][1] / sh[-5][1] - 1
    gp = ttm("gp") if "gp" in T else None
    if rev and gp is not None:
        m["gross_margin"] = gp / rev
    return m


METRIC_KEYS = ("roic", "operating_margin", "net_margin", "gross_margin", "revenue_growth", "fcf",
               "net_debt_ebitda", "shares_yoy")   # machine-checkable kill-criterion / pillar KPI keys


def recent_8k(cik, since):
    raw = D._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", cache_name=f"sub_{cik}.json", max_age_h=12)
    r = json.loads(raw)["filings"]["recent"]
    out = []
    for form, date, items, acc, doc in zip(r["form"], r["filingDate"], r["items"], r["accessionNumber"], r["primaryDocument"]):
        if form in ("8-K", "8-K/A") and date > since:
            out.append({"date": date, "items": items,
                        "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"})
    return out


def check(th, fw, last_check=None):
    today = dt.date.today()
    tk = th["ticker"]
    sec = D.sec_tickers()[tk]
    cf = D.sec_companyfacts(sec["cik"])
    y = D.yahoo_snapshot(tk)
    issues = []
    def add(typ, sev, detail, src, action):
        issues.append({"date": D.TODAY, "type": typ, "severity": sev, "detail": detail,
                       "source": src, "recommended_action": action, "resolved": False})

    price = y.get("price")
    val = th.get("valuation", {})
    base, entry = gv(val.get("base")), gv(th.get("entry_price_target"))
    if price and base and price > base:
        add("price_vs_value", "medium", f"Price ${price:.2f} above base value ${base:.2f}: margin of safety gone",
            "Yahoo Finance price; thesis valuation", "trim")
    elif price and entry and price < entry:
        add("price_vs_value", "info", f"Price ${price:.2f} below entry target ${entry:.2f}: opportunity",
            "Yahoo Finance price", "hold")

    tr = y.get("eps_trend_+1y") or {}
    if tr.get("current") and tr.get("30daysAgo"):
        r30 = tr["current"] / tr["30daysAgo"] - 1
        if abs(r30) >= 0.05:
            add("estimate_revision", "medium", f"NTM EPS consensus {r30:+.1%} in 30 days",
                "Yahoo Finance EPS trend (single source)", "re-underwrite")

    m = latest_metrics(cf)
    if (m.get("shares_yoy") or 0) >= 0.03:
        add("dilution", "medium", f"Shares outstanding {m['shares_yoy']:+.1%} YoY", "SEC XBRL dei shares", "re-underwrite")
    nde = m.get("net_debt_ebitda")
    prev = (th.get("monitor_state") or {}).get("net_debt_ebitda")
    if nde is not None and (nde > 3 or (prev is not None and nde - prev >= 1.0)):
        add("leverage", "high", f"Net debt/EBITDA {nde:.1f}x (prev {prev})", "SEC XBRL latest 10-Q", "exit review")

    def breach(metric, cur, thr, direction):
        return cur is not None and thr is not None and ((cur < thr) if direction == "below" else (cur > thr))
    kill_status, pillar_status = [], []
    for p in th.get("pillars", []):
        k = (p.get("kpi_key") or "").strip()
        if k in METRIC_KEYS:
            pillar_status.append({"claim": p.get("claim"), "kpi_key": k, "current": m.get(k),
                                  "breach_threshold": p.get("breach_threshold"), "direction": p.get("breach_direction", "below"),
                                  "breached": breach(k, m.get(k), p.get("breach_threshold"), p.get("breach_direction", "below")),
                                  "as_of": D.TODAY, "source": "SEC XBRL (latest 10-Q/10-K)"})
        if k in m and p.get("breach_threshold") is not None:
            direction = p.get("breach_direction", "below")
            if breach(k, m[k], p["breach_threshold"], direction):
                add("kpi_breach", "high", f"Pillar '{p['claim'][:60]}': {k} {m[k]:.3g} {direction} {p['breach_threshold']}",
                    "SEC XBRL", "re-underwrite")
    for kc in th.get("kill_criteria", []):
        k = (kc.get("metric_key") or "").strip()
        kill_status.append({"criterion": kc.get("criterion"), "metric_key": k or None,
                            "current": m.get(k) if k else None, "threshold": kc.get("threshold"),
                            "direction": kc.get("direction", "below"),
                            "breached": breach(k, m.get(k), kc.get("threshold"), kc.get("direction", "below")) if k else None,
                            "check": "machine" if k in METRIC_KEYS else "judgement (weekly news/filings review)",
                            "as_of": D.TODAY})
        if k in m and kc.get("threshold") is not None:
            if breach(k, m[k], kc["threshold"], kc.get("direction", "below")):
                add("kill_criterion", "high", f"KILL: {kc['criterion']} ({k}={m[k]:.3g})", "SEC XBRL", "exit review")

    for c in th.get("catalysts", []):
        ed = c.get("expected_date")
        if c.get("status", "pending") == "pending" and ed and ed < D.TODAY:
            add("catalyst_missed", "medium", f"Catalyst '{c['event']}' expected {ed} not confirmed",
                "thesis catalysts", "re-underwrite")
    nrd = th.get("next_review_date")
    va = val.get("as_of")
    if (nrd and nrd < D.TODAY) or (va and (today - dt.date.fromisoformat(va)).days > gv(fw["data_quality_gates"]["stale_thesis_days"])):
        add("stale", "medium", f"Review due {nrd}; valuation as of {va}", "thesis metadata", "re-underwrite")

    for f in recent_8k(sec["cik"], last_check or (today - dt.timedelta(days=8)).isoformat()):
        sev = "medium" if "5.02" in (f["items"] or "") else "info"
        add("filing", sev, f"8-K {f['date']} items {f['items']}" + (" (5.02: read it - director election is benign, officer departure is not)" if sev == "medium" else ""), f["url"],
            "re-underwrite" if sev == "medium" else "hold")

    sev = {i["severity"] for i in issues}
    health = "Red" if "high" in sev else "Amber" if "medium" in sev else "Green"
    return {"ticker": tk, "checked": D.TODAY, "price": D.fact(price, "Yahoo Finance"), "health": health,
            "issues": issues, "metrics": m, "kill_status": kill_status, "pillar_status": pillar_status,
            "monitor_state": {"net_debt_ebitda": nde, "last_price": price, "last_checked": D.TODAY,
                              "metrics": m, "kill_status": kill_status, "pillar_status": pillar_status}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("thesis"); ap.add_argument("--framework", default="framework.json")
    ap.add_argument("--last-check"); ap.add_argument("--out")
    a = ap.parse_args()
    th = json.load(open(a.thesis)); fw = json.load(open(a.framework))
    r = check(th, fw, a.last_check)
    json.dump(r, open(a.out or f"health_{th['ticker']}.json", "w"), indent=1, default=str)
    print(json.dumps(r, indent=1, default=str)[:3000])
