# ValueInvestor

A small Flask app (Vercel) that shows the weekly research cycle stored in Google Drive.

## Weekly cycle (Singapore time, cloud scheduled tasks, see `research/RUNBOOK.md`)
| When | Step | Output in Drive `ValueInvestor/` |
|---|---|---|
| Sat 08:52 | Screen: List A value · List B ≥30% off 52-week high · List C high growth + moat | `screens/` |
| Sat 13:52 | 1 deep dive per list + draft thesis with trackable kill criteria | `deep-dives/TICKER/`, `theses/TICKER/thesis.json`, `theses/index.json` |
| any time | You approve or reject each draft (app or "approve TICKER" in the Stock project) | `theses/` |
| Sun 08:52 | Health check of approved theses: kill criteria, KPIs, filings, news | `alerts/`, `theses/` |
| Sun 17:52 | Digest: Telegram (1 companies to watch · 2 thesis changes) + one email | — |

## App pages
- **This week** — cycle status, companies to watch, thesis changes, drafts awaiting you.
- **Screen** — this week's Lists A, B and C.
- **Theses** — each deep dive's thesis, pillars and kill criteria; Approve & track / Reject.
- **Tracker** — approved theses: health and every kill criterion's current value vs its trigger.

## Configuration (environment variables only — never commit secrets)
| Variable | Purpose |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account key; the `ValueInvestor` folder is shared with it as Editor |
| `DRIVE_ROOT_FOLDER_ID` | ID of the `ValueInvestor` Drive folder |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Used only by `/api/notify` (sends to Ming's chat only) |
| `DATABASE_URL` | Legacy Postgres; only used by the one-time `/api/admin/legacy-db/backup-and-wipe` route |

## Run locally
```bash
pip install -r requirements.txt
VI_LOCAL_DATA=/path/to/folder/mirroring/drive python app.py   # http://localhost:5001
```

## Rebuilt 2026-10-02
The old multi-tab app (stock lookups, DCF tools, watchlist, portfolio, shortlist, triggers, research history) and its Postgres tables were retired. Research now lives in Drive; the legacy database is exported to `archive/legacy/` before it is wiped.
