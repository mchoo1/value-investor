"""Smart-money signals from SEC 13F filings (free, quarterly, ~45 days after quarter end).

Two groups of managers:
  growth specialists  concentrated growth investors; a new or larger position is a real conviction signal
  large institutions  BlackRock, Vanguard, Fidelity, T. Rowe, Capital Group; shows broad institutional flow

For every ticker: which specialists hold it, which opened or added (20%+ more shares) last quarter,
which trimmed or sold out, and the large institutions' combined change in shares.
CUSIPs are mapped to tickers with the SEC fails-to-deliver files (CUSIP|SYMBOL).

  python tools\\smartmoney.py            print the strongest signals (test)
"""
import re, io, json, time, zipfile, datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
import os, requests

UA = os.environ.get("SEC_USER_AGENT", "")
CACHE = Path(__file__).with_name(".cache") / "13f"
CACHE.mkdir(parents=True, exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})

SPECIALISTS = {
    1088875: "Baillie Gifford", 1697748: "ARK Invest", 1020066: "Sands Capital", 1034524: "Polen Capital",
    53417: "Jennison", 1167483: "Tiger Global", 1135730: "Coatue", 1798849: "Durable Capital",
    1387322: "Whale Rock", 1061165: "Lone Pine", 1541617: "Altimeter", 1747057: "D1 Capital",
    1569049: "Light Street", 1777813: "Atreides", 1103804: "Viking Global", 1647251: "TCI",
    1336528: "Pershing Square",
}
INSTITUTIONS = {2012383: "BlackRock", 102909: "Vanguard", 315066: "Fidelity (FMR)", 80255: "T. Rowe Price",
                1422849: "Capital World Investors"}
ADD, CUT = 0.20, -0.20
MIN_WEIGHT = 0.0025


def get(url, binary=False, tries=3):
    for i in range(tries):
        r = S.get(url, timeout=120)
        if r.status_code == 200:
            return r.content if binary else r.text
        if r.status_code in (429, 503):
            time.sleep(2 + 3 * i)
            continue
        return None
    return None


def cached(name, fetch):
    p = CACHE / name
    if p.exists():
        return json.loads(p.read_text())
    data = fetch()
    if data is not None:
        p.write_text(json.dumps(data))
    return data


def cusip_map():
    """CUSIP (first 9) -> ticker, from the four latest SEC fails-to-deliver files."""
    def build():
        page = get("https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data") or ""
        links = re.findall(r'href="(/files/data/[^"]*cnsfails\d+[ab]\.zip)"', page)[:4]
        out = {}
        for link in links:
            raw = get("https://www.sec.gov" + link, binary=True)
            if not raw:
                continue
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for line in z.read(z.namelist()[0]).decode("latin-1").splitlines()[1:]:
                    f = line.split("|")
                    if len(f) > 2 and f[1] and f[2]:
                        out.setdefault(f[1].strip().upper(), f[2].strip().upper().replace(".", "-"))
            time.sleep(0.3)
        return out
    return cached(f"cusips-{dt.date.today():%Y-%m}.json", build)


def last_two_13f(cik):
    """Accession numbers of the two latest original 13F-HR filings (by report period)."""
    d = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json") or "{}")
    r = (d.get("filings") or {}).get("recent") or {}
    rows = sorted({(r["reportDate"][i], r["accessionNumber"][i]) for i in range(len(r.get("form", [])))
                   if r["form"][i] == "13F-HR"}, reverse=True)
    seen, out = set(), []
    for period, acc in rows:
        if period not in seen:
            seen.add(period)
            out.append((period, acc))
        if len(out) == 2:
            break
    return out


def holdings(cik, acc):
    """{cusip: shares} for one 13F filing (cached forever: filings don't change)."""
    def fetch():
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
        idx = json.loads(get(base + "/index.json") or "{}")
        names = [i["name"] for i in idx.get("directory", {}).get("item", []) if i["name"].lower().endswith(".xml")
                 and "primary_doc" not in i["name"].lower()]
        if not names:
            return None
        xml = get(f"{base}/{names[0]}")
        if not xml:
            return None
        xml = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml)
        xml = re.sub(r"<(/?)\w+:", r"<\1", xml)
        xml = re.sub(r'\s\w+:\w+="[^"]*"', "", xml)          # prefixed attributes (xsi:schemaLocation)
        out = {}
        for it in ET.fromstring(xml).iter("infoTable"):
            if (it.findtext("putCall") or "").strip():
                continue                                   # options, not shares
            cu = (it.findtext("cusip") or "").strip().upper()
            sh = it.find("shrsOrPrnAmt")
            n = float(sh.findtext("sshPrnamt") or 0) if sh is not None else 0
            v = float(it.findtext("value") or 0)
            if cu and (sh is None or (sh.findtext("sshPrnamtType") or "SH").strip() == "SH"):
                o = out.setdefault(cu, [0.0, 0.0])
                o[0] += n
                o[1] += v
        return out
    return cached(f"v2-{cik}-{acc}.json", fetch)


def signals(tickers=None, log=print):
    """{ticker: {...}} for tickers held or changed by the tracked managers."""
    cmap = cusip_map() or {}
    if not cmap:
        log("    [warn] smart money: CUSIP map unavailable")
        return {}
    want = set(tickers) if tickers else None
    out, period = {}, None

    def row(t):
        return out.setdefault(t, {"holders": [], "new": [], "added": [], "trimmed": [], "sold": [],
                                  "inst_now": 0.0, "inst_prev": 0.0, "inst_names": []})

    for group, managers in (("spec", SPECIALISTS), ("inst", INSTITUTIONS)):
        for cik, name in managers.items():
            try:
                two = last_two_13f(cik)
                if len(two) < 2:
                    continue
                (p_now, a_now), (_, a_prev) = two
                period = max(period or p_now, p_now)
                now, prev = holdings(cik, a_now) or {}, holdings(cik, a_prev) or {}
            except Exception as e:                       # one manager failing never stops the screen
                log(f"    [warn] 13F {name}: {e.__class__.__name__} {str(e)[:80]}")
                continue
            total = sum(v for _, v in now.values()) or 1
            floor = max(10e6, MIN_WEIGHT * total)          # ignore token positions (under $10M or 0.25% of the fund)
            for cu in set(now) | set(prev):
                t = cmap.get(cu[:9])
                if not t or (want and t not in want):
                    continue
                n, nv = now.get(cu, [0, 0])
                p, pv = prev.get(cu, [0, 0])
                if group == "spec" and nv < floor and pv < floor:
                    continue
                if group == "spec" and nv < floor:
                    n = 0                                   # shrank to a token position: treat as sold
                r = row(t)
                if group == "inst":
                    r["inst_now"] += n
                    r["inst_prev"] += p
                    if n:
                        r["inst_names"].append(name)
                    continue
                if n:
                    r["holders"].append(name)
                if n and not p:
                    r["new"].append(name)
                elif n and p and n / p - 1 >= ADD:
                    r["added"].append(name)
                elif p and not n:
                    r["sold"].append(name)
                elif n and p and n / p - 1 <= CUT:
                    r["trimmed"].append(name)
            time.sleep(0.15)
    for t, r in out.items():
        r["inst_change"] = (r["inst_now"] / r["inst_prev"] - 1) if r["inst_prev"] else None
        buys, sells = len(r["new"]) + len(r["added"]), len(r["trimmed"]) + len(r["sold"])
        r["net_buyers"] = buys - sells
        # funnel: two or more specialists bought, or a crowd of specialists holds and nobody is leaving,
        # or the big institutions grew their combined stake by 10%+ (split-like jumps ignored)
        ic = r["inst_change"]
        r["hit"] = (buys >= 2 and buys > sells) or (len(r["holders"]) >= 4 and sells == 0) or \
                   (ic is not None and 0.10 <= ic <= 0.9 and buys >= 1)
        r["period"] = period
    return out


if __name__ == "__main__":
    sig = signals()
    top = sorted(sig.items(), key=lambda kv: (-(len(kv[1]["new"]) + len(kv[1]["added"])), -len(kv[1]["holders"])))[:25]
    print(f"Quarter {next(iter(sig.values()))['period'] if sig else '?'}: {sum(r['hit'] for r in sig.values())} funnel hits")
    for t, r in top:
        ic = r["inst_change"]
        print(f"{t:6} holders {len(r['holders']):2} new {','.join(r['new'])[:40]:40} added {','.join(r['added'])[:40]:40} "
              f"inst {('%+.0f%%' % (ic * 100)) if ic is not None else '—'}")
