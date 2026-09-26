# bd-bank_address_scraper

Scrapes branch / sub-branch addresses for the 62 banks in `banks.csv`
(central state banks, private commercial, Islamic, foreign) into per-bank
part files (`output/parts/*.json`) and one merged `output/banks_branches.json`.

## Usage

```powershell
# full refresh of all banks — re-scrape everything, rewrite the final JSON;
# run this whenever branches change, pages move or banks.csv is updated
.venv\Scripts\python bank_branch_scraper.py

# quick partial run: skip banks whose part already says success
.venv\Scripts\python bank_branch_scraper.py --resume

# one bank (substring match), force re-scrape
.venv\Scripts\python bank_branch_scraper.py --bank "Modhumoti" --force

# never touch the network for search; local requests only
.venv\Scripts\python bank_branch_scraper.py --no-firecrawl
```

### Firecrawl (JS / anti-bot sites)

Some locators only render via JS or sit behind bot protection; the scraper
escalates such fetches to [Firecrawl](https://firecrawl.dev). Provide a key
any of three ways:

1. `--api-key fc-XXXX` on the command line
2. `FIRECRAWL_API_KEY` environment variable
3. a `.firecrawl_key` file in the repo root (gitignored) containing just the key

Roughly 1 credit per scraped page; the handful of blocked banks need <50.

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
  "branch": [
    {"Ashuganj Branch": ["Kashem Plaza, Ashuganj Sadar, Brahmanbaria", null, "02-334431574"]},
    {"Islampur Branch": ["New Islampur Road, Dhaka", null, null]}
  ],
  "sub_branch": [
    {"Gulshan Sub Branch": ["The Skymark, 18, Gulshan Avenue, Gulshan 1., Dhaka 1212", null, null]}
  ]
}
```

Each branch / sub-branch entry is a one-key object: the key is the branch
name, the value is `[address, page_url, contact_number]` (`null` when a site
doesn't publish that field). `status` drives `--resume` skipping; failed banks
additionally carry an `error` field, special cases a `note` field (e.g.
Sammilito). Part files additionally carry a leading `_bank` key (the exact
bank name) that is consumed when merging.

## Results (final run)

62 banks in `banks.csv` → 62 keys in `output/banks_branches.json`;
**59 succeeded** with **16,756 branches + 2,793 sub-branches** total
(one-off "merged-constituents" entry included, see below). Details per bank
(status, method, counts, source) live in each `output/parts/*.json` and in the
merged file.

### Special-case: Sammilito Islami Bank PLC

2025 merger of First Security Islami + EXIM + Social Islami (SIBL) + Union +
Global Islami. The bank's own locator (`location.php`) builds its filter form
with JS and returns no server-side list (same shell for every query, no
sitemap). Its part is therefore the **deduplicated union of the five
constituents' own official-site parts** — 2,471 branches + 222 sub-branches —
marked `method: "merged-constituents"` with a `note` field explaining this.
The five constituents also keep their own rows/parts, so their branches appear
under both names during the transition period.

## Known no-data banks (documented in their part files)

| Bank | Reason (all routes exhausted: local fetch, Firecrawl render, seeds, PDFs) |
|---|---|
| Bangladesh Development Bank (BDBL) | bdbl.com.bd is an empty JS SPA (0 branch text server-side, Firecrawl render also empty); site has no sitemap; the underlying widget is the shared bangladesh.gov.bd national office directory which lists only BDBL's head office (portal itself a blocked 612-byte shell); broken SSL adds verify=False retries |
| Southeast Bank | locator ajax endpoint (`location/data-provider-service.php`) sits behind F5 TSPD bot protection (403 for plain requests *and* Firecrawl, even with waitFor); newest branch PDF is a scanned image (no text layer); 2020 PDFs are COVID day-schedules with names only, no addresses |
| Citibank N.A. | no Bangladesh consumer site/branch listing anymore |

Previously blocked banks (Standard Chartered, Ceylon, NBP, SBI, Woori, Bengal,
Shimanto) were all recovered in later runs — Standard Chartered via its public
`all-atms-branches.json` feed seeded in `banks.csv`, the others via CSV URL
seeds and/or the Firecrawl re-render fallback.


