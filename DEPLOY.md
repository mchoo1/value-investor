# Deploy Value Investor App — Free

---

## Option A: Same WiFi (instant, no setup)

1. Run `start.bat` on your PC
2. Look for the **PHONE / TABLET** line — e.g. `http://192.168.1.5:5001`
3. Open that on your phone while on the same WiFi

---

## Option B: Vercel + Neon (100% free, public internet)

No credit card needed. Works from any device, any network.

### Step 1 — Free Postgres database (Neon)

1. Go to **https://neon.tech** → sign up free (GitHub login works)
2. Create a new project, name it `value-investor`
3. Copy the **Connection string** — looks like:
   `postgresql://user:password@ep-xxx.neon.tech/neondb?sslmode=require`

### Step 2 — Deploy on Vercel

1. Go to **https://vercel.com** → sign in with GitHub (free)
2. Click **Add New → Project**
3. Import your `mchoo1/value-investor` repo
4. Before deploying, add an **Environment Variable**:
   - Name: `DATABASE_URL`
   - Value: paste the Neon connection string from Step 1
5. Click **Deploy** — takes ~2 minutes
6. Vercel gives you a URL like `https://value-investor-xxx.vercel.app`

**Bookmark that URL on your phone — done!**

---

## Updating the app

After any code change:
```
git add .
git commit -m "Update"
git push
```
Vercel auto-redeploys in ~1 minute.

---

## How data is stored

- **Local (`start.bat`):** SQLite file in the `data/` folder on your PC
- **Vercel:** Neon Postgres database (free 512MB — more than enough)

The app detects which one to use automatically via the `DATABASE_URL` environment variable.
