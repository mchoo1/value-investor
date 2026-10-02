"""
drive_store.py — read/write the ValueInvestor Google Drive folder for the app.

Production: service account from env GOOGLE_SERVICE_ACCOUNT_JSON, root folder DRIVE_ROOT_FOLDER_ID
(the "ValueInvestor" folder, shared with the service account as Editor).
Local dev/tests: set VI_LOCAL_DATA=/path/to/folder that mirrors the Drive layout:
  screens/  deep-dives/TICKER/  theses/index.json  theses/TICKER/thesis.json  alerts/  archive/legacy/

Drive allows several files with the same name in a folder; readers always take the newest by modifiedTime.
"""
import json, os, time, glob, datetime as dt

FOLDER = "application/vnd.google-apps.folder"
_TTL = 300
_cache = {}


def _cached(key, fn, ttl=_TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def clear_cache():
    _cache.clear()


class DriveError(RuntimeError):
    pass


# ── Google Drive backend ─────────────────────────────────────────────
class GDrive:
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self):
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.root = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
        if not raw or not self.root:
            raise DriveError("GOOGLE_SERVICE_ACCOUNT_JSON / DRIVE_ROOT_FOLDER_ID not configured")
        from google.oauth2 import service_account
        self.creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"])
        import requests
        self.http = requests.Session()

    def _token(self):
        from google.auth.transport.requests import Request
        if not self.creds.valid:
            self.creds.refresh(Request())
        return self.creds.token

    def _req(self, method, url, **kw):
        h = kw.pop("headers", {})
        h["Authorization"] = f"Bearer {self._token()}"
        r = self.http.request(method, url, headers=h, timeout=30, **kw)
        if r.status_code >= 400:
            raise DriveError(f"Drive {method} {url.split('?')[0].rsplit('/', 1)[-1]}: {r.status_code} {r.text[:200]}")
        return r

    def list(self, q, fields="files(id,name,mimeType,modifiedTime,parents,webViewLink,size)"):
        out, token = [], None
        while True:
            params = {"q": q + " and trashed=false", "fields": f"nextPageToken,{fields}", "pageSize": 1000,
                      "orderBy": "modifiedTime desc", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            d = self._req("GET", f"{self.API}/files", params=params).json()
            out += d.get("files", [])
            token = d.get("nextPageToken")
            if not token:
                return out

    def children(self, folder_id):
        return self.list(f"'{folder_id}' in parents")

    def read(self, file_id):
        return self._req("GET", f"{self.API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}).content

    def update(self, file_id, data, mime="application/json"):
        self._req("PATCH", f"{self.UPLOAD}/files/{file_id}", params={"uploadType": "media", "supportsAllDrives": "true"},
                  data=data, headers={"Content-Type": mime})

    def create(self, folder_id, name, data, mime="application/json"):
        meta = {"name": name, "parents": [folder_id]}
        boundary = "vi_boundary_7f3a"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
        r = self._req("POST", f"{self.UPLOAD}/files", params={"uploadType": "multipart", "supportsAllDrives": "true",
                                                              "fields": "id,name,size"},
                      data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return r.json()


# ── Local folder backend (dev/tests) ─────────────────────────────────
class LocalDrive:
    def __init__(self, base):
        self.base = os.path.abspath(base)
        self.root = self.base

    def _meta(self, p):
        st = os.stat(p)
        return {"id": p, "name": os.path.basename(p), "mimeType": FOLDER if os.path.isdir(p) else "application/json",
                "modifiedTime": dt.datetime.utcfromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "parents": [os.path.dirname(p)], "webViewLink": "file://" + p, "size": str(st.st_size)}

    def children(self, folder_id):
        return sorted((self._meta(p) for p in glob.glob(os.path.join(folder_id, "*"))),
                      key=lambda m: m["modifiedTime"], reverse=True)

    def read(self, file_id):
        return open(file_id, "rb").read()

    def update(self, file_id, data, mime=None):
        open(file_id, "wb").write(data)

    def create(self, folder_id, name, data, mime=None):
        p = os.path.join(folder_id, name)
        open(p, "wb").write(data)
        return {"id": p, "name": name, "size": str(len(data))}


_backend = None


def backend():
    global _backend
    if _backend is None:
        local = os.environ.get("VI_LOCAL_DATA")
        _backend = LocalDrive(local) if local else GDrive()
    return _backend


# ── Helpers on top of either backend ─────────────────────────────────
def _child(folder_id, name, folder=None):
    """Newest child with this exact name (folder=True/False to filter)."""
    for f in backend().children(folder_id):
        if f["name"] == name and (folder is None or (f["mimeType"] == FOLDER) == folder):
            return f
    return None


def folder(*path):
    def find():
        cur = backend().root
        for name in path:
            f = _child(cur, name, folder=True)
            if not f:
                raise DriveError(f"Drive folder not found: {'/'.join(path)}")
            cur = f["id"]
        return cur
    return _cached("folder:" + "/".join(path), find, ttl=3600)


def read_json(file_id):
    return json.loads(backend().read(file_id).decode("utf-8"))


def newest(folder_id, suffix):
    files = [f for f in backend().children(folder_id) if f["name"].endswith(suffix) and f["mimeType"] != FOLDER]
    return max(files, key=lambda f: f["modifiedTime"]) if files else None


def latest_screen():
    def get():
        f = newest(folder("screens"), "_weekly-screen.json")
        if not f:
            return None, None
        return f, read_json(f["id"])
    return _cached("screen", get)


def latest_screen_html():
    return newest(folder("screens"), "_weekly-screen.html")


def theses_index():
    def get():
        f = _child(folder("theses"), "index.json", folder=False)
        if not f:
            return None, {"theses": []}
        return f, read_json(f["id"])
    return _cached("index", get)


def thesis_folder(ticker):
    f = _child(folder("theses"), ticker, folder=True)
    return f["id"] if f else None


def thesis(ticker):
    def get():
        fid = thesis_folder(ticker)
        f = _child(fid, "thesis.json", folder=False) if fid else None
        if not f:
            return None, None
        return f, read_json(f["id"])
    return _cached("thesis:" + ticker, get)


def deep_dives(ticker):
    """All deep-dive files for a ticker, newest first: [{date, html, json}]."""
    def get():
        f = _child(folder("deep-dives"), ticker, folder=True)
        if not f:
            return []
        by_date = {}
        for x in backend().children(f["id"]):
            n = x["name"]
            if "_deep-dive." in n:
                d, ext = n.split("_deep-dive.", 1)
                cur = by_date.setdefault(d, {"date": d})
                if ext not in cur or x["modifiedTime"] > cur[ext]["modifiedTime"]:
                    cur[ext] = x
        return sorted(by_date.values(), key=lambda r: r["date"], reverse=True)
    return _cached("dd:" + ticker, get)


def all_deep_dives():
    def get():
        out = []
        for f in backend().children(folder("deep-dives")):
            if f["mimeType"] == FOLDER:
                for r in deep_dives(f["name"]):
                    out.append({"ticker": f["name"], "date": r["date"],
                                "html_id": (r.get("html") or {}).get("id"), "json_id": (r.get("json") or {}).get("id")})
        return sorted(out, key=lambda r: (r["date"], r["ticker"]), reverse=True)
    return _cached("dd:all", get)


def latest_alerts():
    def get():
        f = newest(folder("alerts"), "_alerts.json")
        return (f, read_json(f["id"])) if f else (None, [])
    return _cached("alerts", get)


def write_json(file_meta, data):
    backend().update(file_meta["id"], json.dumps(data, indent=1, ensure_ascii=False, default=str).encode("utf-8"))
    clear_cache()
