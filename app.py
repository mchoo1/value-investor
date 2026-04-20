"""
Value Investor - Flask Backend
Run: python app.py
Then open: http://localhost:5001
"""
import os
import sys
import json
import math
from datetime import datetime

# Ensure the project directory is in path
sys.path.insert(0, os.path.dirname(__file__))

# Support Railway / cloud: data dir can be overridden via DATA_DIR env var
# On Vercel the app directory is read-only — fall back to /tmp
_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    _DATA_DIR = "/tmp/valueinvestor_data"
    os.makedirs(_DATA_DIR, exist_ok=True)
os.environ.setdefault("DATA_DIR", _DATA_DIR)

from flask import Flask, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
import database as db
import stock_data as sd
import valuation as val


def _nan_to_null(obj):
    """Recursively convert float NaN / Inf to None so JSON output is always valid."""
    if isinstance(obj, dict):
        return {k: _nan_to_null(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nan_to_null(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


class _NaNSafeJSONProvider(DefaultJSONProvider):
    """Flask 3.x JSON provider that converts NaN/Inf → null before serialisation."""
    def dumps(self, obj, **kwargs):
        return super().dumps(_nan_to_null(obj), **kwargs)


app = Flask(__name__, static_folder="static")
app.json_provider_class = _NaNSafeJSONProvider
app.json = _NaNSafeJSONProvider(app)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0   # never cache static files

try:
    db.init_db()
except Exception as _e:
    print(f"[warn] db.init_db() failed: {_e}")


# ── Serve Frontend ───────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def health():
    """Quick ping so start.bat can wait until Flask is ready."""
    return jsonify({"status": "ok"})


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# ══════════════════════════════════════════════════════════════════
# STOCK DATA ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.route("/api/stock/<ticker>", methods=["GET"])
def get_stock(ticker):
    """Get stock fundamentals and info."""
    info = sd.get_stock_info(ticker.upper())
    return jsonify(info)


@app.route("/api/stock/<ticker>/history", methods=["GET"])
def get_stock_history(ticker):
    """Get historical price data (monthly, 5 years)."""
    period = request.args.get("period", "5y")
    data = sd.get_price_history(ticker.upper(), period)
    return jsonify(data)


@app.route("/api/stock/<ticker>/financials", methods=["GET"])
def get_financials(ticker):
    """Get 5-year financial history."""
    data = sd.get_financial_history(ticker.upper())
    return jsonify(data)


# ══════════════════════════════════════════════════════════════════
# SCREENER
# ══════════════════════════════════════════════════════════════════

@app.route("/api/screen", methods=["POST"])
def screen():
    """
    Screen stocks based on value investing filters.
    Body: { market, filters, custom_tickers, sector }
    New strategy-aligned filters supported inside filters{}:
      min_market_cap ($B), min_fcf_yield (%), min_rev_growth (%), max_net_debt_ebitda (x)
    """
    body = request.json or {}
    market = body.get("market", "US")
    filters = body.get("filters", {})
    custom_tickers = body.get("custom_tickers", [])
    sector = body.get("sector", "All")
    if sector:
        filters["sector"] = sector

    # On Vercel (serverless) use a curated ~100-stock universe to stay within
    # the 60-second function timeout.  Full ~800-stock scan works locally.
    _on_vercel = bool(os.environ.get("VERCEL"))

    # Choose universe
    if custom_tickers:
        tickers = [t.upper() for t in custom_tickers]
    elif market == "SGX":
        tickers = sd.STI_COMPONENTS
    elif _on_vercel:
        tickers = sd.CLOUD_UNIVERSE
    else:
        tickers = sd.US_UNIVERSE

    results = sd.screen_stocks(tickers, filters)
    return jsonify({
        "results": results,
        "total": len(results),
        "screened": len(tickers),
        "cloud_mode": _on_vercel,
    })


@app.route("/api/screen/formula", methods=["GET"])
def score_formula():
    """Return the score formula weights for UI display."""
    return jsonify(sd.SCORE_FORMULA)


@app.route("/api/thesis/tickers", methods=["GET"])
def thesis_tickers():
    """Return all unique tickers that have ever had a thesis entry (prior picks)."""
    return jsonify(db.get_thesis_tickers())


# ── Research Queue ────────────────────────────────────────────────
@app.route("/api/research-queue", methods=["GET"])
def get_research_queue():
    return jsonify(db.get_research_queue())


@app.route("/api/research-queue", methods=["POST"])
def add_research_queue():
    b = request.json or {}
    db.add_to_research_queue(b.get("ticker", ""), b.get("notes", ""))
    return jsonify({"status": "ok"})


@app.route("/api/research-queue/<ticker>", methods=["DELETE"])
def remove_research_queue(ticker):
    db.remove_from_research_queue(ticker)
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════════════
# INSIDER BUYING, CATALYSTS, RED FLAGS, COMPETITORS
# ══════════════════════════════════════════════════════════════════

@app.route("/api/stock/<ticker>/insiders", methods=["GET"])
def get_insiders(ticker):
    """Insider transactions (last 60 days) with net buy/sell signal."""
    return jsonify(sd.get_insider_activity(ticker.upper()))


@app.route("/api/stock/<ticker>/catalysts", methods=["GET"])
def get_catalysts(ticker):
    """Upcoming earnings, ex-div dates, and recent catalyst news."""
    return jsonify(sd.get_upcoming_catalysts(ticker.upper()))


@app.route("/api/stock/<ticker>/redflags", methods=["GET"])
def get_redflags(ticker):
    """Quantitative red flags + negative news from last 60 days."""
    return jsonify(sd.get_red_flags(ticker.upper()))


@app.route("/api/stock/<ticker>/competitors", methods=["GET"])
def get_competitors(ticker):
    """Top 5 competitors by industry + market cap, with growth metrics and recent news."""
    return jsonify(sd.get_competitors(ticker.upper()))


@app.route("/api/stock/<ticker>/moat", methods=["GET"])
def get_moat(ticker):
    """Auto-scored competitive moat rating (Wide / Narrow / None)."""
    return jsonify(sd.get_moat_rating(ticker.upper()))


@app.route("/api/stock/<ticker>/risk", methods=["GET"])
def get_risk(ticker):
    """Composite risk rating (Low / Medium / High)."""
    return jsonify(sd.get_risk_rating(ticker.upper()))


@app.route("/api/stock/<ticker>/targets", methods=["GET"])
def get_targets(ticker):
    """Bull / base / bear price targets."""
    return jsonify(sd.get_price_targets(ticker.upper()))


@app.route("/api/stock/<ticker>/metrics-history", methods=["GET"])
def get_metrics_history(ticker):
    """1Y and 5Y historical anchors: Rev Growth, Net Margin, FCF Margin, Op Margin, ROIC.
    Also returns current price, EPS TTM/Forward, shares, revenue TTM, P/E, Forward P/E."""
    return jsonify(sd.get_historical_metrics(ticker.upper()))


# ══════════════════════════════════════════════════════════════════
# VALUATION
# ══════════════════════════════════════════════════════════════════

@app.route("/api/valuation/dcf", methods=["POST"])
def dcf():
    """
    Run DCF valuation.
    Body: { current_fcf, growth_1_5, growth_6_10, wacc, terminal_growth, shares, net_debt }
    """
    b = request.json or {}
    result = val.dcf_valuation(
        current_fcf=float(b.get("current_fcf", 0)),
        growth_rate_1_5=float(b.get("growth_1_5", 10)),
        growth_rate_6_10=float(b.get("growth_6_10", 5)),
        wacc=float(b.get("wacc", 10)),
        terminal_growth=float(b.get("terminal_growth", 2.5)),
        shares_outstanding=float(b.get("shares", 1)),
        net_debt=float(b.get("net_debt", 0)),
    )
    return jsonify(result)


@app.route("/api/valuation/quick", methods=["POST"])
def quick_val():
    """Quick valuation (Graham + P/E + Lynch)."""
    b = request.json or {}
    result = val.quick_valuation(
        eps=float(b.get("eps", 0)),
        reasonable_pe=float(b.get("reasonable_pe", 15)),
        growth_rate=float(b.get("growth_rate", 0)),
    )
    return jsonify(result)


@app.route("/api/valuation/mos", methods=["POST"])
def mos():
    """Margin of safety calculation."""
    b = request.json or {}
    result = val.margin_of_safety(
        current_price=float(b.get("current_price", 0)),
        intrinsic_value=float(b.get("intrinsic_value", 0)),
    )
    return jsonify(result)


@app.route("/api/valuation/comparable", methods=["POST"])
def comparable():
    """Comparable/multiples valuation."""
    b = request.json or {}
    result = val.comparable_valuation(
        eps=float(b.get("eps", 0)),
        ebitda=float(b.get("ebitda", 0)),
        sector_pe=float(b.get("sector_pe", 15)),
        sector_ev_ebitda=float(b.get("sector_ev_ebitda", 10)),
        net_debt=float(b.get("net_debt", 0)),
        shares_outstanding=float(b.get("shares", 1)),
    )
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# WATCHLIST
# ══════════════════════════════════════════════════════════════════

@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    return jsonify(db.get_watchlist())


@app.route("/api/watchlist", methods=["POST"])
def add_watchlist():
    b = request.json or {}
    db.add_to_watchlist(
        ticker=b.get("ticker", ""),
        name=b.get("name", ""),
        market=b.get("market", "US"),
        notes=b.get("notes", ""),
    )
    return jsonify({"status": "ok"})


@app.route("/api/watchlist/<ticker>", methods=["DELETE"])
def remove_watchlist(ticker):
    db.remove_from_watchlist(ticker)
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════════════
# PORTFOLIO
# ══════════════════════════════════════════════════════════════════

@app.route("/api/portfolio/snapshot", methods=["GET"])
def portfolio_snapshot():
    """Lightweight portfolio snapshot — raw DB data, no live price fetching."""
    positions = db.get_portfolio()
    return jsonify(positions)


@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    positions = db.get_portfolio()
    # Enrich with current prices
    enriched = []
    for pos in positions:
        if pos.get("status") == "open":
            info = sd.get_stock_info(pos["ticker"])
            if "error" not in info:
                price = info.get("current_price", 0)
                entry = pos.get("entry_price", 0)
                shares = pos.get("shares", 0)
                pos["current_price"] = price
                pos["current_value"] = round(price * shares, 2)
                pos["cost_basis"] = round(entry * shares, 2)
                pos["gain_loss"] = round((price - entry) * shares, 2)
                pos["gain_loss_pct"] = round((price - entry) / entry * 100, 1) if entry else 0
                pos["vs_target"] = round((pos.get("target_price", 0) - price) / price * 100, 1) if pos.get("target_price") else 0
        enriched.append(pos)
    return jsonify(enriched)


@app.route("/api/portfolio", methods=["POST"])
def add_portfolio():
    b = request.json or {}
    db.add_position(
        ticker=b.get("ticker", ""),
        name=b.get("name", ""),
        entry_price=float(b.get("entry_price", 0)),
        shares=float(b.get("shares", 0)),
        entry_date=b.get("entry_date", datetime.now().strftime("%Y-%m-%d")),
        target_price=float(b.get("target_price", 0)),
        stop_loss=float(b.get("stop_loss", 0)),
        notes=b.get("notes", ""),
    )
    return jsonify({"status": "ok"})


@app.route("/api/portfolio/<int:pos_id>", methods=["PUT"])
def update_portfolio(pos_id):
    b = request.json or {}
    db.update_position(pos_id, **b)
    return jsonify({"status": "ok"})


@app.route("/api/portfolio/<int:pos_id>", methods=["DELETE"])
def delete_portfolio(pos_id):
    db.delete_position(pos_id)
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════════════
# THESIS
# ══════════════════════════════════════════════════════════════════

@app.route("/api/thesis", methods=["GET"])
def get_thesis():
    ticker = request.args.get("ticker")
    return jsonify(db.get_thesis(ticker))


@app.route("/api/thesis", methods=["POST"])
def save_thesis():
    b = request.json or {}
    thesis_id = db.save_thesis(b)
    return jsonify({"status": "ok", "id": thesis_id})


@app.route("/api/thesis/<int:thesis_id>", methods=["DELETE"])
def delete_thesis(thesis_id):
    db.delete_thesis(thesis_id)
    return jsonify({"status": "ok"})


@app.route("/api/thesis/weekly-tracker", methods=["GET"])
def thesis_weekly_tracker():
    """
    For each active thesis, return current live metrics (price, P/E, ROE, etc.)
    alongside the entry-snapshot metrics saved when the thesis was created.
    Also pulls recent news for each ticker.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as afc
    theses = db.get_thesis()
    active = [t for t in theses if t.get("status", "active") == "active"]

    def enrich(t):
        ticker = t["ticker"]
        try:
            info = sd.get_stock_info(ticker)
            news_raw = []
            try:
                stock = __import__("yfinance").Ticker(ticker)
                news_raw = stock.news or []
            except Exception:
                pass
            recent_news = []
            cutoff = __import__("time").time() - 7 * 86400  # last 7 days
            for n in news_raw[:10]:
                if n.get("providerPublishTime", 0) >= cutoff:
                    recent_news.append({
                        "title": n.get("title", ""),
                        "url":   n.get("link", ""),
                        "date":  __import__("pandas").Timestamp(n["providerPublishTime"], unit="s").strftime("%Y-%m-%d"),
                    })
            current_price = info.get("current_price") or 0
            entry_price   = t.get("current_price") or 0
            price_chg = round((current_price - entry_price) / entry_price * 100, 1) if entry_price else None
            target    = t.get("target_price") or t.get("intrinsic_value") or 0
            mos_now   = round((target - current_price) / target * 100, 1) if target and current_price else None
            return {
                **t,
                "current_price_live":     current_price,
                "price_change_since_entry": price_chg,
                "mos_now":                mos_now,
                "current_pe":    info.get("pe_ratio"),
                "current_roe":   round((info.get("roe") or 0) * 100, 1),
                "current_rev_growth": round((info.get("revenue_growth") or 0) * 100, 1),
                "current_net_margin": round((info.get("net_margin") or 0) * 100, 1),
                "recent_news":   recent_news[:5],
            }
        except Exception as e:
            return {**t, "error": str(e)}

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(enrich, t): t for t in active}
        for f in afc(futures):
            results.append(f.result())

    results.sort(key=lambda x: x.get("updated_date", ""), reverse=True)
    return jsonify(results)


# ══════════════════════════════════════════════════════════════════
# WEEKLY REVIEWS
# ══════════════════════════════════════════════════════════════════

@app.route("/api/reviews", methods=["GET"])
def get_reviews():
    ticker = request.args.get("ticker")
    thesis_id = request.args.get("thesis_id")
    return jsonify(db.get_weekly_reviews(ticker, int(thesis_id) if thesis_id else None))


@app.route("/api/reviews", methods=["POST"])
def save_review():
    b = request.json or {}
    db.save_weekly_review(b)
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════════════
# MARKET OVERVIEW (for dashboard) — fast batch fetch
# ══════════════════════════════════════════════════════════════════

@app.route("/api/market/overview", methods=["GET"])
def market_overview():
    """Get major index prices via single batch yf.download call (fast)."""
    result = sd.get_index_prices()
    return jsonify(result)


@app.route("/api/market/buffett-indicator", methods=["GET"])
def buffett_indicator():
    """Buffett Indicator: US market cap / GDP, sourced from FRED public CSV endpoints.
    Cached for 6 hours — GDP is quarterly, market cap updates daily."""
    import time as _time
    from urllib.request import urlopen

    CACHE_KEY = "__buffett_indicator__"
    CACHE_TTL  = 6 * 3600  # 6 hours

    with sd._cache_lock:
        entry = sd._cache.get(CACHE_KEY)
    if entry and (_time.time() - entry["ts"]) < CACHE_TTL:
        return jsonify(entry["data"])

    try:
        def fetch_fred_csv(series_id):
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
            with urlopen(url, timeout=12) as resp:
                text = resp.read().decode("utf-8")
            rows = []
            for line in text.strip().splitlines()[1:]:
                parts = line.strip().split(",")
                if len(parts) == 2 and parts[1] != ".":
                    rows.append({"date": parts[0], "value": float(parts[1])})
            return rows

        mc_data  = fetch_fred_csv("WILL5000INDFC")  # billions USD, daily
        gdp_data = fetch_fred_csv("GDP")             # billions USD, quarterly, annualised

        if not mc_data or not gdp_data:
            return jsonify({"error": "FRED returned no data"}), 502

        latest_mc  = mc_data[-1]
        latest_gdp = gdp_data[-1]
        ratio      = round(latest_mc["value"] / latest_gdp["value"] * 100, 1)

        # 1-year-ago ratio
        from datetime import datetime as _dt, timedelta as _td
        one_yr_ago = (_dt.now() - _td(days=365)).strftime("%Y-%m-%d")
        mc_1y  = next((d for d in reversed(mc_data)  if d["date"] <= one_yr_ago), mc_data[0])
        gdp_1y = next((d for d in reversed(gdp_data) if d["date"] <= one_yr_ago), gdp_data[0])
        ratio_1y = round(mc_1y["value"] / gdp_1y["value"] * 100, 1)

        # Historical series (quarterly, last 32 quarters ≈ 8 years) for sparkline
        history = []
        for gdp_pt in gdp_data[-32:]:
            mc_pt = next((d for d in reversed(mc_data) if d["date"] <= gdp_pt["date"]), None)
            if mc_pt:
                history.append({"date": gdp_pt["date"],
                                 "ratio": round(mc_pt["value"] / gdp_pt["value"] * 100, 1)})

        # Zone classification
        if ratio < 75:
            zone, zone_color = "Undervalued",              "emerald"
        elif ratio < 100:
            zone, zone_color = "Fair Value",               "yellow"
        elif ratio < 130:
            zone, zone_color = "Overvalued",               "orange"
        elif ratio < 175:
            zone, zone_color = "Significantly Overvalued", "red"
        else:
            zone, zone_color = "Strongly Overvalued",      "red"

        # Interpretation blurb
        blurbs = {
            "Undervalued":              "Market appears cheap relative to the economy — historically a good entry window for long-term investors.",
            "Fair Value":               "Market is broadly in line with economic output. Stock-picking matters more than macro timing here.",
            "Overvalued":               "Market is running ahead of GDP. Expect lower future returns; a margin-of-safety approach is prudent.",
            "Significantly Overvalued": "Valuations are stretched. Buffett has historically held cash or been cautious at these levels.",
            "Strongly Overvalued":      "Extreme overvaluation — above the dot-com bubble peak. Risk management is paramount.",
        }

        data = {
            "ratio":          ratio,
            "ratio_1y":       ratio_1y,
            "change_1y":      round(ratio - ratio_1y, 1),
            "zone":           zone,
            "zone_color":     zone_color,
            "market_cap_b":   round(latest_mc["value"],  0),
            "gdp_b":          round(latest_gdp["value"], 0),
            "market_cap_date": latest_mc["date"],
            "gdp_date":        latest_gdp["date"],
            "history":        history,
            "blurb":          blurbs[zone],
        }

        with sd._cache_lock:
            sd._cache[CACHE_KEY] = {"ts": _time.time(), "data": data}

        return jsonify(data)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    """Clear the in-memory data cache (forces fresh fetch next request)."""
    import stock_data
    with stock_data._cache_lock:
        stock_data._cache.clear()
    return jsonify({"status": "ok", "message": "Cache cleared"})


def _open_browser():
    """Wait until Flask is ready, then open the browser automatically."""
    import threading, time, webbrowser
    try:
        from urllib.request import urlopen
    except ImportError:
        import urllib2 as urlopen  # Python 2 fallback (unlikely)

    def _wait_and_open():
        for _ in range(30):          # try for up to 60 seconds
            time.sleep(2)
            try:
                urlopen("http://localhost:5001/api/health", timeout=2)
                webbrowser.open("http://localhost:5001")
                return
            except Exception:
                pass

    t = threading.Thread(target=_wait_and_open, daemon=True)
    t.start()


if __name__ == "__main__":
    # When running locally, open browser automatically
    # When running on Railway/cloud (PORT env set), skip browser open
    port = int(os.environ.get("PORT", 5001))
    is_cloud = "PORT" in os.environ

    print("\n" + "="*60)
    print("  Value Investor App")
    if is_cloud:
        print(f"  Running on port {port} (cloud mode)")
    else:
        print("  Starting... browser will open automatically.")
    print("  Press Ctrl+C to stop.")
    print("="*60 + "\n")

    if not is_cloud:
        _open_browser()

    app.run(debug=False, port=port, host="0.0.0.0", threaded=True)
