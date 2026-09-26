#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bank_branch_scraper.py — Bangladesh bank branch & sub-branch scraper.

banks.csv is the single source of truth — all bank URLs and listing-page seeds
live there; this file contains no bank-specific data. A plain rerun is a full
refresh: updated branches, moved pages and new CSV rows are all picked up.

Pipeline per bank (from banks.csv):
  1. Find the official website   -> banks.csv bank_url hint / DuckDuckGo / Firecrawl / Bing / Wikipedia
  2. Find branch pages           -> banks.csv page seeds -> sitemap.xml / homepage links
  3. Fetch pages                 -> local requests first, Firecrawl escalation (JS & anti-bot)
                                    (+ bounded ?page=N auto-pagination on listing pages)
  4. Extract records             -> embedded JSON -> HTML tables -> HTML cards -> markdown -> PDF
  5. Write JSON                  -> output/parts/<bank>.json checkpoints + merged banks_branches.json

Rerun safety: a failed re-scrape never overwrites a previously successful
part; the pre-run merged JSON is kept as banks_branches.json.bak; parts for
banks no longer in banks.csv are dropped from the merged output.

Usage:
  python bank_branch_scraper.py                          # all banks, full refresh
  python bank_branch_scraper.py --bank "City Bank PLC"   # single bank (substring match)
  python bank_branch_scraper.py --limit 2                # first N banks
  python bank_branch_scraper.py --resume                 # skip banks already scraped OK
  python bank_branch_scraper.py --api-key fc-XXXX        # or FIRECRAWL_API_KEY env var, or .firecrawl_key file
  python bank_branch_scraper.py --no-firecrawl           # local stack only
  banks.csv optional columns (highest priority, spaces in header ok):
    website / bank_url        -> official site URL (skips search discovery)
    branch_pages              -> ';'-separated listing page/PDF URLs
    branch_page_url           -> branch listing page/PDF URL
    sub_branch_page_url       -> sub-branch listing page/PDF URL
"""
import argparse
import csv
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import urllib.parse as up

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4")
try:
    import pdfplumber
except ImportError:
    pdfplumber = None
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "output"
PARTS_DIR = OUT_DIR / "parts"

FC_API = "https://api.firecrawl.dev/v2"
FC = {"enabled": True, "key": ""}
ARGS = None
LOG = logging.getLogger("scraper")
_WIKI_CACHE = None

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}

# one pooled session (HTTP keep-alive) for all local fetches: reuses TCP/TLS
# connections across the hundreds of page requests in a run
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

WIKI_LIST_URL = "https://en.wikipedia.org/wiki/List_of_banks_in_Bangladesh"

SEARCH_BLACKLIST = (
    "wikipedia.org", "wikidata.org", "facebook.com", "linkedin.com", "twitter.com",
    "x.com", "instagram.com", "youtube.com", "google.com", "gmail.com", "medium.com",
    "github.com", "reddit.com", "quora.com", "crunchbase.com", "glassdoor.com",
    "indeed.com", "bdjobs.com", "prothomalo.com", "thedailystar.net", "bdnews24.com",
    "dhakatribune.com", "tbsnews.net", "bb.org.bd", "bangladeshbank.org.bd",
    "banks.com.bd", "bankinfobd.com", "britannica.com", "tripadvisor.com",
    "ambitionbox.com", "tradekey.com", "bing.com", "duckduckgo.com", "yandex",
    "search.yahoo.com", "yahoo.com", "msn.com",
)

# banks.csv is the single source of truth for bank websites and listing-page
# seeds (bank_url / branch_page_url / sub_branch_page_url columns, ';'-separated
# when a bank needs several pages); nothing bank-specific is hardcoded here.
# Banks without a CSV hint fall back to search-engine + Wikipedia discovery
# (see find_website), and paginated card locators are followed automatically
# (see follow_pagination).

STOP_TOKENS = {"the", "and", "of", "for", "plc", "ltd", "limited", "bank", "banks", "bangladesh"}

def setup_logging():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(logging.INFO)
    con.setFormatter(logging.Formatter("%(message)s"))
    fh = logging.FileHandler(OUT_DIR / "scrape.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(con)
    LOG.addHandler(fh)

def clean_ws(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:80] or "bank"

def domain_of(url):
    try:
        net = up.urlsplit(str(url)).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""

def absolutize(href, base):
    if not href:
        return ""
    href = str(href).strip()
    if href.startswith(("javascript:", "tel:", "mailto:", "data:", "#")):
        return ""
    try:
        return up.urljoin(base, href)
    except Exception:
        return ""

def make_soup(html_text):
    try:
        return BeautifulSoup(html_text or "", "lxml")
    except Exception:
        return BeautifulSoup(html_text or "", "html.parser")

def polite_sleep():
    d = 1.2
    if ARGS is not None:
        d = max(0.2, ARGS.delay)
    time.sleep(d * (0.7 + random.random() * 0.6))

PHONE_RE = re.compile(r"(?<!\d)(?:\+?880|0)(?:[\s\-().]?\d){8,12}(?!\d)")

def find_phones(text):
    out = []
    for m in PHONE_RE.finditer(str(text or "")):
        raw = m.group(0).strip(" -.,;")
        digits = re.sub(r"\D", "", raw)
        if 9 <= len(digits) <= 14:
            out.append(clean_ws(raw))
    return out

SUB_RE = re.compile(r"sub[\s._-]*(?:br|b|o)|up[oa][\s._-]*sha[\s._-]*kha|উপ\s*শাখা", re.I)  # sub branch /
# sub-branch / "SUB BANCH" (BCBL typo) / sub office / "Uposhakha" — the
# romanized উপশাখা used as the outlet-name suffix (Global Islami, IFIC)
# an outlet's OWN name ("ASAMPARA SUB BRANCH") as opposed to a bare
# "Sub Branch" type-label line riding along in card text (ONE Bank) —
# there must be a name prefix before the sub marker
SUB_NAME_LINE_RE = re.compile(r"^\S[^,;|:]{2,}?\s+sub[\s._-]*(?:br|b|o)", re.I)
BRANCH_NAME_RE = re.compile(r"branch|শাখা", re.I)
# ATM/CDM/CRM booths share tables with branches ("X Branch ATM",
# "Sub Branch ATM Unit-1") — never a branch/sub-branch itself.
# "…(Adjacent to Branch Premises)…" rows are the ATM-booth listing beside
# the branch (IBBL), so "adjacent to branch" marks a booth too.
ATM_NAME_RE = re.compile(
    r"(?<![a-z0-9])(atm|cdm|crm|booth|এটিএম|বুথ|"
    r"adjacent\s+to\s+(?:the\s+)?branch)(?![a-z0-9])", re.I)
# press releases leak into results as pseudo-branches ("AB Bank opened its
# 101st Branch at Jhenaidah") — past-tense news verbs never name a branch.
# Auction/tender notices name customers, not branches (SIBL /media#auction).
NEWS_NAME_RE = re.compile(
    r"\b(opened|opens|inaugurat\w+|relocat\w+|launched|launches|"
    r"published\s+(?:in|on)|auction|tender|"
    # holiday-schedule notice headlines (SEB): "LIST OF THE BRANCHES AND
    # UPOSHAKHAS** **SHALL REMAIN OPEN FROM May 17, 2020"
    r"remain(?:s|ing|ed)?\s+open|list\s+of\s+(?:the\s+)?branch(?:es)?)\b", re.I)
# rowspan layouts & card labels leak header/label junk as "names"
GENERIC_NAME_RE = re.compile(
    r"^(branch|branches|branch\s*name|name\s*\(en\)|branch\s*(code|no|manager)|"
    r"branches?\s*(&|and)\s*offices?|sub[\s_-]*branch(es)?(\s*name)?|"
    r"(visit|find|locate)[\s-]*a?[\s-]*branch(es)?|our\s+branch(es)?|"
    r"manager|officer|address|phone|mobile|email|sl|serial|no|name|district|"
    r"division|routing|code|type|details|view|status|total|"
    # detail-table field labels leak as names (Midland "Branch Address:",
    # ONE Bank "OBPLC Branches:", "Branch Locations")
    r"branch\s+(details|address|e-?mail|pabx|fax|phone|mobile|contact|zone|"
    r"location|swift|routing|service|bank(?:ing)?|network|info(?:rmation)?|list)|"
    r"(customer\s+)?service\s+manager|obplc\s+branches|branch\s+locations|"
    r"routing\s*no|transaction\s+hours?|swift\s*(code)?|pbx|fax|hotline|"
    # batch4 leaks: locator headings, breadcrumbs, rowspan field labels,
    # notice-board blurbs (IBBL) — never real branch names
    r"fax\b.*|phone\s*ext\.?:?|locker\b.*|islamic\s+window|opening\s+date.*|"
    r"controlling\s+branch|home\s*[»>].*|find\s+branch(?:es)?\b.*|"
    r"branch(?:es)?\s*,?\s*(?:&|and|,)\s*atms?\b.*|"
    r".*branches?\s+are\s+on-?line.*|services\s+available.*|"
    r"m/s[\s.,].*|.*\bproprietor\b.*|total\s+branch(?:es)?\s+list\b.*)$", re.I)
BENGALI_RE = re.compile(r"[\u0980-\u09ff]")
# address fragments captured as names (Uttara "Holding No.241", EXIM
# "Ranu Plaza Holding No.136" premises cells) — no branch name ever contains
# these tokens anywhere in the string
ADDR_NAME_RE = re.compile(
    r"\bholding\s*(no|nr|[:#])|\bplot\s*(no|nr|#)|\bhouse\s*(no|#)|"
    r"\bshop\s*(no|#)|\bward\s*(no|#)|\bpost(?:al)?\s*code|"
    r"\b(?:level|floor|flat|suite|room|road)\s*(?:no|#)?\s*\d", re.I)
# promo/campaign content riding branch arrays (BRAC "Holiday Inn Offer…",
# HSBC "…Offers Convenient…") and section headings ("Branch List Dhaka")
PROMO_NAME_RE = re.compile(
    r"\b(offers?|campaign|fest(?:ival)?|discount|promo(?:tion)?s?|giveaway|"
    r"branch\s*list)\b", re.I)
# card/table labels captured as an address value ("District", "Area") are junk;
# bare division names / routing digits are table fields, not street addresses
GENERIC_ADDR_RE = re.compile(
    r"^(district|division|area|region|zone|city|town|address|location|"
    r"dhaka|chattogram|sylhet|khulna|rajshahi|barishal|rangpur|mymensingh|"
    r"rural|urban)$", re.I)

def classify_sub(name="", type_val="", page_sub=False):
    t = f"{type_val or ''} {name or ''}".lower()
    if SUB_RE.search(t):
        return True
    if re.search(r"\bbranch\b", t):
        return False
    return bool(page_sub)

ADDR_KWS = ("road", "avenue", "block", "sector", "area", "sadar", "district", "post office",
            "p.o", "p. o", "pobox", "p.o.box", "floor", "level", "tower", "building", "complex",
            "c/a", "commercial area", "plot", "lane", "street", "thana", "upazila", "bazar",
            "market", "junction", "circle", "nagar", "para", "bridge", "airport", "college",
            "university", "school", "hospital", "dhaka", "chattogram", "chittagong", "khulna",
            "rajshahi", "sylhet", "barishal", "rangpur", "mymensingh", "bogura", "comilla",
            "cumilla", "jessore", "rangamati", "ঢাকা", "রোড", "এলাকা", "থানা", "জেলা")

def bank_tokens(name):
    s = name.lower()
    toks = [t for t in re.findall(r"[a-z0-9]{3,}", s) if t not in STOP_TOKENS]
    caps = [w.lower() for w in re.findall(r"\b[A-Z]{2,5}(?=[\s(])", name)
            if w.lower() not in STOP_TOKENS]
    parens = re.findall(r"\(([a-z0-9&.\-]{2,8})\)", s)
    for t in caps + parens:
        t = re.sub(r"[^a-z0-9]", "", t)
        if len(t) >= 2 and t not in toks:
            toks.append(t)
    return toks or [re.sub(r"[^a-z0-9]", "", s)[:6]]

def domain_score(dom, toks):
    if not dom or not toks:
        return 0.0
    toks = [t for t in toks if len(t) >= 2]
    if not toks:
        return 0.0
    base = dom.split(".")[0].replace("-", "").replace(".", "")
    matched = sum(1 for t in toks if t in base)
    score = matched / len(toks)
    if dom.endswith(".bd"):
        score += 0.1
    return min(score, 1.0)

def pick_official_site(bank, results):
    toks = bank_tokens(bank)
    best_url, best_sc = None, 0.0
    for r in results or []:
        url = (r or {}).get("url") or ""
        if not url.lower().startswith("http"):
            continue
        dom = domain_of(url)
        if not dom or any(b in dom for b in SEARCH_BLACKLIST):
            continue
        sc = domain_score(dom, toks)
        if sc > best_sc:
            best_url, best_sc = url, sc
    return best_url, best_sc

def http_get(url, timeout=30, retries=2):
    last = None
    for i in range(retries + 1):
        verify = i < retries  # last attempt tolerates broken SSL certs (common on .bd sites)
        try:
            return SESSION.get(url, timeout=timeout,
                               verify=verify, allow_redirects=True)
        except requests.exceptions.SSLError:
            LOG.debug("ssl error (verify=%s): %s", verify, url)
        except requests.exceptions.RequestException as e:
            last = e
            LOG.debug("http error: %s -> %s", url, e)
        time.sleep(1.5 * (i + 1))
    LOG.debug("giving up on %s (%s)", url, last)
    return None

def fc_call(path, payload, timeout=90):
    """POST to Firecrawl v2 REST API. Returns parsed data or None. Disables FC on 402/403."""
    if not FC["enabled"]:
        return None
    headers = {"Content-Type": "application/json"}
    if FC["key"]:
        headers["Authorization"] = "Bearer " + FC["key"]
    for _attempt in (1, 2):
        try:
            r = requests.post(f"{FC_API}/{path}", json=payload, headers=headers, timeout=timeout)
        except Exception as e:
            LOG.debug("firecrawl %s error: %s", path, e)
            return None
        if r.status_code == 200:
            try:
                j = r.json()
            except ValueError:
                return None
            if j.get("success", True):
                return j.get("data", j)
            LOG.debug("firecrawl %s unsuccessful: %.200s", path, str(j))
            return None
        if r.status_code == 429:
            wait = 8 if not FC["key"] else 20
            LOG.info("firecrawl rate-limited; waiting %ss", wait)
            time.sleep(wait)
            continue
        if r.status_code in (402, 403):
            LOG.warning("firecrawl disabled (HTTP %s): %.160s", r.status_code, r.text)
            FC["enabled"] = False
            return None
        if 500 <= r.status_code < 600 and _attempt == 1:
            LOG.debug("firecrawl %s -> HTTP %s (retrying once)", path, r.status_code)
            time.sleep(5)
            continue
        LOG.debug("firecrawl %s -> HTTP %s", path, r.status_code)
        return None
    return None

def fc_scrape(url):
    d = fc_call("scrape", {"url": url, "formats": ["markdown", "html"]})
    if isinstance(d, dict) and (d.get("markdown") or d.get("html")):
        return {"markdown": d.get("markdown") or "", "html": d.get("html") or ""}
    return None

def fc_search(query, limit=6):
    d = fc_call("search", {"query": query, "limit": limit})
    out = []
    if isinstance(d, list):
        for r in d:
            if isinstance(r, dict) and r.get("url"):
                out.append({"url": r["url"], "title": r.get("title", "")})
            elif isinstance(r, str):
                out.append({"url": r, "title": ""})
    return out

def fc_map(site, search_term, limit=150):
    d = fc_call("map", {"url": site, "search": search_term, "limit": limit})
    links = []
    if isinstance(d, dict):
        links = d.get("links") or (d.get("data") or {}).get("links") or []
    elif isinstance(d, list):
        links = d
    out = []
    for l in links:
        u = l.get("url") if isinstance(l, dict) else str(l)
        if u:
            out.append(u)
    return out

def ddg_search(query, n=10):
    try:
        from ddgs import DDGS
    except ImportError:
        LOG.debug("ddgs not installed; skipping duckduckgo search")
        return []
    try:
        with DDGS(timeout=20) as dd:
            raw = list(dd.text(query, max_results=n))
    except Exception as e:
        LOG.debug("ddg search failed: %s", e)
        return []
    out = []
    for r in raw:
        u = r.get("href") or r.get("url") or r.get("link") or ""
        if u:
            out.append({"url": u, "title": r.get("title", "")})
    return out

def bing_search(query, n=10):
    url = "https://www.bing.com/search?q=" + up.quote_plus(query) + f"&count={n}"
    r = http_get(url, timeout=20, retries=1)
    if r is None or r.status_code != 200:
        return []
    out = []
    for li in make_soup(r.text).select("li.b_algo"):
        a = li.find("a", href=True)
        if a and a["href"].startswith("http"):
            out.append({"url": a["href"], "title": a.get_text(" ", strip=True)})
    return out

def wiki_bank_sites():
    global _WIKI_CACHE
    if _WIKI_CACHE is not None:
        return _WIKI_CACHE
    _WIKI_CACHE = {}
    r = http_get(WIKI_LIST_URL, timeout=25, retries=1)
    if r is not None and r.status_code == 200:
        for row in make_soup(r.text).select("table.wikitable tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            name = clean_ws(cells[0].get_text(" ", strip=True))
            link = ""
            for a in reversed(row.find_all("a", href=True)):
                if a["href"].startswith("http") and "wikipedia" not in a["href"]:
                    link = a["href"]
                    break
            if name and link:
                _WIKI_CACHE[name.lower()] = link
    return _WIKI_CACHE

def wiki_match(bank, mapping):
    bt = set(bank_tokens(bank))
    best, best_sc = None, 0.0
    for name, url in mapping.items():
        nt = set(bank_tokens(name)) or {name.lower()}
        sc = len(bt & nt) / max(1, len(bt))
        if sc > best_sc:
            best, best_sc = url, sc
    return best if best_sc >= 0.6 else None

def find_website(bank):
    """Stage 1: locate the official website. Returns (url, source_description)."""
    cands = []
    hint = clean_ws(CSV_HINTS.get(bank, {}).get("website") or "")
    if hint:
        cands.append((hint, "csv-hint", 1.05))  # user-supplied: try first
    query = f"{bank} Bangladesh official website"
    for source, fn in (("ddg", ddg_search), ("firecrawl", fc_search), ("bing", bing_search)):
        if len(cands) >= 3:
            break
        try:
            results = fn(query)
        except Exception as e:
            LOG.debug("%s failed: %s", source, e)
            results = []
        url, sc = pick_official_site(bank, results)
        if url and sc >= 0.3:
            cands.append((url, f"{source}(score={sc:.2f})", sc))
    if not cands:
        m = wiki_match(bank, wiki_bank_sites())
        if m:
            cands.append((m, "wikipedia", 0.5))
    if not cands:
        return None, "not_found"
    for url, source, _sc in cands:
        r = http_get(url, timeout=20, retries=1)
        if r is not None and r.status_code < 400:
            final = r.url if str(r.url).startswith("http") else url
            fdom = domain_of(final)
            if fdom and not any(b in fdom for b in SEARCH_BLACKLIST):
                return final, source
            LOG.debug("candidate %s redirected to blacklisted %s", url, fdom)
    return cands[0][0], f"{cands[0][1]}|unverified"

URL_KW_WEIGHTS = [
    (r"sub[\s_+~-]*branch|subbranch", 4.0),
    (r"branch", 2.5),
    (r"up[oa][\s_+~-]*sha[\s_+~-]*kha", 4.0),  # romanized উপশাখা ("uposhakha")
    (r"উপশাখা", 4.0),
    (r"শাখা", 2.5),
    (r"network", 1.8),
    (r"location", 1.6),
    (r"address", 1.2),
    # ATM pages are NOT branch pages: demote hard (word-bounded so
    # "/atmaram-branch" style slugs are unaffected; combined
    # "/branch-atm-network" pages still pass via their branch/network score)
    (r"(?<![a-z0-9])atms?(?![a-z0-9])|atm[-_/](booth|locator|list|finder)", -2.5),
    (r"office", 1.0),
]
EXCLUDE_URL_RE = re.compile(
    r"career|vacanc|job|recruit|tender|notice|news|press|report|annual|investor|"
    r"share|loan|deposit|card\w*|smes|faq|gallery|photo|video|event|blog|terms|"
    r"privacy|about|history|board|director|management|profile|financial|statement|"
    r"login|signup|register|"
    # agent-banking outlet listings & notice/auction boards (SIBL
    # /agentoutlets, /home/media#auction) never hold branch rows
    r"agent|outlet|auction|media|"
    r"launch|inaugurat|signing|agreement|starts[-_]operation|formally|ceremon|"
    r"distribut|donat|seminar|confer|training|customer|achiev|award|rating|"
    r"dividend|agm|remitt|cricket|sponsor|csr|camp|festival|offer|scheme", re.I)

NEWS_URL_RE = re.compile(
    r"launch|inaugurat|signing|agreement|starts[-_]operation|formally|ceremon|"
    r"distribut|donat|seminar|confer|training|customer|achiev|award|rating|"
    r"dividend|agm|remitt|cricket|sponsor|csr|camp|festival|offer|scheme|"
    r"celebrat|observ|organiz|visit|published|edition", re.I)
BAD_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|css|js|zip|rar|7z|mp[34]|m4v|mov|avi|"
    r"woff2?|ttf|eot)($|\?)", re.I)

def score_branch_url(url, anchor=""):
    try:
        u = up.unquote(str(url)).lower()
    except Exception:
        u = str(url).lower()
    a = (anchor or "").lower()
    if BAD_EXT_RE.search(u):
        return -1.0
    if (EXCLUDE_URL_RE.search(u) and "branch" not in u
            and "শাখা" not in u and "উপশাখা" not in u and "uposhakha" not in u):
        return -1.0
    s = 0.0
    for pat, w in URL_KW_WEIGHTS:
        if re.search(pat, u):
            s += w
        elif re.search(pat, a):
            s += w * 0.8
    if NEWS_URL_RE.search(u):
        s -= 1.5  # news article that happens to mention a branch
    last_seg = u.rstrip("/").rsplit("/", 1)[-1]
    if len(last_seg) > 40:  # long slugs are usually news articles, not listing pages
        s *= 0.2
    return s

def fetch_sitemap_urls(site, max_urls=3000):
    urls, seen_sm = [], set()

    def parse_sm(sm_url, depth=0):
        if depth > 3 or len(urls) >= max_urls or sm_url in seen_sm:
            return
        seen_sm.add(sm_url)
        r = http_get(sm_url, timeout=25, retries=1)
        if r is None or r.status_code != 200:
            return
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            return
        is_index = root.tag.lower().endswith("sitemapindex")
        for el in root.iter():
            tag = el.tag.lower().split("}")[-1]
            text = (el.text or "").strip()
            if not text.startswith("http"):
                continue
            if is_index and tag == "loc" and text.endswith(".xml"):
                parse_sm(text, depth + 1)
            elif not is_index and tag == "loc":
                urls.append(text)
                if len(urls) >= max_urls:
                    return

    dom = domain_of(site)
    candidates = []
    r = http_get(up.urljoin(site, "/robots.txt"), timeout=15, retries=1)
    if r is not None and r.status_code == 200:
        for line in r.text.splitlines():
            m = re.match(r"\s*sitemap\s*:\s*(\S+)", line, re.I)
            if m:
                candidates.append(m.group(1))
    candidates += [f"https://{dom}/sitemap.xml", f"https://{dom}/sitemap_index.xml",
                   f"https://www.{dom}/sitemap.xml", f"https://{dom}/wp-sitemap.xml",
                   f"https://{dom}/sitemap1.xml"]
    for c in candidates:
        if len(urls) >= max_urls:
            break
        parse_sm(c)
    return urls

def find_branch_pages(site, bank="", skip_sitemap=False):
    """Stage 2: discover branch/location pages. Returns (pages, pdf_candidates)."""
    scored, pdfs = {}, {}
    home_dom = domain_of(site)
    # sister domains (pmis.janatabank-bd.com etc.) carry bank-name tokens
    bank_tokens = {w for w in re.split(r"[^a-z]+", (bank or "").lower())
                   if len(w) > 3 and w not in STOP_TOKENS}
    # country microsites (sc.com/bd/, hbl.com/bangladesh/) live on a GROUP
    # domain: when the site URL has a path, discovery stays inside that
    # subtree so we never wander into other countries' branch pages
    try:
        _pu = up.urlparse(site)
        scope = (f"{_pu.scheme}://{_pu.netloc}{_pu.path.rstrip('/')}"
                 if _pu.path.strip("/") else "")
    except Exception:
        scope = ""

    def add(url, score, is_pdf=False):
        if not url or not url.lower().startswith("http"):
            return
        dom = domain_of(url)
        if (dom != home_dom and not any(t in dom for t in bank_tokens)
                and not any(t in url.lower() for t in bank_tokens)):
            return
        if scope and not (url == scope or url.startswith(scope + "/")):
            return
        store = pdfs if is_pdf else scored
        store[url] = max(store.get(url, 0.0), score)

    sm_urls = []
    if not skip_sitemap:
        sm_urls = fetch_sitemap_urls(site)
        for u in sm_urls:
            s = score_branch_url(u)
            if s > 0:
                add(u, s, is_pdf=u.lower().endswith(".pdf"))
    LOG.info("  sitemap: %d urls scanned -> %d page candidates, %d pdfs",
             len(sm_urls), len(scored), len(pdfs))

    r = http_get(site, timeout=30)
    if r is not None and r.status_code == 200:
        for a in make_soup(r.text).find_all("a", href=True):
            href = absolutize(a["href"], site)
            if not href:
                continue
            anchor = clean_ws(a.get_text(" ", strip=True))
            s = score_branch_url(href, anchor)
            if href.lower().endswith(".pdf"):
                if s >= 2.0:
                    add(href, s, is_pdf=True)
            elif s > 0:
                add(href, s)

    if len(scored) < 3 and FC["enabled"]:
        try:
            for u in fc_map(site, "branch"):
                s = score_branch_url(u)
                if s > 0:
                    add(u, s, is_pdf=u.lower().endswith(".pdf"))
        except Exception as e:
            LOG.debug("fc_map failed: %s", e)

    max_pages = ARGS.max_pages if ARGS else 25
    pages = [u for u, s in sorted(scored.items(), key=lambda kv: -kv[1])
             if s >= 1.2][:max_pages]
    pdf_list = [u for u, _ in sorted(pdfs.items(), key=lambda kv: -kv[1])][:6]
    if not pages:
        pages = [site]  # last resort: try to parse the homepage itself
    return pages, pdf_list

STRONG_BLOCK_MARKERS = ("just a moment", "attention required", "verify you are human",
                        "checking your browser", "enable javascript and cookies",
                        "request blocked", "access denied")
CHALLENGE_HEAD_RE = re.compile(
    r"cf-chl|challenge-platform|captcha-delivery|_incapsula|radware|perimeterx", re.I)

def looks_blocked(html, text_len):
    """Detect real anti-bot interstitials. A stray 'captcha' mention deep in the
    body (e.g. a reCAPTCHA script for a contact form) must NOT block a full page."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html[:4096], re.S | re.I)
    title = (m.group(1) or "").lower() if m else ""
    if any(s in title for s in STRONG_BLOCK_MARKERS) or "captcha" in title:
        return True
    head = html[:5000].lower()
    if any(s in head for s in STRONG_BLOCK_MARKERS):
        return True
    if text_len < 800 and CHALLENGE_HEAD_RE.search(head):
        return True
    return False

def smart_fetch(url):
    """Stage 3: local requests first; Firecrawl escalation on block/JS-shell.
    Returns (html, markdown, source_or_reason)."""
    r = http_get(url, timeout=40)
    if r is not None and r.status_code == 200:
        ctype = r.headers.get("content-type", "").lower()
        if "json" in ctype and r.text.lstrip()[:1] in "[{":
            return r.text, None, "local"  # raw api payload — json_api extractor handles it
        if "html" in ctype or "text" in ctype or not ctype:
            html = r.text
            soup = make_soup(html)
            for t in soup(["script", "style", "noscript"]):
                t.decompose()
            text_len = len(soup.get_text(" ", strip=True))
            if not looks_blocked(html, text_len):
                n_scripts = html.lower().count("<script")
                if text_len >= 500 or n_scripts <= 3:
                    return html, None, "local"
                LOG.debug("possible js-shell: %s", url)
    if FC["enabled"]:
        d = fc_scrape(url)
        if d:
            return d["html"], d["markdown"], "firecrawl"
    code = r.status_code if r is not None else "error"
    return None, None, f"http_{code}"

NAME_STRONG_COL = re.compile(r"branch[\s_-]*name|^name$|শাখা", re.I)
NAME_COL = re.compile(r"branch(?!\s*(code|no|number|manager|type))|name|place|outlet|"
                      r"office|শাখা", re.I)
ADDR_COL = re.compile(r"address|location|ঠিকানা|অবস্থান", re.I)
PHONE_COL = re.compile(r"phone|tel|mobile|contact|hotline|যোগাযোগ", re.I)
TYPE_COL = re.compile(r"type|category|nature|ধরন", re.I)

def map_columns(headers):
    colm = {"name": None, "address": [], "phone": None, "type": None}
    skip = set()
    for i, h in enumerate(headers):
        if not h:
            continue
        if ADDR_COL.search(h):
            colm["address"].append(i)
            skip.add(i)
        elif PHONE_COL.search(h):
            if colm["phone"] is None:
                colm["phone"] = i
            skip.add(i)
        elif TYPE_COL.search(h):
            if colm["type"] is None:
                colm["type"] = i
            skip.add(i)
        elif NAME_STRONG_COL.search(h) and colm["name"] is None:
            colm["name"] = i  # "Branch Name" beats "Branch Code"
            skip.add(i)
    if colm["name"] is None:
        for i, h in enumerate(headers):
            if h and i not in skip and NAME_COL.search(h):
                colm["name"] = i
                break
    if colm["name"] is not None and (colm["address"] or colm["phone"] is not None):
        return colm
    return None

def row_record(texts, colm, page_sub, url="", alts=None):
    def txt(i):
        return texts[i] if i is not None and 0 <= i < len(texts) else ""
    name = clean_ws(txt(colm.get("name")))
    # stacked name cells (Trust Bank eservice): the Name column holds
    # <a>Branch Name</a> with address/phone divs beneath — flattened cell
    # text buries the name, so a short anchor title inside a much longer
    # cell is the row heading
    anchor = clean_ws((alts or {}).get(colm.get("name"), ""))
    if (anchor and len(anchor) >= 4 and len(name) >= len(anchor) + 25
            and anchor.lower() not in ("view details", "details", "more", "map",
                                       "website", "location", "click here")):
        name = anchor
    address = ", ".join(t for t in (clean_ws(txt(i)) for i in colm.get("address", [])) if t)
    phone_raw = txt(colm.get("phone"))
    phones = find_phones(phone_raw)
    if not phones and re.search(r"\d{3}", phone_raw):
        phones = [clean_ws(phone_raw)]
    type_val = txt(colm.get("type"))
    if not name and not address:
        return None
    return {"name": name[:150], "address": address[:300], "url": url or "",
            "phone": "; ".join(dict.fromkeys(phones))[:120],
            "is_sub": classify_sub(name, type_val, page_sub)}

def heuristic_row(texts, page_sub, url=""):
    if len(texts) < 2:
        return None
    phone, phone_idx = "", None
    for i, t in enumerate(texts):
        ph = find_phones(t)
        if ph:
            phone = "; ".join(ph)
            phone_idx = i
            break
    # name first (a "X Branch" cell must never be mistaken for an address just
    # because it contains a district keyword like "sylhet")
    name_idx = None
    for i, t in enumerate(texts):
        if i != phone_idx and t and len(clean_ws(t)) <= 150 and BRANCH_NAME_RE.search(t):
            name_idx = i
            break
    if name_idx is None:
        for i, t in enumerate(texts):
            if i != phone_idx and t and len(clean_ws(t)) <= 90:
                name_idx = i
                break
    addr_idx = None
    for i, t in enumerate(texts):
        if i in (phone_idx, name_idx) or not t:
            continue
        if any(k in t.lower() for k in ADDR_KWS):
            addr_idx = i
            break
    if addr_idx is None:
        cands = [(len(t), i) for i, t in enumerate(texts)
                 if t and i not in (phone_idx, name_idx)]
        if cands:
            addr_idx = max(cands)[1]
    addr = clean_ws(texts[addr_idx]) if addr_idx is not None else ""
    name = clean_ws(texts[name_idx]) if name_idx is not None else ""
    if not name and not addr:
        return None
    return {"name": name[:150], "address": addr[:300], "url": url or "",
            "phone": phone[:120], "is_sub": classify_sub(name, "", page_sub)}

def row_url(row_el, base_url):
    if row_el is None:
        return ""
    for a in row_el.find_all("a", href=True):
        u = absolutize(a["href"], base_url)
        if u and domain_of(u) == domain_of(base_url):
            return u
    return ""

def blob_records(texts, page_sub, url=""):
    """Headerless listings that pack one branch per cell as
    'Name, address, Phone: ...' (Modhumoti's Urban/Rural columns): every
    comma-blob cell whose tail looks like an address becomes its own row.
    Prefers the cell's <br>-line structure (first line = name) and falls back
    to splitting the flattened text on the first comma."""
    out = []

    def split_cell(t):
        # t may contain '\n' from <br> separators (get_text('\n'))
        lines = [clean_ws(x) for x in t.split("\n") if clean_ws(x)]
        if len(lines) >= 2 and len(lines[0]) <= 60:
            return lines[0].rstrip(",; -"), " ".join(lines[1:])
        flat = " ".join(lines)
        if "," in flat:
            head, rest = flat.split(",", 1)
            return clean_ws(head), clean_ws(rest)
        return None, None

    for t in texts:
        if not t or re.fullmatch(r"[\d\W]+", t):
            continue
        head, rest = split_cell(t)
        if not head or len(head) > 60 or len(rest or "") < 12:
            continue
        if not any(k in rest.lower() for k in ADDR_KWS) and not find_phones(rest):
            continue
        out.append({"name": head[:150], "address": rest[:300], "url": url or "",
                    "phone": "; ".join(dict.fromkeys(find_phones(t)))[:120],
                    "is_sub": classify_sub(head, "", page_sub)})
    return out

def single_column_records(rows, page_sub):
    """One-cell-per-row 'vertical card' tables (EXIM location pages): a name
    row ('AGRABAD BRANCH') followed by 'Routing No/Address/Phone/Email:'
    label rows. Walk the rows, open a record at each branch-named row and
    attach labelled fields to it; rows that match nothing are ignored."""
    label_re = re.compile(
        r"^(address|phone|fax|e-?mail|routing(?:\s*no)?|swift(?:\s*code)?|"
        r"mobile|tel|pabx)\s*[:\-]\s*(.*)$", re.I)
    out, cur = [], None
    for tr in rows:
        cell = clean_ws(tr.get_text(" ", strip=True))
        if not cell:
            continue
        m = label_re.match(cell)
        if m:
            if cur is not None:
                field, val = m.group(1).lower(), clean_ws(m.group(2))
                if field.startswith("addr") and not cur.get("address"):
                    cur["address"] = val[:300]
                elif not cur.get("phone") and val and any(
                        k in field for k in ("phone", "fax", "mobile", "tel", "pabx")):
                    cur["phone"] = val[:120]
            continue
        if ((BRANCH_NAME_RE.search(cell) or SUB_NAME_LINE_RE.match(cell))
                and len(cell) <= 90
                and not GENERIC_NAME_RE.match(cell.rstrip(" :;,.|-"))):
            cur = {"name": cell[:150], "address": "", "url": "",
                   "phone": "", "is_sub": classify_sub(cell, "", page_sub)}
            out.append(cur)
    return [r for r in out if r.get("address") or r.get("phone")]


def extract_html_tables(html, base_url, page_sub=False):
    out = []
    for table in make_soup(html).find_all("table"):
        if table.find("table") is not None:
            # wrapper table with a nested table inside (ONE Bank division
            # sections): its flattened view mangles rows (labels become
            # names, columns misalign) — the nested table parses below with
            # its own header row, so the wrapper only adds junk duplicates
            continue
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        # ATM sections (#atms, class atm-*) hold booth tables whose rows are
        # plain site names ("PHQ", "Mymensingh Police Lines") — skip them
        if table.find_parent(lambda t: t.name in ("section", "div") and (
                re.search(r"atm", str(t.get("id") or ""), re.I)
                or any(re.search(r"atm", c, re.I) for c in (t.get("class") or [])))):
            continue
        header_texts = [clean_ws(c.get_text(" ", strip=True))
                        for c in rows[0].find_all(["th", "td"])]
        # agent-banking outlet listings ("Agent Outlet Name/Address", "Outlet
        # Owner") share the site with branch pages — never branch data.
        # MTB locator: ATM/CDM/CRM/merchant tables sit on the SAME page with
        # identical ids/classes; the header cells ("ATM Name", "CDM Name",
        # "Merchant Name", "MTB Agent Banking…") are the only reliable label
        # of what the rows are — trust them.
        if any(re.search(r"agent|outlet|merchant", h, re.I)
               or NON_BRANCH_FEED_RE.search(h) for h in header_texts):
            continue
        # single-column vertical cards (EXIM): map_columns can't see a header
        # row here because the first row is already the first branch name
        widths = [len(tr.find_all(["td", "th"])) for tr in rows]
        if widths and max(widths) <= 1:
            got = single_column_records(rows, page_sub)
            if got:
                out.extend(got)
                continue
        colm = map_columns(header_texts) if any(header_texts) else None
        if colm:
            for tr in rows[1:]:
                tds = tr.find_all(["td", "th"])
                texts = [clean_ws(td.get_text(" ", strip=True)) for td in tds]
                alts = {}
                for i, td in enumerate(tds):
                    a = td.find("a")
                    if a is not None:
                        at = clean_ws(a.get_text(" ", strip=True))
                        if at:
                            alts[i] = at
                rec = row_record(texts, colm, page_sub,
                                 url=row_url(tr, base_url), alts=alts)
                if rec:
                    out.append(rec)
        elif find_phones(table.get_text(" ", strip=True)):
            # transposed mobile tables (HSBC) repeat the desktop listing as
            # label/value rows ('Branch','Tejgaon')('Location','…') — skip them
            firsts = []
            for tr in rows:
                cells = tr.find_all(["td", "th"])
                firsts.append(clean_ws(cells[0].get_text(" ", strip=True)).lower()
                              if cells else "")
            if sum(1 for c in firsts if c in (
                    "branch", "location", "phone", "phone number", "status",
                    "address", "name")) >= max(2, len(firsts) * 0.5):
                continue
            for tr in rows:
                tds = tr.find_all(["td", "th"])
                texts = [clean_ws(td.get_text(" ", strip=True)) for td in tds]
                # newline-preserved cells let blob_records split name/address
                # at the real <br> boundary instead of the first comma
                nl_texts = [td.get_text("\n", strip=True) for td in tds]
                blobs = blob_records(nl_texts, page_sub, url=row_url(tr, base_url))
                if blobs:
                    out.extend(blobs)
                    continue
                rec = heuristic_row(texts, page_sub, url=row_url(tr, base_url))
                if rec:
                    out.append(rec)
    return out

def extract_html_cards(html, base_url, page_sub=False):
    soup = make_soup(html)
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    # Some sites (NRBC) never close <header>, so the parser nests the WHOLE
    # page (nav + hundreds of branch cards) inside it. Only drop boilerplate
    # nav/footer/header shells that carry no branch titles themselves.
    for t in soup(["nav", "footer", "header"]):
        titles = t.find_all(["h4", "h5", "strong"])
        branchish = sum(1 for x in titles
                        if BRANCH_NAME_RE.search(x.get_text(" ", strip=True)))
        if branchish < 5:
            t.decompose()
    out = []

    # Some branch pages (NRBC, etc.) render each card as a Bootstrap column like
    # <div class="col-md-3 ..."><h5>PRINCIPAL BRANCH</h5><p>Principal Branch,...</p>...</div>.
    # The generic row-based card scan misses these because the whole row contains too much text.
    for card in soup.select("div.col-md-3, div.col-sm-3, div.col-xs-3, div.col-lg-3, li, article, section"):
        htags = [clean_ws(h.get_text(" ", strip=True))
                 for h in card.find_all(["h5", "h4", "strong"])]
        htags = [h for h in htags if h]
        title = None
        if htags:
            # parent-listing cards (BCBL): heading #1 names the parent
            # branch and the sub-branch heading further down is the outlet
            # itself; blank leading headings (<h5><a></a></h5>) are skipped.
            # SUB_NAME_LINE_RE (not bare SUB_RE) so "Sub Branch" type-label
            # headings keep the leading-title behaviour
            subs = [h for h in htags if SUB_NAME_LINE_RE.match(h)]
            title = subs[-1] if subs else htags[0]
        if not title or not (BRANCH_NAME_RE.search(title) or SUB_RE.search(title)):
            continue
        if re.search(r"loan|finance|sm e|sme|retail loan|business loan|home loan|green finance", title, re.I):
            continue
        text = " ".join(clean_ws(x) for x in card.get_text("\n").split("\n") if clean_ws(x))
        if not (25 <= len(text) <= 800):
            continue
        if not re.search(r"cell:|phone|routing number|address|district|city|road|thana|upazila", text, re.I):
            continue
        phones = find_phones(text)
        addr = None
        # tightest element first: a wrapping div's get_text() would smear the
        # title + routing number into the address line (NRBC cards)
        for tag in ("p", "span", "div"):
            for p in card.find_all(tag):
                ptxt = clean_ws(p.get_text(" ", strip=True))
                if not ptxt or ptxt == title:
                    continue
                if any(k in ptxt.lower() for k in ADDR_KWS) or "cell:" in ptxt.lower() or "routing number" in ptxt.lower():
                    addr = ptxt
                    break
            if addr is not None:
                break
        if addr is None:
            cands = [clean_ws(x) for x in card.get_text("\n").split("\n") if clean_ws(x) and clean_ws(x) != title]
            if cands:
                addr = max(cands, key=len)
        out.append({"name": title.strip(" -|,")[:150],
                    "address": clean_ws(addr or "")[:300], "url": row_url(card, base_url),
                    "phone": "; ".join(dict.fromkeys(phones))[:120],
                    "is_sub": classify_sub(title, "", page_sub)})

    # fall back to the original generic card scan, but skip duplicate names created
    # by the more specific block above.
    seen = set()
    for rec in out:
        seen.add(re.sub(r"[^a-z0-9]", "", (rec.get("name") or "").lower()))
    for el in soup.find_all(["div", "li", "article", "section", "tr"]):
        lines = [clean_ws(x) for x in el.get_text("\n").split("\n")]
        lines = [x for x in lines if x]
        if not (2 <= len(lines) <= 12):
            continue
        joined = " ".join(lines)
        if not (25 <= len(joined) <= 500):
            continue
        phones = find_phones(joined)
        # prefer an outlet-specific heading line: parent-listing cards show
        # "Parent Branch" first and "X SUB BRANCH" underneath — the sub
        # line names the outlet itself. SUB_NAME_LINE_RE so bare "Sub
        # Branch" type-label lines are never picked as the name
        name = next((ln for ln in lines if SUB_NAME_LINE_RE.match(ln)
                     and len(ln) < 110), None)
        name = name or next((ln for ln in lines
                             if BRANCH_NAME_RE.search(ln) and len(ln) < 110), None)
        if not name:
            continue
        nk = re.sub(r"[^a-z0-9]", "", name.lower())
        if nk in seen:
            continue
        seen.add(nk)
        addr = None
        for ln in lines:
            if ln != name and any(k in ln.lower() for k in ADDR_KWS):
                addr = ln
                break
        if addr is None:
            cands = [ln for ln in lines if ln != name and not find_phones(ln)]
            if cands:
                addr = max(cands, key=len)
        if not phones and not addr:
            continue
        out.append({"name": name.strip(" -|,")[:150],
                    "address": clean_ws(addr or "")[:300], "url": row_url(el, base_url),
                    "phone": "; ".join(dict.fromkeys(phones))[:120],
                    "is_sub": classify_sub(name, "", page_sub)})
    return out

BRANCH_DATA_KEY_RE = re.compile(
    r"branch|address|location|phone|mobile|email|district|thana|city|latitude|"
    r"longitude|শাখা|ঠিকানা", re.I)

def _pick(d, pats):
    for p in pats:
        for k, v in d.items():
            if re.search(p, str(k), re.I):
                if isinstance(v, dict):
                    v = ", ".join(str(x) for x in v.values() if x)
                if v not in (None, "", [], {}):
                    return str(v)
    return ""

def looks_like_branch_list(lst):
    """Key-based detection: real branch datasets expose address/location/phone-ish
    KEYS. News/SEO schema exposes only name/url/@type keys and must not match."""
    dicts = [d for d in lst if isinstance(d, dict)]
    if len(dicts) < 2 or len(dicts) < len(lst) * 0.6:
        return False
    sample, hit = dicts[:20], 0
    for d in sample:
        keys = " ".join(str(k) for k in d.keys())
        if BRANCH_DATA_KEY_RE.search(keys):
            hit += 1
    return hit >= max(2, int(len(sample) * 0.4))

def find_branch_lists(obj, depth=0):
    found = []
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for v in obj.values():
            found.extend(find_branch_lists(v, depth + 1))
    elif isinstance(obj, list) and looks_like_branch_list(obj):
        found.append(obj)
    return found

def json_records(lst, base_url, page_sub=False):
    out = []
    for d in lst:
        if not isinstance(d, dict):
            continue
        name = _pick(d, [r"branch[\s_-]*name", r"^name$", r"^title$",
                         r"^branch(es)?$", r"শাখা"])
        addr = _pick(d, [r"address", r"location", r"ঠিকানা"])
        phone = _pick(d, [r"phone", r"tel", r"mobile", r"contact", r"hotline"])
        urlv = _pick(d, [r"^(url|link|slug|website|page|permalink)$"])
        typ = _pick(d, [r"type", r"category"])
        nm = clean_ws(name)
        if NON_BRANCH_FEED_RE.search(nm):
            continue  # agent outlets / atm booths leaking through a locator feed
        if not (addr or phone):
            # keep name-only records only if they look like branch names,
            # not SEO page titles ("X - Bank | Excellence in Banking")
            if " | " in nm or len(nm) > 70 or not nm:
                continue
        if not nm and not addr:
            continue
        phones = find_phones(phone) or ([clean_ws(phone)] if phone else [])
        out.append({"name": nm[:150], "address": clean_ws(addr)[:300],
                    "url": absolutize(urlv, base_url) if urlv else "",
                    "phone": "; ".join(dict.fromkeys(phones))[:120],
                    "is_sub": classify_sub(nm, typ, page_sub)})
    return out

def extract_script_json(html, base_url, page_sub=False):
    out = []
    stripped = html.lstrip()
    if stripped[:1] in "[{":
        # raw JSON api response (no html wrapper at all)
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
        if isinstance(obj, list) and looks_like_branch_list(obj):
            out.extend(json_records(obj, base_url, page_sub))
        elif isinstance(obj, dict):
            for lst in find_branch_lists(obj):
                out.extend(json_records(lst, base_url, page_sub))
        if out:
            return out
    soup = make_soup(html)
    decoder = json.JSONDecoder()
    found, seen = [], set()

    def scan(body):
        if not body or len(body) > 2_000_000:
            return
        if "[{" not in body and '{"' not in body:
            return
        for m in re.finditer(r"[\[{]", body):
            try:
                obj, _end = decoder.raw_decode(body, m.start())
            except ValueError:
                continue
            if isinstance(obj, list) and looks_like_branch_list(obj):
                found.append(obj)
            elif isinstance(obj, dict):
                found.extend(find_branch_lists(obj))

    def scan_escaped(body):
        # Next.js RSC/flight chunks embed JSON string-escaped (\"title\")
        # inside script strings (BRAC locator) — unescape quotes and re-scan
        if '\\"' not in body:
            return
        scan(body.replace('\\"', '"'))

    for sc in soup.find_all("script"):
        if sc.get("src"):
            continue
        stype = (sc.get("type") or "").lower()
        body = sc.string or sc.get_text()
        if not body:
            continue
        if "ld+json" in stype or '"@context"' in body or '"@type"' in body:
            continue  # SEO/structured-data schema, not branch data
        scan(body)
        scan_escaped(body)
    out = []
    # one page, several outlet-type arrays (BRAC locator): branch/sub-branch
    # lists sit beside agent-outlet, premium-lounge and promo arrays with the
    # same record shape — when branch-dominated arrays exist, drop arrays
    # whose titles are almost never branch-like
    def _name_ratio(lst):
        if not lst:
            return 0.0
        hit = sum(1 for d in lst if isinstance(d, dict)
                  and BRANCH_NAME_RE.search(str(d.get("name")
                                               or d.get("title") or "")))
        return hit / len(lst)
    if any(_name_ratio(l) >= 0.6 for l in found):
        found = [l for l in found if _name_ratio(l) >= 0.2]
    for lst in found:
        if id(lst) in seen:
            continue
        seen.add(id(lst))
        out.extend(json_records(lst, base_url, page_sub))
    return out

INFOWIN_RE = re.compile(
    r"\{\s*['\"]?id['\"]?\s*:\s*['\"]?\d+['\"]?\s*,\s*"
    r"['\"]?title['\"]?\s*:\s*(['\"])(.*?)\1\s*,\s*"
    r"['\"]?detail['\"]?\s*:\s*(['\"])(.*?)\3"
    r"(?:\s*,\s*['\"]?location['\"]?\s*:\s*(['\"])(.*?)\5)?", re.S)
COORDS_RE = re.compile(r"^[\d.,\s\-]+$")

def extract_map_infowindows(html, base_url, page_sub=False):
    """Google-Maps info windows embedded as JS literals (Agrani et al.):
    branch_infowin.push({'id':93, 'title':"X", 'detail':"<address>",
    'location': '...'}). When 'location' holds text (site bug) it is the
    proper branch name; otherwise it is "lat, lng" coordinates."""
    hits = INFOWIN_RE.findall(html)
    if len(hits) < 5:  # a real branch map, not incidental JS objects
        return []
    out = []
    for _q1, title, _q2, detail, _q3, location in hits:
        name = clean_ws(title)
        addr = clean_ws(detail)
        loc = clean_ws(location)
        if loc and not COORDS_RE.match(loc):
            name = loc  # e.g. 'SS COLLEGE BRANCH ,DHAKA' beats 'S S College'
        if not name and not addr:
            continue
        phones = find_phones(addr)
        out.append({"name": name[:150], "address": addr[:300], "url": "",
                    "phone": "; ".join(dict.fromkeys(phones))[:120],
                    "is_sub": classify_sub(name, "", page_sub)})
    return out

MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
BARE_URL_RE = re.compile(r"(https?://[^\s|)\]]+)")

def md_cell_text(c):
    return clean_ws(MD_LINK_RE.sub(r"\1", c)).strip("`* ")

def md_table_records(rows, base_url, page_sub=False):
    parsed = []
    for r in rows:
        parsed.append([c.strip() for c in r.strip().strip("|").split("|")])
    parsed = [cs for cs in parsed
              if not all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cs)]
    if len(parsed) < 2:
        return []
    out = []
    colm = map_columns([md_cell_text(c) for c in parsed[0]])
    if colm:
        for cs in parsed[1:]:
            texts = [md_cell_text(c) for c in cs]
            url = ""
            if colm.get("name") is not None and colm["name"] < len(cs):
                m = MD_LINK_RE.search(cs[colm["name"]])
                if m:
                    url = absolutize(m.group(2), base_url)
            rec = row_record(texts, colm, page_sub, url=url)
            if rec:
                out.append(rec)
    elif find_phones(" ".join(" ".join(cs) for cs in parsed)):
        for cs in parsed:
            rec = heuristic_row([md_cell_text(c) for c in cs], page_sub)
            if rec:
                out.append(rec)
    return out

def records_from_lines(lines, page_sub=False, base_url=""):
    blocks, blk = [], []
    for raw in lines:
        t = raw.strip()
        if t and not t.startswith(("|", "![", "---", "===")):
            blk.append(clean_ws(t.strip("#*- ").strip("`")))
        elif not t and blk:
            blocks.append(blk)
            blk = []
    if blk:
        blocks.append(blk)
    out = []
    for b in blocks:
        if len(b) == 1 and "," in b[0]:
            b = [clean_ws(x) for x in b[0].split(",") if clean_ws(x)]
        joined = " ".join(b)
        if not (20 <= len(joined) <= 700):
            continue
        phones = find_phones(joined)
        name = next((ln for ln in b
                     if BRANCH_NAME_RE.search(ln) and len(ln) < 110), None)
        if not name:
            continue
        addr = next((ln for ln in b
                     if ln != name and any(k in ln.lower() for k in ADDR_KWS)), None)
        if addr is None:
            cands = [ln for ln in b if ln != name and not find_phones(ln) and len(ln) > 8]
            if cands:
                addr = max(cands, key=len)
        if not phones and not addr:
            continue
        url = ""
        m = MD_LINK_RE.search(joined)
        if m:
            url = absolutize(m.group(2), base_url)
        else:
            m2 = BARE_URL_RE.search(joined)
            if m2:
                url = m2.group(1)
        out.append({"name": name.strip(" -|,")[:150],
                    "address": clean_ws(addr or "")[:300], "url": url,
                    "phone": "; ".join(dict.fromkeys(phones))[:120],
                    "is_sub": classify_sub(name, "", page_sub)})
    return out

def parse_markdown_records(md, base_url, page_sub=False):
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            out.extend(md_table_records(tbl, base_url, page_sub))
        else:
            i += 1
    out.extend(records_from_lines(lines, page_sub, base_url))
    return out

LABEL_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z /()&.\-]{2,40}):\s*(.*)$")

def label_block_records(lines, page_sub=False, url=""):
    """Label-styled PDFs (Bank Alfalah BD): a 'Branch Name:' line starts a
    record; following 'Label: value' lines (Holding No./Village-Road/Thana/
    District...) build the address. Used when the PDF has table lines whose
    header row can't be column-mapped."""
    out, cur = [], None

    def flush():
        if cur and (cur["addr"] or cur["phone"]):
            out.append({"name": cur["name"], "address": ", ".join(cur["addr"])[:300],
                        "url": url or "",
                        "phone": "; ".join(dict.fromkeys(cur["phone"]))[:120],
                        "is_sub": classify_sub(cur["name"], "", page_sub)})

    for ln in lines:
        ln = clean_ws(ln)
        m = re.match(r"(?i)^branch\s*name\s*:\s*(.+)$", ln)
        if not m:
            m = re.match(r"(?i)^name\s*:\s*(.+)$", ln)
        if m and len(clean_ws(m.group(1))) <= 70 and " of the bank" not in ln.lower():
            flush()
            cur = {"name": clean_ws(m.group(1))[:150], "addr": [], "phone": []}
            continue
        if cur is None:
            continue
        cur["phone"].extend(find_phones(ln))
        lm = LABEL_LINE_RE.match(ln)
        label = (lm.group(1).strip().rstrip(".").lower() if lm else "")
        val = clean_ws(lm.group(2)) if lm else ln
        if not val:
            continue
        if label in ("address", "serial no", "link of google map", "name") \
                or re.fullmatch(r"\d{1,3}", val) or "http" in val:
            continue
        cur["addr"].append(val)
    flush()
    return out

def extract_pdf(url, page_sub=False):
    """Extract records from a PDF: local pdfplumber first, Firecrawl markdown fallback."""
    LOG.info("    pdf: %s", url)
    records = []
    if pdfplumber is not None:
        try:
            r = http_get(url, timeout=90)
            if (r is not None and r.status_code == 200
                    and (r.content[:4] == b"%PDF"
                         or "pdf" in r.headers.get("content-type", "").lower())):
                with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                    text_lines, got_tables = [], False
                    for page in pdf.pages:
                        for tbl in (page.extract_tables() or []):
                            got_tables = True
                            norm = [[clean_ws(c) for c in row] for row in tbl if row]
                            if len(norm) < 2:
                                continue
                            colm = map_columns(norm[0])
                            if colm:
                                for row in norm[1:]:
                                    rec = row_record(row, colm, page_sub)
                                    if rec:
                                        records.append(rec)
                            elif find_phones(" ".join(" ".join(r2) for r2 in norm)):
                                for row in norm:
                                    rec = heuristic_row(row, page_sub)
                                    if rec:
                                        records.append(rec)
                        text_lines.extend((page.extract_text() or "").splitlines())
                    if not records and text_lines:
                        if got_tables:
                            # column-mapped tables failed (label-styled rows):
                            # fall back to 'Branch Name:' label blocks
                            records.extend(label_block_records(text_lines, page_sub, url))
                        else:
                            records.extend(records_from_lines(text_lines, page_sub, url))
        except Exception as e:
            LOG.debug("pdfplumber failed on %s: %s", url, e)
    if not records and FC["enabled"]:
        d = fc_scrape(url)
        if d and d.get("markdown"):
            records = parse_markdown_records(d["markdown"], url, page_sub)
    return records

DT_AJAX_RE = re.compile(
    r"['\"]?ajax['\"]?\s*:\s*\{(?:[^{}]|\{[^{}]*\}){0,800}?['\"]?url['\"]?\s*:\s*['\"]([^'\"]+)['\"]")
# DataTables `columns:` JS array entries pair data:'col' with name:'col'
DT_COL_RE = re.compile(
    r"data\s*:\s*['\"]([\w.-]+)['\"]\s*,\s*name\s*:\s*['\"]([\w.-]+)['\"]")

def _dt_cell_text(v):
    from html import unescape
    if isinstance(v, (dict, list)):
        v = " ".join(str(x) for x in (v.values() if isinstance(v, dict) else v))
    return clean_ws(unescape(re.sub(r"<[^>]+>", " ", str(v or ""))))

def extract_datatables(html, base_url, page_sub=False):
    """DataTables 'serverSide' pages (Sonali et al.) render rows via POST ajax;
    the visible <table> is only a header shell. Fetch the ajax endpoint with
    pagination and map rows via the page's own <th> headers."""
    m = ("DataTable" in html) and DT_AJAX_RE.search(html)
    if not m:
        return []
    ajax_url = absolutize(m.group(1), base_url)
    # Laravel/yajra endpoints (Prime Bank) 500 unless the ajax call carries
    # the columns[i][...] grid the DataTables client builds from the page's
    # own `columns:` JS array, plus any extra ajax data:{} keys (variables
    # resolve to '' at first render). Sites without a parseable columns
    # array keep the legacy minimal params (Sonali et al.).
    dt_cols = [c for c, _n in DT_COL_RE.findall(html)]
    extra_data = {}
    dm = re.search(r"data\s*:\s*\{([^{}]*)\}", html[m.end():m.end() + 800])
    if dm:
        extra_data = {k: "" for k in re.findall(r"(\w+)\s*:", dm.group(1))}
    headers = [clean_ws(th.get_text(" ", strip=True))
               for th in make_soup(html).find_all("th")]
    colm = map_columns(headers) if headers else None
    # Prime a session on the listing page first: Laravel endpoints (Prime
    # Bank) answer cookieless ajax calls with HTTP 500 but honour the page
    # cookies a real browser would carry.
    session = requests.Session()
    try:
        session.get(base_url, timeout=45, headers=HEADERS)
    except requests.exceptions.SSLError:
        try:
            session.get(base_url, timeout=45, headers=HEADERS, verify=False)
        except Exception:
            pass
    except Exception:
        pass
    out, start, draw, guard = [], 0, 1, 0
    while guard < 60:
        guard += 1
        polite_sleep()
        params = {
            "draw": draw, "start": start, "length": 100,
            "search[value]": "", "search[regex]": "false",
            "order[0][column]": "0", "order[0][dir]": "asc", **extra_data}
        for i, c in enumerate(dt_cols):
            params[f"columns[{i}][data]"] = c
            params[f"columns[{i}][name]"] = c
            params[f"columns[{i}][searchable]"] = "true"
            params[f"columns[{i}][orderable]"] = "true"
            params[f"columns[{i}][search][value]"] = ""
            params[f"columns[{i}][search][regex]"] = "false"
        dt_headers = {
            **HEADERS, "X-Requested-With": "XMLHttpRequest",
            "Referer": base_url,
            "Accept": "application/json, text/javascript, */*; q=0.01"}
        payload = None
        for method in ("post", "get"):
            # Laravel DataTables (Prime) reject POST with an auth redirect;
            # classic ASP.NET sites (Sonali) expect POST — try both
            kw = dict(timeout=45, headers=dt_headers,
                      data=params if method == "post" else None,
                      params=params if method == "get" else None)
            r = None
            for verify in (True, False):
                try:
                    r = getattr(session, method)(ajax_url, verify=verify, **kw)
                    break
                except requests.exceptions.SSLError:
                    continue
                except Exception:
                    r = None
                    break
            if r is None:
                break
            if r.status_code != 200:
                continue
            try:
                payload = r.json()
            except ValueError:
                payload = None
            if payload is not None:
                break
        if payload is None:
            break
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not rows:
            break
        for row in rows:
            if isinstance(row, dict) and (colm is None or len(row) != len(headers)):
                # server returns richer objects than the visible table —
                # remap using the payload's own keys
                keys = list(row.keys())
                kcolm = map_columns(keys)
                if kcolm["name"] is not None:
                    texts = [_dt_cell_text(row[k]) for k in keys]
                    rec = row_record(texts, kcolm, page_sub, url=base_url)
                    if rec:
                        out.append(rec)
                        continue
            cells = list(row.values()) if isinstance(row, dict) else list(row)
            texts = [_dt_cell_text(c) for c in cells]
            rec = None
            if colm and len(texts) == len(headers):
                rec = row_record(texts, colm, page_sub, url=base_url)
            if rec is None:
                rec = heuristic_row(texts, page_sub, url=base_url)
            if rec:
                out.append(rec)
        try:
            total = int(payload.get("recordsTotal") or 0)
        except (TypeError, ValueError):
            total = 0
        start += len(rows)
        draw += 1
        if (total and start >= total) or len(rows) < 10:
            break
    return out

def _js_object_blocks(html, opener_re):
    """Yield brace-balanced {...} bodies for each opener match, honouring
    JS string literals so embedded '}' doesn't end the block early."""
    for m in opener_re.finditer(html):
        i = html.find("{", m.end())
        if i == -1:
            continue
        depth, j, in_str, q = 0, i, False, ""
        while j < len(html):
            c = html[j]
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == q:
                    in_str = False
            elif c in "\"'":
                in_str, q = True, c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield html[i:j + 1]
                    break
            j += 1

JQ_AJAX_OPEN_RE = re.compile(r"\$\s*\.\s*ajax\s*\(")
# feeds we never want as branch rows even if the site exposes them
NON_BRANCH_FEED_RE = re.compile(
    r"(?<![a-z0-9])(atm|ratm|rcdm|cdm|crm|agent|booth|citygem|brta|land|merchant)(?![a-z0-9])", re.I)
# pure atm/agent/brta/land-registration listing pages (NRBC atm_location,
# agent_locations, brta_location, land_registration_location) — their cards
# and ajax feeds carry booth rows that leak as duplicate branches. Combined
# pages keep a branchy segment ('/branch-and-atm/', 'branch-atm-locator').
NON_BRANCH_PAGE_RE = re.compile(
    r"(?<![a-z0-9])(atms?|agents?|booths?|brta|land|cdm|crm)(?![a-z0-9])", re.I)
BRANCHY_SEG_RE = re.compile(r"branch|office|locator|network", re.I)

def non_branch_page(url):
    """True when a URL path segment names an atm/agent/brta/land listing and
    carries no branch/office/locator token (so combined pages stay eligible)."""
    from urllib.parse import urlsplit
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return any(seg and NON_BRANCH_PAGE_RE.search(seg) and not BRANCHY_SEG_RE.search(seg)
               for seg in path.split("/"))

def extract_jquery_ajax_json(html, base_url, page_sub=False):
    """jQuery $.ajax({...}) locator feeds (City Bank legacy Laravel): the page
    POSTs a literal data payload (e.g. srcdata:"Branch") to a literal url,
    guarded by the csrf-token meta. Replay those calls and map JSON rows."""
    from urllib.parse import urlsplit
    if not JQ_AJAX_OPEN_RE.search(html):
        return []
    session = requests.Session()
    # Laravel binds the csrf meta token to the session cookie that rendered
    # the page — refetch once on a sticky session so token+cookie pair up
    try:
        r0 = session.get(base_url, timeout=45, headers=HEADERS)
        if r0.status_code == 200 and len(r0.text) > 500:
            html = r0.text
    except Exception:
        pass
    out, done = [], set()
    csrf = (re.search(r'name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']', html)
            or re.search(r'content=["\']([^"\']+)["\'][^>]*name=["\']csrf-token["\']', html))
    token = csrf.group(1) if csrf else ""
    for blk in _js_object_blocks(html, JQ_AJAX_OPEN_RE):
        um = re.search(r"url\s*:\s*([^,\n}]+)", blk)
        if not um:
            continue
        uval = um.group(1).strip()
        literals = re.findall(r"['\"]([^'\"]+)['\"]", uval)
        if not literals:
            continue  # url built from a bare variable we can't resolve
        path = literals[-1]
        absolute = next((l for l in literals
                         if l.startswith(("http://", "https://"))), None)
        if "location.origin" in uval:
            origin = urlsplit(base_url)._replace(path="", query="",
                                                 fragment="").geturl().rstrip("/")
            ajax_url = origin + ("" if path.startswith("/") else "/") + path
        elif absolute:
            ajax_url = absolutize(absolute, base_url)
        else:
            ajax_url = absolutize(path, base_url)
        # relative endpoints resolve against the page path — when the page is
        # served slash-less (/home/…/branch) but the site routes the ajax
        # call from its directory form (/home/…/branch/br_list/, ONE Bank),
        # the first resolution 404s; keep a directory-style fallback to retry
        alt_url = None
        if not absolute and "location.origin" not in uval:
            alt_url = absolutize(path, base_url.split("?")[0].rstrip("/") + "/")
            if alt_url == ajax_url:
                alt_url = None
        if NON_BRANCH_FEED_RE.search(ajax_url) and not re.search(
                r"branch|office|locator|network", ajax_url, re.I):
            continue  # pure atm/agent feed endpoint — booth rows, not branches
        dm = re.search(r"data\s*:\s*\{([^{}]*)\}", blk)
        dmb = dm.group(1) if dm else ""
        pairs = []
        if dmb:
            pairs = [dict(re.findall(r"([\w]+)\s*:\s*['\"]([^'\"]*)['\"]", dmb))]
            if not any(pairs[0].values()):
                # dropdown-driven feed: data {key: variable} — the values live
                # in a page <select> (ONE Bank division filter) — replay once
                # per non-empty <option>; the select may carry the payload key
                # as its name OR an unrelated div/district-style id
                payload_keys = re.findall(r"([\w]+)\s*:", dmb)
                sel_html = None
                for k in payload_keys:
                    sel_html = re.search(
                        r"<select[^>]*\bname=[\"']" + re.escape(k) + r"[\"'][^>]*>(.*?)</select>",
                        html, re.S | re.I)
                    if sel_html:
                        break
                if not sel_html and payload_keys:
                    sel_html = re.search(
                        r"<select[^>]*\b(?:name|id)=[\"'][^\"']*(?:div|district|zone|region)"
                        r"[^\"']*[\"'][^>]*>(.*?)</select>", html, re.S | re.I)
                if sel_html:
                    vals = re.findall(r"<option[^>]*\bvalue=[\"']([^\"']+)[\"']",
                                      sel_html.group(1), re.I)
                    vals = [v for v in vals if v.strip()][:24]
                    if vals:
                        pairs = [{payload_keys[0]: v} for v in vals]
        else:
            pairs = [{}]
        if not any(pairs) and "=" not in blk:
            continue  # only replay calls with static payloads
        tm = re.search(r"(?:type|method)\s*:\s*['\"](POST|GET)['\"]", blk, re.I)
        for data in pairs:
            key = (ajax_url, tuple(sorted(data.items())))
            if key in done:
                continue
            done.add(key)
            vals = " ".join(data.values())
            if NON_BRANCH_FEED_RE.search(vals):
                continue  # atm/agent/etc. feed — not branch rows
            polite_sleep()
            try:
                meth = tm.group(1).lower() if tm else "post"
                r = getattr(session, meth)(
                    ajax_url, timeout=45, data=data,
                    headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                             "Referer": base_url,
                             **({"X-CSRF-TOKEN": token} if token else {})})
                if alt_url and (r.status_code != 200
                                or len(r.text or "") < 50):
                    # retry relative resolution in directory form (ONE Bank)
                    r_alt = getattr(session, meth)(
                        alt_url, timeout=45, data=data,
                        headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                                 "Referer": base_url,
                                 **({"X-CSRF-TOKEN": token} if token else {})})
                    if (r_alt.status_code == 200
                            and len(r_alt.text or "") > len(r.text or "")):
                        r = r_alt
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                rows = r.json()
            except ValueError:
                rows = None
            if isinstance(rows, dict):
                rows = rows.get("data") or rows.get("results") or rows.get("branches")
            if not isinstance(rows, list):
                # jQuery injects the response as HTML — parse the fragment;
                # tables beat the card view of the same listing when present
                frag = (r.text or "").strip()
                if frag[:1] == "<" and len(frag) > 200:
                    # filter params the server ignores (BCBL's division
                    # dropdown) echo the same listing once per option —
                    # parse an identical response body only once
                    if hash(frag) in done:
                        continue
                    done.add(hash(frag))
                    trows = extract_html_tables(frag, base_url, page_sub)
                    if len(trows) >= 3:
                        out.extend(trows)
                    else:
                        out.extend(extract_html_cards(frag, base_url, page_sub))
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                rtype = _dt_cell_text(row.get("type") or row.get("branch_type") or "")
                if NON_BRANCH_FEED_RE.search(rtype):
                    continue
                keys = list(row.keys())
                kcolm = map_columns(keys)
                if kcolm is None or kcolm["name"] is None:
                    continue
                texts = [_dt_cell_text(row[k]) for k in keys]
                is_sub = page_sub or ("sub" in rtype.lower())
                rec = row_record(texts, kcolm, is_sub, url=base_url)
                if rec:
                    out.append(rec)
    return out

GET_TIME_URL_RE = re.compile(r"url\s*:\s*['\"]([^'\"]*get-time[^'\"]*)['\"]", re.I)

def extract_validator_key_feeds(html, base_url, page_sub=False):
    """Map locators gated by a server-time validator key (Southeast Bank):
    every $.ajax fetches get-time.php first and passes its plain-text body
    as request_validator_key to a data-provider endpoint behind an F5 bot
    filter. A session warmed on the page URL carries the F5 TS* cookies, so
    the handshake replays cleanly with plain requests."""
    if "request_validator_key" not in html:
        return []
    km = GET_TIME_URL_RE.search(html)
    if not km:
        return []
    session = requests.Session()
    try:  # warm-up: the page response sets the F5 TS*/BIGip cookies
        session.get(base_url, timeout=45, headers=HEADERS)
    except Exception:
        pass
    key = ""
    try:
        rk = session.get(absolutize(km.group(1), base_url), timeout=30,
                         headers={**HEADERS, "Referer": base_url,
                                  "X-Requested-With": "XMLHttpRequest"})
        if rk.status_code == 200:
            cand = rk.text.strip()
            if 16 <= len(cand) <= 200 and " " not in cand:
                key = cand
    except Exception:
        pass
    if not key:
        return []
    # literal branch-ish `type:` payloads among the validator-guarded calls
    types = [t for t in dict.fromkeys(re.findall(
        r"type\s*:\s*['\"]([^'\"]+)['\"]", html))
        if "branch" in t.lower() and "atm" not in t.lower()][:4]
    endpoints = []
    for um in re.finditer(r"url\s*:\s*['\"]([^'\"]+)['\"]", html):
        u = um.group(1)
        if "get-time" in u.lower() or u in endpoints:
            continue
        ctx = html[max(0, um.start() - 400): um.end() + 400]
        if "request_validator_key" in ctx:
            endpoints.append(u)
    out = []
    for ep in endpoints[:2]:
        feed = absolutize(ep, base_url)
        for typ in types or ["all_branch"]:
            polite_sleep()
            try:
                r = session.get(feed, timeout=45, params={
                    "type": typ, "request_validator_key": key},
                    headers={**HEADERS, "Referer": base_url,
                             "X-Requested-With": "XMLHttpRequest",
                             "Accept": "application/json, text/javascript, "
                                       "*/*; q=0.01"})
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                j = r.json()
            except ValueError:
                continue
            for lst in find_branch_lists(j):
                out.extend(json_records(lst, base_url, page_sub))
    return out



ASPX_SEL_RE = re.compile(
    r"<select[^>]*name=[\"']([^\"']+)[\"'][^>]*>(.*?)</select>", re.S | re.I)
ASPX_OPT_RE = re.compile(
    r"<option[^>]*value=[\"']([^\"']*)[\"'][^>]*>(.*?)</option>", re.S | re.I)

def extract_aspx_postback(html, base_url, page_sub=False):
    """ASP.NET WebForms pages (Janata PMIS): the visible table is empty and a
    <select> ('All Branches') postback loads every row. Replay the postback
    with the page's own ViewState and reuse the table extractor."""
    if "__doPostBack" not in html:
        return []
    best = None
    for sel_name, body in ASPX_SEL_RE.findall(html):
        for val, label in ASPX_OPT_RE.findall(body):
            lab = re.sub(r"<[^>]+>", " ", label).strip().lower()
            if ("all" in lab and ("branch" in lab or "office" in lab)) or lab == "all":
                best = (sel_name, val)
                break
        if best:
            break
    if not best:
        return []
    sel_name, val = best
    data = {}
    for f in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
              "__EVENTTARGET", "__EVENTARGUMENT"):
        m = re.search(rf"name=[\"']{re.escape(f)}[\"'][^>]*value=[\"']([^\"']*)[\"']", html)
        data[f] = m.group(1) if m else ""
    data[sel_name] = val
    data["__EVENTTARGET"] = sel_name
    try:
        r = requests.post(base_url, data=data, timeout=90, headers=HEADERS)
    except Exception:
        return []
    if r.status_code != 200 or len(r.text) <= len(html) + 500:
        return []  # postback didn't expand the table
    # the filter <select> lives inside the table; drop it so the first row
    # of the data table is the real header row
    rtext = re.sub(r"<select.*?</select>", " ", r.text, flags=re.S | re.I)
    return extract_html_tables(rtext, base_url, page_sub)

def dedupe_records(records):
    seen, out = {}, []

    def key(r):
        n = re.sub(r"[^a-z0-9]", "", (r.get("name") or "").lower())
        a = re.sub(r"[^a-z0-9]", "", (r.get("address") or "").lower())[:40]
        return f"{n}|{a}"

    for r in records:
        nm = (r.get("name") or "").strip()
        nm = re.sub(r"<br\s*/?>", " ", nm, flags=re.I)
        r["name"] = nm
        addr = (r.get("address") or "").strip()
        if not nm and addr:
            # gov-portal blobs bury the branch name inside the address text
            m = (re.search(r"([A-Za-z][^,]{1,44}?)\s+[Bb]ranch\b", addr)
                 or re.search(r"([\u0980-\u09ff][^,]{1,44}?)\s*শাখা", addr))
            if m:
                base = m.group(1).strip()
                nm = f"{base} শাখা" if BENGALI_RE.search(base) else f"{base} Branch"
                r["name"] = nm
        if (ATM_NAME_RE.search(nm)                     # ATM/CDM booth row
                or NEWS_NAME_RE.search(nm)             # press-release headlines
                or GENERIC_NAME_RE.match(nm.rstrip(" :;,.|-"))  # header/label junk
                or ADDR_NAME_RE.search(nm)             # address fragments
                or PROMO_NAME_RE.search(nm)            # offers / "Branch List"
                or re.fullmatch(r"[\d\W]+", nm)        # "9", "#12", "-"
                or not nm):                            # nothing salvageable
            continue
        k = key(r)
        if k == "|":
            continue
        if k in seen:
            ex = seen[k]
            for f in ("address", "phone", "url"):
                if not ex.get(f) and r.get(f):
                    ex[f] = r[f]
            continue
        seen[k] = r
        out.append(r)
    # bn/ translated pages duplicate the en network with Bengali names that
    # key() can never match — drop PURELY Bengali names when Latin names
    # dominate (mixed "English বাংলা" names and Bengali-only sites keep rows).
    def _has_latin(r):
        nm = r.get("name") or ""
        return bool(re.search(r"[a-z]", nm, re.I)) or not BENGALI_RE.search(nm)

    latin = [r for r in out if _has_latin(r)]
    if len(latin) >= 10:
        out = latin
    for r in out:
        addr = (r.get("address") or "").strip()
        # JSON feeds (MTB admin-ajax) ship raw "<br>" separators
        addr = re.sub(r"<br\s*/?>", " ", addr, flags=re.I)
        # labelled phone segments ("Phone: 88 02-8613807", "Cell: 01678 433
        # 101") ride along in address text - move them to the phone field
        # when empty, drop them when the digits were already captured there
        m = re.search(r"\b(?:phone|tel|mobile|pabx|cell)\w*\s*[:\-]\s*"
                      r"([0-9+()\[\]/.,\s-]{6,60})", addr, re.I)
        if m and len(re.sub(r"\D", "", m.group(1))) >= 6:
            if not (r.get("phone") or "").strip():
                r["phone"] = clean_ws(m.group(1)).strip(" ,.-")
            addr = addr[:m.start()] + " " + addr[m.end():]
        if GENERIC_ADDR_RE.match(addr) or re.fullmatch(r"[\d\W]+", addr):
            addr = ""
        # "Division: Dhaka" label-cards carry only the phone — treat as empty
        if re.match(r"^\s*(division|district|zone|region)\s*[:\-]", addr, re.I):
            addr = ""
        # card layouts paste the field label into the value ("Address: 29
        # Dilkusha…") — keep the value only
        addr = re.sub(r"^\s*(?:address|addr|location)\s*[:\-]\s*", "", addr, flags=re.I)
        # cloudflare-style obfuscated emails leak into card text
        addr = re.sub(r"\[?\s*e?mail\s*protected\s*\]?", "", addr, flags=re.I)
        # manager/branch emails pasted into address cells (BKB, RAKUB)
        addr = re.sub(r"[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*,?\s*", "", addr)
        # dangling field labels with nothing left behind ("... C/A, Email:",
        # "... Jessore. Phone:") — strip them so they don't poison the
        # name+address comparisons below
        addr = re.sub(r"[,\s;]*\b(?:e?mail|phone|tel|mobile|pabx|cell)\w*\s*[:\-]?\s*$",
                      "", addr, flags=re.I)
        r["address"] = clean_ws(addr)
    # one listing rendered twice (card + table views of the same fragment)
    # leaves two rows per name — keep the richer address/phone when the
    # addresses agree (one empty, one contains the other, or merely
    # formatted differently: trigram similarity catches "1st"/"Ist" and
    # spacing/case variants like the BCBL table vs its admin-ajax feed)
    def _tri(s):
        s = re.sub(r"[^a-z0-9]", "", s.lower())
        return {s[i:i + 3] for i in range(len(s) - 2)} or {s}

    def _sim(a, b):
        # trigram similarity catches "1st"/"Ist" and spacing/case variants;
        # token-set similarity catches the same fields in a different order
        # ("Hossain Tower,75 Greenroad" vs "75 Greenroad Hossain Tower")
        A, B = _tri(a), _tri(b)
        ta = {t for t in re.split(r"[^a-z0-9]+", a.lower()) if t}
        tb = {t for t in re.split(r"[^a-z0-9]+", b.lower()) if t}
        return max(len(A & B) / max(1, len(A | B)),
                   len(ta & tb) / max(1, len(ta | tb)))

    def _lev_ratio(a, b):
        # scaled edit distance — catches same-outlet spelling variants
        # ("Zigatoal"/"Zigatola", "Daulatpur"/"Daulotpur") that n-grams can't
        if abs(len(a) - len(b)) > 6:
            return 0.0
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[-1] + 1,
                               prev[j - 1] + (ca != cb)))
            prev = cur
        return 1 - prev[-1] / max(1, max(len(a), len(b)))

    def _tokset(s):
        return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 2}

    def _fuzzy_subset(ta, tb):
        # the ajax feed abbreviates table addresses ("Puraton Bandura
        # Nawabgonj, Dhaka" for the full holding number) — count a token as
        # matched on an exact or near-spelling basis ("nawabgonj" ~
        # "nawabganj", "batajor" ~ "batajore")
        small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if not small:
            return True
        hit = sum(1 for t in small
                  if t in big or any(len(t) >= 5 and len(u) >= 5
                                     and _lev_ratio(t, u) >= 0.8 for u in big))
        return hit / len(small) >= 0.7

    def _addr_compat(a, b):
        ea = (a.get("address") or "").lower()
        ra = (b.get("address") or "").lower()
        if not ea or not ra or ea in ra or ra in ea:
            return True
        if ea and ra and _sim(ea, ra) >= 0.55:
            return True
        return bool(ea and ra and _fuzzy_subset(_tokset(ea), _tokset(ra)))

    byname, keep = {}, []
    for r in out:
        nk = re.sub(r"[^a-z0-9]", "", (r.get("name") or "").lower())
        if nk and nk in byname:
            ex = byname[nk]
            if _addr_compat(ex, r):
                if len((r.get("address") or "")) > len((ex.get("address") or "")):
                    ex["address"] = r.get("address")
                if not ex.get("phone") and r.get("phone"):
                    ex["phone"] = r["phone"]
                continue
        keep.append(r)
        if nk:
            byname[nk] = r
    # last chance: the bank itself spells one outlet differently across its
    # own pages ("Zigatoal Branch" table vs "ZIGATOLA BRANCH" feed, or the
    # feed's district-suffixed "Lohagara Branch,Chittagonj"). Merge
    # near-identical names only when the outlets agree on classification,
    # digits ("Mirpur" != "Mirpur 10") and address; keep the richer record.
    def _nk(r):
        return re.sub(r"[^a-z0-9]", "", (r.get("name") or "").lower())

    merged = True
    while merged:
        merged = False
        for i in range(len(keep)):
            a = keep[i]
            na = _nk(a)
            if not na:
                continue
            for j in range(i + 1, len(keep)):
                b = keep[j]
                nb = _nk(b)
                if not nb:
                    continue
                if (a.get("is_sub") != b.get("is_sub")
                        or re.findall(r"\d", na) != re.findall(r"\d", nb)):
                    continue
                close = _lev_ratio(na, nb) >= 0.8
                prefix = (na.startswith(nb) or nb.startswith(na)) and \
                    min(len(na), len(nb)) >= 0.5 * max(len(na), len(nb))
                if not (close or prefix) or not _addr_compat(a, b):
                    continue
                if len(b.get("address") or "") > len(a.get("address") or ""):
                    a, b = b, a  # a survives: the record with the richer data
                for f in ("phone", "url"):
                    if not a.get(f) and b.get(f):
                        a[f] = b[f]
                keep.remove(b)
                merged = True
                break
            if merged:
                break
    return keep

def bank_result_template():
    # schema: bank_url, method, status + grouped records:
    #   branch / sub_branch: [{"<name>": [address, url, contact_number]}, ...]
    return {"bank_url": None, "method": None, "status": "failed",
            "branch": [], "sub_branch": []}

def build_result(result, records, methods):
    for r in records:
        p = "sub_branch" if r.get("is_sub") else "branch"
        result[p].append({(r.get("name") or ""): [
            r.get("address") or None,
            r.get("url") or None,
            r.get("phone") or None]})
    result["method"] = "+".join(sorted(methods)) if methods else None

EXTRACTORS = [
    (extract_script_json, "json_api"),
    (extract_jquery_ajax_json, "jquery_ajax"),
    (extract_validator_key_feeds, "validator_key_ajax"),
    (extract_html_tables, "html_table"),
    (extract_html_cards, "html_cards"),
    (extract_map_infowindows, "map_infowindow"),
]

def run_extractors(html, md, page, page_sub, src, records, methods):
    """Run the full extraction chain on one fetched page; mutates records and
    the methods counter dict. Returns the winning method name, or None."""
    method = None
    if html:
        for fn, mname in EXTRACTORS:
            try:
                got = fn(html, page, page_sub)
            except Exception as e:
                LOG.debug("    %s failed: %s", mname, e)
                got = []
            if got:
                records.extend(got)
                methods[mname] = methods.get(mname, 0) + len(got)
                LOG.info("    %s -> %d records (%s)", mname, len(got), src)
                named = sum(1 for r in got if (r.get("name") or "").strip())
                if named >= 3:  # tiny/nameless yields keep deeper extractors running
                    method = mname
                    break
    if method is None and html:
        # DataTables serverSide shells (Sonali): rows only exist via ajax
        try:
            got = extract_datatables(html, page, page_sub)
        except Exception as e:
            LOG.debug("    datatables failed: %s", e)
            got = []
        if got:
            records.extend(got)
            methods["datatables_json"] = methods.get("datatables_json", 0) + len(got)
            method = "datatables_json"
            LOG.info("    datatables_json -> %d records (%s)", len(got), src)
    if method is None and html:
        # ASP.NET WebForms shells (Janata PMIS): 'All Branches' postback
        try:
            got = extract_aspx_postback(html, page, page_sub)
        except Exception as e:
            LOG.debug("    aspx postback failed: %s", e)
            got = []
        if got:
            records.extend(got)
            methods["aspx_postback"] = methods.get("aspx_postback", 0) + len(got)
            method = "aspx_postback"
            LOG.info("    aspx_postback -> %d records (%s)", len(got), src)
    if method is None and md:
        try:
            got = parse_markdown_records(md, page, page_sub)
        except Exception as e:
            LOG.debug("    markdown failed: %s", e)
            got = []
        if got:
            records.extend(got)
            methods["markdown"] = methods.get("markdown", 0) + len(got)
            method = "markdown"
            LOG.info("    markdown -> %d records (%s)", len(got), src)
    return method

def record_sig(r):
    return ((r.get("name") or "").strip().lower(),
            (r.get("address") or "").strip().lower())

def follow_pagination(base_url, page_sub, records, methods, seen):
    """Bounded '?page=N' auto-pagination for card-grid locators (Next.js etc.):
    fetch consecutive pages locally while each contributes NEW records; stop at
    the first page that adds nothing (also covers redirect-to-canonical, 404s
    and end-of-list). Firecrawl is never used for these probes."""
    base = base_url.split("#", 1)[0]
    for n in range(2, 27):
        sep = "&" if up.urlsplit(base).query else "?"
        nxt = f"{base}{sep}page={n}"
        polite_sleep()
        resp = http_get(nxt, timeout=40, retries=1)
        if resp is None or resp.status_code != 200:
            return
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return
        before = len(records)
        run_extractors(resp.text, None, nxt, page_sub, "local", records, methods)
        new = {record_sig(r) for r in records[before:]}
        if not new or new <= seen:
            return
        seen |= new
        LOG.info("    pagination: page %d -> %d more records", n, len(new))

def process_bank(bank):
    result = bank_result_template()
    site, source = find_website(bank)
    result["bank_url"] = site
    if not site:
        result["error"] = "official website not found"
        return result
    LOG.info("  website: %s (%s)", site, source)
    # banks.csv seeds are trusted endpoints: skip the (often global and huge)
    # sitemap crawl that burned 13+ minutes on sc.com
    csv_pages = CSV_HINTS.get(bank, {}).get("branch_pages") or []
    pages, pdfs = find_branch_pages(site, bank, skip_sitemap=bool(csv_pages))
    if csv_pages:
        seed_pages = [s for s in csv_pages if not s.lower().endswith(".pdf")]
        seed_pdfs = [s for s in csv_pages if s.lower().endswith(".pdf")]
        pages = seed_pages + [p for p in pages if p not in seed_pages]
        pdfs = seed_pdfs + [p for p in pdfs if p not in seed_pdfs]
    LOG.info("  %d candidate pages, %d pdf candidates", len(pages), len(pdfs))
    records, methods, extra_pdfs = [], {}, []
    for i, page in enumerate(pages, 1):
        if non_branch_page(page):
            LOG.debug("    skipping non-branch listing page: %s", page)
            continue
        polite_sleep()
        LOG.info("  page %d/%d: %s", i, len(pages), page)
        page_start = len(records)
        page_sub = bool(re.search(r"sub[\s_+~-]*branch|up[oa][\s_+~-]*sha[\s_+~-]*kha|উপ\s*শাখা", page, re.I))
        html, md, src = smart_fetch(page)
        if html is None and md is None:
            LOG.warning("    fetch failed (%s)", src)
            continue
        method = run_extractors(html, md, page, page_sub, src, records, methods)
        local_named = sum(1 for r in records[page_start:]
                          if (r.get("name") or "").strip())
        if (method is None and local_named == 0 and src == "local"
                and FC["enabled"] and FC["key"]):
            # JS-rendered locators (Standard Chartered): the server HTML is a
            # fat shell that extracts nothing; pay 1 credit for a real browser
            # render and re-run the chain on it. Pages that already produced
            # named records locally (per-branch pages, Mutual Trust) are NOT
            # re-rendered — that was pure credit burn with no new records.
            LOG.info("    no solid records locally; firecrawl re-render")
            polite_sleep()
            d = fc_scrape(page)
            if d:
                method = run_extractors(d["html"], d["markdown"], page,
                                        page_sub, "firecrawl", records, methods)
        if method is not None and "page=" not in (up.urlsplit(page).query or ""):
            # card-grid locators (Next.js etc.) serve a handful of rows per
            # ?page=N: walk consecutive pages while they add NEW records
            seen = {record_sig(r) for r in records[page_start:]}
            if seen:
                follow_pagination(page, page_sub, records, methods, seen)
        if method is None:
            LOG.info("    no solid records (%s); scanning for pdf/iframe links", src)
            if html:
                soup = make_soup(html)
                bank_tokens = {w for w in re.split(r"[^a-z]+", (bank or "").lower())
                               if len(w) > 3 and w not in STOP_TOKENS}
                for a in soup.find_all("a", href=True):
                    href = absolutize(a["href"], page)
                    if (href.lower().endswith(".pdf")
                            and (domain_of(href) == domain_of(site)
                                 or any(t in href.lower() for t in bank_tokens))
                            and score_branch_url(href, a.get_text(" ", strip=True)) >= 2.0
                            and href not in pdfs and href not in extra_pdfs):
                        extra_pdfs.append(href)
                # branch lists often live inside an <iframe> (e.g. Sonali)
                for ifr in soup.find_all("iframe", src=True):
                    u = absolutize(ifr["src"], page)
                    dom = domain_of(u)
                    if (u not in pages
                            and (dom == domain_of(site)
                                 or any(t in dom for t in bank_tokens))
                            and score_branch_url(u) >= 1.2 and len(pages) < 30):
                        pages.append(u)
                        LOG.info("    + iframe candidate: %s", u)
    for purl in (pdfs + extra_pdfs)[:6]:
        polite_sleep()
        got = extract_pdf(purl)
        if got:
            records.extend(got)
            methods["pdf"] = methods.get("pdf", 0) + len(got)
            LOG.info("    pdf -> %d records", len(got))
    records = dedupe_records(records)
    build_result(result, records, methods)
    result["status"] = "success" if records else "failed"
    if not records:
        result["error"] = "no branch data extracted (site may need JS rendering)"
    return result

CSV_HINTS = {}  # filled by load_banks(): bank name -> {"website", "branch_pages"}
BANKS = []      # filled in main(): every bank in banks.csv (merge scope)

def load_banks():
    p = Path(ARGS.csv)
    if not p.exists():
        p = BASE_DIR / "banks.csv"
    banks = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            # normalize header keys: strip padding spaces (" bank_url"), lower-case
            row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
            vals = list(row.values())
            name = clean_ws(row.get("bank_name") or (vals[0] if vals else ""))
            if name and name.lower() != "bank_name":
                banks.append(name)
                # optional user-supplied hints (highest priority everywhere):
                #   website / bank_url  -> official site, skips search engines
                #   branch_pages        -> ';'-separated listing page/PDF URLs
                #   branch_page_url     -> branch listing page / PDF URL
                #   sub_branch_page_url -> sub-branch listing page / PDF URL
                # page hints are seeded directly; sitemap crawl is skipped
                hint = {}
                ws = clean_ws(row.get("website") or row.get("bank_url") or "")
                pages = []
                for col in ("branch_pages", "branch_page_url", "sub_branch_page_url"):
                    for u in re.split(r"[;|]", clean_ws(row.get(col) or "")):
                        u = u.strip()
                        if u and u not in pages:
                            pages.append(u)
                if ws:
                    hint["website"] = ws
                if pages:
                    hint["branch_pages"] = pages
                if hint:
                    CSV_HINTS[name] = hint
    return banks

def write_part(bank, result):
    path = PARTS_DIR / f"{slugify(bank)}.json"
    if result.get("status") != "success" and path.exists():
        # rerun safety: never let a failed re-scrape (site down, structure
        # change, network error) clobber a previously successful part
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if prev.get("status") == "success":
                LOG.warning("  keeping previous successful part (new run failed): %s", bank)
                return
        except Exception:
            pass
    payload = {"_bank": bank}
    payload.update(result)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")

def merge_and_write_final():
    merged = {}
    keep = {slugify(b) for b in BANKS}  # banks currently in banks.csv
    for part in sorted(PARTS_DIR.glob("*.json")):
        try:
            r = json.loads(part.read_text(encoding="utf-8"))
        except Exception:
            continue
        if keep and part.stem not in keep:
            LOG.debug("dropping stale part (bank not in banks.csv): %s", part.stem)
            continue
        bank = r.pop("_bank", part.stem)
        merged[bank] = r
    out = Path(ARGS.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged

def main():
    global ARGS
    ap = argparse.ArgumentParser(description="Scrape Bangladesh bank branches & sub-branches.")
    ap.add_argument("--csv", default=str(BASE_DIR / "banks.csv"))
    ap.add_argument("--out", default=str(OUT_DIR / "banks_branches.json"))
    ap.add_argument("--bank", help="process a single bank (substring match)")
    ap.add_argument("--limit", type=int, default=0, help="only first N banks")
    ap.add_argument("--resume", action="store_true", help="skip banks already scraped OK")
    ap.add_argument("--force", action="store_true", help="re-scrape even if part exists")
    ap.add_argument("--api-key", default=os.environ.get("FIRECRAWL_API_KEY", ""))
    ap.add_argument("--no-firecrawl", action="store_true", help="disable firecrawl entirely")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--delay", type=float, default=1.2, help="polite delay between pages (s)")
    ARGS = ap.parse_args()
    setup_logging()
    FC["enabled"] = not ARGS.no_firecrawl
    key_file = BASE_DIR / ".firecrawl_key"
    if not ARGS.api_key and key_file.exists():
        ARGS.api_key = key_file.read_text(encoding="utf-8").strip()
    FC["key"] = (ARGS.api_key or "").strip()
    LOG.info("firecrawl: %s", "key set" if FC["key"] else "keyless mode (low rate limits)")
    banks = load_banks()
    BANKS[:] = banks  # merge scope = full CSV list (before --bank/--limit filters)
    if ARGS.bank:
        banks = [b for b in banks if ARGS.bank.lower() in b.lower()]
    if ARGS.limit:
        banks = banks[:ARGS.limit]
    if not banks:
        LOG.error("no banks matched")
        return 1
    LOG.info("processing %d bank(s)", len(banks))
    final_path = Path(ARGS.out)
    if final_path.exists():
        try:  # keep the pre-run merged JSON as a rollback safety net
            shutil.copyfile(final_path, final_path.with_suffix(".json.bak"))
        except Exception:
            pass
    stats = {"success": 0, "failed": 0, "skipped": 0, "branches": 0, "sub_branches": 0}
    for bank in banks:
        part_path = PARTS_DIR / f"{slugify(bank)}.json"
        if ARGS.resume and part_path.exists() and not ARGS.force:
            try:
                prev = json.loads(part_path.read_text(encoding="utf-8"))
                if prev.get("status") == "success":
                    LOG.info("SKIP (resume): %s", bank)
                    stats["skipped"] += 1
                    stats["branches"] += len(prev.get("branch", []))
                    stats["sub_branches"] += len(prev.get("sub_branch", []))
                    continue
            except Exception:
                pass
        LOG.info("=" * 72)
        LOG.info("BANK: %s", bank)
        t0 = time.time()
        try:
            result = process_bank(bank)
        except Exception:
            LOG.exception("unexpected error on %s", bank)
            result = bank_result_template()
            result["error"] = "unexpected exception (see scrape.log)"
        write_part(bank, result)
        nb = len(result.get("branch", []))
        ns = len(result.get("sub_branch", []))
        stats["success" if result["status"] == "success" else "failed"] += 1
        stats["branches"] += nb
        stats["sub_branches"] += ns
        LOG.info("DONE %s: %s | branches=%d subs=%d method=%s (%.1fs)",
                 bank, result["status"], nb, ns, result.get("method"),
                 time.time() - t0)
        merge_and_write_final()
    merge_and_write_final()
    LOG.info("SUMMARY: %s", json.dumps(stats))
    return 0

if __name__ == "__main__":
    sys.exit(main())