"""Shared helpers for the scheduled jobs: database, Telegram, Google Drive, alerts."""
import os, json, time, datetime as dt

import requests
import psycopg2, psycopg2.extras

TODAY = dt.date.today()
# Nasdaq industry text -> theme (lower-case substring match). Used by the screen's growth funnel.
THEME_KEYWORDS = {
    "AI and energy infrastructure": ["semiconductor", "computer manufacturing", "electric utilities", "power generation",
                                     "electrical products", "edp services", "computer communications", "prepackaged software",
                                     "programming data processing", "electronic components"],
    "Automation and robotics": ["industrial machinery", "industrial specialties", "electronic test", "auto parts"],
    "Healthcare": ["biotechnology", "medical", "pharmaceutical", "hospital", "health"],
    "Space and satellites": ["aerospace", "military/government", "telecommunications equipment"],
    "Digital finance": ["investment bankers", "finance: consumer services", "business services", "finance companies"],
    "Quantum computing": ["quantum"],
    "Critical resources": ["metal mining", "precious metals", "mining", "uranium", "steel", "aluminum", "copper"],
}


def theme_of(industry):
    s = (industry or "").lower()
    return [t for t, kws in THEME_KEYWORDS.items() if any(k in s for k in kws)]


APP_URL = os.environ.get("APP_URL", "https://value-investor-weld.vercel.app")

# Drive folders (ValueInvestor/v2-outbox and v2-inbox); the service account has edit access
DRIVE_STATE_FILE = os.environ.get("DRIVE_STATE_FILE", "1ayGcP8pbqi_dys9ELCJUVz7-Nt3pTqH2")
DRIVE_INBOX = os.environ.get("DRIVE_INBOX", "1qBWYh4QaIyv07CtOCM3bx8iwxeQuV4WL")

# Seven core themes -> basket ETFs used for theme sell-off alerts
THEMES = {
    "AI and energy infrastructure": ["SMH", "XLU", "GRID"],
    "Automation and robotics": ["BOTZ", "ROBO"],
    "Healthcare": ["XLV", "XBI"],
    "Space and satellites": ["UFO", "ITA"],
    "Digital finance": ["ARKF", "IPAY"],
    "Quantum computing": ["QTUM"],
    "Critical resources": ["COPX", "URA", "REMX"],
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)


def J(o):
    return psycopg2.extras.Json(o, dumps=lambda x: json.dumps(x, default=str))


def ensure_schema(cur):
    cur.execute("""
      CREATE TABLE IF NOT EXISTS v2_research (ticker TEXT, as_of DATE, data JSONB, PRIMARY KEY (ticker, as_of));
      CREATE TABLE IF NOT EXISTS v2_inbox_log (file_id TEXT, modified TEXT, name TEXT, kind TEXT,
        loaded_at TIMESTAMPTZ DEFAULT now(), result TEXT, PRIMARY KEY (file_id, modified));
      CREATE TABLE IF NOT EXISTS v2_settings (key TEXT PRIMARY KEY, value JSONB, updated_at TIMESTAMPTZ DEFAULT now());
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS level_since DATE;
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS growth_score NUMERIC;
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS style TEXT;
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS refresh_due DATE;
      CREATE TABLE IF NOT EXISTS v2_decisions (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, decided_at TIMESTAMPTZ DEFAULT now(),
          action TEXT, decision TEXT, price NUMERIC, shares NUMERIC, note TEXT, alert_id INT, spy_price NUMERIC);
      CREATE TABLE IF NOT EXISTS v2_calendar (id SERIAL PRIMARY KEY, date DATE NOT NULL, time_et TEXT, kind TEXT, ticker TEXT,
          title TEXT NOT NULL, detail JSONB, source TEXT, UNIQUE (date, kind, title));
      ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS growth_top10 JSONB;
      ALTER TABLE v2_screen_scores ADD COLUMN IF NOT EXISTS growth_score NUMERIC;
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS health TEXT;
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS health_why TEXT;
      ALTER TABLE v2_names ADD COLUMN IF NOT EXISTS research_requested BOOLEAN DEFAULT FALSE;
      ALTER TABLE v2_alerts ADD COLUMN IF NOT EXISTS note TEXT;
      ALTER TABLE v2_events ADD COLUMN IF NOT EXISTS tag TEXT;
      ALTER TABLE v2_events ADD COLUMN IF NOT EXISTS note TEXT;
      ALTER TABLE v2_alerts ADD COLUMN IF NOT EXISTS sent_via TEXT;
      ALTER TABLE v2_alerts ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
      ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS market JSONB;
      ALTER TABLE v2_screen_runs ADD COLUMN IF NOT EXISTS stages JSONB;
    """)


def alert(cur, ticker, kind, severity, detail, dedupe, source="daily", action=None):
    """Insert an alert once per dedupe key. Returns True if new."""
    cur.execute("""INSERT INTO v2_alerts (ticker, kind, severity, detail, source, action, dedupe_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
                (ticker, kind, severity, detail, source, action, dedupe))
    return cur.fetchone() is not None


def week_key(d=None):
    y, w, _ = (d or TODAY).isocalendar()
    return f"{y}W{w:02d}"


# ------------------------------------------------------------------ Telegram
def _tg(method, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    return r.json()


def chat_id():
    cid = os.environ.get("TELEGRAM_CHAT_ID")
    if cid:
        return cid
    res = _tg("getUpdates") or {}
    for u in reversed(res.get("result", [])):
        msg = u.get("message") or u.get("channel_post") or {}
        if msg.get("chat", {}).get("id"):
            return str(msg["chat"]["id"])
    return None


def telegram(text):
    """Send HTML text. With TELEGRAM_DRY=1 or no token, print instead. Returns True if delivered or printed."""
    if os.environ.get("TELEGRAM_DRY") or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("---- TELEGRAM (not sent) ----\n" + text + "\n----------------------------")
        return True
    cid = chat_id()
    if not cid:
        print("Telegram: no chat id. Set TELEGRAM_CHAT_ID, or send your bot a message first.")
        return False
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        res = _tg("sendMessage", chat_id=cid, text=chunk, parse_mode="HTML", disable_web_page_preview=True) or {}
        if not res.get("ok"):
            print("Telegram error:", res.get("description"))
            return False
        time.sleep(0.4)
    return True


def esc(s):
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ Google Drive (service account)
_token = {"value": None, "exp": 0}


def drive_token():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"])
    creds.refresh(Request())
    _token.update(value=creds.token, exp=time.time() + 3000)
    return creds.token


def drive_list(folder_id):
    tok = drive_token()
    if not tok:
        return None
    files, page = [], None
    while True:
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {tok}"},
                         params={"q": f"'{folder_id}' in parents and trashed = false",
                                 "fields": "nextPageToken, files(id, name, modifiedTime, mimeType)",
                                 "pageSize": 200, "pageToken": page, "orderBy": "modifiedTime",
                                 "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=60)
        r.raise_for_status()
        d = r.json()
        files += d.get("files", [])
        page = d.get("nextPageToken")
        if not page:
            return files


def drive_read(file_id):
    tok = drive_token()
    r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}", params={"alt": "media"},
                     headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    r.raise_for_status()
    return r.text


def drive_update(file_id, text):
    """Replace the content of an existing file (owned by Ming, so no service-account quota is needed)."""
    tok = drive_token()
    if not tok:
        return False
    r = requests.patch(f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
                       params={"uploadType": "media", "supportsAllDrives": "true"},
                       headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                       data=text.encode(), timeout=120)
    r.raise_for_status()
    return True
