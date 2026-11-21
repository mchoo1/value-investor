"""Research report data (Morningstar-level detail, 21 Nov 2026): one builder used by the stock page, the PDF and Telegram.

Everything numeric is computed in code from the thesis assumptions (valuation.py) and the weekly research data;
Claude writes the assumptions, the moat metrics, the peer table, the capital allocation view and the notes.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import valuation as V


def _n(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build(cur, ticker):
    """Collect everything the research report shows. cur: a dict cursor. Returns a dict (never raises on missing data)."""
    t = ticker.upper()
    cur.execute("SELECT * FROM v2_names WHERE ticker = %s", (t,))
    n = cur.fetchone() or {}
    cur.execute("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version", (t,))
    versions = [dict(r) for r in cur.fetchall()]
    th = versions[-1] if versions else {}
    cur.execute("SELECT data FROM v2_research WHERE ticker = %s ORDER BY as_of DESC LIMIT 1", (t,))
    r = (cur.fetchone() or {}).get("data") or {}
    cur.execute("SELECT date, close FROM v2_prices WHERE ticker = %s ORDER BY date DESC LIMIT 1", (t,))
    pr = cur.fetchone()
    price = _n(pr["close"]) if pr else _n(r.get("price"))
    secs = th.get("sections") or {}
    val = th.get("valuation") or {}
    a = (val.get("engine") or {}).get("assumptions")
    basis = "Claude's thesis assumptions"
    if not a:
        a = V.default_assumptions(r)
        basis = "the company's own last 3 years (no thesis assumptions yet: values are indicative)"
    out = {"ticker": t, "name": n.get("name") or r.get("name") or t, "sector": n.get("sector") or r.get("sector"),
           "industry": n.get("industry") or r.get("industry"), "level": n.get("level"), "price": price,
           "price_date": str(pr["date"]) if pr else None, "mcap": _n(r.get("mcap")), "thesis": th, "sections": secs,
           "exec": secs.get("exec_summary") if isinstance(secs.get("exec_summary"), dict) else {},
           "basis": basis, "has_assumptions": bool((val.get("engine") or {}).get("assumptions")),
           "fair_value": _n(val.get("iv_base")), "entry_target": _n(th.get("entry_target")),
           "moat": (val.get("moat_source") or {}).get("type") if isinstance(val.get("moat_source"), dict) else None,
           "moat_width": ((val.get("moat_source") or {}).get("width") if isinstance(val.get("moat_source"), dict) else None),
           "moat_profile": ((a or {}).get("profile") or {}).get("moat"),
           "capital_allocation": secs.get("capital_allocation") if isinstance(secs.get("capital_allocation"), dict) else None,
           "moat_metrics": secs.get("moat_metrics") if isinstance(secs.get("moat_metrics"), list) else [],
           "peer_table": secs.get("peer_table") if isinstance(secs.get("peer_table"), list) else [],
           "generated": dt.datetime.utcnow().strftime("%d %b %Y %H:%M UTC")}
    # ---- engine-derived: model, DCF, uncertainty, stars
    if a:
        try:
            run = V.run(a, r, price)
            if run.get("dcf_valid"):
                out["run"] = run
                out["projection"] = V.projection(a, r, price)
                out["dcf"] = V.dcf_breakdown(a, r, price)
                if not out["fair_value"]:
                    out["fair_value"] = run.get("iv_base")
                u = V.uncertainty(a, run.get("scenarios"))
                out["uncertainty"] = u
                out["stars"] = V.stars(price, out["fair_value"], u["rating"])
            else:
                out["engine_note"] = run.get("note")
        except (ValueError, KeyError, ZeroDivisionError, TypeError) as e:
            out["engine_note"] = f"Engine could not run: {str(e)[:120]}"
    # ---- actuals: last 3 fiscal years from the weekly research data (SEC filings)
    hist = [h for h in (r.get("history") or []) if h.get("revenue")]
    out["actuals"] = hist[-3:]
    # ---- notes archive and fair value history (one entry per thesis version)
    notes, fv_hist = [], []
    prev = None
    for v in versions:
        vv = v.get("valuation") or {}
        s_ = v.get("sections") or {}
        note = s_.get("note") if isinstance(s_.get("note"), dict) else {}
        fv = _n(vv.get("iv_base"))
        if fv:
            fv_hist.append((str(v["created_at"])[:10], fv))
        es = s_.get("exec_summary") if isinstance(s_.get("exec_summary"), dict) else {}
        notes.append({"version": v["version"], "date": str(v["created_at"])[:10], "author": v.get("author"),
                      "title": note.get("title") or (es.get("one_line") if v is versions[0] else None)
                      or ("Initiating coverage" if v is versions[0] else "Thesis update"),
                      "why_it_matters": note.get("why_it_matters"),
                      "bottom_line": note.get("bottom_line") or (vv.get("refresh") or {}).get("summary"),
                      "fv": fv, "fv_prev": _n(((prev or {}).get("valuation") or {}).get("iv_base")),
                      "conviction": _n(v.get("conviction")), "verdict": v.get("verdict"), "approval": v.get("approval")})
        prev = v
    out["notes"] = list(reversed(notes))
    out["fv_history"] = fv_hist
    # ---- forecast revisions: latest vs previous version with assumptions
    with_a = [v for v in versions if ((v.get("valuation") or {}).get("engine") or {}).get("assumptions")]
    if len(with_a) >= 2:
        out["revisions"] = V.revisions(with_a[-2]["valuation"]["engine"]["assumptions"],
                                       with_a[-1]["valuation"]["engine"]["assumptions"], r, price)
        out["revisions_since"] = str(with_a[-2]["created_at"])[:10]
    # ---- price history since a month before the first thesis (or 2 years) for the price vs fair value chart
    since = (dt.date.fromisoformat(fv_hist[0][0]) - dt.timedelta(days=30)) if fv_hist else dt.date.today() - dt.timedelta(days=730)
    since = min(since, dt.date.today() - dt.timedelta(days=365))
    cur.execute("SELECT date, close FROM v2_prices WHERE ticker = %s AND date >= %s ORDER BY date", (t, since))
    out["prices"] = [(str(x["date"]), _n(x["close"])) for x in cur.fetchall() if x["close"] is not None]
    # ---- our own fair value for covered peers (P/FV column)
    peers = [p.get("ticker") for p in out["peer_table"] if isinstance(p, dict) and p.get("ticker")]
    out["peer_fv"] = {}
    if peers:
        cur.execute("""SELECT DISTINCT ON (ticker) ticker, valuation->>'iv_base' AS fv FROM v2_theses
                       WHERE ticker = ANY(%s) ORDER BY ticker, version DESC""", (peers,))
        out["peer_fv"] = {x["ticker"]: _n(x["fv"]) for x in cur.fetchall() if x["fv"]}
    return out


def svg_price_fv(rep, w=760, h=220):
    """Inline SVG: daily price line and the fair value as a step line (one step per thesis version), with the star bands."""
    pts = [(d, p) for d, p in rep.get("prices") or [] if p]
    if len(pts) < 5:
        return ""
    fvh = rep.get("fv_history") or []
    lo_b, hi_b = (rep.get("uncertainty") or {}).get("bands") or (None, None)
    vals = [p for _, p in pts] + [f for _, f in fvh]
    if fvh and lo_b:
        vals += [fvh[-1][1] * lo_b, fvh[-1][1] * hi_b]
    vmin, vmax = min(vals) * 0.95, max(vals) * 1.05
    d0 = dt.date.fromisoformat(pts[0][0])
    span = max((dt.date.fromisoformat(pts[-1][0]) - d0).days, 1)
    pad_l, pad_b = 46, 22

    def X(d):
        return pad_l + (dt.date.fromisoformat(d) - d0).days / span * (w - pad_l - 8)

    def Y(v):
        return 8 + (vmax - v) / (vmax - vmin) * (h - pad_b - 8)
    line = " ".join(f"{X(d):.1f},{Y(p):.1f}" for d, p in pts)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Price vs fair value" style="max-width:{w}px">']
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        parts.append(f'<line x1="{pad_l}" x2="{w - 8}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--line,#3334)" stroke-width="1"/>'
                     f'<text x="{pad_l - 6}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end" fill="var(--muted,#888)">{v:,.0f}</text>')
    if fvh:
        steps, last = [], None
        for d, f in fvh:
            d = max(d, pts[0][0])
            if last is not None:
                steps.append(f"{X(d):.1f},{Y(last):.1f}")
            steps.append(f"{X(d):.1f},{Y(f):.1f}")
            last = f
        steps.append(f"{X(pts[-1][0]):.1f},{Y(last):.1f}")
        if lo_b:
            x0 = X(max(fvh[-1][0], pts[0][0]))
            parts.append(f'<rect x="{x0:.1f}" y="{Y(last * hi_b):.1f}" width="{X(pts[-1][0]) - x0:.1f}" height="{Y(last * lo_b) - Y(last * hi_b):.1f}" '
                         f'fill="#e0a43a" opacity="0.10"/>')
        parts.append(f'<polyline points="{" ".join(steps)}" fill="none" stroke="#e0a43a" stroke-width="2"/>')
    parts.append(f'<polyline points="{line}" fill="none" stroke="#4aa3ff" stroke-width="1.6"/>')
    for i in (0, len(pts) // 2, len(pts) - 1):
        parts.append(f'<text x="{X(pts[i][0]):.1f}" y="{h - 6}" font-size="11" text-anchor="middle" fill="var(--muted,#888)">{pts[i][0][:7]}</text>')
    parts.append("</svg>")
    return "".join(parts)
