# research/ — ValueInvestor research engine (free data)

Used by the Claude scheduled tasks (see RUNBOOK.md). Not imported by the Flask app.

| Script | Purpose |
|---|---|
| `vi_data.py` | Free data layer: SEC EDGAR (XBRL frames, companyfacts, submissions), Nasdaq screener, Yahoo Finance, FRED |
| `screen.py` | Weekly framework screen → ranked candidates with test results, value-trap flags, quality gates |
| `valuation.py` | Triangulated intrinsic value (DCF-FCFF, EPV, peer multiples, asset floor), MoS, entry target, sensitivity |
| `health.py` | Mechanical thesis health checks → issues + Green/Amber/Red |

`framework.json` here is a mirror; the canonical copy lives in Google Drive `ValueInvestor/config/`.
