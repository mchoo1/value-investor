"""Monitoring for Watchlist and Book names.

  python tools\\monitor.py daily    prices, filings, analyst moves, buy signals, price / theme / market alerts,
                                    kill criteria, health (Mon-Fri 07:00 SGT)
  python tools\\monitor.py weekly   3-5 headlines per name for Claude to tag (Sunday 08:00 SGT)
  python tools\\monitor.py export   write outbox/state.json (and Drive v2-outbox) for Claude's tasks
Data: Yahoo (yfinance), SEC EDGAR. All free.
"""
import os, sys, json, math, time, datetime as dt
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
from common import (db, J, alert, ensure_schema, heartbeat, HEARTBEAT_MAX_H, week_key, TODAY, THEMES, APP_URL, DRIVE_STATE_FILE,
                    DRIVE_INBOX, drive_update)

MODE = sys.argv[1] if len(sys.argv) > 1 else "daily"
UA = os.environ.get("SEC_USER_AGENT", "")
SEC = requests.Session()
SEC.headers.update({"User-Agent": UA or "ValueInvestor research", "Accept-Encoding": "gzip, deflate"})
CACHE = Path(__file__).with_name(".cache")
CACHE.mkdir(exist_ok=True)
ITEMS_8K = {"1.01": "Material agreement", "1.02": "Agreement ended", "1.03": "Bankruptcy", "2.01": "Acquisition or sale",
            "2.02": "Earnings release", "2.05": "Restructuring costs", "2.06": "Impairment", "3.01": "Listing notice",
            "4.01": "Auditor change", "4.02": "Prior financials no longer reliable", "5.02": "Director or officer change",
            "5.07": "Shareholder vote", "7.01": "Reg FD disclosure", "8.01": "Other event"}
HIGH_8K = {"1.03", "4.01", "4.02", "3.01"}
THEME_WORDS = {
    "AI and energy infrastructure": ["ai", "data center", "datacenter", "semiconductor", "semis", "power", "grid",
                                     "electrification", "storage", "compute", "nuclear", "energy infra"],
    "Automation and robotics": ["robot", "automation"],
    "Healthcare": ["health", "pharma", "biotech", "medtech", "medical", "glp", "obesity"],
    "Space and satellites": ["space", "satellite", "launch", "defense", "defence"],
    "Digital finance": ["fintech", "payment", "digital finance", "brokerage", "crypto", "exchange"],
    "Quantum computing": ["quantum"],
    "Critical resources": ["copper", "uranium", "lithium", "rare earth", "mining", "critical resource", "materials"],
}
health_log = {"prices_missing": [], "info_failed": [], "sec_failed": [], "news_failed": []}


def themes_of(tags):
    out = set()
    for t in tags or []:
        tl = t.lower()
        for theme, words in THEME_WORDS.items():
            if tl == theme.lower() or any(w == tl or (len(w) > 3 and w in tl) for w in words):
                out.add(theme)
    return out


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def cik_map():
    f = CACHE / "company_tickers.json"
    if f.exists() and time.time() - f.stat().st_mtime < 6 * 86400:
        data = json.loads(f.read_text())
    else:
        data = SEC.get("https://www.sec.gov/files/company_tickers.json", timeout=60).json()
        f.write_text(json.dumps(data))
    return {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in data.values()}


def load(cur):
    cur.execute("SELECT * FROM v2_names WHERE level IN ('watchlist','book') ORDER BY ticker")
    names = [dict(r) for r in cur.fetchall()]
    t = [n["ticker"] for n in names]
    cur.execute("SELECT DISTINCT ON (ticker) * FROM v2_theses WHERE ticker = ANY(%s) ORDER BY ticker, version DESC", (t,))
    theses = {r["ticker"]: dict(r) for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT ON (ticker) ticker, data FROM v2_research WHERE ticker = ANY(%s) "
                "ORDER BY ticker, as_of DESC", (t,))
    research = {r["ticker"]: r["data"] for r in cur.fetchall()}
    return names, theses, research


def entry_of(tk, theses, research):
    th = theses.get(tk) or {}
    if num(th.get("entry_target")):
        return num(th["entry_target"]), "thesis"
    v = (research.get(tk) or {}).get("valuation") or {}
    if num(v.get("entry_target")):
        return num(v["entry_target"]), "model"
    return None, None


# macro series (Yahoo): S&P, VIX, 3-month / 5-year / 10-year yields, dollar index, oil, gold, high-yield and long Treasuries
MACRO_SYMS = ["^GSPC", "SPY", "QQQ", "^VIX", "^IRX", "^FVX", "^TNX", "DX-Y.NYB", "CL=F", "GC=F", "HYG", "TLT"]
LEAD_SCORE, LEAD_DROP = 75, 0.12   # screen names scoring 75+ that fall 12%+ after the screen get a heads-up
GROWTH_HURDLE = 0.15          # growth buy zone: price where the 5-year TAM case returns 15%+ a year


def growth_zone(th):
    """Price at or below which the thesis's 5-year TAM target returns GROWTH_HURDLE a year. None without a TAM target."""
    tam = ((th or {}).get("valuation") or {}).get("tam") or {}
    for sc in tam.get("scenarios") or []:
        try:
            if int(sc["years"]) != 5:
                continue
            ni = sum(float(g["tam_bn"]) * float(g["share_pct"]) / 100 * float(g["net_margin_pct"]) / 100
                     for g in sc.get("segments") or [])
            ni += float(sc.get("other_rev_bn") or 0) * float(sc.get("other_margin_pct") or 0) / 100
            then = ni * float(sc["exit_pe"]) * 1000 / float(sc["shares_m"])
            return then / (1 + GROWTH_HURDLE) ** 5, then
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None, None
    return None, None


TRIM_HURDLE = 0.05            # growth names: price where the 5-year TAM case returns under 5% a year = priced beyond the case


def style_of(n, th):
    """'value' or 'growth'. Set per name (Book import / stock page); otherwise growth if the thesis has a TAM
    target and the name scores higher on the growth track."""
    if n.get("style") in ("value", "growth"):
        return n["style"]
    has_tam = bool(((th or {}).get("valuation") or {}).get("tam"))
    return "growth" if has_tam and (num(n.get("growth_score")) or 0) > (num(n.get("score")) or 0) else "value"


def book_zones(n, th, research):
    """Add and trim levels for a Book name, by style.
    value : add at the entry target; review at Claude's base value; trim above the high value.
    growth: add in the growth zone (5-year TAM case returns 15%+ a year); trim only when the price is beyond the
            5-year case (returns under 5% a year). Growth names are held for years; valuation alone rarely ends them."""
    style = style_of(n, th)
    val = (th or {}).get("valuation") or {}
    if style == "growth":
        zone, then = growth_zone(th)
        trim = then / (1 + TRIM_HURDLE) ** 5 if then else None
        return style, {"add": zone, "review": None, "trim": trim, "then": then}
    entry, _ = entry_of(n["ticker"], {n["ticker"]: th} if th else {}, research)
    return style, {"add": entry, "review": num(val.get("iv_base")), "trim": num(val.get("iv_high")), "then": None}


SCENARIOS = {   # shocks: S&P %, 10-year yield in percentage points, dollar index %, oil %
    "Rates up: 10-year +0.5pt": {"spx": 0, "y10": 0.5, "usd": 0, "oil": 0},
    "Rates down: 10-year -0.5pt": {"spx": 0, "y10": -0.5, "usd": 0, "oil": 0},
    "Market -10%": {"spx": -0.10, "y10": 0, "usd": 0, "oil": 0},
    "Risk-off: S&P -10%, 10-year -0.3pt, dollar +3%": {"spx": -0.10, "y10": -0.3, "usd": 0.03, "oil": -0.10},
    "Dollar +5%": {"spx": 0, "y10": 0, "usd": 0.05, "oil": 0},
    "Oil +20%": {"spx": 0, "y10": 0, "usd": 0, "oil": 0.20},
    "Reflation: 10-year +0.5pt, oil +20%, S&P +5%": {"spx": 0.05, "y10": 0.5, "usd": -0.02, "oil": 0.20},
}


def macro_view(cur, closes, names):
    """Regime readings plus each name's weekly sensitivity (2 years) to the S&P, the 10-year yield, the dollar and oil,
    and what common macro shocks would do to the Book. Stored in v2_settings['macro']."""
    import numpy as np
    wk = lambda s: s.resample("W-FRI").last().dropna()
    need = {"spx": "^GSPC", "y10": "^TNX", "usd": "DX-Y.NYB", "oil": "CL=F"}
    if any(v not in closes for v in need.values()):
        return None
    X = pd.concat({k: wk(closes[v]) for k, v in need.items()}, axis=1).dropna()
    dX = pd.DataFrame({"spx": X["spx"].pct_change(), "y10": X["y10"].diff(), "usd": X["usd"].pct_change(),
                       "oil": X["oil"].pct_change()}).dropna()
    sens = {}
    for n in names:
        c = closes.get(n["ticker"])
        if c is None:
            continue
        y = wk(c).pct_change().dropna()
        d = dX.join(y.rename("y"), how="inner").dropna()
        if len(d) < 40:
            continue
        A = np.column_stack([np.ones(len(d)), d[["spx", "y10", "usd", "oil"]].values])
        b, *_ = np.linalg.lstsq(A, d["y"].values, rcond=None)
        pred = A @ b
        r2 = 1 - ((d["y"] - pred) ** 2).sum() / ((d["y"] - d["y"].mean()) ** 2).sum()
        sens[n["ticker"]] = {"beta_spx": float(b[1]), "per_y10_pt": float(b[2]), "per_usd": float(b[3]), "per_oil": float(b[4]),
                             "r2": float(r2), "level": n["level"], "weeks": len(d)}
    # Book weights from positions
    cur.execute("SELECT DISTINCT ON (ticker) ticker, shares FROM v2_positions WHERE exited IS NULL ORDER BY ticker, entered DESC")
    mv = {r["ticker"]: float(r["shares"] or 0) * float(closes[r["ticker"]].iloc[-1])
          for r in cur.fetchall() if r["ticker"] in closes and r["shares"]}
    tot = sum(mv.values()) or 1
    scen = {}
    for name, sh in SCENARIOS.items():
        by = {t: s["beta_spx"] * sh["spx"] + s["per_y10_pt"] * sh["y10"] + s["per_usd"] * sh["usd"] + s["per_oil"] * sh["oil"]
              for t, s in sens.items()}
        book = sum(by.get(t, 0) * v for t, v in mv.items()) / tot if mv else None
        held = sorted([(t, v) for t, v in by.items() if t in mv], key=lambda kv: kv[1])
        scen[name] = {"book": book, "worst": held[:3], "best": held[-3:][::-1], "by_name": by}
    last = lambda s, n=0: float(closes[s].iloc[-1 - n]) if s in closes and len(closes[s]) > n else None
    ma200 = float(closes["^GSPC"].tail(200).mean()) if "^GSPC" in closes else None
    regime = {
        "spx": last("^GSPC"), "spx_vs_200d": (last("^GSPC") / ma200 - 1) if ma200 else None,
        "vix": last("^VIX"), "y10": last("^TNX"), "y10_chg_1m": (last("^TNX") - last("^TNX", 21)) if last("^TNX", 21) else None,
        "y3m": last("^IRX"), "y5": last("^FVX"), "curve_10y_3m": (last("^TNX") - last("^IRX")) if last("^IRX") else None,
        "usd": last("DX-Y.NYB"), "usd_chg_1m": (last("DX-Y.NYB") / last("DX-Y.NYB", 21) - 1) if last("DX-Y.NYB", 21) else None,
        "oil": last("CL=F"), "oil_chg_1m": (last("CL=F") / last("CL=F", 21) - 1) if last("CL=F", 21) else None,
        "gold": last("GC=F"), "hyg_chg_1m": (last("HYG") / last("HYG", 21) - 1) if last("HYG", 21) else None,
    }
    flags = []
    if regime["curve_10y_3m"] is not None and regime["curve_10y_3m"] < 0:
        flags.append("Yield curve inverted (10-year below 3-month): historically a recession warning")
    if regime["hyg_chg_1m"] is not None and regime["hyg_chg_1m"] < -0.02:
        flags.append("High-yield bonds down 2%+ in a month: credit stress building")
    if regime["vix"] and regime["vix"] > 25:
        flags.append("VIX above 25: stressed market")
    if regime["spx_vs_200d"] is not None and regime["spx_vs_200d"] < 0:
        flags.append("S&P below its 200-day average: downtrend")
    if regime["y10_chg_1m"] is not None and abs(regime["y10_chg_1m"]) >= 0.4:
        flags.append(f"10-year yield {'up' if regime['y10_chg_1m'] > 0 else 'down'} {abs(regime['y10_chg_1m']):.2f}pt in a month")
    out = {"as_of": str(TODAY), "regime": regime, "flags": flags, "sens": sens, "scenarios": scen}
    cur.execute("""INSERT INTO v2_settings (key, value, updated_at) VALUES ('macro', %s, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", (J(out),))
    return out


# ------------------------------------------------------------------ daily
def daily(conn):
    cur = conn.cursor()
    names, theses, research = load(cur)
    tickers = [n["ticker"] for n in names]
    etfs = sorted({e for v in THEMES.values() for e in v})
    print(f"Daily check: {len(names)} names ({sum(n['level'] == 'book' for n in names)} in Book), {len(etfs)} theme ETFs")
    created = 0
    rerun_screen = False

    # ---- screen leads: the best Screen names (either track), so a sharp drop is caught before they reach the Watchlist
    cur.execute("""SELECT s.ticker, s.score, s.growth_score, (s.metrics->'px'->>'price')::numeric AS screen_price, s.run_date
                   FROM v2_screen_scores s JOIN v2_names n USING (ticker)
                   WHERE s.run_date = (SELECT MAX(run_date) FROM v2_screen_scores) AND n.level = 'screen'
                     AND COALESCE(n.status, '') <> 'rejected' AND (s.score >= %s OR s.growth_score >= %s)
                   ORDER BY GREATEST(COALESCE(s.score, 0), COALESCE(s.growth_score, 0)) DESC LIMIT 60""", (LEAD_SCORE, LEAD_SCORE))
    leads = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT n.ticker, t.entry_target, t.valuation FROM v2_names n
                   JOIN LATERAL (SELECT * FROM v2_theses WHERE ticker = n.ticker ORDER BY version DESC LIMIT 1) t ON TRUE
                   WHERE n.level = 'screen'""")
    screen_theses = {r["ticker"]: dict(r) for r in cur.fetchall()}

    # ---- prices
    syms = sorted(set(tickers + etfs + [l["ticker"] for l in leads] + list(screen_theses) + MACRO_SYMS))
    df = yf.download(syms, period="2y", interval="1d", group_by="ticker", auto_adjust=True, threads=True, progress=False)
    closes = {}
    for s in syms:
        try:
            c = (df[s] if isinstance(df.columns, pd.MultiIndex) else df)["Close"].dropna()
            if len(c) >= 6:
                closes[s] = c
        except KeyError:
            pass
    health_log["prices_missing"] = [t for t in tickers if t not in closes]
    for t in sorted(set(tickers) | {l["ticker"] for l in leads} | set(MACRO_SYMS)):
        c = closes.get(t)
        if c is None:
            continue
        rows = [(t, d.date(), round(float(v), 4)) for d, v in c.tail(260).items()]
        psycopg2_execute_values(cur, "INSERT INTO v2_prices (ticker, date, close) VALUES %s "
                                     "ON CONFLICT (ticker, date) DO UPDATE SET close = EXCLUDED.close", rows)
    chg = lambda s, n: float(s.iloc[-1] / s.iloc[-1 - n] - 1) if s is not None and len(s) > n else None
    spx5 = chg(closes.get("^GSPC"), 5)

    # ---- market regime
    vix = closes.get("^VIX")
    if vix is not None and float(vix.iloc[-1]) > 25:
        created += alert(cur, "MARKET", "Market stress", "medium",
                         f"VIX at {float(vix.iloc[-1]):.1f} (above 25)", f"vix-{week_key()}")
    tnx = closes.get("^TNX")
    if tnx is not None and len(tnx) > 5:
        bp = (float(tnx.iloc[-1]) - float(tnx.iloc[-6])) * 100
        if abs(bp) >= 25:
            created += alert(cur, "MARKET", "Rates move", "medium",
                             f"US 10-year yield {'up' if bp > 0 else 'down'} {abs(bp):.0f}bp in 5 days to "
                             f"{float(tnx.iloc[-1]):.2f}%", f"tnx-{TODAY}")

    # ---- screen leads on sale, and Screen names with a thesis (passed, rejected or sold) back at their buy level
    for l in leads:
        c = closes.get(l["ticker"])
        if c is None or not l["screen_price"]:
            continue
        p, drop = float(c.iloc[-1]), 1 - float(c.iloc[-1]) / float(l["screen_price"])
        if drop >= LEAD_DROP:
            track = "growth" if (num(l["growth_score"]) or 0) > (num(l["score"]) or 0) else "value"
            sc = max(num(l["growth_score"]) or 0, num(l["score"]) or 0)
            created += alert(cur, l["ticker"], "Screen idea on sale", "medium",
                             f"{track.title()} score {sc:.0f}; price ${p:,.2f} is {drop:.0%} below the screen price of "
                             f"{l['run_date']:%d %b}. Not on your Watchlist yet: + Watch or request a deep dive",
                             f"lead-{l['ticker']}-{week_key()}", action="Consider a deep dive")
    for tk, t in screen_theses.items():
        c = closes.get(tk)
        if c is None:
            continue
        p = float(c.iloc[-1])
        entry = num(t["entry_target"])
        zone, _ = growth_zone(t)
        hit = (entry and p <= entry and "entry target") or (zone and p <= zone and "growth zone")
        if hit:
            created += alert(cur, tk, "Old idea at buy level", "medium",
                             f"${p:,.2f} is at or below its {hit} (${(entry if hit == 'entry target' else zone):,.2f}) "
                             "from an earlier thesis. It is on the Screen, not the Watchlist", f"old-{tk}-{week_key()}",
                             action="Re-check the thesis")

    # ---- macro regime and sensitivities; warn when a plausible shock would hit the Book hard
    try:
        mv_ = macro_view(cur, closes, names)
        if mv_:
            for nm, sc in mv_["scenarios"].items():
                if sc["book"] is not None and sc["book"] <= -0.08 and nm.startswith(("Rates", "Dollar", "Oil")):
                    created += alert(cur, "MARKET", "Macro exposure", "info",
                                     f"{nm} would move the Book about {sc['book']:+.0%} on past sensitivities "
                                     f"(most exposed: {', '.join(t for t, _ in sc['worst'])})", f"macro-{nm[:12]}-{week_key()}")
    except Exception as e:
        print("Macro view failed:", e.__class__.__name__, str(e)[:100])

    # ---- calendar (Fed, BEA, BLS, expiries, earnings) and earnings previews for Watchlist and Book names
    try:
        from events import refresh as cal_refresh
        conn.commit()
        cal_refresh(conn, tickers, log=print)
    except Exception as e:
        print("Calendar refresh failed:", e.__class__.__name__)
        health_log["calendar"] = [e.__class__.__name__]
    cur.execute("""SELECT c.*, n.level FROM v2_calendar c JOIN v2_names n USING (ticker)
                   WHERE c.kind = 'earnings' AND c.date BETWEEN CURRENT_DATE AND CURRENT_DATE + 3 AND n.level IN ('watchlist','book')""")
    for ev in cur.fetchall():
        tk = ev["ticker"]
        th = theses.get(tk) or {}
        pil = ((th.get("sections") or {}).get("pillars") or "")
        im = (ev["detail"] or {}).get("implied_move")
        n_ = next((n for n in names if n["ticker"] == tk), {})
        style, z = book_zones(n_, th, research) if n_ else ("value", {})
        lvl = (f" Buy level {('growth zone' if style == 'growth' else 'entry')} ${z['add']:,.2f}." if z.get("add") else "")
        created += alert(cur, tk, "Earnings preview", "medium" if ev["level"] == "book" else "info",
                         f"Results {ev['date']:%a %d %b}" + (f"; options imply a ±{im:.0%} move" if im else "") + "." + lvl
                         + (f" Watch the pillars: {str(pil)[:220]}" if pil else ""),
                         f"preview-{tk}-{ev['date']}", action="Decide before results: add, wait or hedge")

    # ---- theme sell-offs
    theme_moves = {}
    for theme, basket in THEMES.items():
        moves = [chg(closes.get(e), 5) for e in basket if closes.get(e) is not None]
        moves = [m for m in moves if m is not None]
        if moves:
            theme_moves[theme] = sum(moves) / len(moves)
    for theme, m in theme_moves.items():
        if m <= -0.05:
            held = [n for n in names if theme in themes_of(n["themes"])]
            sev = "high" if m <= -0.10 else "medium"
            detail = (f"{theme} basket down {abs(m):.1%} in 5 days. Exposed: "
                      + (", ".join(f"{n['ticker']} ({n['level']})" for n in held) or "none of your names"))
            created += alert(cur, "MARKET", "Theme sell-off", sev, detail, f"theme-{theme}-{week_key()}")
            for n in held:
                created += alert(cur, n["ticker"], "Theme sell-off", "high" if n["level"] == "book" else "medium",
                                 f"{theme} down {abs(m):.1%} in 5 days", f"theme-{theme}-{n['ticker']}-{week_key()}")
            if m <= -0.10:
                rerun_screen = True

    # ---- per name: price moves, analysts, kill criteria, buy signals
    cik_of = {}
    try:
        cik_of = cik_map()
    except Exception:
        health_log["sec_failed"].append("ticker map")
    kill_hit, news_break = set(), set()
    cur.execute("SELECT DISTINCT ticker FROM v2_news WHERE tag = 'breaks' AND published > CURRENT_DATE - 60")
    news_break = {r["ticker"] for r in cur.fetchall()}

    for n in names:
        tk, book = n["ticker"], n["level"] == "book"
        c = closes.get(tk)
        price = float(c.iloc[-1]) if c is not None else None
        r1, r5 = chg(c, 1), chg(c, 5)
        if r1 is not None and abs(r1) >= 0.07:
            created += alert(cur, tk, "Sharp move", "high" if book else "medium",
                             f"{'Up' if r1 > 0 else 'Down'} {abs(r1):.1%} in one day to ${price:,.2f}", f"move1-{tk}-{TODAY}")
        if r5 is not None and abs(r5) >= 0.12:
            created += alert(cur, tk, "Sharp move", "medium" if book else "info",
                             f"{'Up' if r5 > 0 else 'Down'} {abs(r5):.1%} in 5 days", f"move5-{tk}-{week_key()}")
        if book and r5 is not None and spx5 is not None and r5 - spx5 <= -0.10:
            created += alert(cur, tk, "Lagging the market", "medium",
                             f"{r5:+.1%} vs S&P 500 {spx5:+.1%} over 5 days", f"lag-{tk}-{week_key()}")

        # analysts
        info = {}
        try:
            yt = yf.Ticker(tk)
            info = yt.info or {}
            ud = yt.upgrades_downgrades
            if ud is not None and len(ud):
                ud = ud.reset_index()
                dcol = next((x for x in ud.columns if "date" in str(x).lower()), ud.columns[0])
                ud[dcol] = pd.to_datetime(ud[dcol], errors="coerce").dt.tz_localize(None)
                for _, a in ud[ud[dcol] >= pd.Timestamp(TODAY - dt.timedelta(days=3))].iterrows():
                    if a.get("Action") in ("up", "down"):
                        created += alert(cur, tk, f"Analyst {'upgrade' if a['Action'] == 'up' else 'downgrade'}",
                                         ("medium" if book else "info"),
                                         f"{a.get('Firm')}: {a.get('FromGrade') or '?'} to {a.get('ToGrade')}"
                                         + (f", target ${a['currentPriceTarget']:,.0f}" if num(a.get("currentPriceTarget")) else ""),
                                         f"rating-{tk}-{a.get('Firm')}-{str(a[dcol])[:10]}")
        except Exception:
            health_log["info_failed"].append(tk)
        if info.get("shortPercentOfFloat") is not None or info.get("sharesShort") is not None:
            cur.execute("""INSERT INTO v2_short_interest (ticker, date, short_float, short_ratio, shares_short)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ticker, date) DO UPDATE SET short_float = EXCLUDED.short_float,
                           short_ratio = EXCLUDED.short_ratio, shares_short = EXCLUDED.shares_short""",
                        (tk, TODAY, num(info.get("shortPercentOfFloat")), num(info.get("shortRatio")), num(info.get("sharesShort"))))
        mean = num(info.get("targetMeanPrice"))
        if mean:
            cur.execute("SELECT mean, date FROM v2_analyst WHERE ticker = %s AND date <= %s ORDER BY date DESC LIMIT 1",
                        (tk, TODAY - dt.timedelta(days=5)))
            prev = cur.fetchone()
            cur.execute("""INSERT INTO v2_analyst (ticker, date, mean, high, low, n) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (ticker, date) DO UPDATE SET mean=EXCLUDED.mean, high=EXCLUDED.high,
                             low=EXCLUDED.low, n=EXCLUDED.n""",
                        (tk, TODAY, mean, num(info.get("targetHighPrice")), num(info.get("targetLowPrice")),
                         info.get("numberOfAnalystOpinions")))
            if prev and num(prev["mean"]):
                m = mean / float(prev["mean"]) - 1
                if abs(m) >= 0.05:
                    created += alert(cur, tk, "Analyst targets moved", "medium" if book else "info",
                                     f"Mean target {'up' if m > 0 else 'down'} {abs(m):.0%} to ${mean:,.2f} "
                                     f"(from ${float(prev['mean']):,.2f} on {prev['date']})", f"tgt-{tk}-{week_key()}")
            if book and price and abs(price / mean - 1) <= 0.05:
                created += alert(cur, tk, "Near analyst target", "info",
                                 f"Price ${price:,.2f} is within 5% of the mean target ${mean:,.2f}", f"neartgt-{tk}-{week_key()}")

        # kill criteria (machine-checkable ones in thesis.valuation.kill)
        th = theses.get(tk) or {}
        for k in ((th.get("valuation") or {}).get("kill") or []):
            kind, val = k.get("type"), num(k.get("value"))
            hit = ((kind == "price_below" and price and val and price < val) or
                   (kind == "analyst_mean_below" and mean and val and mean < val))
            if hit:
                kill_hit.add(tk)
                created += alert(cur, tk, "Kill criterion hit", "high",
                                 k.get("text") or f"{kind} {val}", f"kill-{tk}-{kind}-{week_key()}", action="Review the thesis")

        # buy signal (Watchlist)
        entry, src = entry_of(tk, theses, research)
        if n["level"] == "watchlist" and price and entry:
            gap = price / entry - 1
            paused = tk in kill_hit or tk in news_break or n.get("health") == "red"
            if gap <= 0:
                if paused:
                    created += alert(cur, tk, "Buy signal paused", "medium",
                                     f"Price ${price:,.2f} is at the {src} entry target ${entry:,.2f}, but the thesis "
                                     "is flagged (kill criterion, breaking news or red health)", f"buyp-{tk}-{week_key()}")
                else:
                    new = alert(cur, tk, "Buy signal", "high" if src == "thesis" else "medium",
                                f"Price ${price:,.2f} is at or below the {src} entry target ${entry:,.2f} ({gap:+.1%})"
                                + ("" if src == "thesis" else " · no approved thesis yet, so treat as a lead"),
                                f"buy-{tk}-{week_key()}", action="Awaiting Claude's fresh check")
                    if new:
                        cur.execute("UPDATE v2_alerts SET sent_via = 'hold' WHERE dedupe_key = %s", (f"buy-{tk}-{week_key()}",))
                    created += new
            elif gap <= 0.05:
                created += alert(cur, tk, "Approaching entry", "medium",
                                 f"Price ${price:,.2f} is {gap:.1%} above the {src} entry target ${entry:,.2f}",
                                 f"near-{tk}-{week_key()}")

        # Book: add and trim levels by style (value or growth)
        if n["level"] == "book" and price:
            style, z = book_zones(n, theses.get(tk), research)
            paused = tk in kill_hit or tk in news_break or n.get("health") == "red"
            if z["add"] and price <= z["add"]:
                why = (f"the 5-year TAM case returns {((z['then'] / price) ** 0.2 - 1):.0%} a year from here"
                       if style == "growth" and z["then"] else f"at or below the entry target ${z['add']:,.2f}")
                created += alert(cur, tk, "Add zone" if not paused else "Add zone, thesis flagged", "medium",
                                 f"{style.title()} holding at ${price:,.2f}: {why}"
                                 + (" · but the thesis is flagged (kill criterion, breaking news or red health)" if paused else ""),
                                 f"add-{tk}-{week_key()}", action="Consider adding" if not paused else "Review first")
            elif z["trim"] and price >= z["trim"]:
                why = ("beyond the 5-year TAM case (under 5% a year from here)" if style == "growth"
                       else f"above Claude's high value ${z['trim']:,.2f}")
                created += alert(cur, tk, "Trim zone", "medium", f"{style.title()} holding at ${price:,.2f}: {why}",
                                 f"trim-{tk}-{week_key()}", action="Consider trimming")
            elif style == "value" and z["review"] and price >= z["review"]:
                created += alert(cur, tk, "Fair value reached", "info",
                                 f"Price ${price:,.2f} has reached Claude's base value ${z['review']:,.2f}: "
                                 "the margin of safety is gone; hold only if the value has grown", f"fair-{tk}-{week_key()}")

        # growth buy zone (Watchlist names with a TAM target): separate from the value entry target
        zone, then = growth_zone(theses.get(tk))
        if n["level"] == "watchlist" and price and zone and zone > 0:
            gap = price / zone - 1
            irr = (then / price) ** (1 / 5) - 1 if then and then > 0 else None
            paused = tk in kill_hit or tk in news_break or n.get("health") == "red"
            if gap <= 0 and not paused:
                key = f"gbuy-{tk}-{week_key()}"
                new = alert(cur, tk, "Buy signal", "high",
                            f"Growth zone: price ${price:,.2f} is at or below ${zone:,.2f}, where the 5-year TAM case "
                            f"returns {irr:.0%} a year (hurdle {GROWTH_HURDLE:.0%})", key, action="Awaiting Claude's fresh check")
                if new:
                    cur.execute("UPDATE v2_alerts SET sent_via = 'hold' WHERE dedupe_key = %s", (key,))
                created += new
            elif gap <= 0 and paused:
                created += alert(cur, tk, "Buy signal paused", "medium",
                                 f"Growth zone reached (${zone:,.2f}) but the thesis is flagged", f"gbuyp-{tk}-{week_key()}")
            elif gap <= 0.05:
                created += alert(cur, tk, "Approaching growth zone", "medium",
                                 f"Price ${price:,.2f} is {gap:.1%} above the growth zone ${zone:,.2f} "
                                 f"(5-year TAM case would return {irr:.0%} a year from here)", f"gnear-{tk}-{week_key()}")

        # filings (EDGAR)
        cik = cik_of.get(tk)
        if cik:
            try:
                created += filings(cur, tk, cik, book)
            except Exception:
                health_log["sec_failed"].append(tk)
        time.sleep(0.15)

    # ---- buy signals not re-checked by Claude within 20 hours: release them anyway
    cur.execute("""UPDATE v2_alerts SET sent_via = NULL, detail = detail || ' (not yet re-checked by Claude)'
                   WHERE sent_via = 'hold' AND NOT sent AND fired_at < now() - interval '26 hours'""")

    # ---- health
    for n in names:
        tk = n["ticker"]
        why, level = [], "green"
        cur.execute("""SELECT kind, severity FROM v2_alerts WHERE ticker = %s AND NOT addressed
                       AND fired_at > now() - interval '7 days' AND kind NOT IN ('Buy signal','Approaching entry')""", (tk,))
        open_ = cur.fetchall()
        cur.execute("SELECT tag FROM v2_news WHERE ticker = %s AND published > CURRENT_DATE - 30 AND tag IN ('weakens','breaks')", (tk,))
        tags = [r["tag"] for r in cur.fetchall()]
        cur.execute("""SELECT (SELECT mean FROM v2_analyst WHERE ticker = %s ORDER BY date DESC LIMIT 1) AS now_,
                              (SELECT mean FROM v2_analyst WHERE ticker = %s AND date <= CURRENT_DATE - 30 ORDER BY date DESC LIMIT 1) AS then_""",
                    (tk, tk))
        a = cur.fetchone()
        rev = (float(a["now_"]) / float(a["then_"]) - 1) if a and a["now_"] and a["then_"] else None
        if tk in kill_hit:
            level = "red"; why.append("kill criterion hit")
        if "breaks" in tags:
            level = "red"; why.append("news breaks a pillar")
        if any(r["severity"] == "high" for r in open_) and level != "red":
            level = "amber"; why.append("open high alert")
        if "weakens" in tags and level == "green":
            level = "amber"; why.append("news weakens a pillar")
        if rev is not None and rev <= -0.10:
            level = "red" if level == "amber" else "amber" if level == "green" else level
            why.append(f"analyst targets {rev:+.0%} in 30 days")
        rs = research.get(tk) or {}
        si = num((rs.get("ownership") or {}).get("short_float"))
        if si is not None and si > 0.10 and level == "green":
            level = "amber"; why.append(f"short interest {si:.0%} of float")
        ins = rs.get("insiders") or {}
        if (ins.get("signal") == "Net selling" and num(ins.get("sell_value")) and num(rs.get("mcap"))
                and ins["sell_value"] / rs["mcap"] > 0.005 and level == "green"):
            level = "amber"; why.append("heavy insider selling")
        c = closes.get(tk)
        r5 = chg(c, 5)
        if r5 is not None and r5 <= -0.12 and level == "green":
            level = "amber"; why.append(f"price {r5:+.0%} in 5 days")
        # missing or stale data is never shown as healthy
        gaps = []
        if tk in health_log["prices_missing"]:
            gaps.append("no price today")
        if tk in health_log["info_failed"]:
            gaps.append("Yahoo data failed")
        as_of = (rs or {}).get("as_of")
        if not rs:
            gaps.append("no research data")
        elif as_of and (TODAY - dt.date.fromisoformat(str(as_of)[:10])).days > 14:
            gaps.append(f"research data from {as_of}")
        if gaps and level == "green":
            level = "grey"
        why += [f"data: {g}" for g in gaps]
        cur.execute("UPDATE v2_names SET health = %s, health_why = %s WHERE ticker = %s", (level, "; ".join(why), tk))

    # scheduled jobs that have not reported in time
    heartbeat(cur, "daily")
    cur.execute("SELECT key, updated_at FROM v2_settings WHERE key LIKE 'heartbeat:%%'")
    beats = {r["key"].split(":", 1)[1]: r["updated_at"] for r in cur.fetchall()}
    now = dt.datetime.now(dt.timezone.utc)
    late = [f"{j} (last {beats[j]:%d %b %H:%M} UTC)" for j, hrs in HEARTBEAT_MAX_H.items()
            if j in beats and (now - beats[j]).total_seconds() > hrs * 3600]   # a job not yet reporting is not late
    if late:
        health_log["late_jobs"] = late
    failures = {k: v for k, v in health_log.items() if v}
    cur.execute("""INSERT INTO v2_settings (key, value) VALUES ('data_health', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (J({"date": str(TODAY), "failures": failures, "theme_moves": theme_moves,
                    "names": len(names), "rerun_screen": rerun_screen}),))
    conn.commit()
    print(f"Created {created} new alerts. Theme 5-day moves: "
          + ", ".join(f"{k.split(' ')[0]} {v:+.1%}" for k, v in theme_moves.items()))
    if failures:
        print("Data issues:", {k: len(v) for k, v in failures.items()})
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"rerun_screen={'true' if rerun_screen else 'false'}\n")


def psycopg2_execute_values(cur, sql, rows):
    from psycopg2.extras import execute_values
    if rows:
        execute_values(cur, sql, rows)


def filings(cur, tk, cik, book):
    data = SEC.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", timeout=60).json()
    rec = data.get("filings", {}).get("recent", {})
    made = 0
    since = (TODAY - dt.timedelta(days=4)).isoformat()
    for i, form in enumerate(rec.get("form", [])):
        filed = rec["filingDate"][i]
        if filed < since:
            break
        if form not in ("8-K", "8-K/A", "10-Q", "10-K", "4", "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"):
            continue
        acc = rec["accessionNumber"][i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{rec['primaryDocument'][i]}"
        items = [x.strip() for x in (rec.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
        kind = ("; ".join(ITEMS_8K.get(x, f"Item {x}") for x in items) if form.startswith("8-K") else
                {"10-Q": "Quarterly report", "10-K": "Annual report", "4": "Insider transaction"}.get(form, "Activist / 5%+ stake"))
        cur.execute("""INSERT INTO v2_events (ticker, filed, form, kind, detail, url) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (url) DO NOTHING RETURNING id""", (tk, filed, form, kind, ", ".join(items), url))
        if not cur.fetchone():
            continue
        if form == "4":
            continue
        if "2.02" in items or form in ("10-Q", "10-K"):
            cur.execute("UPDATE v2_names SET refresh_due = %s WHERE ticker = %s AND (refresh_due IS NULL OR refresh_due < %s)",
                        (filed, tk, filed))                     # thesis refresh after results (Claude, overnight)
        sev = "high" if set(items) & HIGH_8K else ("medium" if book or "2.02" in items or "13D" in form else "info")
        made += alert(cur, tk, f"New filing: {form}", sev, kind, f"filing-{acc}", source="sec",
                      action="Claude reads it against the thesis" if book else None)
    return made


# ------------------------------------------------------------------ weekly headlines
BAD = ("lawsuit", "investigation", "probe", "subpoena", "restat", "fraud", "downgrade", "recall", "guidance",
       "warns", "layoff", "bankrupt", "default", "short seller", "delist", "charges", "class action", "resigns")


def weekly(conn):
    cur = conn.cursor()
    names, _, _ = load(cur)
    added = 0
    for n in names:
        tk = n["ticker"]
        try:
            yt = yf.Ticker(tk)
            info = yt.info or {}
            news = yt.news or []
        except Exception:
            health_log["news_failed"].append(tk)
            continue
        short = (info.get("shortName") or n.get("name") or "").replace(",", "").split()
        keys = {tk.lower()} | ({short[0].lower()} if short and len(short[0]) > 3 else set())
        picked = []
        for item in news:
            c = item.get("content", item)
            title = c.get("title") or ""
            url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else None) or c.get("link")
            date = (c.get("pubDate") or "")[:10] or None
            tl = f" {title.lower()} "
            if not url or not any(f" {k} " in tl or f"{k}'" in tl or f"({k})" in tl or f" {k}:" in tl for k in keys):
                continue
            if date and date < (TODAY - dt.timedelta(days=8)).isoformat():
                continue
            pub = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
            picked.append((title, url, date, pub))
        for title, url, date, pub in picked[:5]:
            cur.execute("""INSERT INTO v2_news (ticker, published, headline, url, source) VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (url) DO NOTHING RETURNING id""", (tk, date or TODAY, title, url, pub or "Yahoo"))
            added += cur.fetchone() is not None
        time.sleep(0.2)
    conn.commit()
    print(f"Weekly headlines: {added} new for {len(names)} names.")


# ------------------------------------------------------------------ export for Claude's tasks
TAM_EXAMPLE = {   # growth target: reference only, shown on the stock page; never feeds the entry target or signals
    "market": "what market(s) the company sells into", "tam_now_bn": 400, "source": "who sizes the market, with date",
    "why_share": "why this share is reachable (right to win, current share and trend)",
    "what_must_go_right": "the 2-3 things that must happen",
    "scenarios": [{"years": 5, "segments": [{"name": "Segment", "tam_bn": 800, "share_pct": 25, "net_margin_pct": 30}],
                   "other_rev_bn": 0, "other_margin_pct": 0, "exit_pe": 25, "shares_m": 2500, "dr": 10,
                   "note": "growth rate the TAM assumes, and the source"},
                  {"years": 10, "segments": [{"name": "Segment", "tam_bn": 1500, "share_pct": 25, "net_margin_pct": 30}],
                   "other_rev_bn": 0, "other_margin_pct": 0, "exit_pe": 20, "shares_m": 2500, "dr": 10, "note": "..."}]}

EXEC_EXAMPLE = {   # executive summary: shown first on the stock page, the Decide page, the Book and in Telegram
    "recommendation": "BUY|WAIT|PASS for Watchlist names; ADD|HOLD|TRIM|CUT for Book names",
    "style": "value|growth", "horizon": "e.g. 5-10 years",
    "one_line": "the whole case and the call in one or two sentences, with the key number",
    "how_it_makes_money": [{"segment": "what it sells and to whom", "share": "~x% of revenue / profit", "note": "latest figure and growth"}],
    "why": ["3 reasons for the call, each with a number"],
    "key_risks": [{"risk": "...", "watch": "the KPI or event and its breach level"}],
    "plan": {"action": "exactly what to do (and how much, if Ming holds it)", "add_below": 0.0, "trim_above": 0.0, "cut_if": "..."},
    "position": "shares, cost, weight if held"}

ASSUMPTIONS_EXAMPLE = {
    "revenue0": None, "shares0": None, "net_debt": None,
    "profile": {"model": "standard|bank|insurer|reit|commodity|pre_profit", "cyclical": False, "nd_ebitda": 0.5,
                "pre_profit": False, "moat": "widening|stable|narrowing|unproven", "roic_10y_above_15": True, "stable_revenue": True},
    "scenarios": {k: {"growth": [0.10, 0.09, 0.08, 0.07, 0.06], "margin": [0.30, 0.32], "reinvest_pct": 0.06, "tax": 0.18,
                      "share_change": -0.01, "terminal_growth": 0.025, "exit_multiple": None, "grade": "E",
                      "why": "one line per scenario"} for k in ("bear", "base", "bull")},
    "weights": {"bear": 0.25, "base": 0.5, "bull": 0.25}}

SIM_SCENARIO_EXAMPLE = {
    "id": "seat-to-agent", "name": "AI agents replace 20% of seats; agent pricing recovers half",
    "kind": "pricing|risk|tam|upside", "probability": 0.25, "grade": "C|D|E",
    "description": "what happens, the mechanism, and the evidence (with source and date)",
    "changes": {"growth_delta": [-0.02, -0.03, -0.03, -0.02, -0.01], "margin_delta": -0.02,
                "revenue_shocks": [{"year": 2, "pct": -0.08, "phase_years": 3}], "share_change_delta": 0.0},
    "watch": "the KPI or event that tells us this is happening"}

RESEARCH_RULES = [
    "Never type in a valuation number the engine can compute: write 'assumptions' (bear, base, bull) and let the app compute values.",
    "Grade every material claim A-E (A primary source, B reliable third party, C management, D analyst estimate, E AI assumption), "
    "with source and date. Grade E claims can support a pillar only with a measurable KPI.",
    "Name the moat source with grade A-C evidence, or the moat is 'unproven' and conviction is capped at 60.",
    "Quality gate before any deep dive: check 'quality_gate' on each queue item; a failed gate means write a short PASS note, "
    "not a full thesis, unless Ming requested it.",
    "Market confirmation (momentum, estimates, 13F, insiders, newsletter buzz) can raise rank but never replaces business or industry quality.",
    "Banks, insurers, REITs and commodity producers: set profile.model; the engine marks DCF not valid.",
    "Every thesis states 'change_my_mind' and tags 1-3 'drivers' (shared economic exposures).",
    "Every thesis carries 3-6 'sim_scenarios' (SIM_SCENARIO_EXAMPLE shape): one per key risk, any pricing-model change "
    "(e.g. per-seat to usage or AI-agent pricing: revenue_shocks for the transition dip and growth_delta for the new run-rate), "
    "and the TAM upside; each with a probability, an evidence grade and the KPI to watch.",
    "Second opinion: write your own assumptions from the data pack BEFORE reading Claude's thesis; then attack it. Never average values.",
]

INBOX_FORMAT = {
    "where": f"Save one JSON file per task run into Drive folder ValueInvestor/v2-inbox (id {DRIVE_INBOX}). "
             "Name it <type>-<YYYY-MM-DD>-<short>.json. Never edit or delete other files.",
    "reason_cards": {"type": "reason_cards", "items": [{"ticker": "AMAT", "why_cheap": "...", "who_selling": "...",
                                                       "market_missing": "...", "buyers_back": "..."}]},
    "thesis": {"type": "thesis", "ticker": "AMAT", "add_to_watchlist": True, "themes": ["AI and energy infrastructure"],
               "conviction": 72, "verdict": "BUY|WATCH|PASS", "entry_target": 150.0,
               "valuation": {"iv_low": 140, "iv_base": 200, "iv_high": 260,
                             "inputs": {"fcf_ps": 7.5, "g1": 8, "g2": 5, "tg": 2.5, "dr": 9.5},
                             "kill": [{"type": "price_below|analyst_mean_below|manual", "value": 120.0, "text": "..."}],
                             "tam": TAM_EXAMPLE},
               "exec_summary": EXEC_EXAMPLE,
               "sections": {k: "text" for k in ["industry_map", "size_growth", "structure", "demand_supply",
                                                 "industry_risks", "outlook", "winners", "business", "right_to_win",
                                                 "moat", "tam_competitors", "management", "financials",
                                                 "company_risks", "variant_view", "valuation", "pillars",
                                                 "catalysts_flow", "premortem"]}},
    "thesis_v2_additions": {
        "note": "Add these keys to every thesis and refresh item (9 Nov 2026 revisions). The app's valuation engine computes "
                "iv_low/base/high, entry_target and the growth zone from 'assumptions'; typed-in values are only a fallback.",
        "run_id": "dd-<TICKER>-<YYYYMMDD> (unique per run; a re-saved file with the same run_id is skipped)",
        "style": "value|growth",
        "assumptions": ASSUMPTIONS_EXAMPLE,
        "claims": [{"claim": "Revenue grew 16% in FY25", "grade": "A", "source": "10-K FY25", "date": "2025-07-30", "pillar": 1},
                   {"claim": "Can reach 25% share of the market by 2030", "grade": "E", "source": None, "pillar": 2}],
        "moat_source": {"type": "pricing power|switching costs|network effects|cost advantage|distribution|IP or regulation",
                        "grade": "A|B|C", "evidence": "the fact that proves it, with its source"},
        "change_my_mind": "the evidence that would invalidate the thesis even if the price rises",
        "drivers": ["hyperscaler capex", "rates", "China", "oil", "consumer spending", "enterprise IT spend"],
        "expected": {"revenue_growth_next_year": 0.12, "op_margin_next_year": 0.30, "notes": "what must be true in 12 months"},
        "sim_scenarios": "3-6 ready-made what-if scenarios for the stock-page simulator, each exactly SIM_SCENARIO_EXAMPLE: "
                         "the key risks, any pricing-model change, the TAM upside; changes are deltas to the base case"},
    "second_opinion": {"type": "second_opinion", "run_id": "so-<TICKER>-<YYYYMMDD>", "ticker": "AMAT",
                       "model": "claude-second|chatgpt", "verdict": "BUY|WATCH|PASS",
                       "assumptions": "ASSUMPTIONS_EXAMPLE shape, written BEFORE reading Claude's thesis",
                       "attacks": [{"point": "the bear argument", "evidence": "fact + source", "grade": "A-E",
                                    "pillar": 1, "category": "factual|financial|valuation|interpretation"}],
                       "summary": "two lines"},
    "scenarios": {"type": "scenarios", "items": [{"ticker": "NOW", "scenarios": [SIM_SCENARIO_EXAMPLE]}]},
    "answers": {"type": "answers", "run_id": "qa-<YYYYMMDDHH>", "items": [{"id": 12, "answer": "plain-English answer with numbers",
                "claims": "check only: [{claim, verdict supported|partly|contradicted|unverifiable, evidence (fact + source), grade A-E}]",
                "review": "challenge only: {verdict, assumptions, scores, improvements, attacks, summary} exactly like second_opinion",
                "sources": [{"title": "10-Q Q3 FY26", "url": "https://...", "date": "2026-10-29"}],
                "proposal": {"ticker": "NOW", "change": "what should change in the thesis and why", "priority": "high|medium|low"}}],
                "scenarios": [{"ticker": "NOW", "scenarios": [SIM_SCENARIO_EXAMPLE]}]},
    "rebuttal": {"type": "second_opinion", "run_id": "rb-<TICKER>-<YYYYMMDD>", "ticker": "AMAT", "model": "claude",
                 "rebuttals": [{"attack": 0, "response": "answer with evidence", "resolved": True}],
                 "improvements_applied": [0, 2]},
    "second_opinion_improvements": [{"area": "evidence|assumptions|pillars|kill_criteria|risks|valuation|consistency|process",
                                     "suggestion": "the specific change", "why": "what is weak now, with the fact",
                                     "how": "the source or step that fixes it", "priority": "high|medium|low"}],
    "second_opinion_scores": {"evidence": "1-5", "assumptions": "1-5", "pillars_measurable": "1-5",
                              "kill_criteria": "1-5", "risk_coverage": "1-5", "consistency": "1-5"},
    "tam": {"type": "tam", "items": [{"ticker": "AMAT", "tam": TAM_EXAMPLE}]},
    "exec_summary": {"type": "exec_summary", "items": [{"ticker": "AMAT", "exec_summary": EXEC_EXAMPLE}]},
    "refresh": {"type": "refresh", "items": [{"ticker": "AMAT", "conviction": 70, "verdict": "BUY|WATCH|PASS", "entry_target": 150.0,
                                              "valuation": {"iv_low": 140, "iv_base": 200, "iv_high": 260,
                                                            "inputs": {"fcf_ps": 7.5, "g1": 8, "g2": 5, "tg": 2.5, "dr": 9},
                                                            "kill": [], "tam": TAM_EXAMPLE},
                                              "pillars": [{"pillar": "...", "status": "intact|watch|breached", "note": "result vs the KPI"}],
                                              "summary": "two or three lines: what the results said and what changed",
                                              "full_reunderwrite": False, "exec_summary": EXEC_EXAMPLE}]},
    "week_ahead": {"type": "week_ahead", "summary": "3-5 lines: what matters this week and why",
                   "events": [{"date": "2026-11-18", "title": "...", "why": "...", "names": ["NVDA"],
                               "playbook": "what to do before/after (e.g. wait to add until after results)"}],
                   "calendar": [{"date": "2026-11-20", "time_et": "10:00", "kind": "policy|macro|election|company",
                                 "title": "...", "detail": {"why": "...", "source": "url"}}]},
    "newsletter_tags": {"type": "newsletter_tags", "sources_read": ["Morningstar Morning Digest", "Money Stuff"],
                        "digest": {"top": [{"headline": "the story in one line, with its number",
                                            "why": "why it matters for an investor (which sectors, rates, earnings)",
                                            "tickers": ["NVDA"], "sources": ["Morningstar", "The Daily Upside"],
                                            "url": "Gmail viewUrl of the best issue covering it"}],
                                   "mood": "one line: the overall market tone across the newsletters"},
                        "issues": [{"date": "2026-11-10", "source": "The Daily Upside", "subject": "email subject",
                                    "headline": "one line: the issue's single most important story, with its number",
                                    "summary": "2-3 lines: the other top stories", "tickers": ["NVDA"],
                                    "url": "the Gmail viewUrl of the email thread"}],
                        "items": [{"date": "2026-11-10", "source": "The Daily Upside", "subject": "email subject",
                                   "kind": "stock|macro|theme", "ticker": "NVDA (stock only)", "topic": "rates|election|AI capex|... (macro/theme)",
                                   "stance": "positive|negative|neutral", "point": "one line: the claim and the number behind it",
                                   "thesis_relevant": False, "hype": False}]},
    "news_tags": {"type": "news_tags", "items": [{"id": 123, "tag": "no change|strengthens|weakens|breaks",
                                                   "note": "one line on why"}]},
    "filing_tags": {"type": "filing_tags", "items": [{"id": 45, "tag": "no change|strengthens|weakens|breaks",
                                                       "note": "one line"}]},
    "buy_checks": {"type": "buy_checks", "items": [{"ticker": "VST", "verdict": "holds|weakened",
                                                     "note": "what changed since the last review"}]},
    "outlook": {"type": "outlook", "theme": "Critical resources", "kind": "light|full",
                "content": {"summary": "...", "demand_supply": "...", "risks": "...", "beneficiaries": ["..."]}},
}


def quality_gate(r, track="value"):
    """Deep-dive quality gate (proposed, unvalidated until the screening lab reports). Uses the weekly research data.
    Pass when: gross margin stable or rising over 3 years OR ROIC above 10%; free cash flow positive OR runway above
    24 months; share count growing under 5% a year; no accounting red flag. Growth names failing only the cash test
    pass with a moat score of 65+ (moat points as a share of the maximum)."""
    hist = [h for h in (r.get("history") or []) if h]
    if len(hist) < 3:
        return {"pass": None, "why": ["not enough history in the research data"]}
    last3 = hist[-3:]
    gm = [h.get("gross_margin") for h in last3 if h.get("gross_margin") is not None]
    roic = [h.get("roic") if h.get("roic") is not None else h.get("roc") for h in last3]
    roic = [x for x in roic if x is not None]
    fails, notes = [], []
    margin_ok = (len(gm) >= 2 and gm[-1] >= gm[0] - 0.01) or (roic and roic[-1] > 0.10)
    if not margin_ok:
        fails.append("gross margin falling and ROIC 10% or less")
    fcf = [h.get("fcf") for h in last3 if h.get("fcf") is not None]
    cash_ok = bool(fcf and fcf[-1] > 0)
    if not cash_ok:
        cash = num(((r.get("valuation") or {}).get("assumptions") or {}).get("Net debt"))
        burn = -fcf[-1] if fcf and fcf[-1] < 0 else None
        if cash is not None and burn and -cash / burn > 2:
            cash_ok = True
            notes.append("cash-burning but runway above 24 months")
    sh = [h.get("shares") for h in hist[-4:] if h.get("shares")]
    if len(sh) >= 2 and (sh[-1] / sh[0]) ** (1 / (len(sh) - 1)) - 1 >= 0.05:
        fails.append("share count growing 5%+ a year")
    flags = " ".join(str(f.get("text") or f) for f in (r.get("red_flags") or [])).lower()
    if any(w in flags for w in ("restat", "auditor", "late filing", "material weakness")):
        fails.append("accounting red flag")
    moat = r.get("moat") or {}
    moat_pct = (moat.get("points") or 0) / moat["of"] if moat.get("of") else 0
    if not cash_ok:
        if track == "growth" and moat_pct >= 0.65:
            notes.append("fails the cash test but passes on moat score")
        else:
            fails.append("free cash flow negative and runway under 24 months")
    return {"pass": not fails, "why": fails + notes, "rule_status": "proposed (unvalidated)"}


def export(conn):
    cur = conn.cursor()
    ensure_schema(cur)
    names, theses, research = load(cur)
    tickers = [n["ticker"] for n in names]
    px = {}
    cur.execute("SELECT DISTINCT ON (ticker) ticker, close, date FROM v2_prices WHERE ticker = ANY(%s) ORDER BY ticker, date DESC", (tickers,))
    for r in cur.fetchall():
        px[r["ticker"]] = float(r["close"])
    cur.execute("SELECT * FROM v2_screen_runs ORDER BY run_date DESC LIMIT 1")
    run = cur.fetchone()
    screen = None
    if run:
        top = run["top10"] or []
        cur.execute("SELECT ticker, metrics FROM v2_screen_scores WHERE run_date = %s AND ticker = ANY(%s)",
                    (run["run_date"], [t["ticker"] for t in top]))
        met = {r["ticker"]: r["metrics"] for r in cur.fetchall()}
        cur.execute("SELECT ticker, reason_card IS NOT NULL AS has_card FROM v2_names WHERE ticker = ANY(%s)",
                    ([t["ticker"] for t in top],))
        cards = {r["ticker"]: r["has_card"] for r in cur.fetchall()}
        screen = {"date": str(run["run_date"]), "stages": run.get("stages"), "market": run.get("market"),
                  "top10": [{**t, "has_reason_card": cards.get(t["ticker"], False),
                             "name": (met.get(t["ticker"]) or {}).get("name"),
                             "sector": (met.get(t["ticker"]) or {}).get("sector"),
                             "industry": (met.get(t["ticker"]) or {}).get("industry"),
                             "fin": {k: ((met.get(t["ticker"]) or {}).get("fin") or {}).get(k) for k in
                                     ("pe", "pb", "ev_ebitda", "fcf_yield", "roc_avg", "rev_cagr", "net_margin",
                                      "nd_ebitda", "tests_passed", "tests_available")},
                             "px": {k: ((met.get(t["ticker"]) or {}).get("px") or {}).get(k) for k in
                                    ("price", "drawdown", "ret_3m", "high_52w")}} for t in top]}
    if run:
        gtop = run.get("growth_top10") or []
        cur.execute("SELECT ticker, metrics FROM v2_screen_scores WHERE run_date = %s AND ticker = ANY(%s)",
                    (run["run_date"], [t["ticker"] for t in gtop]))
        gmet = {r["ticker"]: r["metrics"] or {} for r in cur.fetchall()}
        cur.execute("SELECT ticker, reason_card IS NOT NULL AS has_card FROM v2_names WHERE ticker = ANY(%s)",
                    ([t["ticker"] for t in gtop],))
        gcards = {r["ticker"]: r["has_card"] for r in cur.fetchall()}
        screen["growth_top10"] = [{**t, "has_reason_card": gcards.get(t["ticker"], False),
                                   "name": gmet.get(t["ticker"], {}).get("name"),
                                   "industry": gmet.get(t["ticker"], {}).get("industry"),
                                   "growth": {k: (gmet.get(t["ticker"], {}).get("growth") or {}).get(k) for k in
                                              ("g_now", "accel", "cagr", "rule_of_40", "themes", "peer_growth",
                                               "peer_accel", "smart_money", "rs_6m")},
                                   "fin": {k: (gmet.get(t["ticker"], {}).get("fin") or {}).get(k) for k in
                                           ("gross_margin", "op_margin", "roc_latest", "ev_rev", "pe", "fcf_margin")},
                                   "px": {k: (gmet.get(t["ticker"], {}).get("px") or {}).get(k) for k in
                                          ("price", "drawdown", "ret_6m")}} for t in gtop]
    promoted = [t["ticker"] for t in (screen or {}).get("top10", []) if t.get("promote")]
    g_promoted = [t["ticker"] for t in (screen or {}).get("growth_top10", []) if t.get("promote")]
    cur.execute("""SELECT n.ticker, n.name, n.sector, n.industry,
                     CASE WHEN n.status = 'deep_dive_requested' THEN 1
                          WHEN n.ticker = ANY(%s) OR n.ticker = ANY(%s) THEN 2 ELSE 3 END AS priority,
                     CASE WHEN n.ticker = ANY(%s) THEN 'growth' ELSE 'value' END AS track,
                     CASE WHEN n.status = 'deep_dive_requested' THEN 'requested by Ming'
                          WHEN n.ticker = ANY(%s) THEN 'cleared this week''s growth deep-dive bar'
                          WHEN n.ticker = ANY(%s) THEN 'cleared this week''s value deep-dive bar'
                          ELSE 'legacy candidate (CORE/HIGH) to re-underwrite' END AS why
                   FROM v2_names n
                   WHERE n.status = 'deep_dive_requested'
                      OR ((n.ticker = ANY(%s) OR n.ticker = ANY(%s) OR n.status = 'candidate')
                          AND NOT EXISTS (SELECT 1 FROM v2_theses t WHERE t.ticker = n.ticker))
                   ORDER BY priority, n.ticker""",
                (promoted, g_promoted, g_promoted, g_promoted, promoted, promoted, g_promoted))
    dd = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT ON (ticker) ticker, data FROM v2_research WHERE ticker = ANY(%s) ORDER BY ticker, as_of DESC",
                ([d["ticker"] for d in dd],))
    rs = {r["ticker"]: r["data"] for r in cur.fetchall()}
    for i, d in enumerate(dd):
        r = (rs.get(d["ticker"]) or {}) if i < 8 else {}       # full research for the next 8 only, to keep the file small
        d["research"] = {k: r.get(k) for k in ("valuation", "moat", "risk", "red_flags", "peers", "analysts", "history",
                                                "business", "insiders", "ownership")} if r else None

    for d in dd:
        d["quality_gate"] = quality_gate(rs.get(d["ticker"]) or {}, d["track"])
    # second opinion: new theses (last 10 days) with no review yet; open attacks waiting for Claude's answer
    cur.execute("""SELECT t.ticker, t.version, t.verdict, t.conviction, t.created_at, n.level, n.style
                   FROM v2_theses t JOIN v2_names n USING (ticker)
                   WHERE t.author = 'claude' AND t.created_at > now() - interval '10 days'
                     AND NOT EXISTS (SELECT 1 FROM v2_second_opinion s WHERE s.ticker = t.ticker AND s.thesis_version = t.version)
                   ORDER BY (n.level = 'book') DESC, t.created_at LIMIT 6""")
    so_queue = [dict(r) for r in cur.fetchall()]
    for r in so_queue:
        rr = research.get(r["ticker"])
        if rr is None:
            cur.execute("SELECT data FROM v2_research WHERE ticker = %s ORDER BY as_of DESC LIMIT 1", (r["ticker"],))
            rr = (cur.fetchone() or {}).get("data") or {}
        r["data_pack"] = {k: rr.get(k) for k in ("price", "shares", "mcap", "sector", "industry", "history", "peers",
                                                 "analysts", "moat", "risk", "red_flags")}
    cur.execute("""SELECT DISTINCT ON (ticker) ticker, thesis_version, result->'attacks' AS attacks,
                          result->'improvements' AS improvements, created_at
                   FROM v2_second_opinion WHERE kind = 'review'
                     AND ((result->>'open_attacks')::int > 0
                          OR EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(result->'improvements', '[]'::jsonb)) i
                                     WHERE NOT COALESCE((i->>'applied')::boolean, FALSE)))
                     AND created_at > now() - interval '30 days' ORDER BY ticker, created_at DESC""")
    open_attacks = [dict(r) for r in cur.fetchall()]
    # Watchlist and Book theses still without an assumptions file: the engine cannot compute their values yet
    cur.execute("""SELECT q.id, q.ticker, q.kind, q.text, q.url, q.parent_id, q.section, q.via, q.asked_at, n.level FROM v2_questions q
                   LEFT JOIN v2_names n USING (ticker) WHERE q.status = 'open' ORDER BY q.asked_at LIMIT 10""")
    open_q = [dict(r) for r in cur.fetchall()]
    for q in open_q:
        thread, pid, hops = [], q.get("parent_id"), 0          # the earlier questions and answers in this thread
        while pid and hops < 10:
            cur.execute("SELECT id, kind, text, answer, parent_id, url FROM v2_questions WHERE id = %s", (pid,))
            p_ = cur.fetchone()
            if not p_:
                break
            thread.insert(0, {"id": p_["id"], "kind": p_["kind"], "you": p_["text"], "url": p_["url"], "claude": p_["answer"]})
            pid, hops = p_["parent_id"], hops + 1
        q["thread"] = thread
        th = theses.get(q["ticker"]) if q["ticker"] else None
        if q["ticker"] and not th:
            cur.execute("SELECT * FROM v2_theses WHERE ticker = %s ORDER BY version DESC LIMIT 1", (q["ticker"],))
            th = cur.fetchone()
        if th and q.get("section"):
            q["section_text"] = (th.get("sections") or {}).get(q["section"])
        if th:
            v = th.get("valuation") or {}
            q["thesis"] = {"version": th.get("version"), "verdict": th.get("verdict"), "conviction": th.get("conviction"),
                           "entry_target": th.get("entry_target"), "exec_summary": (th.get("sections") or {}).get("exec_summary"),
                           "pillars": (th.get("sections") or {}).get("pillars"), "variant_view": (th.get("sections") or {}).get("variant_view"),
                           "assumptions": (v.get("engine") or {}).get("assumptions"), "sim_scenarios": v.get("sim_scenarios")}
    scen_queue = [n["ticker"] for n in sorted(names, key=lambda n: n["level"] != "book")
                  if theses.get(n["ticker"]) and not (theses[n["ticker"]].get("valuation") or {}).get("sim_scenarios")][:2]
    cur.execute("""SELECT i.ticker, i.via, i.source, i.note, i.url, i.added_at, n.level, n.name, n.sector, n.industry
                   FROM v2_ideas i JOIN v2_names n USING (ticker) WHERE i.status = 'pending' ORDER BY i.added_at""")
    idea_queue = [dict(r) for r in cur.fetchall()]
    for r in idea_queue:
        cur.execute("SELECT data FROM v2_research WHERE ticker = %s ORDER BY as_of DESC LIMIT 1", (r["ticker"],))
        rr = (cur.fetchone() or {}).get("data") or {}
        r["research"] = {k: rr.get(k) for k in ("as_of", "price", "shares", "mcap", "sector", "industry", "valuation", "moat",
                                                "risk", "red_flags", "peers", "analysts", "history", "business", "insiders",
                                                "ownership")} if rr else None
    backfill = [{"ticker": n["ticker"], "level": n["level"], "style": n.get("style"),
                 "thesis_version": (theses.get(n["ticker"]) or {}).get("version"),
                 "iv_base_typed": ((theses.get(n["ticker"]) or {}).get("valuation") or {}).get("iv_base"),
                 "price": px.get(n["ticker"]),
                 "data_pack": {k: (research.get(n["ticker"]) or {}).get(k) for k in ("shares", "sector", "industry", "history")}}
                for n in sorted(names, key=lambda n: n["level"] != "book")
                if theses.get(n["ticker"]) and not ((theses[n["ticker"]].get("valuation") or {}).get("engine") or {}).get("assumptions")][:3]

    def summary(n):
        th = theses.get(n["ticker"]) or {}
        s = th.get("sections") or {}
        entry, src = entry_of(n["ticker"], theses, research)
        return {"ticker": n["ticker"], "name": n["name"], "level": n["level"], "core": n["is_core"],
                "themes": n["themes"], "health": n.get("health"), "price": px.get(n["ticker"]),
                "entry_target": entry, "entry_source": src, "thesis_version": th.get("version"),
                "thesis_date": str(th.get("created_at"))[:10] if th else None, "conviction": th.get("conviction"),
                "variant_view": s.get("variant_view"), "pillars": s.get("pillars"),
                "kill": (th.get("valuation") or {}).get("kill"),
                "legacy_thesis": None if th else ((n.get("legacy") or {}).get("thesis") or {}).get("investment_case")}

    cur.execute("SELECT id, ticker, published, headline, url, source FROM v2_news WHERE tag IS NULL "
                "AND published > CURRENT_DATE - 14 ORDER BY ticker, published DESC")
    news = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT e.id, e.ticker, e.filed, e.form, e.kind, e.url FROM v2_events e JOIN v2_names n USING (ticker)
                   WHERE n.level = 'book' AND e.form <> '4' AND e.filed > CURRENT_DATE - 7 AND e.tag IS NULL
                   ORDER BY e.filed DESC""")
    filings_ = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT ticker, detail, fired_at FROM v2_alerts WHERE kind = 'Buy signal' AND sent_via = 'hold' AND NOT sent")
    pending = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT ON (theme) theme, as_of, kind FROM v2_industry_outlooks ORDER BY theme, as_of DESC")
    outlooks = [dict(r) for r in cur.fetchall()]
    # thesis refresh after results: Watchlist and Book names with a thesis older than their latest results
    cur.execute("""SELECT n.ticker, n.level, n.style, n.refresh_due, t.version, t.created_at, t.conviction, t.verdict,
                          t.entry_target, t.valuation, t.sections->'pillars' AS pillars, t.sections->'variant_view' AS variant_view
                   FROM v2_names n JOIN LATERAL (SELECT * FROM v2_theses WHERE ticker = n.ticker ORDER BY version DESC LIMIT 1) t ON TRUE
                   WHERE n.level IN ('watchlist','book') AND n.refresh_due IS NOT NULL AND t.created_at::date <= n.refresh_due
                   ORDER BY (n.level = 'book') DESC, n.refresh_due LIMIT 6""")
    refresh = []
    for r in cur.fetchall():
        r = dict(r)
        cur.execute("SELECT form, kind, filed, url FROM v2_events WHERE ticker = %s AND filed >= %s AND form <> '4' ORDER BY filed",
                    (r["ticker"], r["refresh_due"] - dt.timedelta(days=3)))
        r["filings"] = [dict(f) for f in cur.fetchall()]
        refresh.append(r)
    cur.execute("SELECT date, time_et, kind, ticker, title, detail FROM v2_calendar WHERE date BETWEEN CURRENT_DATE AND CURRENT_DATE + 21 "
                "ORDER BY date, time_et")
    calendar = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT value FROM v2_settings WHERE key = 'macro'")
    macro = (cur.fetchone() or {}).get("value")
    cur.execute("""SELECT date, source, kind, ticker, topic, stance, point, thesis_relevant, hype FROM v2_newsletter_mentions
                   WHERE date >= CURRENT_DATE - 7 ORDER BY date DESC, kind, ticker NULLS LAST LIMIT 150""")
    newsletters = [dict(r) for r in cur.fetchall()]
    state = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "app_url": APP_URL, "inbox_folder_id": DRIVE_INBOX,
             "inbox_format": INBOX_FORMAT, "themes": list(THEMES), "screen": screen, "deep_dive_queue": dd,
             "watchlist_and_book": [summary(n) for n in names], "untagged_news": news, "new_book_filings": filings_,
             "pending_buy_checks": pending, "latest_outlooks": outlooks, "refresh_queue": refresh,
             "calendar_next_21d": calendar, "macro": macro,
             "newsletters_7d": newsletters, "research_rules": RESEARCH_RULES,
             "second_opinion_queue": so_queue, "open_red_team_points": open_attacks, "engine_backfill_queue": backfill, "idea_queue": idea_queue,
             "open_questions": open_q, "sim_scenario_queue": scen_queue}
    text = json.dumps(state, default=str, indent=1)
    out = Path(__file__).parent.parent / "outbox"
    out.mkdir(exist_ok=True)
    (out / "state.json").write_text(text)
    try:
        sent = drive_update(DRIVE_STATE_FILE, text)
    except Exception as e:
        sent = False
        print("Drive update failed:", e.__class__.__name__, str(e)[:120])
    print(f"Exported state ({len(text) // 1024} KB): {len(names)} names, {len(dd)} deep-dive requests, "
          f"{len(news)} untagged headlines, {len(filings_)} new Book filings, {len(pending)} buy checks"
          + (" · saved to Drive" if sent else " · local only (no Drive credentials)"))


if __name__ == "__main__":
    conn = db()
    c = conn.cursor()
    ensure_schema(c)
    conn.commit()
    {"daily": daily, "weekly": weekly, "export": export}[MODE](conn)
    conn.close()
