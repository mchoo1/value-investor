# ValueInvestor

Personal value-investing research app (Flask on Vercel).

## Run locally
```bash
./start.sh            # installs requirements, serves http://localhost:5001
```
Without `DATABASE_URL` the app uses a local SQLite file in `data/` (git-ignored).

## Deploy
Vercel project `value-investor` builds from `main` automatically (`vercel.json`, `@vercel/python`).
Branches get preview deployments; production changes only after review.

## Configuration (environment variables only — never commit secrets)
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Neon Postgres connection string (legacy store; being replaced by Google Drive) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account key for Drive access (Phase 2) |
| `DRIVE_ROOT_FOLDER_ID` | ID of the `ValueInvestor` Drive folder (Phase 2) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather — used only by `/api/notify` |
| `TELEGRAM_CHAT_ID` | Ming's chat id (find it via `/api/notify/setup` after sending the bot /start) |

Automated research tasks do not use a Vercel protection-bypass secret; protected URLs are reached through the Vercel connector and research data lives in Google Drive.

## Data sources (free)
SEC EDGAR (XBRL company facts, filings, Form 4) → company IR → Yahoo Finance (price, consensus) → FRED (risk-free rate) → Damodaran datasets (ERP, industry betas). Every figure carries a source and as-of date.

## Removed in cleanup/v1 (2026-09-29)
First-Cut tab + memo upload (Task B merged into the Round 1 snapshot), research queue, weekly reviews, import-from-local-DOCX (paths never existed on Vercel), Railway/Render/Procfile/.bat deploy scripts. Database tables were left untouched.
