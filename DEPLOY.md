# Deploy Value Investor App — Free & Public

Two ways to access the app from your phone anywhere:

---

## Option A: Same WiFi (instant, zero setup)

1. Double-click `start.bat` on your PC
2. Look for the `PHONE / TABLET` line — it shows your local IP, e.g. `http://192.168.1.5:5001`
3. Open that URL on your phone (must be on the same WiFi as your PC)

**Limitation:** Only works when your PC is on and on the same network.

---

## Option B: Render — 100% free, public internet

No credit card needed. App is accessible from any device, anywhere.

### Step 1 — Push to GitHub

Run `setup_github.bat` in the `ValueInvestor` folder.
It will ask you to create a GitHub repo and paste the URL. Follow the on-screen steps.

> Requires Git for Windows: https://git-scm.com/download/win

### Step 2 — Deploy on Render

1. Go to https://render.com and sign in with GitHub (free)
2. Click **New +** → **Web Service**
3. Connect your `value-investor` GitHub repo
4. Render reads `render.yaml` automatically — no manual config needed
5. Click **Deploy** — takes ~3 minutes
6. You get a permanent URL like: `https://value-investor.onrender.com`

### Step 3 — Bookmark on your phone

Done! Open it from any device, any network.

---

## Keeping your data safe

The `render.yaml` sets up a persistent disk at `/var/data` — your portfolio, watchlist, and thesis data survive redeployments.

## Updating the app

```
git add .
git commit -m "Update"
git push
```

Render auto-redeploys within ~2 minutes.

---

## Free tier notes

- **Render free tier:** App sleeps after 15 min of inactivity. First visit after sleep takes ~30 sec to wake. No credit card needed.
- **Railway:** Also free ($5/month credit, no credit card). Does not sleep. Use `railway.json` (already included) if you prefer Railway over Render.
