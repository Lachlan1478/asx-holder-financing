"""Screen downloaded filings for financing language, extract them with Claude, and build the event, change and holding tables."""
import argparse
import base64
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from schema import DirectorFiling, SubstantialHolderFiling

DATA = Path(__file__).parent / "data"
MODEL = "claude-opus-5"
DIRECTOR_FORMS = {"3X", "3Y", "3Z"}
STRONG_RE = re.compile(r"collar|forward (?:sale|contract|agreement|transaction)|prepaid|isda|equity swap|total return swap|cash.?settled|physically.?settled"
                       r"|put option|call option|derivative|hedg|margin (?:loan|lend|call|facilit|account)|loan (?:facility|agreement)"
                       r"|financing (?:facility|arrangement|transaction)|security interest|mortgage|pledg|encumb|secured (?:by|over|against)|as security", re.I)
WEAK = {"pledg", "security interest", "as security", "secured by", "secured over", "secured against", "mortgage", "encumb", "loan agreement"}  # stock-lending boilerplate
INSTITUTION_RE = re.compile(r"state street|vanguard|blackrock|super|corporation and subsidiaries|asset management|investment management|capital|investors|funds? management|partners|nominees|\bplc\b", re.I)
CUSTODY_RE = re.compile(r"^\W{0,3}(nominees|custody|custodian|investment management|super|wrap|equities limited)", re.I)
BANK_RE = re.compile(r"macquarie|\bubs\b|citi(?:group|bank|corp)|j\.?\s?p\.?\s?morgan|goldman|morgan stanley|merrill|bank of america|barclays|deutsche"
                     r"|credit suisse|nomura|hsbc|jarden|bnp|natixis|mizuho|commonwealth bank|commsec|westpac|national australia bank|\bnab\b|\banz\b"
                     r"|leveraged equities|bendigo|canaccord|bell potter|ord minnett|morgans|shaw and partners|wilsons|euroz|jefferies|\brbc\b", re.I)
SYSTEM = """You are an equity-financing analyst at a bank reading ASX filings to find directors and substantial holders who have financed or hedged their shareholding with a bank: collars, prepaid forwards, OTC options or swaps, margin loans, or other loans secured over the shares.

How these arrangements surface in the filings:
- A director must disclose contracts conferring a right to call for or deliver shares (s205G Corporations Act). Collars, forwards and options appear either in the interests-in-contracts part of an Appendix 3X/3Y, or in Part 1 as a 'change in the form of relevant interest' whose nature of change describes the transaction (e.g. 'entered into a collar derivative transaction and related financing facility with Macquarie Bank Limited').
- Shares moving to a bank nominee as registered holder with no change in number can mean they were pledged as collateral. Custodians for superannuation funds, wrap platforms or family trusts (e.g. 'Citicorp Nominees as custodian for X super fund', 'HSBC Custody Nominees on behalf of a family trust') are ordinary custody, not financing: record those as vehicle nominee_or_custodian with no signal. Record security_transfer only when the notice links the holder to a loan, security, pledge or margin facility, with medium confidence if the purpose is implied rather than stated.
- A margin lender's security interest gives it no relevant interest, so margin loans surface only when a notice mentions them, or through a forced sale by a lender (lender_sale).
- A bank gets a relevant interest, and files its own substantial holder notice, when a physically settled forward or collar gives it a right to acquire, or when the client lends it the shares under a securities loan to support the hedge or as security for the facility (e.g. 'power to control disposal pursuant to stock borrowing and lending activities' where the lender is a director's vehicle). Either is bank_relevant_interest; name the client as the holder.
- A bank's substantial holder notice that aggregates its own trading, prime-brokerage or derivatives book with no named client is not a signal; record it as holder_kind bank_or_broker with no signal.
- Institutional substantial holders annex securities-lending agreements (GMSLA, 'lender can recall') with custodians and fund managers. That is stock lending, not client financing: record the agreement as stock_lending with no signal.
- Company-run loan-funded share plans and limited-recourse plan loans are company_loan_plan, not bank financing.

Rules:
- Extract only what the document says. Use empty strings or null when not stated.
- One DirectorNotice per director; an announcement often bundles several.
- Acquisitions are positive numbers, disposals negative. Derive price from consideration and number when both are given.
- Quote evidence verbatim and keep it short.
- signals is an empty list when nothing evidences financing, hedging, security or lender involvement."""


def filer_name(text):
    m = re.search(r"Details of substantial holder.{0,300}?\n\s*Name\s*\n((?:[^\n]*\n){1,6})", text, re.S | re.I)
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()] if m else []
    return next((l for l in lines if not re.match(r"(ACN|ARSN|ABN|\(|The holder|There was|\d)", l)), "")


def screen(kind, text):
    """Cheap pre-filter: which filings are worth sending to the model when running --flagged."""
    terms = {m.group(0).lower() for m in STRONG_RE.finditer(text)}
    part1 = re.search(r"Details of substantial holder(.{0,1500}?)(voting power|\n\s*2\.)", text, re.S | re.I)
    head = text if kind in DIRECTOR_FORMS else part1.group(1) if part1 else text[:1500]
    bank = any(not CUSTODY_RE.match(head[m.end():m.end() + 40]) for m in BANK_RE.finditer(head))
    if kind in DIRECTOR_FORMS:
        flagged, filer = bank or bool(terms), ""
    else:
        filer = filer_name(text)
        flagged = bank or bool(terms - WEAK) or bool(terms and filer and not INSTITUTION_RE.search(filer))
    return {"filer": filer, "bank": int(bank), "terms": " | ".join(sorted(terms)), "flagged": int(flagged)}


def clip(text, limit=100_000):
    """Head of the text plus windows around financing terms, so a 400-page bank annexure fits."""
    if len(text) <= limit:
        return text
    windows = [text[max(0, m.start() - 600):m.end() + 600] for m in list(STRONG_RE.finditer(text, limit))[:40]]
    return text[:limit] + "\n\n[... truncated; excerpts around financing terms follow ...]\n\n" + "\n---\n".join(windows)


def schema_for(row):
    return DirectorFiling if row["kind"] in DIRECTOR_FORMS else SubstantialHolderFiling


def content(row):
    key, form = row["document_key"], row["kind"]
    prompt = f"ASX code: {row['symbol']}. Announcement type: Appendix {form} lodged {row['date']}." if form in DIRECTOR_FORMS else \
             f"ASX code: {row['symbol']}. Announcement type: Form {form} substantial holder notice lodged {row['date']}."
    if row["scanned"] == "1":
        data = base64.b64encode((DATA / "pdf" / f"{key}.pdf").read_bytes()).decode()
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}, {"type": "text", "text": prompt}]
    return [{"type": "text", "text": f"{prompt}\n\n<filing>\n{clip((DATA / 'text' / f'{key}.txt').read_text())}\n</filing>"}]


def save(row, text):
    parsed = schema_for(row).model_validate_json(text)
    (DATA / "parsed" / f"{row['document_key']}.json").write_text(parsed.model_dump_json(indent=1))


def extract(row, model):
    import anthropic
    r = anthropic.Anthropic().beta.messages.create(
        model=model, max_tokens=16000, system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": schema_for(row).model_json_schema()}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        messages=[{"role": "user", "content": content(row)}])
    if r.stop_reason == "refusal":
        raise RuntimeError(f"refused: {r.stop_details and r.stop_details.category}")
    save(row, next(b.text for b in r.content if b.type == "text"))
    return r.usage


def process(row, model):
    try:
        u = extract(row, model)
        print(f"{row['symbol']:6} {row['kind']:4} {row['date']} ok in={u.input_tokens} out={u.output_tokens}", file=sys.stderr)
    except Exception as e:
        print(f"{row['symbol']:6} {row['kind']:4} {row['date']} error: {str(e)[:300]}", file=sys.stderr)


def batch(rows, model):
    """Same extraction through the Message Batches API at half price; blocks until every batch has ended."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    client = anthropic.Anthropic()
    by_key = {r["document_key"]: r for r in rows}
    reqs = [Request(custom_id=k, params=MessageCreateParamsNonStreaming(
        model=model, max_tokens=16000, system=SYSTEM, messages=[{"role": "user", "content": content(r)}],
        output_config={"format": {"type": "json_schema", "schema": schema_for(r).model_json_schema()}})) for k, r in by_key.items()]
    ids = [client.messages.batches.create(requests=reqs[i:i + 5000]).id for i in range(0, len(reqs), 5000)]
    (DATA / "batches.json").write_text(json.dumps(ids))
    print(f"submitted {len(reqs)} requests in {len(ids)} batch(es): {ids}", file=sys.stderr)
    collect(ids)


def collect(ids):
    import anthropic
    client = anthropic.Anthropic()
    rows = {r["document_key"]: r for r in csv.DictReader(open(DATA / "filings.csv"))}
    for bid in ids:
        while (b := client.messages.batches.retrieve(bid)).processing_status != "ended":
            print(f"{bid}: {b.request_counts.processing} processing, {b.request_counts.succeeded} done", file=sys.stderr)
            time.sleep(60)
        for res in client.messages.batches.results(bid):
            if res.result.type != "succeeded":
                print(f"{res.custom_id} {res.result.type}: {getattr(res.result, 'error', '')}", file=sys.stderr)
                continue
            msg = res.result.message
            try:
                if msg.stop_reason == "refusal":
                    raise RuntimeError("refused")
                save(rows[res.custom_id], next(b.text for b in msg.content if b.type == "text"))
            except Exception as e:
                print(f"{res.custom_id} error: {str(e)[:300]}", file=sys.stderr)


def report(rows):
    tables = {k: [] for k in ("events", "changes", "holdings", "contracts", "holders", "agreements")}
    parsed = 0
    for row in rows:
        path = DATA / "parsed" / f"{row['document_key']}.json"
        if not path.exists():
            continue
        parsed += 1
        base = {"symbol": row["symbol"], "filing_date": row["date"], "form": row["kind"], "document_key": row["document_key"]}
        if row["kind"] in DIRECTOR_FORMS:
            for n in DirectorFiling.model_validate_json(path.read_text()).notices:
                d = {**base, "director": n.director}
                tables["changes"] += [{**d, **c.model_dump()} for c in n.changes]
                tables["holdings"] += [{**d, **h.model_dump()} for h in n.holdings]
                tables["contracts"] += [{**d, **c.model_dump()} for c in n.contracts]
                tables["events"] += [{**d, **s.model_dump()} for s in n.signals]
        else:
            f = SubstantialHolderFiling.model_validate_json(path.read_text())
            d = {**base, "holder": f.holder, "holder_kind": f.holder_kind}
            tables["holders"].append({**d, **{k: getattr(f, k) for k in ("date_of_change", "voting_power_before", "voting_power_after", "issued_capital")},
                                      "relevant_interests": " | ".join(f"{r.holder}: {r.nature} ({r.number})" for r in f.relevant_interests)})
            tables["agreements"] += [{**d, **a.model_dump()} for a in f.agreements]
            tables["events"] += [{**base, "director": "", **s.model_dump()} for s in f.signals]
    for name, rows_ in tables.items():
        if rows_:
            with open(DATA / "tables" / f"{name}.csv", "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(k for r in rows_ for k in r)))
                w.writeheader()
                w.writerows(rows_)
    print(f"{parsed} filings parsed -> data/tables/{{{','.join(k for k, v in tables.items() if v)}}}.csv", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--kinds", nargs="*", default=["3X", "3Y", "3Z", "603", "604", "605"])
    p.add_argument("--flagged", action="store_true", help="only extract filings the keyword screen flags (cheap first pass for the label set)")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--batch", action="store_true", help="use the Message Batches API (half price, up to 24h)")
    p.add_argument("--collect", nargs="*", metavar="BATCH_ID", help="collect results of earlier batches (default: ids in data/batches.json)")
    p.add_argument("--force", action="store_true", help="re-extract even if a parsed JSON exists")
    p.add_argument("--limit", type=int, help="stop after this many extractions")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    for d in ("parsed", "tables"):
        (DATA / d).mkdir(exist_ok=True)
    rows = [r for r in csv.DictReader(open(DATA / "filings.csv")) if r["status"] == "ok" and r["kind"] in args.kinds]
    if args.symbols:
        rows = [r for r in rows if r["symbol"] in args.symbols]
    if args.collect is not None:
        collect(args.collect or json.loads((DATA / "batches.json").read_text()))
    elif not args.report_only:
        screened = []
        for r in rows:
            s = screen(r["kind"], (DATA / "text" / f"{r['document_key']}.txt").read_text())
            screened.append({**{k: r[k] for k in ("symbol", "kind", "date", "document_key", "scanned")}, **s})
        with open(DATA / "tables" / "screen.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=screened[0].keys())
            w.writeheader()
            w.writerows(screened)
        flagged = {s["document_key"] for s in screened if s["flagged"]}
        print(f"{len(flagged)}/{len(screened)} filings flagged by the keyword screen -> data/tables/screen.csv", file=sys.stderr)
        todo = [r for r in rows if (args.force or not (DATA / "parsed" / f"{r['document_key']}.json").exists()) and (not args.flagged or r["document_key"] in flagged)]
        todo = todo[:args.limit] if args.limit is not None else todo
        print(f"{len(todo)} filings to extract with {args.model}", file=sys.stderr)
        if args.batch:
            batch(todo, args.model)
        else:
            with ThreadPoolExecutor(args.workers) as ex:
                list(ex.map(lambda r: process(r, args.model), todo))
    report(rows)


if __name__ == "__main__":
    main()
