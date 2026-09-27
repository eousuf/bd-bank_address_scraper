# bd-bank_address_scraper

Scrapes branch / sub-branch addresses for the 62 banks in `banks.csv`
(central state banks, private commercial, Islamic, foreign) into per-bank
part files (`output/parts/*.json`) and one merged `output/banks_branches.json`,
then enriches everything offline with Bangladesh Bank's official IDs and
routing numbers.

## Repository layout

| file | role |
|---|---|
| `bank_branch_scraper.py` | the whole pipeline — scraping, BB registry build, part repair (single file by design) |
| `banks.csv` | single source of truth: bank names, site URLs, listing-page seeds |
| `geo_bank.pdf`, `All-Bank-Routing-Number.pdf` | BB registry PDFs (Bank ID / FI Branch ID / BRSTN), downloaded manually in a browser — required by `--build-ids` |
| `.firecrawl_key` | optional Firecrawl API key (gitignored — copy it manually to each machine) |
| `_verify_parts.py` | post-run sanity check: per-bank counts, junk-name scan, empty-address ratio |
| `output/` | `parts/*.json` checkpoints, `banks_branches.json` merged result, `bb_registry.json`, `scrape.log` |

## Usage

```bash
# full refresh of all banks — re-scrape everything, rewrite the final JSON;
# run this whenever branches change, pages move or banks.csv is updated
python3 bank_branch_scraper.py                        # macOS / Linux
.venv\Scripts\python bank_branch_scraper.py           # Windows

# quick partial run: skip banks whose part already says success
python3 bank_branch_scraper.py --resume

# one bank (substring match), force re-scrape
python3 bank_branch_scraper.py --bank "Modhumoti" --force

# first N banks only
python3 bank_branch_scraper.py --limit 2

# local stack only — no Firecrawl escalation at all
python3 bank_branch_scraper.py --no-firecrawl

# offline BB enrichment (no scraping; needs the two registry PDFs)
python3 bank_branch_scraper.py --build-ids            # PDFs -> output/bb_registry.json
python3 bank_branch_scraper.py --upgrade-parts        # repair + enrich parts, re-merge
python3 bank_branch_scraper.py --upgrade-parts --dry-run   # preview, write nothing
```

### Firecrawl (JS / anti-bot sites)

Some locators only render via JS or sit behind bot protection; the scraper
escalates such fetches to [Firecrawl](https://firecrawl.dev). Provide a key
any of three ways:

1. `--api-key fc-XXXX` on the command line
2. `FIRECRAWL_API_KEY` environment variable
3. a `.firecrawl_key` file in the repo root (gitignored) containing just the key

Roughly 1 credit per scraped page; the handful of blocked banks need <50.
Because the key file is gitignored, `git pull` does **not** carry it between
machines — create it manually on every PC you scrape from.

### Optional `banks.csv` columns (highest priority)

`bank_name,bank_url,branch_page_url,sub_branch_page_url`

- `bank_url` (alias `website`) – official site URL; skips search-engine
  discovery entirely (still live-verified; dead hint falls through to normal
  discovery)
- `branch_page_url` / `sub_branch_page_url` – branch / sub-branch listing
  page or PDF URLs; seeded directly, sitemap crawl skipped. Multiple URLs in
  one cell may be `;`-separated; the legacy `branch_pages` column still works.

Rows may leave any column empty; a plain single-column CSV works as before.

## Pipeline per bank

`banks.csv` is the single source of truth — every bank URL and listing-page
seed lives in its columns; the scraper contains no bank-specific data. Moving
a locator or adding a bank is a CSV edit, not a code change.

1. **Find website** – `banks.csv` `bank_url` hint → DuckDuckGo / Firecrawl /
   Bing search → Wikipedia (all live-verified; dead hints fall through)
2. **Find listing pages** – `banks.csv` page seeds → sitemap.xml →
   homepage links, scored by URL keywords (branch/sub-branch weights, ATM
   demotion); microsite scoping keeps e.g. `sc.com/bd/` off global pages
3. **Fetch** – local `requests` (one pooled keep-alive session) first,
   Firecrawl escalation on failure; listing pages get bounded `?page=N`
   auto-pagination (keeps walking while a page contributes new records)
4. **Extract** – embedded JSON → admin-ajax/DataTables feeds → HTML tables
   (incl. headerless "blob" cells and label blocks) → HTML cards → markdown →
   PDF (column-mapped → label blocks → line heuristics)
5. **Dedupe** – exact name+address, then same-name variants (trigram + token
   similarity, abbreviated-address fuzzy-subset match), then same-outlet
   spelling variants (edit distance ≥0.8 or district-suffix prefix; digits
   and branch/sub classification must match)
6. **Write** – checkpoint part + merged final JSON

### Refreshing & rerun safety

A plain rerun is a full refresh — updated branches, moved pages and new
`banks.csv` rows are all picked up automatically. Safety nets:

- a failed re-scrape never overwrites a previously successful part
- the pre-run merged JSON is kept as `output/banks_branches.json.bak`
- parts for banks no longer in `banks.csv` are dropped from the merged output
- `--resume` skips banks whose part already says success (fast partial runs)

## Output format

`output/parts/*.json` and `output/banks_branches.json` share one per-bank schema:

```json
"<Bank Name>": {
  "bank_url": "https://...",
  "method": "html_cards+html_table",
  "status": "success",
  "bank_id": "15",
  "total_branch": 1302,
  "total_sub_branch": 0,
  "branch": [
    {"Ashuganj Branch": ["Kashem Plaza, Ashuganj Sadar, Brahmanbaria", null, "02-334431574", "150001257", "265570198"]}
  ],
  "sub_branch": [
    {"Gulshan Sub Branch": ["The Skymark, 18, Gulshan Avenue, Gulshan 1., Dhaka 1212", null, null, null, null]}
  ]
}
```

### What each field means

| field | meaning | source |
|---|---|---|
| `_bank` | exact bank name as written in `banks.csv` (part files only — dropped when merging) | your CSV |
| `bank_url` | the official website that was actually scraped (after redirects) | scraper |
| `method` | which extractors produced this bank's records, `+`-joined — tells you *how* the data was won | scraper |
| `status` | `success` / `failed`; `--resume` skips banks already marked success | scraper |
| `bank_id` | BB's official **Bank ID** for the whole bank (Sonali = 15, Agrani = 11, City Bank = 44) | `geo_bank.pdf` |
| `total_branch` | number of entries in `branch` — convenience count, always equals `len(branch)` | computed |
| `total_sub_branch` | number of entries in `sub_branch` | computed |
| `branch` | array of full-branch records (below) | scraper + PDFs |
| `sub_branch` | array of sub-branch / উপশাখা records (same shape) | scraper + PDFs |

Each record is a one-key object — the key is the branch name exactly as
published on the bank's own website, and the value is a 5-element list:

| position | field | meaning | source |
|---|---|---|---|
| `[0]` | address | street address of the outlet (`null` if the site publishes none) | bank's website |
| `[1]` | page_url | listing/detail page the record was taken from | bank's website |
| `[2]` | phone | contact number | bank's website |
| `[3]` | fi_branch_id | BB's unique **FI Branch ID** — note it begins with the bank id (City Bank Agrabad = `440001`) | `geo_bank.pdf` |
| `[4]` | routing_no | 9-digit **BRSTN routing number** used for inter-bank transfers | `All-Bank-Routing-Number.pdf` |

Any element is `null` when the source doesn't publish it or the branch name
didn't confidently match a PDF row — nothing is ever guessed. Failed banks
additionally carry an `error` field.

## Bangladesh Bank IDs & routing numbers (offline enrichment)

Bangladesh Bank (BB) publishes two official PDFs listing every scheduled
bank branch in the country. They are the source of `bank_id`,
`fi_branch_id` and `routing_no`:

| PDF | what it gives |
|---|---|
| `geo_bank.pdf` (bb.org.bd/econdata/bs/geo_bank.pdf, ~156 pages) | Bank ID per bank + a unique FI Branch ID per branch (+ division/district/thana) |
| `All-Bank-Routing-Number.pdf` (~250 pages) | the 9-digit BRSTN routing number of every branch |

BB's website shows a CAPTCHA to programs, so these two files must be
downloaded **manually in a browser** (once) and saved next to this script.

### The two commands, in order

```bash
python3 bank_branch_scraper.py --build-ids       # step 1: PDFs -> output/bb_registry.json
python3 bank_branch_scraper.py --upgrade-parts   # step 2: attach IDs to parts + repairs + re-merge
```

1. **`--build-ids`** reads both PDFs and merges them into one lookup file,
   `output/bb_registry.json` — think of it as a phone book:
   *bank name → Bank ID* and *bank + branch name → FI Branch ID + routing no*.
2. **`--upgrade-parts`** walks every part file, looks each branch up in that
   phone book and writes the IDs into the records (it also performs the data
   repairs listed above and re-merges `banks_branches.json`).
   `--upgrade-parts --dry-run` previews the changes without writing.

### How a scraped branch finds its IDs (name matching)

Your scraped names look like `"AGRABAD BRANCH"`; BB's PDF just says
`"AGRABAD"`. Both sides are cleaned first (lower-cased; punctuation and
words like "Branch", "PLC", "Sub Branch", "Uposhakha" removed), then
compared in three rounds: **exact** → **containment** ("agrabad" inside
"agrabad chattogram") → **near-spelling**. Banks whose short names differ
from BB's long names (EXIM ↔ "Export Import Bank", HSBC, NCC) are handled
by a small alias list. **No match → the field stays `null`; nothing is
guessed.** That is why 8,711 of ~14,000 records carry an FI Branch ID and
6,115 a routing number — the rest are spellings the matcher cannot safely
pair with a PDF row.

### Keeping IDs fresh / turning enrichment off

* **BB publishes updated PDFs later?** Download the new ones in a browser,
  overwrite the two files, re-run `--build-ids` + `--upgrade-parts`. The
  phone book is rebuilt from zero every time (nothing stale survives), and
  re-running is always safe.
* **Normal scraping runs** automatically use the existing registry, so
  freshly scraped banks get their IDs at write time — no extra step.
* **Don't want enrichment anymore?** Delete `output/bb_registry.json`:
  future runs simply write `null` for the three fields. One caution —
  running `--upgrade-parts` *while no registry exists* would erase existing
  IDs too, because it re-computes every value.

## Current results (2026-09-27, after BB enrichment)

62 banks in `banks.csv` → 62 keys in `output/banks_branches.json`;
**60 succeeded, 2 failed** (BDBL, Citibank — see gaps below);
**11,136 branches + 2,885 sub-branches**; 8,711 records carry an FI Branch
ID and 6,115 a routing number (name-based matching; the rest keep `null`).

The `method` field records how each bank's data was won: 14 banks through
HTML cards + tables, 10 through plain HTML tables, 8 through card layouts
alone, and the rest needed special tricks — embedded JSON feeds, replaying
the page's own jQuery/CSRF ajax calls, DataTables server-side endpoints,
ASP.NET postbacks, F5-protected feeds, PDFs and markdown renders. In other
words: every extraction strategy in the scraper is actually used by at
least one bank.

### Data repairs applied by `--upgrade-parts`

* **Sub-branch marker tightened:** `Sub Branch`, `Sub-b`, `Sub office`, the
  BCBL "SUB BANCH" typo, `Uposhakha`/`উপশাখা` (all spellings) all match —
  but words that merely *start* like "sub…" no longer do. This fixed
  records such as Agrani's "Subornochor branch" that an older loose pattern
  had misfiled as sub-offices (7 records moved back to `branch`).
* **143 unmarked sub-branches kept:** records whose names carry no marker
  but were typed as sub via their listing page or a type column stay in
  `sub_branch` — the original typing is trusted.
* **8 junk rows dropped:** Sonali's "LIST OF DOMESTIC BRANCHES…" and
  "List of SWIFT Branch/Office" table titles, Sammilito's "No Branch
  Available", UCB's "Online Branch Visit Appointment", City Bank's
  "Search your nearest…", two Woori address-blobs, one fused Bengal
  Commercial record. Dhaka Bank's real "Banani Road No 11 Branch" was
  explicitly protected from the address-fragment filter.

### Special case: Sammilito Islami Bank PLC

2025 merger of First Security Islami + EXIM + Social Islami (SIBL) + Union +
Global Islami. Its own locator (`location.php`) is a JS shell with no
server-side list. An earlier plan built its part as the deduplicated union
of the five constituents' parts — **but that merged part was never
committed**; the pushed history contains only a junk "No Branch Available"
row (verify: `git show HEAD:output/parts/sammilito_islami_bank_plc.json`).
Current state after cleanup: 0 records. The five constituents keep their
own full parts, so those outlets exist under their own names. TODO: recover
the merged part from the home-PC working tree (or rebuild it from the
constituent parts) and commit it.

## Known gaps & no-data banks

| Bank | Status | Reason / next step |
|---|---|---|
| Bangladesh Development Bank (BDBL) | 0 records | bdbl.com.bd is an empty JS SPA (0 branch text server-side, Firecrawl render also empty); no sitemap; the underlying widget is the shared bangladesh.gov.bd national office directory which lists only the head office; broken SSL adds verify=False retries |
| Citibank N.A. | 0 records | no Bangladesh consumer site/branch listing anymore (BB's registry knows 4 offices) |
| Sammilito Islami Bank PLC | 0 records | constituent merge pending — see above |
| IFIC Bank | 20 records ⚠️ | expected 200+; only `html_table` won 20 rows — their locator needs investigation |
| Sonali, Janata, BKB, RAKUB, Bank Asia, UCB, NCC, Meghna, Probashi Kallyan, Southeast, SC, Woori, ICB Islamic | 0 sub-branches | their scraped listings simply contain no sub-branch/Uposhakha rows — a coverage gap (sub-branch listing pages need seeding), not a classification bug |

Recovered after earlier blocks: Southeast Bank (131 branches via
`pdf+validator_key_ajax` — its ajax endpoint sits behind F5 TSPD but the
validator-key replay passes), Standard Chartered (public
`all-atms-branches.json` feed seeded in `banks.csv`), Ceylon, NBP, SBI,
Woori, Bengal, Shimanto (CSV seeds and/or Firecrawl re-render).

## Ideas / next steps

* re-apply the Sammilito constituent merge and commit it
* investigate the IFIC undercount; seed sub-branch pages for the state banks
* district/division normalization from address text (Cumilla/Comilla,
  Chattogram/Chittagong spellings), optional geocoding
* regression tests for the extractors (pure html→records functions) and
  the BB registry matcher; per-run diff report (outlets added/removed)


