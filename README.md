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

### Working on two machines (e.g. Windows at home + macOS at the office)

Everything the pipeline needs travels with the repo — both registry PDFs,
`output/bb_registry.json`, all part files. The only rule that matters:
**`git pull` before you start working** (especially before a multi-hour
full refresh, which rewrites many part files) **and push when you stop**,
one machine at a time. If a push is rejected, the other machine simply
pushed first — `git pull`, then push again. Beyond the `.firecrawl_key`
file noted above, the only per-machine difference is the interpreter:
`python3 bank_branch_scraper.py` on macOS vs
`.venv\Scripts\python bank_branch_scraper.py` on Windows.

### Optional `banks.csv` columns (highest priority)

`bank_name,bank_url,branch_page_url,sub_branch_page_url,constituents,registry`

- `bank_url` (alias `website`) – official site URL; skips search-engine
  discovery entirely (still live-verified; dead hint falls through to normal
  discovery)
- `branch_page_url` / `sub_branch_page_url` – branch / sub-branch listing
  page or PDF URLs; seeded directly, sitemap crawl skipped. Multiple URLs in
  one cell may be `;`-separated; the legacy `branch_pages` column still works.
- `constituents` – `;`-separated bank names this row is a merger of. The part
  is then built **offline** as the deduplicated union of those parts
  (`method: "merged-constituents"`) — never scraped, refreshed automatically
  on every run. Used by Sammilito Islami Bank (see special case below).
- `registry` – `yes` for banks with no scrapeable website at all: the part is
  built **offline** from BB's own registry PDFs (`method: "bb_registry"`),
  with district as the address and FI Branch IDs attached. Used by Citibank
  N.A. and BDBL (see special case below).

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
4. **Extract** – embedded JSON → admin-ajax/DataTables feeds → Bootstrap
   modal detail blocks → HTML tables (the real header row is hunted past
   junk pre-rows; incl. headerless "blob" cells and label blocks) → HTML
   cards → markdown → PDF (column-mapped → label blocks → line heuristics)
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
| `bank_id` | BB's official **Bank ID** for the whole bank (Sonali = 15, Agrani = 11, City Bank = 44); `null` for merged-bank rows BB has not registered yet (Sammilito) | `geo_bank.pdf` |
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
guessed.** That is why 10,232 of ~15,600 records carry an FI Branch ID and
9,678 a routing number — routings also arrive straight from the banks' own
listing pages ("Routing No" columns/cards), which is why they grew faster
than FI IDs; the rest are spellings the matcher cannot safely pair.

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

## Current results (2026-09-29, after extractor hardening)

62 banks in `banks.csv` → 62 keys in `output/banks_branches.json`;
**62 succeeded, 0 failed**;
**12,453 branches + 3,184 sub-branches** (Sammilito's merged part includes
its five constituents' outlets, which also keep their own parts); 10,232
records carry an FI Branch ID and 9,678 a routing number (BB-registry
matching plus routing numbers captured from the banks' own pages; the rest
keep `null`).

The `method` field records how each bank's data was won: 14 banks through
HTML cards + tables, 10 through plain HTML tables, 8 through card layouts
alone, and the rest needed special tricks — embedded JSON feeds, replaying
the page's own jQuery/CSRF ajax calls, DataTables server-side endpoints,
ASP.NET postbacks, F5-protected feeds, PDFs and markdown renders, plus two
banks (Citibank, BDBL) built fully offline from BB's own registry PDFs. In
other words: every extraction strategy in the scraper is actually used by
at least one bank.

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
* **Media-URL rows no longer dropped wholesale (2026-09-27):** RAKUB's
  listing links every *real* branch row to a citizen-charter `.jpg`, which
  the old rule would have deleted (384 branches). Now a media URL is
  blanked and the record kept — only rows whose *name* is also junk
  ("Khulna Branch Inaugration Program", AB Bank's "Opening: 30.12.2012"
  promo rows, `…branch-opening` slugs) are dropped (20 rows this pass). Two
  real AB Bank sub-branches whose names merely end in `_Opening (1)`
  (link-text pollution from AB's site) are deliberately kept — cosmetic
  wart, real outlets.
* **Soft-404 junk dropped (2026-09-27, live filters):** combank.net.bd
  serves its error page ("You reached this page…") with HTTP 200 for
  `?page=N`, so auto-pagination walked all 25 probes and each error page
  parsed as one URL-named record — 25 junk rows, plus the site's
  "Branch / Service Centers Network" section heading and the "Direction
  to Branch" map-link duplicate of Agrabad. URL-named records now never
  pass the name filters, and pagination only counts pages that add
  quality-named records, stopping at the first soft-404. Ceylon rebuilt
  live: 38 → **11 branches + 3 subs**.
* **Existing IDs survive re-upgrades:** when the registry cannot re-resolve
  a branch, `--upgrade-parts` keeps the record's existing `fi_branch_id` /
  `routing_no` instead of nulling them — required by merged-constituents
  parts, whose records belong to their original banks in BB's registry.
* **Extractor hardening (2026-09-29):** listing tables whose real header
  sits under junk pre-rows (Uttara's "251 branch's found…" banner) are now
  column-mapped properly — Uttara went 243 → 319 records with real branch
  names and site routing numbers (previously building names posed as
  branch names). A new `modal_tables` extractor reads Bootstrap modal
  detail blocks (NCC's 2026 redesign: outlet name in the modal header,
  Address / Phone / Routing No in the body table) — NCC went 69 → 140
  records including site routing numbers. Floor/building fragments
  ("2nd Floor", "(1st Floor)", "SUVASTU IMAM SQUARE (4th & 5th FLOOR)")
  are dropped as junk names, and same-name merging is guarded: outlets
  sharing a name across districts ("Sadar Branch" everywhere) survive
  unless one copy is an all-caps feed duplicate — fixing a rerun
  regression that silently lost records (SIBL restored to 417; BASIC,
  RAKUB and Rupali losses caught by review and their parts held at the
  previous commit).

### Special case: Sammilito Islami Bank PLC

2025 merger of First Security Islami + EXIM + Social Islami (SIBL) + Union +
Global Islami. Its own locator (`location.php`) is a JS shell with no
server-side list, so the row carries a `constituents` column in
`banks.csv`: the part is built **offline** as the deduplicated union of the
five constituents' parts (`method: "merged-constituents"`, currently
**1,190 branches + 282 sub-branches**), deduping on name+address so
same-named outlets of different banks ("Agrabad Br.") both survive. Every
refresh rebuilds it automatically from the current parts — no scraping, no
credits. Per-record `fi_branch_id` / `routing_no` keep their original-bank
values (882 branches carry an ID). The bank-level `bank_id` is `null`:
BB's registry PDFs still list only the five constituents, and the fuzzy
bank-name matcher is explicitly barred from assigning Sammilito to Islami
Bank Bangladesh's registry key `'islami'` (Bank ID 42).

**When BB eventually registers the merged bank:** download the two new
registry PDFs in a browser, re-run `--build-ids` and `--upgrade-parts` —
nothing else. An exact registry-key match outranks the alias bar, so the
real Bank ID flows in automatically. Only if BB lists the bank under a
name whose normalized key differs from `sammilito islami` do you need to
delete the `"sammilito islami": None` line in `BB_BANK_ALIASES`
(`bank_branch_scraper.py`) by hand.

### Special case: `registry: yes` banks (Citibank N.A., BDBL)

Citi's Bangladesh consumer presence is gone — `asia.citibank.com/bangladesh`
and `citibank.com.bd` are dead domains and citigroup.com has no Bangladesh
page — but BB's `geo_bank.pdf` still lists its **4 offices** (Chittagong,
Gulshan, Head Office, Motijheel). Its `banks.csv` row therefore carries
`registry: yes`: the part is built **offline** from `output/bb_registry.json`
(`method: "bb_registry"`, `bank_id` 26), with the district as the address,
FI Branch IDs attached, and phone/page/routing honestly `null` (BB lists no
routing numbers for Citi). The seeded `bank_url` is the MCCI member page —
the only live web reference left. Every run rebuilds it in milliseconds; if
BB's registry ever drops the bank, the scraper falls back to a normal (and
currently hopeless) site search.

BDBL is the same pattern for the opposite reason: bdbl.com.bd *looks*
empty to a scraper but isn't. Its nav's `ব্রাঞ্চ অফিস` (Branch Offices)
link 404s server-side (an unpublished duplicate page id) and there is no
sitemap — but the legacy `/site/page/a6d8eb47-…` route redirects to the
live Branch Office page (`/pages/static-pages/6922e032…`), which is fully
server-rendered: **50 branch cards** under 6 zonal offices (Dhaka
North/South, Chattogram, Sylhet, Khulna, Rajshahi) whose headings carry
each branch's street address, phones, e-mail and manager as **plain
text** — the per-branch JPGs on that page are just building photos, not
data. (A second legacy page, `/site/page/d9cfd863-…`, lists help-desk
entries — 25–38 rows depending on when you look — whose detail pages
hold only shared boilerplate: a watermark JPG and a citizen-charter
PDF.) The site's 50 branches are a strict subset of BB's registry (52
real branches; Chitalmari and Madaripur are absent from the site list),
so the part stays registry-built — but enriched: the site's 50 branch
cards (street addresses, phones; English translations manual) were
extracted 2026-09-27 and **baked into the scraper** as the
`BDBL_SITE_ADDRESSES` constant — no extra data file. At build time
`_bdbl_site_enrich()` gives the 50 site-listed branches their street
address, site phones and the Branch Office page URL (method
`bb_registry+bdbl_site`); Jashore (no address published) and the
registry-only six keep district-as-address — nothing invented, and a
full rescrape rebuilds the same enriched part offline.
BB's two PDFs mention the bank 77 times and are the only structured
source.
Those rows overlap heavily — the geo PDF carries 53 offices with FI Branch
IDs (29 of which also match a routing row exactly), while the routing PDF
re-spells 21 of the *same* offices ("dhaka south motijheel" vs BB's own
typo "MOTIJHEL", jessore/jashore, bogra/bogura) and adds 3 clearing offices
(Dhaka-South Remittance, Dhaka-South Truncation Point, a generic
"Chittagong"). `registry_part()` folds each re-spelling into its office so
one record carries **both** IDs — **56 offices, 53 FI IDs, 53 distinct
routing numbers**, nothing guessed; the three offices BB lists no routing
row for (Head Office, Madaripur, Agrabad) keep `null`. BB's spellings are
kept verbatim ("Motijhel Branch", "Cox's Bazar Branch").

## Known gaps & no-data banks

| Bank | Status | Reason / next step |
|---|---|---|
| Bangladesh Development Bank (BDBL) | 56 records ✓ | recovered 2026-09-27 via `registry: yes` — the nav's branch link 404s server-side, but the legacy `/site/page/a6d8eb47-…` route reaches the live Branch Office page (50 branch cards, addresses as plain text): street addresses + site phones merged into the part (`bb_registry+bdbl_site`; data baked into the scraper as `BDBL_SITE_ADDRESSES`, no extra file); see special case above |
| Citibank N.A. | 4 records ✓ | recovered 2026-09-27 via `registry: yes` (bb_registry offline part) — no consumer site/branch listing exists anymore; see special case above |
| IFIC Bank | 20 records ⚠️ | expected 200+; only `html_table` won 20 rows — their locator needs investigation |
| BKB, BASIC, RAKUB, Rupali | parts held at 2026-09-27 commit ⚠️ | a 2026-09-29 office re-scrape lost real records (BASIC 14 sub-branches, Rupali 16 outlets, RAKUB 4) to since-fixed merge rules — re-scrape from home and review before accepting newer versions |
| IBBL, NRBC, Shahjalal, FSIBL, Dhaka Bank, Citizens | parts restored ✓ | their feeds served partial data from the office IP on 2026-09-29 (IBBL 888→469, Shahjalal's new interactive locator) — retry from home; a "success" with far fewer records than the previous part is always worth a second look |
| Sonali, Janata, BKB, RAKUB, Bank Asia, UCB, NCC, Meghna, Probashi Kallyan, Southeast, SC, Woori, ICB Islamic | 0 sub-branches | their scraped listings simply contain no sub-branch/Uposhakha rows — a coverage gap (sub-branch listing pages need seeding), not a classification bug |

Recovered after earlier blocks: Southeast Bank (131 branches via
`pdf+validator_key_ajax` — its ajax endpoint sits behind F5 TSPD but the
validator-key replay passes), Standard Chartered (public
`all-atms-branches.json` feed seeded in `banks.csv`), Ceylon, NBP, SBI,
Woori, Bengal, Shimanto (CSV seeds and/or Firecrawl re-render).

## Ideas / next steps

* retry the office-IP-partial banks from home (IBBL, NRBC, FSIBL, Dhaka
  Bank, Citizens) and hunt Shahjalal's new interactive locator endpoint
* re-scrape BKB/BASIC/RAKUB/Rupali from home and review against the held
  parts; fix the Rupali all-caps feed merge edge case
* investigate the IFIC undercount; seed sub-branch pages for the state banks
* district/division normalization from address text (Cumilla/Comilla,
  Chattogram/Chittagong spellings), optional geocoding
* regression tests for the extractors (pure html→records functions) and
  the BB registry matcher; per-run diff report (outlets added/removed)


