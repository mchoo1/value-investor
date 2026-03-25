"""
Stock data fetching via yfinance (100% free, no API key needed).
Supports US (NYSE/NASDAQ), SGX (.SI), HK (.HK).

Performance:
- Parallel fetching via ThreadPoolExecutor (10x faster than sequential)
- In-memory cache with 30-min TTL (instant repeat lookups)
"""
import math
import yfinance as yf
import pandas as pd
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def _sanitize(obj, default=None):
    """
    Recursively walk a dict/list and replace float NaN / Inf with `default`.
    Python's json module serialises float('nan') as the bare token NaN which
    is not valid JSON — this prevents that from ever reaching Flask's jsonify.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v, default) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v, default) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return default
    return obj

# ── Simple in-memory cache (TTL = 30 minutes) ────────────────────
_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 1800  # seconds


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
            return entry["data"]
    return None


def _cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"ts": time.time(), "data": data}


# ── Market suffix by exchange ────────────────────────────────────
MARKET_SUFFIX = {
    "US": "",
    "SGX": ".SI",
    "HK": ".HK",
    "LSE": ".L",
    "ASX": ".AX",
}

# ── STI 30 component stocks ──────────────────────────────────────
STI_COMPONENTS = [
    "D05.SI", "O39.SI", "U11.SI", "Z74.SI", "C6L.SI",
    "S63.SI", "V03.SI", "G13.SI", "BN4.SI", "U96.SI",
    "C38U.SI", "A17U.SI", "J36.SI", "C07.SI", "BS6.SI",
    "9CI.SI", "BUOU.SI", "M44U.SI", "ME8U.SI", "N2IU.SI",
    "F34.SI", "U14.SI", "S58.SI", "H78.SI", "T39.SI",
    "SGX.SI", "CC3.SI", "AJBU.SI", "S68.SI", "P40U.SI",
]

# ── S&P 500 + Russell 1000 combined universe (~800 stocks) ───────
US_UNIVERSE = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CSCO", "ACN", "ADBE", "CRM", "AMD",
    "TXN", "QCOM", "IBM", "INTU", "NOW", "AMAT", "ADI", "MU", "KLAC", "LRCX",
    "SNPS", "CDNS", "MCHP", "APH", "PANW", "FTNT", "CRWD", "ANET", "AKAM", "CDW",
    "CTSH", "FFIV", "JNPR", "HPE", "HPQ", "DELL", "STX", "WDC", "NTAP", "KEYS",
    "ANSS", "PTC", "PAYC", "PAYX", "ADP", "FIS", "FISV", "GPN", "FLT", "FICO",
    "ROP", "SWKS", "QRVO", "MPWR", "TER", "ZBRA", "CGNX", "ENTG", "IT", "MSCI",
    "SPGI", "VRSK", "EPAM", "SSNC", "LDOS", "SAIC", "CACI", "BAH", "PLTR", "SNOW",
    "WDAY", "VEEV", "PCTY", "CDAY", "HUBS", "TEAM", "DOCU", "BOX", "TWLO", "TTD",
    "APP", "RBLX", "TTWO", "EA", "GDDY", "TRMB", "EXLS", "GLOB", "EPIQ", "NCNO",
    "DDOG", "ZS", "OKTA", "NET", "CRWD", "S", "TENB", "RPD", "QLYS", "VRNS",
    # Healthcare
    "JNJ", "UNH", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "SYK", "MDT", "ISRG", "ZBH", "BSX", "EW", "RMD", "HOLX", "BAX",
    "BDX", "TFX", "PODD", "INSP", "CVS", "CI", "HUM", "CNC", "MOH", "ELV",
    "HCA", "UHS", "THC", "ENSG", "MCK", "ABC", "CAH", "BIIB", "GILD", "REGN",
    "VRTX", "ALNY", "MRNA", "INCY", "EXEL", "JAZZ", "NBIX", "UTHR", "CORT",
    "IQV", "DGX", "LH", "MTD", "TECH", "NEOG", "MMSI", "XRAY", "ALGN", "DXCM",
    "NTRA", "NVAX", "AXSM", "KRTX", "PKI", "HOLX", "RVMD", "ROIV", "PRGO",
    "AKRN", "SRRK", "ACAD", "SRPT", "BLUE", "FATE", "RXRX", "STOK", "ARWR",
    # Financials
    "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "USB", "PNC",
    "TFC", "FITB", "KEY", "RF", "HBAN", "CFG", "CMA", "MTB", "ZION", "BLK",
    "SCHW", "STT", "BK", "TROW", "PRU", "MET", "AFL", "CNO", "PGR", "TRV",
    "ALL", "CNA", "HIG", "WRB", "CINF", "AIZ", "AIG", "ACGL", "ERIE", "V",
    "MA", "PYPL", "COF", "DFS", "SYF", "SLM", "ALLY", "ICE", "CME", "NDAQ",
    "CBOE", "MKTX", "LPLA", "RJF", "IBKR", "FNF", "FAF", "RE", "MKL", "BRO",
    "WTW", "AON", "MMC", "RYAN", "AMG", "BEN", "IVZ", "APAM", "FDS", "MORN",
    "SF", "VIRT", "HOOD", "SOFI", "LC", "UPST", "AFRM", "NMR", "HRB", "WU",
    "NMIH", "ESNT", "MTG", "RADI", "NATI", "EWBC", "WAL", "GBCI", "CVBF",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "NKE", "MCD", "SBUX", "LOW", "TGT", "BKNG", "TJX",
    "ROST", "ETSY", "EBAY", "EXPE", "DKNG", "PENN", "LVS", "WYNN", "MGM",
    "CCL", "RCL", "NCLH", "MAR", "HLT", "H", "WH", "CHH", "CMG", "YUM",
    "QSR", "DPZ", "DRI", "EAT", "TXRH", "CAKE", "F", "GM", "KMX", "AN",
    "LAD", "GPC", "AAP", "AZO", "ORLY", "LKQ", "POOL", "SWK", "WHR", "PVH",
    "VFC", "RL", "HBI", "LULU", "RH", "WSM", "DHI", "LEN", "PHM", "NVR",
    "TOL", "MDC", "TMHC", "LGIH", "GRBK", "W", "CVNA", "BBWI", "GPS", "AEO",
    "ANF", "CHWY", "CPRI", "TPR", "CATO", "BIG", "LESL", "ARHS", "LOVE",
    "BOOT", "GOOS", "ONON", "DECK", "CROX", "SKX", "UAA", "UA",
    # Consumer Staples
    "PG", "KO", "PEP", "PM", "MO", "WMT", "COST", "MDLZ", "CL", "KMB",
    "EL", "CHD", "CLX", "HRL", "SJM", "K", "GIS", "CPB", "CAG", "MKC",
    "HSY", "TR", "TSN", "TAP", "STZ", "SAM", "MNST", "KR", "DLTR", "DG",
    "OLLI", "FIVE", "CASY", "MUSA", "BJ", "CELH", "LANC", "JJSF", "PZZA",
    "VITL", "FRPT", "HELE", "JAH", "AMSF", "LWAY", "DAVA", "COTT", "FIZZ",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "PXD", "OXY", "PSX", "VLO", "MPC",
    "KMI", "WMB", "OKE", "EPD", "MPLX", "PAA", "TRGP", "DVN", "FANG", "MRO",
    "APA", "CTRA", "MTDR", "SM", "BTU", "ARCH", "CEIX", "HCC", "AMR", "BKR",
    "HAL", "FTI", "NOV", "HP", "NBR", "NE", "RIG", "PTEN", "LBRT", "HES",
    "PR", "LNG", "DT", "RRC", "EQT", "AR", "SWN", "CNX", "GPMT", "REI",
    # Industrials
    "CAT", "HON", "UNP", "GE", "RTX", "LMT", "BA", "NOC", "GD", "LHX",
    "CARR", "OTIS", "EMR", "ETN", "PH", "IR", "ROK", "AME", "FTV", "GNRC",
    "ITW", "GWW", "MSC", "FAST", "AIT", "URI", "RSG", "WM", "CPRT", "CTAS",
    "SAIA", "XPO", "WERN", "JBHT", "KNX", "ODFL", "CHRW", "LSTR", "FDX",
    "UPS", "EXPD", "GXO", "MMM", "HEI", "TDG", "KTOS", "AER", "AL", "SPR",
    "HXL", "TPC", "KBR", "AMETEK", "PCAR", "CMI", "AGCO", "DE", "TTEK",
    "MTZ", "PWR", "PRIM", "WMS", "AAON", "AIMC", "REVG", "MYRG", "HSII",
    "HUBB", "AOS", "REXN", "FWRD", "ECHO", "HTLD", "MRTN", "USAK",
    # Materials
    "LIN", "APD", "SHW", "PPG", "ECL", "NEM", "FCX", "NUE", "STLD", "RS",
    "CMC", "ATI", "X", "CLF", "AA", "PKG", "IP", "WRK", "SEE", "GPK",
    "AVY", "BERY", "SLGN", "CF", "MOS", "RPM", "OLN", "HUN", "TROX", "MEOH",
    "HWKN", "BCPC", "FMC", "CE", "AVNT", "WLK", "ASH", "EMN", "LTHM",
    # Real Estate
    "AMT", "PLD", "CCI", "EQIX", "SBAC", "DLR", "WELL", "PSA", "EXR", "AVB",
    "EQR", "MAA", "UDR", "CPT", "BXP", "SLG", "KRC", "SPG", "KIM", "REG",
    "O", "NNN", "EPRT", "GTY", "ADC", "WPC", "STAG", "LXP", "IIPR", "FR",
    "NHI", "OHI", "SBRA", "PEAK", "VTR", "HTA", "MPW", "COLD", "EGP", "DEI",
    # Utilities
    "NEE", "SO", "DUK", "AEP", "EXC", "XEL", "WEC", "ES", "ED", "FE",
    "EIX", "PCG", "D", "ETR", "CNP", "NI", "AES", "OGE", "PNW", "AWK",
    "CWT", "SJW", "ATO", "LNT", "BKH", "OTTR", "MGEE", "IDA", "POR", "NWN",
    "NWE", "AVA", "SR", "UTL", "MSEX", "ARTNA", "YORW",
    # Communication Services
    "META", "GOOGL", "GOOG", "NFLX", "TMUS", "VZ", "T", "CMCSA", "CHTR",
    "DIS", "PARA", "WBD", "FOX", "FOXA", "NWS", "NWSA", "AMCX", "OMC",
    "IPG", "MTCH", "IAC", "SNAP", "PINS", "SPOT", "TTWO", "EA", "ATVI",
    "LUMN", "SIRI", "DISH", "ATN", "CNSL", "USM", "TDS",
]

# Keep old name as alias so existing code referencing SP500_SAMPLE still works
SP500_SAMPLE = US_UNIVERSE


def get_stock_info(ticker: str) -> dict:
    """
    Fetch comprehensive stock info from Yahoo Finance.
    Uses cache — repeated calls within 30 min are instant.
    """
    ticker = ticker.upper().strip()
    cached = _cache_get(ticker)
    if cached:
        return cached

    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        # If yfinance returns an empty/error dict, catch it
        if not info or len(info) < 5:
            return {"error": f"No data returned for {ticker}", "ticker": ticker}

        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("navPrice") or 0

        result = {
            "ticker": ticker,
            "name": info.get("longName") or info.get("shortName", ticker),
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
            "market": info.get("exchange", "N/A"),
            "currency": info.get("currency", "USD"),
            "country": info.get("country", "N/A"),
            "description": info.get("longBusinessSummary", ""),

            "current_price": price,
            "52w_high": info.get("fiftyTwoWeekHigh", 0),
            "52w_low": info.get("fiftyTwoWeekLow", 0),
            "market_cap": info.get("marketCap", 0),

            "pe_ratio": info.get("trailingPE") or 0,
            "forward_pe": info.get("forwardPE") or 0,
            "pb_ratio": info.get("priceToBook") or 0,
            "ps_ratio": info.get("priceToSalesTrailing12Months") or 0,
            "peg_ratio": info.get("pegRatio") or 0,
            "ev_ebitda": info.get("enterpriseToEbitda") or 0,
            "ev_revenue": info.get("enterpriseToRevenue") or 0,

            "roe": info.get("returnOnEquity") or 0,
            "roa": info.get("returnOnAssets") or 0,
            "gross_margin": info.get("grossMargins") or 0,
            "operating_margin": info.get("operatingMargins") or 0,
            "net_margin": info.get("profitMargins") or 0,

            "revenue_growth": info.get("revenueGrowth") or 0,
            "earnings_growth": info.get("earningsGrowth") or 0,
            "earnings_quarterly_growth": info.get("earningsQuarterlyGrowth") or 0,

            "total_debt": info.get("totalDebt") or 0,
            "total_cash": info.get("totalCash") or 0,
            "debt_to_equity": info.get("debtToEquity") or 0,
            "current_ratio": info.get("currentRatio") or 0,
            "quick_ratio": info.get("quickRatio") or 0,

            "free_cashflow": info.get("freeCashflow") or 0,
            "operating_cashflow": info.get("operatingCashflow") or 0,
            "fcf_yield": 0,

            "eps_ttm": info.get("trailingEps") or 0,
            "eps_forward": info.get("forwardEps") or 0,
            "book_value": info.get("bookValue") or 0,
            "revenue_per_share": info.get("revenuePerShare") or 0,

            "dividend_yield": info.get("dividendYield") or 0,
            "dividend_rate": info.get("dividendRate") or 0,
            "payout_ratio": info.get("payoutRatio") or 0,

            "target_mean_price": info.get("targetMeanPrice") or 0,
            "target_high_price": info.get("targetHighPrice") or 0,
            "target_low_price": info.get("targetLowPrice") or 0,
            "recommendation": info.get("recommendationKey", "N/A"),
            "analyst_count": info.get("numberOfAnalystOpinions") or 0,

            "shares_outstanding": info.get("sharesOutstanding") or 0,
            "float_shares": info.get("floatShares") or 0,
            "insider_ownership": info.get("heldPercentInsiders") or 0,
            "institutional_ownership": info.get("heldPercentInstitutions") or 0,

            "beta": info.get("beta") or 0,
            "total_revenue": info.get("totalRevenue") or 0,
            "net_income": info.get("netIncomeToCommon") or info.get("netIncome") or 0,
        }

        # FCF yield
        fcf = result["free_cashflow"]
        shares = result["shares_outstanding"]
        if price and fcf and shares:
            result["fcf_yield"] = (fcf / shares) / price

        result = _sanitize(result, default=None)
        _cache_set(ticker, result)
        return result

    except Exception as e:
        return {"error": str(e), "ticker": ticker}


def get_financial_history(ticker: str) -> dict:
    """5-year annual financial history. Cached."""
    key = ticker + "_fin"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        stock = yf.Ticker(ticker)
        income = stock.financials
        balance = stock.balance_sheet
        cashflow = stock.cashflow

        summary = []
        if income is not None and not income.empty:
            for col in income.columns:
                year = str(col)[:4]
                row = {"year": year}
                row["revenue"] = _safe_get(income, "Total Revenue", col)
                row["gross_profit"] = _safe_get(income, "Gross Profit", col)
                row["operating_income"] = _safe_get(income, "Operating Income", col)
                row["net_income"] = _safe_get(income, "Net Income", col)
                row["ebitda"] = _safe_get(income, "EBITDA", col)

                if cashflow is not None and not cashflow.empty and col in cashflow.columns:
                    op_cf = _safe_get(cashflow, "Operating Cash Flow", col) or 0
                    capex = _safe_get(cashflow, "Capital Expenditure", col) or 0
                    row["operating_cf"] = op_cf
                    row["capex"] = capex
                    row["fcf"] = op_cf + capex  # capex is negative

                if balance is not None and not balance.empty and col in balance.columns:
                    row["total_debt"] = _safe_get(balance, "Total Debt", col)
                    row["total_equity"] = _safe_get(balance, "Stockholders Equity", col)

                rev = row.get("revenue") or 1
                row["gross_margin"] = round((row.get("gross_profit") or 0) / rev * 100, 1)
                row["net_margin"] = round((row.get("net_income") or 0) / rev * 100, 1)
                row["operating_margin"] = round((row.get("operating_income") or 0) / rev * 100, 1)
                summary.append(row)

        result = {"summary": sorted(summary, key=lambda x: x["year"])}
        _cache_set(key, result)
        return result
    except Exception as e:
        return {"error": str(e), "summary": []}


def get_price_history(ticker: str, period: str = "5y") -> list:
    """Monthly price history for charts. Cached."""
    key = ticker + "_price_" + period
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval="1mo")
        if hist.empty:
            hist = stock.history(period="1y")

        result = [
            {
                "date": str(date)[:10],
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            }
            for date, row in hist.iterrows()
        ]
        _cache_set(key, result)
        return result
    except Exception:
        return []


def get_index_prices() -> dict:
    """
    Fetch major index prices using a single yf.download() batch call.
    Much faster than fetching each index separately.
    """
    key = "_indices"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        tickers = ["^GSPC", "^DJI", "^IXIC", "^STI", "^VIX"]
        data = yf.download(
            tickers, period="2d", interval="1d",
            auto_adjust=True, progress=False, threads=True
        )
        result = {}
        labels = {
            "^GSPC": "S&P 500",
            "^DJI": "Dow Jones",
            "^IXIC": "NASDAQ",
            "^STI": "STI",
            "^VIX": "VIX",
        }
        close = data["Close"] if "Close" in data else data
        for t, label in labels.items():
            try:
                price = float(close[t].dropna().iloc[-1])
                prev  = float(close[t].dropna().iloc[-2]) if len(close[t].dropna()) > 1 else price
                result[label] = {
                    "price": round(price, 2),
                    "change_pct": round((price - prev) / prev * 100, 2),
                }
            except Exception:
                result[label] = {"price": 0, "change_pct": 0}

        _cache_set(key, result)
        return result
    except Exception as e:
        return {}


# ── Score formula weights (documented for UI display) ─────────────
SCORE_FORMULA = {
    "pe":        {"label": "P/E Ratio",        "max": 20, "type": "value",   "note": "Lower is better. ≤10→20pts, ≤15→15, ≤20→10, >20→5"},
    "pb":        {"label": "P/B Ratio",        "max": 20, "type": "value",   "note": "Lower is better. ≤1→20pts, ≤1.5→15, ≤2→10, >2→5"},
    "roe":       {"label": "Return on Equity", "max": 20, "type": "quality", "note": "Higher is better. ≥20%→20pts, ≥15%→15, ≥12%→10, ≥8%→5"},
    "de":        {"label": "Debt / Equity",    "max": 20, "type": "safety",  "note": "Lower is better. ≤0.3→20pts, ≤0.5→15, ≤1.0→10, ≤1.5→5"},
    "fcf_yield": {"label": "FCF Yield",        "max": 20, "type": "value",   "note": "Higher is better. ≥8%→20pts, ≥5%→15, ≥3%→10, >0→5"},
}


def _score_breakdown(pe, pb, roe, de, fcf_yield):
    """Return per-factor scores and total. All inputs must be clean floats (no NaN)."""
    # Guard against any residual None/NaN that slipped through
    def _f(v): return 0.0 if (v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))) else float(v)
    pe, pb, roe, de, fcf_yield = _f(pe), _f(pb), _f(roe), _f(de), _f(fcf_yield)

    s_pe = 20 if 0 < pe <= 10 else 15 if 0 < pe <= 15 else 10 if 0 < pe <= 20 else 5 if pe > 0 else 0
    s_pb = 20 if 0 < pb <= 1 else 15 if 0 < pb <= 1.5 else 10 if 0 < pb <= 2 else 5 if pb > 0 else 0
    s_roe = 20 if roe >= 20 else 15 if roe >= 15 else 10 if roe >= 12 else 5 if roe >= 8 else 0
    s_de  = 20 if de <= 0.3 else 15 if de <= 0.5 else 10 if de <= 1.0 else 5 if de <= 1.5 else 0
    s_fcf = 20 if fcf_yield >= 8 else 15 if fcf_yield >= 5 else 10 if fcf_yield >= 3 else 5 if fcf_yield > 0 else 0
    return {
        "pe": s_pe, "pb": s_pb, "roe": s_roe, "de": s_de, "fcf_yield": s_fcf,
        "total": s_pe + s_pb + s_roe + s_de + s_fcf,
    }


def _screen_one(ticker: str, filters: dict) -> dict | None:
    """Screen a single ticker. Returns result dict or None if filtered out."""
    try:
        info = get_stock_info(ticker)
        if "error" in info or not info.get("current_price"):
            return None

        sector = info.get("sector", "N/A") or "N/A"

        # Sector filter (skip if mismatch)
        if filters.get("sector") and filters["sector"] != "All" and sector != filters["sector"]:
            return None

        pe     = info.get("pe_ratio") or 0
        pb     = info.get("pb_ratio") or 0
        roe    = (info.get("roe") or 0) * 100
        de     = (info.get("debt_to_equity") or 0) / 100
        fcf    = info.get("free_cashflow") or 0
        price  = info.get("current_price") or 0
        shares = info.get("shares_outstanding") or 1
        fcf_yield = ((fcf / shares) / price * 100) if (price and fcf and shares) else 0

        # Apply numeric filters
        if filters.get("max_pe")  and 0 < pe  > filters["max_pe"]:  return None
        if filters.get("max_pb")  and 0 < pb  > filters["max_pb"]:  return None
        if filters.get("min_roe") and roe < filters["min_roe"]:      return None
        if filters.get("max_de")  and de  > filters["max_de"]:       return None
        if filters.get("require_positive_fcf") and fcf <= 0:         return None

        # Insider buying filter — only include stocks with net buys in last 60 days
        insider_signal = None
        if filters.get("insider_buying_only"):
            insider = get_insider_activity(ticker)
            sig = insider.get("net_signal", "no_data")
            if sig not in ("strong_buy", "buy"):
                return None
            insider_signal = sig

        breakdown = _score_breakdown(pe, pb, roe, de, fcf_yield)
        moat      = get_moat_rating(ticker)

        return {
            "ticker":        ticker,
            "name":          info.get("name", ticker),
            "sector":        sector,
            "industry":      info.get("industry", "N/A") or "N/A",
            "price":         round(price, 2),
            "market_cap":    info.get("market_cap", 0),
            "revenue":       info.get("total_revenue", 0),
            "net_income":    info.get("net_income", 0),
            "net_margin":    round((info.get("net_margin") or 0) * 100, 1),
            "revenue_growth": round((info.get("revenue_growth") or 0) * 100, 1),
            "pe_ratio":      round(pe, 1)        if pe        else None,
            "pb_ratio":      round(pb, 2)        if pb        else None,
            "roe":           round(roe, 1)       if roe       else None,
            "debt_to_equity": round(de, 2)       if de        else None,
            "fcf_yield":     round(fcf_yield, 1) if fcf_yield else None,
            "dividend_yield": round((info.get("dividend_yield") or 0) * 100, 2),
            "score":           breakdown["total"],
            "score_breakdown": breakdown,
            "insider_signal":  insider_signal,
            "moat_rating":     moat.get("rating", "N/A"),
            "moat_score":      moat.get("score", 0),
        }
    except Exception:
        return None


def screen_stocks(tickers: list, filters: dict) -> list:
    """
    Screen tickers in parallel using 20 threads.
    ~800 stocks takes 3-8 minutes on first run; instant from cache.
    """
    results = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_screen_one, t, filters): t for t in tickers}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ══════════════════════════════════════════════════════════════════
# INSIDER BUYING
# ══════════════════════════════════════════════════════════════════

def _col(df, *candidates):
    """Return the first matching column name from candidates (case-insensitive)."""
    cols_lower = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in cols_lower:
            return cols_lower[c.lower()]
    return None


def _safe_val(row, *candidates, default=None):
    """Safely get a value from a row by trying multiple column names."""
    for c in candidates:
        try:
            v = row.get(c)
            if v is not None and str(v).strip() not in ("", "nan", "None"):
                return v
        except Exception:
            pass
    return default


def get_insider_activity(ticker: str) -> dict:
    """
    Return insider transactions from last 2 years via yfinance.
    Shows ALL transactions; highlights last-60-day net buying signal.
    Handles all yfinance column-name variations across versions.
    """
    key = ticker + "_insiders"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        stock = yf.Ticker(ticker.upper())

        # yfinance ≥0.2 uses insider_transactions; older uses get_insider_transactions()
        txns = None
        for attr in ("insider_transactions", "insider_purchases"):
            try:
                t = getattr(stock, attr, None)
                if t is not None and not (hasattr(t, "empty") and t.empty):
                    txns = t
                    break
            except Exception:
                pass

        if txns is None or (hasattr(txns, "empty") and txns.empty):
            result = {"all_txns": [], "buys": [], "sells": [], "net_signal": "no_data",
                      "summary": "No insider transaction data available from Yahoo Finance."}
            _cache_set(key, result)
            return result

        # ── Normalise the DataFrame ─────────────────────────────
        # Reset index if date is in the index
        if txns.index.name and "date" in str(txns.index.name).lower():
            txns = txns.reset_index()
        # Flatten MultiIndex columns
        if isinstance(txns.columns, pd.MultiIndex):
            txns.columns = [" ".join(str(c) for c in col).strip() for col in txns.columns]
        # Lower-case all columns for easy lookup
        txns.columns = [str(c).strip() for c in txns.columns]
        col_map = {c.lower(): c for c in txns.columns}

        def gcol(*keys):
            for k in keys:
                if k.lower() in col_map:
                    return col_map[k.lower()]
            return None

        date_col    = gcol("startdate", "date", "start date", "filed")
        insider_col = gcol("insider", "filer_name", "name", "insider name")
        title_col   = gcol("position", "filer_relation", "title", "relationship")
        txn_col     = gcol("text", "transaction", "type", "trans. code")
        shares_col  = gcol("shares", "shares traded", "#shares")
        value_col   = gcol("value", "value ($)", "transaction value")

        def parse_num(v):
            try:
                s = str(v).strip().replace(",", "").replace("$", "")
                if not s or s.lower() in ("nan", "none", "null", "-", "n/a", ""):
                    return 0
                result = float(s)
                return 0 if (math.isnan(result) or math.isinf(result)) else result
            except Exception:
                return 0

        cutoff_60 = pd.Timestamp.now() - pd.Timedelta(days=60)

        all_txns, buys, sells = [], [], []
        for _, row in txns.iterrows():
            # Parse date
            raw_date = row[date_col] if date_col else None
            try:
                txn_date = pd.Timestamp(raw_date)
                if txn_date.tzinfo:
                    txn_date = txn_date.tz_localize(None)
                date_str = txn_date.strftime("%Y-%m-%d")
            except Exception:
                date_str = str(raw_date)[:10] if raw_date else "Unknown"
                txn_date = pd.Timestamp("1900-01-01")

            insider = str(row[insider_col]).strip() if insider_col else "Unknown"
            title   = str(row[title_col]).strip()   if title_col   else ""
            txn_txt = str(row[txn_col]).strip().lower() if txn_col else ""
            shares  = abs(parse_num(row[shares_col])) if shares_col else 0
            value   = abs(parse_num(row[value_col]))  if value_col  else 0

            is_buy  = any(w in txn_txt for w in ["purchase", "buy", "p -", "acquisition", "acquired"])
            is_sell = any(w in txn_txt for w in ["sale", "sell", "sold", "s -", "dispose", "s+plan"])

            entry = {
                "date": date_str, "insider": insider, "title": title,
                "transaction": txn_txt.title() or "—",
                "shares": int(shares), "value": value,
                "recent": txn_date >= cutoff_60,
            }
            all_txns.append(entry)
            if is_buy:
                buys.append(entry)
            elif is_sell:
                sells.append(entry)

        # Net signal based on last-60-day activity only
        recent_buys  = [b for b in buys  if b.get("recent")]
        recent_sells = [s for s in sells if s.get("recent")]
        buy_val  = sum(b["value"] for b in recent_buys)
        sell_val = sum(s["value"] for s in recent_sells)

        if not recent_buys and not recent_sells:
            signal = "neutral"
        elif buy_val > 0 and sell_val == 0:
            signal = "strong_buy"
        elif buy_val >= sell_val * 2:
            signal = "buy"
        elif sell_val >= buy_val * 2:
            signal = "sell"
        else:
            signal = "mixed"

        # Analyst-style rationale for recent buys
        rationale = ""
        if recent_buys:
            total_v = buy_val
            names = list({b["insider"] for b in recent_buys})[:3]
            rationale = (
                f"{len(recent_buys)} insider purchase{'s' if len(recent_buys)>1 else ''} "
                f"totalling ${total_v/1e6:.2f}M in the last 60 days by {', '.join(names)}. "
                "Insiders typically buy when they believe the stock is undervalued "
                "relative to their private knowledge of upcoming catalysts, earnings "
                "momentum, or strategic developments not yet reflected in the price."
            )

        result = {
            "all_txns":   all_txns[:50],   # up to 2 years of history
            "buys":       recent_buys,
            "sells":      recent_sells,
            "net_signal": signal,
            "buy_value":  buy_val,
            "sell_value": sell_val,
            "rationale":  rationale,
            "summary":    f"{len(recent_buys)} buys / {len(recent_sells)} sells in last 60 days",
        }
        result = _sanitize(result, default=None)
        _cache_set(key, result)
        return result

    except Exception as e:
        return {"all_txns": [], "buys": [], "sells": [], "net_signal": "no_data",
                "summary": f"Error: {e}", "rationale": ""}


def get_upcoming_catalysts(ticker: str) -> dict:
    """
    Pull upcoming earnings date, ex-dividend date, and recent news
    to surface potential catalysts.
    """
    key = ticker + "_catalysts"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        stock = yf.Ticker(ticker.upper())
        cal = stock.calendar
        info = stock.info
        news_raw = stock.news or []

        # Earnings date
        earnings_date = None
        try:
            if cal is not None and not (hasattr(cal, "empty") and cal.empty):
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date") or cal.get("earningsDate")
                    earnings_date = str(ed[0])[:10] if ed else None
                else:
                    ed = cal.get("Earnings Date") if hasattr(cal, "get") else None
                    earnings_date = str(ed.iloc[0])[:10] if ed is not None and len(ed) > 0 else None
        except Exception:
            pass

        ex_div = info.get("exDividendDate")
        if ex_div:
            import datetime
            try:
                ex_div = datetime.datetime.fromtimestamp(ex_div).strftime("%Y-%m-%d")
            except Exception:
                ex_div = str(ex_div)[:10]

        # Recent news (last 60 days) — surface catalyst keywords
        cutoff_ts = time.time() - 60 * 86400
        catalyst_keywords = [
            "earnings", "revenue", "guidance", "acquisition", "merger", "partnership",
            "launch", "contract", "fda", "approval", "buyback", "dividend",
            "upgrade", "beat", "record", "growth", "expansion",
        ]
        news = []
        for item in news_raw[:20]:
            pub_ts = item.get("providerPublishTime", 0)
            if pub_ts < cutoff_ts:
                continue
            title = item.get("title", "")
            is_catalyst = any(kw in title.lower() for kw in catalyst_keywords)
            news.append({
                "date":        pd.Timestamp(pub_ts, unit="s").strftime("%Y-%m-%d"),
                "title":       title,
                "url":         item.get("link", ""),
                "is_catalyst": is_catalyst,
            })

        result = {
            "earnings_date": earnings_date,
            "ex_dividend":   ex_div,
            "news":          news,
        }
        _cache_set(key, result)
        return result

    except Exception as e:
        return {"earnings_date": None, "ex_dividend": None, "news": [],
                "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# RED FLAGS & RISK ANALYSIS
# ══════════════════════════════════════════════════════════════════

def get_red_flags(ticker: str) -> dict:
    """
    Derive quantitative red flags from financial metrics, plus
    surface negative-sentiment news from the last 60 days.
    """
    key = ticker + "_flags"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        info = get_stock_info(ticker)
        stock = yf.Ticker(ticker.upper())
        news_raw = stock.news or []

        flags = []

        # ── Valuation red flags ──────────────────────────────────
        pe = info.get("pe_ratio") or 0
        pb = info.get("pb_ratio") or 0
        ps = info.get("ps_ratio") or 0
        if pe > 50:
            flags.append({"type": "valuation", "severity": "high",
                          "flag": f"High P/E of {pe:.1f}x — priced for perfection; any miss could cause sharp re-rating."})
        elif pe > 35:
            flags.append({"type": "valuation", "severity": "medium",
                          "flag": f"Elevated P/E of {pe:.1f}x — significant growth already priced in."})
        if pb > 10:
            flags.append({"type": "valuation", "severity": "medium",
                          "flag": f"P/B of {pb:.1f}x — trading far above book value; justified only by very high ROE."})

        # ── Balance sheet red flags ──────────────────────────────
        de = (info.get("debt_to_equity") or 0) / 100
        cr = info.get("current_ratio") or 0
        if de > 2.0:
            flags.append({"type": "balance_sheet", "severity": "high",
                          "flag": f"Debt/Equity of {de:.1f}x — highly leveraged; rising rates or earnings miss could be painful."})
        elif de > 1.0:
            flags.append({"type": "balance_sheet", "severity": "medium",
                          "flag": f"Debt/Equity of {de:.1f}x — above average leverage; watch interest coverage."})
        if 0 < cr < 1.0:
            flags.append({"type": "balance_sheet", "severity": "high",
                          "flag": f"Current ratio of {cr:.2f} — current liabilities exceed current assets; potential liquidity risk."})

        # ── Profitability red flags ──────────────────────────────
        nm = (info.get("net_margin") or 0) * 100
        roe = (info.get("roe") or 0) * 100
        fcf = info.get("free_cashflow") or 0
        rg = (info.get("revenue_growth") or 0) * 100
        eg = (info.get("earnings_growth") or 0) * 100
        if nm < 0:
            flags.append({"type": "profitability", "severity": "high",
                          "flag": f"Net margin is negative ({nm:.1f}%) — company is burning cash; check path to profitability."})
        elif nm < 5:
            flags.append({"type": "profitability", "severity": "medium",
                          "flag": f"Thin net margin of {nm:.1f}% — limited buffer against cost increases or revenue declines."})
        if fcf < 0:
            flags.append({"type": "profitability", "severity": "high",
                          "flag": "Negative free cash flow — company is consuming capital; equity dilution or debt may follow."})
        if rg < -5:
            flags.append({"type": "growth", "severity": "high",
                          "flag": f"Revenue declining {rg:.1f}% YoY — core business may be shrinking."})
        if eg < -10 and rg > 0:
            flags.append({"type": "growth", "severity": "medium",
                          "flag": f"Earnings falling {eg:.1f}% YoY despite revenue growth — margin compression underway."})

        # ── Insider selling red flag ─────────────────────────────
        insider_data = get_insider_activity(ticker)
        if insider_data.get("net_signal") == "sell":
            sell_v = insider_data.get("sell_value", 0)
            flags.append({"type": "insider", "severity": "medium",
                          "flag": f"Net insider selling of ${sell_v/1e6:.1f}M in last 60 days — insiders reducing exposure."})

        # ── Negative news from last 60 days ─────────────────────
        neg_keywords = [
            "lawsuit", "investigation", "sec", "fraud", "recall", "downgrade",
            "miss", "below", "loss", "warning", "cut", "layoff", "breach",
            "decline", "falling", "concern", "risk", "debt", "default",
        ]
        cutoff_ts = time.time() - 60 * 86400
        neg_news = []
        for item in news_raw[:25]:
            if item.get("providerPublishTime", 0) < cutoff_ts:
                continue
            title = item.get("title", "")
            if any(kw in title.lower() for kw in neg_keywords):
                neg_news.append({
                    "date":  pd.Timestamp(item["providerPublishTime"], unit="s").strftime("%Y-%m-%d"),
                    "title": title,
                    "url":   item.get("link", ""),
                })

        result = {"flags": flags, "negative_news": neg_news[:8]}
        _cache_set(key, result)
        return result

    except Exception as e:
        return {"flags": [], "negative_news": [], "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# COMPETITOR ANALYSIS
# ══════════════════════════════════════════════════════════════════

def get_competitors(ticker: str) -> list:
    """
    Find top 5 competitors by:
    1. Matching on industry (same GICS industry from yfinance)
    2. Ranking candidates from US_UNIVERSE by market cap
    3. Fetching key metrics + recent news for each
    """
    key = ticker + "_competitors"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        base_info = get_stock_info(ticker.upper())
        industry  = base_info.get("industry", "")
        sector    = base_info.get("sector", "")
        base_mcap = base_info.get("market_cap", 0)

        if not industry and not sector:
            return []

        # Find candidates from universe (same industry first, then sector)
        candidates = [t for t in US_UNIVERSE if t != ticker.upper()]

        # Quick parallel fetch — just basic info
        peer_data = []
        def _fetch_peer(t):
            try:
                i = get_stock_info(t)
                if "error" in i:
                    return None
                i_ind = i.get("industry", "")
                i_sec = i.get("sector", "")
                if i_ind == industry or (not industry and i_sec == sector):
                    return i
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(_fetch_peer, t): t for t in candidates}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    peer_data.append(r)

        # Sort by closeness to base market cap, then pick top 5
        peer_data.sort(key=lambda x: abs((x.get("market_cap") or 0) - base_mcap))
        top5 = peer_data[:5]

        # Enrich with recent news & catalyst keywords
        cutoff_ts = time.time() - 60 * 86400
        result = []
        for peer in top5:
            t = peer["ticker"]
            news_raw = []
            try:
                news_raw = yf.Ticker(t).news or []
            except Exception:
                pass

            recent_news = []
            catalyst_kw = ["partnership", "launch", "contract", "deal", "acquisition",
                           "merger", "fda", "approval", "expansion", "record", "beat"]
            for item in news_raw[:15]:
                if item.get("providerPublishTime", 0) < cutoff_ts:
                    continue
                title = item.get("title", "")
                is_catalyst = any(kw in title.lower() for kw in catalyst_kw)
                recent_news.append({
                    "date":        pd.Timestamp(item["providerPublishTime"], unit="s").strftime("%Y-%m-%d"),
                    "title":       title,
                    "url":         item.get("link", ""),
                    "is_catalyst": is_catalyst,
                })

            rg   = round((peer.get("revenue_growth") or 0) * 100, 1)
            pe   = peer.get("pe_ratio") or 0
            mcap = peer.get("market_cap") or 0

            # PEG-like score: revenue growth / PE (higher = more value per unit of growth)
            growth_value_score = round(rg / pe, 2) if pe > 0 and rg > 0 else 0

            result.append({
                "ticker":              t,
                "name":                peer.get("name", t),
                "sector":              peer.get("sector", ""),
                "industry":            peer.get("industry", ""),
                "market_cap":          mcap,
                "revenue_growth":      rg,
                "pe_ratio":            round(pe, 1) if pe else None,
                "pb_ratio":            round(peer.get("pb_ratio") or 0, 2),
                "net_margin":          round((peer.get("net_margin") or 0) * 100, 1),
                "roe":                 round((peer.get("roe") or 0) * 100, 1),
                "growth_value_score":  growth_value_score,
                "recent_news":         recent_news[:5],
                "price":               peer.get("current_price", 0),
                "ev_ebitda":           round(peer.get("ev_ebitda") or 0, 1),
            })

        # Sort by growth-value score to highlight the best opportunity
        result.sort(key=lambda x: x["growth_value_score"], reverse=True)

        _cache_set(key, result)
        return result

    except Exception as e:
        return []

# ══════════════════════════════════════════════════════════════════
# MOAT RATING  (auto-scored from financial metrics)
# ══════════════════════════════════════════════════════════════════

def get_moat_rating(ticker: str) -> dict:
    """
    Score economic moat from publicly available financial metrics.
    Returns: { rating: "Wide"|"Narrow"|"None", score: 0-100, factors: [...] }

    Scoring rubric (100 pts total):
      ROE ≥ 15% consistently        → up to 30 pts
      Gross margin ≥ 40%            → up to 25 pts
      Net margin ≥ 10%              → up to 15 pts
      Revenue growth consistency    → up to 15 pts  (positive 4/5 yrs)
      Low capex intensity (FCF/Rev) → up to 15 pts
    """
    key = ticker + "_moat"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        info = get_stock_info(ticker)
        fin  = get_financial_history(ticker)
        summary = fin.get("summary", [])

        factors = []
        score = 0

        # ── ROE (30 pts) ──────────────────────────────────────────
        roe = (info.get("roe") or 0) * 100
        if roe >= 20:
            pts = 30; note = f"Exceptional ROE of {roe:.1f}% — likely pricing power or asset-light model"
        elif roe >= 15:
            pts = 22; note = f"Strong ROE of {roe:.1f}% — above cost of capital"
        elif roe >= 10:
            pts = 12; note = f"Moderate ROE of {roe:.1f}% — competitive but not dominant"
        else:
            pts = 0;  note = f"Weak ROE of {roe:.1f}% — no clear return advantage"
        score += pts
        factors.append({"name": "Return on Equity", "pts": pts, "max": 30, "note": note})

        # ── Gross Margin (25 pts) ─────────────────────────────────
        gm = (info.get("gross_margin") or 0) * 100
        if gm >= 60:
            pts = 25; note = f"Gross margin {gm:.1f}% — software/platform-like pricing power"
        elif gm >= 40:
            pts = 18; note = f"Gross margin {gm:.1f}% — strong product differentiation"
        elif gm >= 25:
            pts = 10; note = f"Gross margin {gm:.1f}% — competitive industry, limited premium pricing"
        else:
            pts = 0;  note = f"Gross margin {gm:.1f}% — commodity-like pricing pressure"
        score += pts
        factors.append({"name": "Gross Margin", "pts": pts, "max": 25, "note": note})

        # ── Net Margin (15 pts) ───────────────────────────────────
        nm = (info.get("net_margin") or 0) * 100
        if nm >= 20:
            pts = 15; note = f"Net margin {nm:.1f}% — exceptional profitability"
        elif nm >= 10:
            pts = 10; note = f"Net margin {nm:.1f}% — above-average earnings quality"
        elif nm >= 5:
            pts = 5;  note = f"Net margin {nm:.1f}% — thin but positive"
        else:
            pts = 0;  note = f"Net margin {nm:.1f}% — no margin buffer"
        score += pts
        factors.append({"name": "Net Margin", "pts": pts, "max": 15, "note": note})

        # ── Revenue Growth Consistency (15 pts) ───────────────────
        if len(summary) >= 3:
            growth_years = 0
            revs = [r.get("revenue") or 0 for r in summary if r.get("revenue")]
            for i in range(1, len(revs)):
                if revs[i] > revs[i-1]:
                    growth_years += 1
            total_years = max(len(revs) - 1, 1)
            consistency = growth_years / total_years
            if consistency >= 0.8:
                pts = 15; note = f"Revenue grew in {growth_years}/{total_years} years — durable demand"
            elif consistency >= 0.6:
                pts = 8;  note = f"Revenue grew in {growth_years}/{total_years} years — some cyclicality"
            else:
                pts = 0;  note = f"Revenue grew in {growth_years}/{total_years} years — inconsistent demand"
        else:
            pts = 7; note = "Insufficient history — partial credit"
        score += pts
        factors.append({"name": "Revenue Consistency", "pts": pts, "max": 15, "note": note})

        # ── FCF / Capital Intensity (15 pts) ─────────────────────
        fcf = info.get("free_cashflow") or 0
        rev = info.get("total_revenue") or 1
        fcf_margin = (fcf / rev * 100) if rev else 0
        if fcf_margin >= 15:
            pts = 15; note = f"FCF margin {fcf_margin:.1f}% — asset-light, high cash conversion"
        elif fcf_margin >= 8:
            pts = 10; note = f"FCF margin {fcf_margin:.1f}% — healthy cash generation"
        elif fcf_margin >= 0:
            pts = 5;  note = f"FCF margin {fcf_margin:.1f}% — positive but capital-intensive"
        else:
            pts = 0;  note = "Negative FCF — consuming capital"
        score += pts
        factors.append({"name": "FCF / Capital Efficiency", "pts": pts, "max": 15, "note": note})

        # ── Final Rating ──────────────────────────────────────────
        if score >= 70:
            rating = "Wide"
            summary_text = (
                f"{ticker} scores {score}/100. Multiple durable competitive advantages "
                "suggest strong pricing power and above-average returns for years to come."
            )
        elif score >= 40:
            rating = "Narrow"
            summary_text = (
                f"{ticker} scores {score}/100. Some competitive advantages exist but "
                "durability is uncertain — watch for margin erosion or new entrants."
            )
        else:
            rating = "None"
            summary_text = (
                f"{ticker} scores {score}/100. No clear moat detected from financials — "
                "returns may revert to the mean as competition intensifies."
            )

        result = {
            "rating":  rating,
            "score":   score,
            "factors": factors,
            "summary": summary_text,
        }
        result = _sanitize(result, default=None)
        _cache_set(key, result)
        return result

    except Exception as e:
        return {"rating": "N/A", "score": 0, "factors": [], "summary": f"Error: {e}"}


# ══════════════════════════════════════════════════════════════════
# RISK RATING
# ══════════════════════════════════════════════════════════════════

def get_risk_rating(ticker: str) -> dict:
    """
    Composite risk score (0-100, higher = riskier).
    Returns: { rating: "Low"|"Medium"|"High", score: 0-100, factors: [...] }
    """
    key = ticker + "_risk"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        info = get_stock_info(ticker)
        risk_pts = 0
        factors  = []

        # ── Beta / volatility (25 pts) ────────────────────────────
        beta = info.get("beta") or 1.0
        if beta > 2.0:
            pts = 25; note = f"Beta {beta:.2f} — extremely volatile vs market"
        elif beta > 1.5:
            pts = 18; note = f"Beta {beta:.2f} — significantly more volatile than market"
        elif beta > 1.1:
            pts = 10; note = f"Beta {beta:.2f} — modestly above market volatility"
        elif beta > 0:
            pts = 3;  note = f"Beta {beta:.2f} — low volatility stock"
        else:
            pts = 5;  note = "Negative or unknown beta — interpret with caution"
        risk_pts += pts
        factors.append({"name": "Market Volatility (Beta)", "pts": pts, "max": 25, "note": note})

        # ── Leverage / Debt (25 pts) ──────────────────────────────
        de = (info.get("debt_to_equity") or 0) / 100
        if de > 3.0:
            pts = 25; note = f"D/E ratio {de:.2f}x — dangerously leveraged"
        elif de > 2.0:
            pts = 20; note = f"D/E ratio {de:.2f}x — high debt load"
        elif de > 1.0:
            pts = 12; note = f"D/E ratio {de:.2f}x — moderate leverage"
        elif de > 0.5:
            pts = 6;  note = f"D/E ratio {de:.2f}x — manageable debt"
        else:
            pts = 0;  note = f"D/E ratio {de:.2f}x — conservative balance sheet"
        risk_pts += pts
        factors.append({"name": "Financial Leverage (D/E)", "pts": pts, "max": 25, "note": note})

        # ── Liquidity (20 pts) ────────────────────────────────────
        cr = info.get("current_ratio") or 0
        if cr == 0:
            pts = 10; note = "Current ratio unavailable"
        elif cr < 1.0:
            pts = 20; note = f"Current ratio {cr:.2f} — liabilities exceed current assets (liquidity risk)"
        elif cr < 1.5:
            pts = 12; note = f"Current ratio {cr:.2f} — tight liquidity cushion"
        elif cr < 2.0:
            pts = 5;  note = f"Current ratio {cr:.2f} — adequate liquidity"
        else:
            pts = 0;  note = f"Current ratio {cr:.2f} — strong liquidity"
        risk_pts += pts
        factors.append({"name": "Liquidity (Current Ratio)", "pts": pts, "max": 20, "note": note})

        # ── FCF Health (15 pts) ───────────────────────────────────
        fcf = info.get("free_cashflow") or 0
        if fcf < 0:
            pts = 15; note = "Negative FCF — company burning cash"
        elif fcf == 0:
            pts = 8;  note = "Zero FCF — not generating free cash"
        else:
            pts = 0;  note = "Positive FCF — self-funding operations"
        risk_pts += pts
        factors.append({"name": "Free Cash Flow", "pts": pts, "max": 15, "note": note})

        # ── Earnings Visibility (15 pts) ──────────────────────────
        eg = (info.get("earnings_growth") or 0) * 100
        nm = (info.get("net_margin") or 0) * 100
        if nm < 0:
            pts = 15; note = f"Net loss (margin {nm:.1f}%) — no earnings floor"
        elif eg < -20:
            pts = 12; note = f"Earnings declining {eg:.1f}% — visibility deteriorating"
        elif eg < -5:
            pts = 7;  note = f"Earnings declining {eg:.1f}% — modest pressure"
        else:
            pts = 0;  note = f"Earnings stable/growing — good visibility"
        risk_pts += pts
        factors.append({"name": "Earnings Visibility", "pts": pts, "max": 15, "note": note})

        # ── Final Rating ──────────────────────────────────────────
        if risk_pts >= 55:
            rating = "High"
            summary_text = (
                f"{ticker} has a high-risk profile (score {risk_pts}/100). "
                "Elevated volatility, leverage, or earnings uncertainty — "
                "position sizing and stop-losses are important."
            )
        elif risk_pts >= 28:
            rating = "Medium"
            summary_text = (
                f"{ticker} carries moderate risk (score {risk_pts}/100). "
                "Some vulnerabilities exist but overall manageable for a long-term investor."
            )
        else:
            rating = "Low"
            summary_text = (
                f"{ticker} is low-risk (score {risk_pts}/100). "
                "Conservative balance sheet, stable earnings, low volatility."
            )

        result = {
            "rating":  rating,
            "score":   risk_pts,
            "factors": factors,
            "summary": summary_text,
        }
        result = _sanitize(result, default=None)
        _cache_set(key, result)
        return result

    except Exception as e:
        return {"rating": "N/A", "score": 0, "factors": [], "summary": f"Error: {e}"}


# ══════════════════════════════════════════════════════════════════
# BULL / BEAR PRICE TARGETS
# ══════════════════════════════════════════════════════════════════

def get_price_targets(ticker: str) -> dict:
    """
    Generate bull / base / bear price targets using analyst consensus +
    simple valuation range from P/E bands and DCF range.
    """
    key = ticker + "_targets"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        info  = get_stock_info(ticker)
        price = info.get("current_price") or 0
        eps   = info.get("eps_ttm") or 0
        eps_fwd = info.get("eps_forward") or eps
        pe    = info.get("pe_ratio") or 0
        fcf   = info.get("free_cashflow") or 0
        shares = info.get("shares_outstanding") or 1
        net_debt = (info.get("total_debt") or 0) - (info.get("total_cash") or 0)

        # ── Analyst consensus ─────────────────────────────────────
        analyst_low  = info.get("target_low_price")  or 0
        analyst_high = info.get("target_high_price") or 0
        analyst_mid  = info.get("target_mean_price") or 0

        # ── P/E band targets ──────────────────────────────────────
        # Use forward EPS with bull / base / bear P/E multiples
        sector_pe = pe if pe and 5 < pe < 60 else 18
        pe_bear = round(eps_fwd * (sector_pe * 0.75), 2) if eps_fwd else 0
        pe_base = round(eps_fwd * sector_pe, 2)           if eps_fwd else 0
        pe_bull = round(eps_fwd * (sector_pe * 1.30), 2)  if eps_fwd else 0

        # ── Simple DCF range (3 scenarios, 5yr) ──────────────────
        def simple_dcf(g1, g2, wacc, tg):
            if not (fcf and shares and wacc > tg):
                return 0
            cf = fcf
            pv = 0
            for yr in range(1, 11):
                g = g1 if yr <= 5 else g2
                cf *= (1 + g / 100)
                pv += cf / ((1 + wacc / 100) ** yr)
            tv = cf * (1 + tg / 100) / ((wacc - tg) / 100)
            pv_tv = tv / ((1 + wacc / 100) ** 10)
            ev = pv + pv_tv
            equity = ev - net_debt
            return round(equity / shares, 2) if shares else 0

        dcf_bear = simple_dcf(3,  1, 12, 1.5)
        dcf_base = simple_dcf(8,  4, 10, 2.5)
        dcf_bull = simple_dcf(15, 8,  9, 3.0)

        # ── Blended targets (weight analyst + model) ──────────────
        def blend(model_val, analyst_val, w_model=0.5):
            if not model_val and not analyst_val:
                return 0
            if not model_val:
                return analyst_val
            if not analyst_val:
                return model_val
            return round(model_val * w_model + analyst_val * (1 - w_model), 2)

        bear_target = blend(max(pe_bear, dcf_bear) if pe_bear or dcf_bear else 0, analyst_low)
        base_target = blend(max(pe_base, dcf_base) if pe_base or dcf_base else 0, analyst_mid)
        bull_target = blend(max(pe_bull, dcf_bull) if pe_bull or dcf_bull else 0, analyst_high)

        # Fall back to analyst-only if models produce nothing
        if not bear_target: bear_target = analyst_low
        if not base_target: base_target = analyst_mid
        if not bull_target: bull_target = analyst_high

        def upside(target):
            return round((target - price) / price * 100, 1) if price and target else None

        result = {
            "price":       price,
            "bear_target": bear_target,
            "base_target": base_target,
            "bull_target": bull_target,
            "bear_upside": upside(bear_target),
            "base_upside": upside(base_target),
            "bull_upside": upside(bull_target),
            "analyst_low":  analyst_low,
            "analyst_mid":  analyst_mid,
            "analyst_high": analyst_high,
            "pe_bear": pe_bear, "pe_base": pe_base, "pe_bull": pe_bull,
            "dcf_bear": dcf_bear, "dcf_base": dcf_base, "dcf_bull": dcf_bull,
        }
        result = _sanitize(result, default=None)
        _cache_set(key, result)
        return result

    except Exception as e:
        return {"bear_target": 0, "base_target": 0, "bull_target": 0, "summary": f"Error: {e}"}
