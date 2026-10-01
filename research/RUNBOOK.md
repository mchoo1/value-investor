# ValueInvestor Runbook (v1.0, 2026-09-29)

Operating manual for the ValueInvestor scheduled tasks. Each task prompt names one section. Follow it exactly.
The source of truth for thresholds is `framework.json` (Drive: ValueInvestor/config). Never invent thresholds. If the framework is silent, propose a value, mark it "PROPOSED — needs Ming's approval", and carry on.

## 0. Common setup (every task)

1. Code: `git clone --depth 1 -b cleanup/v1 https://github.com/mchoo1/value-investor.git vi && cd vi/research && pip install -q yfinance pandas --break-system-packages`.
   If the clone fails or `research/` is missing, STOP and report "research engine not found in repo". Do not rewrite the engine from memory.
2. Framework: open Drive `config/framework.json` (file id 1G_YN7PXss6ILlpIoR-KWw4tUQvdODael) and save it as `framework.json` in the working directory. Also compare its `_meta.version` with `research/framework.json`, and use the Drive copy if they differ.
3. Drive folder ids (parent = ValueInvestor 1OCSqkETyKYV4liq5k16ceyzC6SZr_YYl):
   screens 1CH34yRup3bt7vgkvuLpknkbhqnIqNBYN · deep-dives 1Pl6gvqxq8hEGAHfHJYDNpyimPJgcEG1w · theses 1WciMU3MbcEGRQg825Pl4XaTjWb5jJwd2 · theses/index.json: search title = 'index.json' and parentId = '1WciMU3MbcEGRQg825Pl4XaTjWb5jJwd2', take the newest modifiedTime · alerts 1mk6cFSvsojjGjNUsqFnQUaGIdjb9b1h1 · requests 17m_fkdwZhSB1YYy9gVH-A2Nt61h-HGlM · config 1WwEKT94FEbtrkAZBbiS-fMoezITM9B1H
   Per-ticker subfolders (deep-dives/TICKER, theses/TICKER): search for them first and create them only if missing.
   The Drive connector cannot overwrite a file. To "update" a file, create the new version with the same title in the same folder and try to trash the older one (trash may be denied; that is fine). Readers ALWAYS take the newest by modifiedTime.
   Drive search syntax: `title = 'x'`, `title contains 'x'`, `fullText contains 'x'`, `parentId = '<id>'` (not name/q=).
4. Data (free only, in priority order): SEC EDGAR (XBRL, filings, Form 4) → company IR → Yahoo Finance → Nasdaq.com → FRED → Damodaran → web news. Every figure is {value, source, as_of}.
   Gates:
   - Price data must be less than 7 days old.
   - Price must agree across 2 sources within 2%, and market cap within 5%. Otherwise mark the field CONFLICT and show both values.
   - Consensus estimates come only from Yahoo, so tag them confidence=Medium.
   - Never estimate silently. Write "NO DATA" or "CONFLICT".
5. Sector: Nasdaq's sector labels are not GICS. Verify the sector against the 10-K SIC code and the Yahoo sector before any valuation, and record the verified sector.
6. Hard rules:
   - US-listed value stocks only.
   - No secrets in outputs.
   - Never auto-close a thesis. Recommend an action and let Ming decide.
   - Email goes ONLY to mchoo1990@gmail.com, and only from the digest task.
   - Never modify the app, Vercel or GitHub.

## 1. Weekly screen (Saturday 09:00 SGT)

1. Novelty gate: read `theses/index.json`. Pass every tracked or draft ticker to `--exclude`.
2. Run `python screen.py --framework framework.json --out screen.json --top 25 --exclude <tickers>`.
3. Round 1 snapshot, applied to the top 25 that have quality_gate=PASS (for a quality_gate=FAIL price conflict, re-check the price with one more source via web search first). For each candidate:
   - What it does, in 1 line.
   - Verified sector.
   - Why it looks cheap or misunderstood (market concern vs reality), in 2 lines.
   - Obvious red flags: litigation, accounting, customer concentration, secular decline, or a pending deal.
   - The value-trap flags from the screen, reviewed.
   - Go / no-go for Round 2.
4. Output a ranked shortlist of at most 10 names, each with "why it passes": tests passed, key metrics and the snapshot thesis.
   Mark a name `investable: true` only if all the gates in section 0.4 pass and no data conflict is unresolved.
5. Save to Drive `screens/`:
   - `YYYY-MM-DD_weekly-screen.json`: the funnel, the shortlist with metrics and tests, snapshots, sources.
   - `YYYY-MM-DD_weekly-screen.html`: a dark, self-contained page with the funnel bar, the shortlist table (tests passed as a pass/fail strip) and the snapshot cards.
6. The final answer is a short summary: the funnel counts and the shortlist with one line per name.

## 2. Round 2 deep dive (Saturday 14:00 SGT, or ad hoc)

**Input.** For the scheduled run, take the top 2–3 `investable` names from this week's screen JSON. For an ad-hoc run, take the ticker given.

**Research.**
- Read the latest 10-K (business, risk factors, MD&A, footnotes on debt, leases, goodwill, revenue recognition), the latest 10-Q, the last 2 earnings releases and call summaries, the proxy (compensation, insider ownership) and Form 4 activity.
- Include news from the last 90 days.

**Framework Phase 2 (qualitative).**
- Business model and moat type: brand, switching costs, cost advantage or regulatory/IP. Show durability evidence from the 5-year trend of ROE and net margin vs the industry.
- Management and capital allocation: buyback prices vs value, M&A record, payout, incentives.
- Industry structure and cyclicality.
- Growth drivers and runway: reinvestment rate × ROIC.
- Earnings quality: net income vs cash flow from operations, receivables and inventory vs sales, dilution.

**Valuation.**
- Choose the company type (wide_moat_compounder / average_quality / cyclical_turnaround / high_growth_early) and justify it.
- Choose 5–15 peers and justify them.
- Decide whether the company is asset-heavy.
- Run `python valuation.py TICKER --framework framework.json --company-type <type> --peers <list> [--asset-heavy] [--extra-risk]`.
  - Use `--extra-risk` if one risk could permanently impair a large part of the value.
  - Use `--growth x` only with a stated reason tied to consensus or the reinvestment math.
- At least 3 methods must carry weight. Show the low / base / high range, never a single point.
- Explain any flag, such as divergent methods or a high terminal-value share.

**Decision layer.**
- Conviction 0–100: score each component 0–5 with a one-line reason and a source. The components are moat_durability 25, financial_strength 20, management_capital_alloc 15, valuation_mos 25 (take this one from `valuation_score`) and catalyst_clarity 15.
- Verdict: BUY if score ≥70 AND MoS ≥ required AND a dated catalyst within 1–2 years AND risks are manageable. WATCH if score is 50–69, or MoS is within 10 points of required, or there is no dated catalyst. PASS if score is <50 or there is a red-flag risk.
- 12-item checklist: size, valuation, profitability, balance_sheet, cash_quality, moat, management, dilution, concentration, mos, catalyst, sensitivity. Each item is PASS / FAIL / N/A / NO DATA, with a detail line.
- Pre-mortem: 3 ways this loses money. Add "what would change my mind", 1 bullish and 1 bearish.

**Thesis (schema: Drive config/thesis.schema.json).**
- 3–4 pillars: claim, evidence, KPI, target, breach_threshold.
  - Machine-checkable KPIs must set `kpi_key` (roic / operating_margin / net_margin / revenue_growth / fcf / net_debt_ebitda) and `breach_direction`.
- Kill criteria: set `metric_key`, `threshold` and `direction` where possible.
- Catalysts with expected dates, and key risks with probability and impact.
- next_review_date = the next earnings date + 7 days (at most 90 days out).
- status = "Watch", approval = "pending", health = "Green", and history[0] = the initial snapshot.

**Outputs.**
- `deep-dives/TICKER/YYYY-MM-DD_deep-dive.html`, dark and self-contained:
  - Page 1 is the decision: a header with ticker, company, verified sector, price and as-of; a verdict chip; a conviction gauge; a price ladder (inline SVG bar with low/base/high value, entry target and current price); the checklist grid; the pre-mortem; kill criteria.
  - Tabs follow: Business & Moat · Management & Capital Allocation · Financials (a 10-year table from SEC) · Valuation (methods table, weights, the sensitivity grid, assumptions) · Risks & Catalysts · Sources (every source with its date).
- `deep-dives/TICKER/YYYY-MM-DD_deep-dive.json` holding the key data.
- `theses/TICKER/thesis.json` (the draft).
- `theses/index.json`: add or replace the entry {ticker, company, sector, status, approval, verdict, conviction, health, price, iv_low, iv_base, iv_high, entry_price_target, next_review_date, last_review, thesis_file_id, deep_dive_file_id}.

**Ad hoc only.** Run Round 1 first and show it to Ming with a go/no-go. Do Round 2 only after Ming says go. At the end, ask Ming to approve, edit or reject the draft.

## 3. Thesis health check (Sunday 09:00 SGT)

1. From `theses/index.json`, take every entry with approval="approved" and status in (Watch, Active, Paused). Drafts pending approval are skipped and listed as "awaiting approval".
2. For each one, download `thesis.json` and run `python health.py thesis.json --framework framework.json --last-check <last_review>`.
3. Add the judgement checks (web search + EDGAR 8-Ks since the last check):
   - Latest earnings vs consensus and vs guidance: a miss or a guidance cut is medium; a large cut is high.
   - Management change: medium.
   - Capital-allocation shift (big M&A, a buyback halt, dividend cut, equity issue): medium or high.
   - Anything that hits a pillar or a kill criterion that isn't machine-checkable.
   Every issue carries date, type, severity, detail, source and recommended_action (hold / re-underwrite / trim / exit review).
4. Merge the issues into thesis.json:
   - Don't duplicate an unresolved issue of the same type and detail.
   - Set health (Red if any high issue or kill criterion, Amber if any medium, else Green).
   - Update the monitor_state.
   - If health, the valuation or the status changed, append a history[] snapshot saying what changed and why.
   Never change the status and never close a thesis.
5. Save to Drive:
   - the updated thesis.json (new version, trash the old one);
   - `alerts/YYYY-MM-DD_alerts.json` as [{ticker, health, health_prev, issues_new[], recommended_action}];
   - `theses/index.json` updated with health, price and last_review.

## 4. Weekly digest (Sunday 18:00 SGT): Telegram summary + email

Read this week's screen JSON, deep dives created in the last 7 days, the latest alerts JSON and `theses/index.json`.

**A. Telegram (phone summary, sent first).**
Build a plain-text message of at most 3,500 characters. Use no Markdown and one short line per item:

```
ValueInvestor weekly — YYYY-MM-DD
🔴 RED: TICKER — issue — action
🟠 AMBER: TICKER — issue — action
Health: G x / A y / R z · awaiting approval: n
New candidates: T1, T2, T3 …
Deep dives: TICKER VERDICT conv NN, price $P vs entry $E
Approve: reply "approve TICKER" in the Stock project
Full digest: email + Drive ValueInvestor/
```

How to send it. The app is behind Vercel Authentication:
1. Call the Vercel connector tool `get_access_to_vercel_url` (teamId team_YHb0xAwRSbM0EAm8QG1szML1, url = the app base below). It returns a `?_vercel_share=...` link.
2. In bash, open that link once with a cookie jar, then POST the message:
   ```
   curl -s -c jar -b jar -L -o /dev/null "<shareableUrl>"
   curl -s -b jar -X POST -H "Content-Type: application/json" --data @msg.json "<base>/api/notify"
   ```
   Write `msg.json` with python `json.dump({"text": message})` so quoting is safe.
3. Fallback: `web_fetch_vercel_url` GET `<base>/api/notify?text=<URL-encoded>`. It is unreliable on protected routes and sometimes returns a 302 to the SSO page.

The app base is `https://value-investor-git-cleanup-v1-mchoo1s-projects.vercel.app` until cleanup/v1 is merged, then `https://value-investor-weld.vercel.app`.
- Success is `{"ok": true}`.
- On 503 (not configured) or any error, retry once, note it and continue with the email.
- Never put the bot token anywhere. The app holds it.

**B. Email (full digest).**
Send ONE email with the Gmail connector:
- To: mchoo1990@gmail.com only.
- Subject: `ValueInvestor weekly — YYYY-MM-DD`.
- Body: light-theme, mobile-safe HTML, single column, max 600px, inline CSS.

Sections:
1. Headline: 2 lines.
2. Red/Amber issues, Red first. Each has a one-line recommended action.
3. Health changes this week.
4. New screen candidates (up to 10) with one line each.
5. Deep dives completed: verdict, conviction, price vs entry target.
6. Drafts awaiting Ming's approval.
7. Links to the Drive files.

If there was no screen or there were no alerts, say so. Never add other recipients, and never send a second email.

## 5. Approving a draft (Ming in chat)

"approve TICKER" sets approval="approved" and status="Watch" (or "Active" if Ming says so), adds a history entry, and updates index.json. From then on the health check covers it.
"reject TICKER" sets approval="rejected" and status="Closed" with the reason, and the ticker stays in index.json for the novelty gate.
