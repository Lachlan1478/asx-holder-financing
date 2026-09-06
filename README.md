# ASX holder financing

Finds ASX directors and substantial holders who have financed or hedged their shareholding with a bank (collars, prepaid forwards, OTC options, margin loans, loans secured over shares), and ranks which director-company pairs are likely to do so next. Everything comes from public ASX filings; market data and trading-policy screening are deliberately out of scope here (see `../asx-trading-policies` for the policy side).

## Where the label comes from

| Source | What it shows | Completeness |
|---|---|---|
| Appendix 3Y, Part 1 | A collar or forward disclosed as a "change in form of relevant interest", with the bank named in the nature of change; transfers of shares into a bank nominee as security; sales by a margin lender | Near-complete for directors' derivatives (s205G Corporations Act); partial for loans |
| Appendix 3X/3Y, interests in contracts | Options, forwards and collars as contracts over the shares | Near-complete for directors |
| Form 604 lodged by the holder | Annexes the collar or facility documents when the holder is substantial | Only holders above 5% |
| Form 603/604 lodged by a bank | A physically settled forward or collar gives the bank a relevant interest, so it files with the ISDA confirmation annexed | Only above 5%; margin loans never appear (s609(1) money-lender carve-out) |

Not covered by code: PPSR searches against the holder's Pty Ltd trustee (a bank as secured party is a strong label; company grantor searches are open, individual ones are restricted) and press coverage.

## Data points extracted from each filing

`schema.py` is the source of truth; this is the same content as a checklist. Every filing becomes one JSON in `data/parsed/`, and every field below is a column in the tables under `data/tables/`.

### Appendix 3X / 3Y / 3Z (director's interest notice) -> `DirectorFiling`

One `DirectorNotice` per director, because an announcement often bundles several.

| Data point | Where it is in the form | Schema field | Why it matters |
|---|---|---|---|
| Director name | Header, "Name of Director" | `director` | Key for the panel; matched across companies to count boards |
| Form type | Header | `form` | 3X = joined the board, 3Z = left; bounds a director's active window |
| Date of last notice | Header | `date_of_last_notice` | Gap-checking the filing history |
| Holding after the change, per class and per vehicle | Part 1, "No. of securities held after change" and "Nature of indirect interest (including registered holder)" | `holdings[]`: `security_class`, `number_before`, `number_after`, `direct_or_indirect`, `registered_holder`, `vehicle` | Size of the stake, split between shares and incentives, and whether it sits in a Pty Ltd or trust (pledgeable), super (not pledgeable) or a nominee |
| Each change | Part 1: "Date of change", "Number acquired / disposed", "Value/Consideration", "Nature of change" | `changes[]`: `date`, `category`, `number` (signed), `consideration`, `price`, `nature_of_change` (verbatim), `registered_holder`, `security_class` | Buy/sell/vesting/participation behaviour, the last disclosed price used to value holdings, cost base, and the text where a collar or nominee transfer is described |
| Change category | Derived from the nature of change | `category` | One of: on/off-market buy or sell, transfer with no change in beneficial interest, issue or allotment, vesting, exercise, lapse, DRP, entitlement or placement, buyback, transfer to nominee or lender, financing arrangement, lender sale, gift or estate, other |
| Interests in contracts | Part 2 of a 3X or 3Y: "Detail of contract", "Nature of interest", "Interest after change" | `contracts[]`: `description`, `counterparty`, `instrument`, `number_before`, `number_after` | Where options, forwards and collars appear when disclosed as contracts under s205G; also incentive plan rights |
| Financing signals | Anywhere in the notice | `signals[]` (see below) | The label |

Not extracted: Part 3 closed-period answers and the ABN header.

### Form 603 / 604 / 605 (substantial holder notice) -> `SubstantialHolderFiling`

| Data point | Where it is in the form | Schema field | Why it matters |
|---|---|---|---|
| Form type | Header | `form` | 603 became, 604 changed, 605 ceased |
| Substantial holder | Section 1, "Name" | `holder`, `holder_kind` | Whether the filer is a bank or broker, a fund manager, a private vehicle or individual, a listed corporate, or government |
| Date of change | Section 1 | `date_of_change` | Event timing |
| Voting power before and after | Section 2 | `voting_power_before`, `voting_power_after`, `issued_capital` | Stake as a percentage; issued capital is stated as the basis and turns any director's share count into a percentage |
| Relevant interests | Sections 3 and 4: holder, nature of relevant interest, registered holder, number | `relevant_interests[]` | Distinguishes ordinary holding, power to dispose under a security deed, right to acquire under a forward, and stock lending |
| Annexed agreements | Annexures: ISDA confirmations, facility agreements, security deeds, securities lending agreements | `agreements[]`: `parties`, `instrument`, `description`, `securities`, `settlement` | A bank's 603 with a physically settled forward or collar annexed names the client counterparty; stock-lending annexures are recorded as `stock_lending` and ignored |
| Financing signals | Anywhere in the notice | `signals[]` (see below) | The label |

### Financing signals -> `Signal` (both filing types)

| Field | Values and meaning |
|---|---|
| `kind` | `collar`, `forward`, `otc_option`, `equity_swap`, `margin_loan`, `secured_loan`, `security_transfer` (shares moved to a lender's nominee as collateral), `lender_sale` (forced sale by a lender), `bank_relevant_interest` (a bank files its own 603/604 because of a physically settled derivative with a client), `company_loan_plan` (company-run loan-funded plan, not bank financing), `other` |
| `holder` | The director or holding entity whose shares are financed or hedged |
| `counterparty` | The bank or lender as named, else empty |
| `securities`, `security_class` | Number covered, if stated |
| `date` | Date of the arrangement or change |
| `settlement` | `cash`, `physical`, `unknown`, `na` |
| `confidence` | `high` when the notice says it outright, `medium` when implied (e.g. a nominee transfer with no stated reason), `low` otherwise |
| `evidence` | Verbatim quote, at most two sentences |

Only the financing kinds (everything except `company_loan_plan` and `other`) at `high` or `medium` confidence are used as labels by `rank.py`. Custody by super-fund, wrap-platform or family-trust nominees is recorded as `vehicle = nominee_or_custodian` with no signal.

## Pipeline

1. `scrape.py` ranks companies by market cap from the ASX directory, lists every announcement ASX types under *security holder details* (3X/3Y/3Z director notices and 603/604/605 substantial holder notices), downloads the PDFs and extracts text. Writes `data/companies.csv`, `data/filings.csv`, `data/pdf/`, `data/text/`. Resumable; re-runs only add.
2. `extract.py` runs a keyword screen over every filing (`data/tables/screen.csv`), then sends filings to Claude with the schema above and writes one JSON per filing to `data/parsed/`. From those it builds:

   | Table | One row per |
   |---|---|
   | `events.csv` | Financing signal |
   | `changes.csv` | Change in a director's interest |
   | `holdings.csv` | Director holding after a notice |
   | `contracts.csv` | Director interest in a contract |
   | `holders.csv` / `agreements.csv` | Substantial holder notice and any annexed agreement |

3. `rank.py` builds a director x company panel at quarterly cutoffs from the tables above, labels each row with whether a financing event follows within twelve months, fits a regularised logistic model on a time-based split, reports AUC, average precision and precision@k, and scores every active director today into `data/tables/leads.csv`. Features are filing-derived only: holding size and value (at the last price disclosed in any director trade), run-up, incentive share, holding vehicle, buy/sell/vesting/participation counts, tenure, number of boards, rank within the company, prior and peer financing events.

## Usage

```bash
uv sync
uv run python scrape.py -n 300 --since 2015-01-01        # ~150 filings per company per five years
export ANTHROPIC_API_KEY=...
uv run python extract.py --flagged --batch                 # label set first: only screened filings, half price via batches
uv run python extract.py --kinds 3X 3Y 3Z --batch          # then every director notice, for the ranker's features
uv run python extract.py --report-only                     # rebuild tables from existing JSON
uv run python rank.py                                      # metrics -> data/tables/model.txt, leads -> data/tables/leads.csv
```

`extract.py --collect` resumes collecting a batch whose ids are in `data/batches.json`. `--symbols`, `--kinds`, `--limit`, `--force` and `--model` narrow or override a run. Scanned PDFs are sent to the model as documents; long bank annexures are clipped to their head plus excerpts around financing terms.

## Cost and scale

Three companies over four and a half years produced 438 filings, so 300 companies over ten years is on the order of 90,000. The keyword screen flags roughly a quarter of substantial holder notices and a few percent of director notices, so the label pass is cheap. The full director-notice pass drives the ranker and is the expensive step; each 3Y is a few thousand tokens, so budget accordingly and use `--batch`.

## Caveats

- The label is positive-unlabelled: undisclosed loans are counted as negatives. Treat scores as lead-ranking, not calibrated probabilities.
- Bank 603/604s on large caps are mostly the bank's aggregated trading book; the prompt tells the model to ignore those unless a client counterparty is named.
- Custodian and platform nominees (super wraps, family-trust custody) are ordinary custody; only a nominee transfer tied to a loan or security counts as a signal.
