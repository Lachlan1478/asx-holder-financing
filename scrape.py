"""Download the director's interest notices and substantial holder notices ASX holds for the largest listed companies."""
import argparse
import csv
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import requests

API = "https://asx.api.cmfyapp.com/asx-research/1.0"
DIRECTORY = "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file?access_token=83ff96335c2d45a094df02a206a39ff4"
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.asx.com.au", "Referer": "https://www.asx.com.au/"}
KINDS = [("Change of Director's Interest Notice", "3Y"), ("Initial Director's Interest Notice", "3X"), ("Final Director's Interest Notice", "3Z"),
         ("Becoming a substantial holder", "603"), ("Change in substantial holding", "604"), ("Ceasing to be a substantial holder", "605")]
DATA = Path(__file__).parent / "data"
session = requests.Session()
session.headers.update(HEADERS)


def get(url, **params):
    for attempt in range(4):
        r = session.get(url, params=params, timeout=60)
        if r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()
            return r
        time.sleep(2 ** attempt)
    r.raise_for_status()


def companies(n):
    """Top n ASX-listed companies by market cap from the ASX directory."""
    rows = [r for r in csv.DictReader(io.StringIO(get(DIRECTORY).text)) if r["Market Cap"].strip().isdigit()]
    rows.sort(key=lambda r: -int(r["Market Cap"]))
    return [{"symbol": r["ASX code"], "name": r["Company name"], "market_cap": int(r["Market Cap"]), "industry": r["GICs industry group"]} for r in rows[:n]]


def entity_xid(symbol):
    items = get(f"{API}/search/predictive", searchText=symbol, useBondsLookup="true").json()["data"]["items"]
    return next((i["xidEntity"] for i in items if i["symbol"] == symbol), None)


def filings(symbol, since):
    """Every 3X/3Y/3Z and 603/604/605 lodged for `symbol` on or after `since`, newest first."""
    xid = entity_xid(symbol)
    page = 0
    while xid:
        d = get(f"{API}/markets/announcements", entityXids=xid, page=page, itemsPerPage=200, announcementTypes="security holder details").json()["data"]
        for a in d["items"]:
            if a["date"][:10] < since:
                return
            kind = next((k for t, k in KINDS if t in a["announcementTypes"]), None)
            if kind and a["symbol"] == symbol:
                yield {"symbol": symbol, "kind": kind, "date": a["date"][:10], "headline": a["headline"], "document_key": a["documentKey"]}
        if not d["items"] or (page + 1) * 200 >= d["count"]:
            return
        page += 1


def pdf_text(pdf):
    """Text, page count, and whether a third or more of the pages lack a usable text layer."""
    with pymupdf.open(pdf) as doc:
        pages = [p.get_text() for p in doc]
    return "\n".join(pages), len(pages), sum(len(p.strip()) < 200 for p in pages) >= len(pages) / 3


def fetch(row):
    key = row["document_key"]
    pdf, txt = DATA / "pdf" / f"{key}.pdf", DATA / "text" / f"{key}.txt"
    try:
        if not pdf.exists():
            pdf.write_bytes(get(f"{API}/file/{key}").content)
        text, pages, scanned = pdf_text(pdf)
        if not txt.exists():
            txt.write_text(text)
        row.update(status="ok", pages=pages, text_chars=len(text), scanned=int(scanned))
    except Exception as e:
        row.update(status=f"error: {e}", pages="", text_chars="", scanned="")
    return row


def write_csv(path, rows, key):
    """Write rows, keeping any existing rows whose `key` is not in this run."""
    old = {r[key]: r for r in csv.DictReader(open(path))} if path.exists() else {}
    old.update({r[key]: r for r in rows})
    rows = sorted(old.values(), key=lambda r: (r.get("symbol", ""), r.get("date", "")))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        w.writeheader()
        w.writerows(rows)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-n", type=int, default=300, help="number of companies by market cap")
    p.add_argument("--symbols", nargs="*", help="only these ASX codes")
    p.add_argument("--since", default="2015-01-01", help="ISO date; ignore filings before it")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    for d in ("pdf", "text"):
        (DATA / d).mkdir(parents=True, exist_ok=True)
    cs = companies(args.n)
    if args.symbols:
        cs = [c for c in cs if c["symbol"] in args.symbols] or [{"symbol": s, "name": s, "market_cap": 0, "industry": ""} for s in args.symbols]
    write_csv(DATA / "companies.csv", cs, "symbol")

    def listing(c):
        try:
            rows = list(filings(c["symbol"], args.since))
        except Exception as e:
            print(f"{c['symbol']:6} listing error: {e}", file=sys.stderr)
            return []
        print(f"{c['symbol']:6} {len(rows)} filings", file=sys.stderr)
        return rows

    with ThreadPoolExecutor(4) as ex:
        todo = [r for rows in ex.map(listing, cs) for r in rows]
    with ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(fetch, todo))
    write_csv(DATA / "filings.csv", rows, "document_key")
    ok = [r for r in rows if r["status"] == "ok"]
    by_kind = {k: sum(r["kind"] == k for r in ok) for _, k in KINDS}
    print(f"{len(ok)}/{len(rows)} filings downloaded {by_kind}, {sum(r['scanned'] for r in ok)} scanned -> {DATA / 'filings.csv'}", file=sys.stderr)


if __name__ == "__main__":
    main()
