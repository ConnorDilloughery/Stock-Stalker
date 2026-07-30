#!/usr/bin/env python3
"""
Undercurrent Congress Scanner — runs on the Pi hourly (via
congress-scanner.timer), pulls newly-filed trading disclosures from two
official government sources, and pushes the results into the same
GitHub repo the web app is deployed from — same pattern as
market_scanner.py, just on a scheduled cadence instead of a continuous
loop. Hourly doesn't mean trades show up within an hour of happening —
the STOCK Act itself allows up to 45 days between a trade and its
public filing — it just means we're never more than ~an hour behind
whatever the government has actually posted. A simple PID-file lock
(see acquire_lock()) keeps an hourly run from overlapping a previous
run that's still deep in a long OCR pass.

Zero subscriptions, zero paid APIs. Three sources, three very
different formats:

  1. U.S. SENATE — efdsearch.senate.gov's Periodic Transaction Reports
     (PTRs), filed electronically as clean, parseable HTML. High
     confidence: ticker, date, type, and amount range all come straight
     from structured markup.

  2. U.S. HOUSE — disclosures-clerk.house.gov's PTRs, filed as PDFs,
     but (unlike the President's) almost always real digital text, not
     scans — tickers appear right in the asset description in
     parentheses, e.g. "Apple Inc. - Common Stock (AAPL)", which a
     simple regex extracts reliably and which naturally skips bonds/
     treasuries (their parenthesized code is a 9-character CUSIP like
     "91282CGH8", not 1-6 uppercase letters, so it just doesn't match).
     Transaction type is a clean single-letter code (P/S/E) and dates
     are real, not OCR-guessed — this is nearly as high-confidence as
     the Senate. pdfplumber's extract_tables() pulls real table rows
     out of these instead of the free-text regex approach the
     President's filings need.

  3. THE PRESIDENT — whitehouse.gov proactively posts President
     Trump's PTRs (same STOCK Act requirement, filed on OGE Form 278-T)
     as PDFs. These are meaningfully lower-confidence: many are pure
     scanned images, so pages with no embedded text layer are run
     through Tesseract OCR (rendered at 200dpi via pdfplumber's
     pypdfium2 backend — no extra system dependency beyond the
     tesseract-ocr binary itself). Even pages with a real text layer
     are often themselves noisy OCR baked in by whoever produced the
     PDF (e.g. "Office" reads as "Ollico") — our own OCR pass on image
     pages is no less noisy. Most line items are municipal/corporate
     bonds identified by free-text description, not ticker — those are
     silently skipped, since there is no reliable way to map a bond
     description to a "ticker." Only lines that confidently match a
     known large-cap company name (see TICKER_NAME_MAP) are kept, and
     only ticker + amount are trusted enough to report — transaction
     dates in these PDFs are frequently mangled (e.g. "3/212028" for
     what should be "3/21/2026"), so Trump entries never carry a
     transactionDate, only the PTR's filed date (which comes from the
     reliable filename, not OCR). OCR also sometimes splits a line's
     amount onto a different visual line than its description — when
     that happens the row simply doesn't match and is dropped, so OCR
     recall is imperfect (some real trades get missed), but precision
     stays high (whatever does match is a genuine ticker match).

WHAT IT DOES
1. Senate: bootstraps a session against efdsearch.senate.gov (accepts
   the required "prohibition on use" agreement), queries for PTRs
   filed since the last run, and parses each one's transaction table.
2. House: submits a blank member search (just a FilingYear) against
   disclosures-clerk.house.gov, filters results to "PTR" filing types,
   downloads each new PDF, and extracts real table rows out of it.
   Checks the current calendar year every run, plus the previous year
   (cheap once backfilled — old years stop producing new filings once
   the year ends, so seen_ids makes repeat checks near-instant).
3. President: fetches whitehouse.gov/disclosures/, finds new Trump PTR
   PDF links, downloads each one, text-extracts every page (OCR'ing
   any page with no embedded text layer), and regex-parses transaction
   lines, keeping only ones that match a recognized ticker.
4. Merges everything into congress.json's existing history (deduped by
   PTR id / PDF url, so a rerun never double-counts), rebuilds the
   per-politician view, and commits + pushes — same as the stock
   scanner, so Vercel redeploys and the app's raw.githubusercontent.com
   fetch picks it up immediately. Checkpoints (writes + pushes) after
   the Senate pass, every ~25 House filings, and after every OCR-heavy
   Trump filing — not just once at the very end. A 100+ page scanned
   Trump filing can take 20+ minutes on Pi hardware, so an interruption
   partway through only costs the filing/batch in progress, not the
   whole run. Individual fast House filings aren't checkpointed one at
   a time (not enough work at risk each to justify an extra Vercel
   build), but batches of ~25 are.

SETUP (one-time)
  1. pip install requests pdfplumber pytesseract --break-system-packages
  2. Install the Tesseract OCR engine itself (a system binary, not a
     Python package): sudo apt-get install tesseract-ocr
  3. Reuses the same REPO_DIR env var as market_scanner.py (see
     scanner.env / undercurrent-scanner.service) — no new API key
     needed for either source.
  4. Run once manually to sanity-check: python3 congress_scanner.py
  5. Install congress-scanner.service + congress-scanner.timer for a
     daily automated run.

HONEST LIMITATIONS
  - PTRs report transaction amounts as ranges, not exact dollar
    amounts or share counts, by law. "Portfolio" here means observed
    buy/sell history per ticker, not an authoritative current holdings
    snapshot with real position sizes.
  - Senate and House data are both high-confidence (structured HTML /
    real PDF tables). President data is lower-confidence (OCR'd PDFs
    either way): bonds and unrecognized company names are silently
    dropped rather than guessed at, transaction dates are omitted
    entirely (only the reliable filed-date is kept), and OCR misses
    some real trades whose amount got split onto a different line than
    expected — it never reports a wrong ticker or amount, only
    sometimes reports fewer than actually exist. OCR is also slow
    (~1-3s/page): a filing with 100+ scanned pages can take several
    minutes, which is fine for a background run but worth knowing if
    running manually.
  - House filings occasionally have a row split awkwardly by
    pdfplumber's table detection (a detail/comment line bleeding into
    the row above it) — rows missing a valid date or amount are
    skipped rather than guessed at, same "skip over guess" philosophy
    as the rest of this script.
  - efdsearch.senate.gov's search backend is occasionally flaky
    (intermittent 503 "Site Under Maintenance" responses). This script
    retries with backoff and logs a warning rather than crashing the
    whole run if that happens.
  - The parsers expect each source's current page/PDF shape. If either
    site redesigns, parsing will start failing for new filings; the
    script logs warnings rather than dying, but you'll want to check
    what changed.
"""

import os
import re
import sys
import json
import time
import hashlib
import logging
import subprocess

import scanner_git
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

import requests

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

REPO_DIR = os.environ.get("REPO_DIR", "").strip()

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_TRADES_KEPT = 5000  # rolling window so congress.json doesn't grow forever

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "congress_scanner.lock")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("congress_scanner")


# ----------------------------------------------------------------------
# SENATE — efdsearch.senate.gov
# ----------------------------------------------------------------------

SENATE_BASE = "https://efdsearch.senate.gov"
SENATE_SEARCH_PAGE = f"{SENATE_BASE}/search/"
SENATE_HOME_PAGE = f"{SENATE_BASE}/search/home/"
SENATE_DATA_URL = f"{SENATE_BASE}/search/report/data/"
SENATE_PAGE_SIZE = 100
SENATE_LOOKBACK_DAYS_FIRST_RUN = 90   # PTRs are due 30-45 days after the trade, so this
                                       # comfortably catches anything filed late
SENATE_LOOKBACK_BUFFER_DAYS = 5       # re-check a few days behind the last run in case
                                       # of late/backdated filings


class _TableParser(HTMLParser):
    """Extracts every <table> on a page as a list of rows of cell text.
    Deliberately simple (no nested-table handling) — efdsearch's PTR
    pages are plain government-generated HTML, not a complex app."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            if self._row is not None:
                self._row.append(text)
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_tables(html):
    p = _TableParser()
    p.feed(html)
    return p.tables


LINK_RE = re.compile(r'href="([^"]+)"')


def extract_href(cell_html):
    m = LINK_RE.search(cell_html)
    return m.group(1) if m else None


def senate_open_session():
    """Bootstraps a session by GETting the search page (redirects to the
    agreement page) and POSTing acceptance of the required 'prohibition
    on use' agreement — the same click-through a human does once."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    r = s.get(SENATE_SEARCH_PAGE, timeout=20)
    r.raise_for_status()
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("Couldn't find csrfmiddlewaretoken on the eFD home page — page layout may have changed")
    csrf = m.group(1)

    r = s.post(SENATE_HOME_PAGE, data={"csrfmiddlewaretoken": csrf, "prohibition_agreement": "1"},
               headers={"Referer": SENATE_HOME_PAGE}, timeout=20)
    r.raise_for_status()
    if "efd-search" not in r.text.lower() and "find reports" not in r.text.lower():
        log.warning("Agreement POST didn't land where expected — search calls below may fail")
    return s


def senate_fetch_ptr_page(session, start, submitted_start_date, retries=3):
    """One page of PTR search results. Retries on the intermittent 503
    'Site Under Maintenance' responses this endpoint occasionally
    returns, same spirit as market_scanner's rate-limit retry."""
    csrf = session.cookies.get("csrftoken")
    payload = {
        "draw": 1,
        "start": start,
        "length": SENATE_PAGE_SIZE,
        "report_types": "[11]",
        "filer_types": "[]",
        "submitted_start_date": submitted_start_date,
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
    }
    headers = {
        "X-CSRFToken": csrf or "",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SENATE_SEARCH_PAGE,
    }
    for attempt in range(1, retries + 1):
        resp = session.post(SENATE_DATA_URL, data=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                log.warning("PTR search returned 200 but non-JSON body — treating as a transient failure")
        else:
            log.warning(f"PTR search returned HTTP {resp.status_code} (attempt {attempt}/{retries})")
        time.sleep(15 * attempt)
    raise RuntimeError(f"PTR search kept failing after {retries} attempts")


def senate_list_new_filings(session, submitted_start_date):
    """Paginates through every PTR filed on/after submitted_start_date,
    returning electronic filings only (paper/PDF ones are skipped)."""
    filings = []
    start = 0
    skipped_paper = 0
    total = None
    while total is None or start < total:
        data = senate_fetch_ptr_page(session, start, submitted_start_date)
        total = data.get("recordsFiltered", 0)
        rows = data.get("data", [])
        if not rows:
            break
        for row in rows:
            try:
                href = None
                filed_date = None
                for cell in row:
                    if href is None:
                        href = extract_href(cell)
                    if re.match(r"^\d{2}/\d{2}/\d{4}$", cell.strip()):
                        filed_date = cell.strip()
                if not href:
                    continue
                if "/search/view/ptr/" not in href:
                    skipped_paper += 1
                    continue
                ptr_id = href.strip("/").split("/")[-1]
                filings.append({
                    "ptrId": ptr_id,
                    "url": SENATE_BASE + href,
                    "firstName": row[0] if len(row) > 0 else "",
                    "lastName": row[1] if len(row) > 1 else "",
                    "office": row[2] if len(row) > 2 else "",
                    "filedDate": filed_date,
                })
            except Exception as e:
                log.warning(f"Couldn't parse a search-result row, skipping it: {e}")
        start += len(rows)
    if skipped_paper:
        log.info(f"Skipped {skipped_paper} paper/PDF Senate filings (not supported)")
    return filings


def senate_fetch_ptr_transactions(session, filing, retries=2):
    """Fetches one PTR's detail page and parses its transaction table."""
    for attempt in range(1, retries + 1):
        resp = session.get(filing["url"], headers={"Referer": SENATE_SEARCH_PAGE}, timeout=20)
        if resp.status_code == 200:
            break
        log.warning(f"PTR detail fetch for {filing['ptrId']} returned HTTP {resp.status_code} (attempt {attempt}/{retries})")
        time.sleep(5 * attempt)
    else:
        log.warning(f"Giving up on PTR {filing['ptrId']} after {retries} attempts")
        return []

    tables = parse_tables(resp.text)
    tx_table = None
    for t in tables:
        if not t:
            continue
        header = [c.lower() for c in t[0]]
        if any("ticker" in h for h in header):
            tx_table = t
            break
    if tx_table is None:
        return []  # normal for filings with only non-security transactions (real estate, etc.)

    header = [c.lower() for c in tx_table[0]]

    def col(name_fragment):
        for i, h in enumerate(header):
            if name_fragment in h:
                return i
        return None

    idx_date = col("transaction date") if col("transaction date") is not None else col("date")
    idx_owner = col("owner")
    idx_ticker = col("ticker")
    idx_asset = col("asset")
    idx_type = col("type")
    idx_amount = col("amount")

    out = []
    for row in tx_table[1:]:
        if idx_ticker is None or idx_ticker >= len(row):
            continue
        ticker = row[idx_ticker].strip().upper()
        if not ticker or ticker in ("--", "N/A"):
            continue
        out.append({
            "politician": f"{filing['firstName']} {filing['lastName']}".strip(),
            "office": filing["office"],
            "transactionDate": row[idx_date].strip() if idx_date is not None and idx_date < len(row) else None,
            "filedDate": filing["filedDate"],
            "owner": row[idx_owner].strip() if idx_owner is not None and idx_owner < len(row) else None,
            "ticker": ticker,
            "assetName": row[idx_asset].strip() if idx_asset is not None and idx_asset < len(row) else None,
            "type": row[idx_type].strip() if idx_type is not None and idx_type < len(row) else None,
            "amountRange": row[idx_amount].strip() if idx_amount is not None and idx_amount < len(row) else None,
            "sourceId": filing["ptrId"],
            "sourceUrl": filing["url"],
            "source": "senate",
        })
    return out


def scan_senate(seen_ids, last_filed_date):
    """Returns new Senate trades not already in seen_ids. Mutates
    seen_ids in place, adding each filing's ptrId once it's been
    attempted — so a filing with zero stock transactions (e.g. only
    real-estate) doesn't get needlessly re-fetched every day."""
    if last_filed_date:
        start_date = datetime.strptime(last_filed_date, "%m/%d/%Y") - timedelta(days=SENATE_LOOKBACK_BUFFER_DAYS)
    else:
        start_date = datetime.now(timezone.utc) - timedelta(days=SENATE_LOOKBACK_DAYS_FIRST_RUN)
    submitted_start_date = start_date.strftime("%m/%d/%Y")
    log.info(f"[senate] Checking for PTRs filed on/after {submitted_start_date}")

    try:
        session = senate_open_session()
        filings = senate_list_new_filings(session, submitted_start_date)
    except RuntimeError as e:
        log.error(f"[senate] Couldn't reach the Senate search backend: {e}")
        return []

    new_filings = [f for f in filings if f["ptrId"] not in seen_ids]
    log.info(f"[senate] Found {len(filings)} filings in the window, {len(new_filings)} not seen before")

    new_trades = []
    for i, filing in enumerate(new_filings):
        if i and i % 20 == 0:
            log.info(f"[senate] Fetched {i}/{len(new_filings)} new filings so far")
        try:
            new_trades.extend(senate_fetch_ptr_transactions(session, filing))
            seen_ids.add(filing["ptrId"])
        except Exception as e:
            log.warning(f"[senate] Failed to parse PTR {filing['ptrId']} for {filing['firstName']} {filing['lastName']}: {e}")
        time.sleep(1.5)  # be polite to a free public government service

    log.info(f"[senate] Parsed {len(new_trades)} new stock transactions")
    return new_trades


# ----------------------------------------------------------------------
# PRESIDENT — whitehouse.gov PDF disclosures
# ----------------------------------------------------------------------

WH_DISCLOSURES_URL = "https://www.whitehouse.gov/disclosures/"

# Only well-known large/mega-cap names + popular ETFs. Anything not in
# here (which is most lines — overwhelmingly municipal and corporate
# bonds identified by free-text description, not ticker) is silently
# skipped rather than guessed at. Matched as a substring against the
# OCR'd, uppercased transaction description.
TICKER_NAME_MAP = {
    "NVDA": ["NVIDIA CORP"], "MSFT": ["MICROSOFT CORP"], "AAPL": ["APPLE INC"],
    "AMZN": ["AMAZON.COM INC", "AMAZON COM INC"], "GOOGL": ["ALPHABET INC"],
    "META": ["META PLATFORMS"], "AVGO": ["BROADCOM INC"],
    "ORCL": ["ORACLE CORPORATION", "ORACLE CORP"], "CRM": ["SALESFORCE INC", "SALESFORCE COM"],
    "ADBE": ["ADOBE INC"], "NOW": ["SERVICENOW INC"], "INTU": ["INTUIT INC"],
    "IBM": ["INTERNATIONAL BUSINESS MACHINES"], "CSCO": ["CISCO SYSTEMS"],
    "QCOM": ["QUALCOMM INC"], "TXN": ["TEXAS INSTR"], "AMAT": ["APPLIED MATERIALS"],
    "MU": ["MICRON TECHNOLOGY"], "ADI": ["ANALOG DEVICES"], "INTC": ["INTEL CORP"],
    "AMD": ["ADVANCED MICRO DEVICES"], "PANW": ["PALO ALTO NETWORKS"],
    "SNPS": ["SYNOPSYS INC"], "CDNS": ["CADENCE DESIGN SYS"], "FTNT": ["FORTINET INC"],
    "ANET": ["ARISTA NETWORKS"], "MSI": ["MOTOROLA SOLUTIONS"], "WDAY": ["WORKDAY INC"],
    "DELL": ["DELL TECHNOLOGIES"], "CDW": ["CDW CORP"], "FIS": ["FIDELITY NATL INFORMATIO"],
    "JBL": ["JABIL INC"], "UBER": ["UBER TECHNOLOGIES"],
    "LLY": ["ELI LILLY"], "UNH": ["UNITEDHEALTH GROUP"], "JNJ": ["JOHNSON & JOHNSON"],
    "ABBV": ["ABBVIE INC"], "MRK": ["MERCK & CO"], "PFE": ["PFIZER INC"],
    "TMO": ["THERMO FISHER SCIENTIFIC"], "AMGN": ["AMGEN INC"], "GILD": ["GILEAD SCIENCES"],
    "CVS": ["CVS HEALTH"], "CI": ["CIGNA GROUP"], "ELV": ["ELEVANCE HEALTH"],
    "MDT": ["MEDTRONIC PLC"], "SYK": ["STRYKER CORP"], "BSX": ["BOSTON SCIENTIFIC"],
    "ZTS": ["ZOETIS INC"], "VRTX": ["VERTEX PHARMACEUTICALS"], "REGN": ["REGENERON PHARMACEUTICALS"],
    "ISRG": ["INTUITIVE SURGICAL"],
    "CAT": ["CATERPILLAR INC"], "HON": ["HONEYWELL INTERNATIONAL"], "UNP": ["UNION PACIFIC CORP"],
    "DE": ["DEERE & CO"], "GE": ["GENERAL ELECTRIC"], "LMT": ["LOCKHEED MARTIN"],
    "RTX": ["RTX CORP"], "UPS": ["UNITED PARCEL SERVICE"], "ETN": ["EATON CORP"],
    "ADP": ["AUTOMATIC DATA PROCESSING"], "CSX": ["CSX CORP"], "EMR": ["EMERSON ELECTRIC"],
    "BA": ["BOEING COMPANY"], "MMM": ["3M CO"], "NSC": ["NORFOLK SOUTHERN"],
    "TT": ["TRANE TECHNOLOGIES"], "TDG": ["TRANSDIGM GROUP"], "AXON": ["AXON ENTERPRISE"],
    "XOM": ["EXXON MOBIL"], "CVX": ["CHEVRON CORP"], "COP": ["CONOCOPHILLIPS"],
    "SLB": ["SCHLUMBERGER"], "EOG": ["EOG RESOURCES"],
    "WMT": ["WALMART INC"], "HD": ["HOME DEPOT"], "PG": ["PROCTER & GAMBLE", "PROCTER GAMBLE"],
    "KO": ["COCA-COLA CO", "COCA COLA CO"], "PEP": ["PEPSICO INC"], "COST": ["COSTCO WHOLESALE"],
    "MCD": ["MCDONALDS CORP"], "NKE": ["NIKE INC"], "SBUX": ["STARBUCKS CORP"],
    "TJX": ["TJX COMPANIES"], "LOW": ["LOWES COS"], "TGT": ["TARGET CORP"],
    "JPM": ["JPMORGAN CHASE"], "BAC": ["BANK OF AMERICA"], "WFC": ["WELLS FARGO"],
    "GS": ["GOLDMAN SACHS"], "MS": ["MORGAN STANLEY"], "SCHW": ["CHARLES SCHWAB"],
    "AXP": ["AMERICAN EXPRESS"], "C": ["CITIGROUP INC"], "BLK": ["BLACKROCK INC"],
    "V": ["VISA INC"], "MA": ["MASTERCARD INC"],
    "LIN": ["LINDE PLC"], "SHW": ["SHERWIN WILLIAMS"], "APD": ["AIR PRODUCTS"],
    "FCX": ["FREEPORT-MCMORAN", "FREEPORT MCMORAN"],
    "VOO": ["VANGUARD S&P 500 ETF"], "SPY": ["SPDR S&P 500"], "QQQ": ["INVESCO QQQ"],
    "VTI": ["VANGUARD TOTAL STOCK MARKET"], "IVV": ["ISHARES CORE S&P 500"],
}

AMOUNT_RE = re.compile(r'\$[\d\s.,]{3,}[-•·]\s*\$[\d\s.,]{3,}')
TYPE_TOKEN_RE = re.compile(r'\b(\S*(?:rch|chao|chuo|chose|sal|xcha)\S*)\b', re.IGNORECASE)
LINE_START_RE = re.compile(r'^\s*(\d{1,4})\D')


def wh_normalize_name(s):
    return re.sub(r'\s+', ' ', s.upper()).strip()


def wh_match_ticker(desc):
    d = wh_normalize_name(desc)
    for ticker, aliases in TICKER_NAME_MAP.items():
        for alias in aliases:
            if alias in d:
                return ticker
    return None


def wh_fuzzy_type(word):
    w = word.lower()
    if any(p in w for p in ("rch", "chao", "chuo", "chose")):
        return "Purchase"
    if "sal" in w:
        return "Sale"
    if "xcha" in w:
        return "Exchange"
    return None


def wh_normalize_amount(raw):
    """Cleans OCR noise like '$1 .000.001 • $5 000 000' into '$1,000,001 - $5,000,000'."""
    parts = re.split(r'[-•·]', raw, maxsplit=1)
    if len(parts) != 2:
        return raw.strip()
    cleaned = []
    for part in parts:
        digits = re.sub(r'[^\d]', '', part)
        if not digits:
            return raw.strip()
        cleaned.append(f"${int(digits):,}")
    return f"{cleaned[0]} - {cleaned[1]}"


def wh_parse_transactions_from_text(text):
    """Regex-parses transaction lines out of one page's extracted text.
    Only returns lines that match a recognized ticker (see
    TICKER_NAME_MAP) — everything else (bonds, unrecognized names) is
    silently dropped, never guessed at."""
    out = []
    for line in text.split("\n"):
        if not LINE_START_RE.match(line):
            continue
        amt_m = AMOUNT_RE.search(line)
        if not amt_m:
            continue
        before_amount = line[:amt_m.start()]
        type_m = TYPE_TOKEN_RE.search(before_amount)
        ttype = wh_fuzzy_type(type_m.group(1)) if type_m else None
        desc = before_amount[:type_m.start()] if type_m else before_amount
        ticker = wh_match_ticker(desc)
        if not ticker:
            continue
        out.append({
            "ticker": ticker,
            "type": ttype,
            "amountRange": wh_normalize_amount(amt_m.group(0)),
        })
    return out


def wh_fetch_trump_links():
    resp = requests.get(WH_DISCLOSURES_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    hrefs = set(re.findall(r'href="([^"]*Periodic-Transaction-Report[^"]*\.pdf)"', resp.text))
    links = []
    for href in hrefs:
        if "trump" not in href.lower():
            continue
        m = re.search(r'(\d{1,2})[.\-](\d{1,2})[.\-](\d{2,4})', href.split("/")[-1])
        filed_date = None
        if m:
            mo, day, yr = m.groups()
            yr = f"20{yr}" if len(yr) == 2 else yr
            try:
                filed_date = datetime(int(yr), int(mo), int(day)).strftime("%m/%d/%Y")
            except ValueError:
                pass
        links.append({"url": href, "filedDate": filed_date})
    return links


WH_OCR_RESOLUTION = 200  # dpi — good balance of accuracy vs. ~1-3s/page speed


def wh_extract_pdf_pages_text(pdf_bytes):
    """Returns list of per-page extracted text. Pages with a real
    embedded text layer are used directly; pages with no text layer at
    all (pure scanned images) are OCR'd via Tesseract instead of being
    skipped outright. Only pages where even OCR yields nothing usable
    are truly skipped. Returns (page_texts, ocr_page_count,
    truly_skipped_page_count)."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed — run: pip install pdfplumber --break-system-packages")
    import io
    texts = []
    ocr_count = 0
    skipped = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if len(txt) >= 50:
                texts.append(txt)
                continue
            if pytesseract is None:
                skipped += 1
                continue
            try:
                img = page.to_image(resolution=WH_OCR_RESOLUTION).original
                ocr_txt = pytesseract.image_to_string(img)
            except Exception:
                ocr_txt = ""
            if len(ocr_txt) >= 50:
                texts.append(ocr_txt)
                ocr_count += 1
            else:
                skipped += 1
    return texts, ocr_count, skipped


def scan_whitehouse_trump(seen_ids, on_filing_done=None):
    """on_filing_done(filing_trades), if given, is called after each
    filing that required OCR — those are the slow, at-risk ones (a
    113-page filing can take 20+ minutes on Pi hardware), so the caller
    can checkpoint (write + push) right after, protecting that work
    from an interruption (SSH drop, Pi reboot, script crash). Fast
    text-only filings aren't checkpointed individually — not enough
    work at risk to justify an extra git push (and extra push = extra
    Vercel build) for each one. Also mutates seen_ids in place, adding
    each filing's url once it's been attempted — so a filing with zero
    recognized stock trades (e.g. all bonds) doesn't get needlessly
    re-downloaded and re-OCR'd every day."""
    if pdfplumber is None:
        log.warning("[president] pdfplumber not installed, skipping this source entirely")
        return []
    if pytesseract is None:
        log.warning("[president] pytesseract not installed — scanned-image pages will be skipped instead of OCR'd")

    try:
        links = wh_fetch_trump_links()
    except Exception as e:
        log.error(f"[president] Couldn't reach whitehouse.gov/disclosures/: {e}")
        return []

    new_links = [l for l in links if l["url"] not in seen_ids]
    log.info(f"[president] Found {len(links)} Trump PTR PDFs listed, {len(new_links)} not seen before")

    new_trades = []
    for link in new_links:
        try:
            resp = requests.get(link["url"], headers={"User-Agent": USER_AGENT}, timeout=60)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"[president] Couldn't download {link['url']}: {e}")
            continue

        try:
            page_texts, ocr_pages, skipped_pages = wh_extract_pdf_pages_text(resp.content)
        except Exception as e:
            log.warning(f"[president] Couldn't extract text from {link['url']}: {e}")
            seen_ids.add(link["url"])  # a permanently broken/corrupt file — don't retry it forever
            continue

        if not page_texts:
            log.info(f"[president] {link['url']} yielded no usable text at all ({skipped_pages} pages, even after OCR) — skipping")
            seen_ids.add(link["url"])
            continue
        if ocr_pages:
            log.info(f"[president] {link['url']}: OCR'd {ocr_pages} scanned page(s)" + (f", {skipped_pages} still unreadable" if skipped_pages else ""))

        source_id = hashlib.sha1(link["url"].encode()).hexdigest()[:16]
        filing_trades = []
        for page_text in page_texts:
            for tx in wh_parse_transactions_from_text(page_text):
                filing_trades.append({
                    "politician": "Donald J. Trump",
                    "office": "President",
                    "transactionDate": None,  # too unreliable from OCR — see module docstring
                    "filedDate": link["filedDate"],
                    "owner": None,
                    "ticker": tx["ticker"],
                    "assetName": None,
                    "type": tx["type"],
                    "amountRange": tx["amountRange"],
                    "sourceId": source_id,
                    "sourceUrl": link["url"],
                    "source": "president",
                })
        log.info(f"[president] {link['url']}: {len(filing_trades)} recognized stock transactions found")
        new_trades.extend(filing_trades)
        seen_ids.add(link["url"])
        if on_filing_done and ocr_pages:
            on_filing_done(filing_trades)
        time.sleep(1)  # be polite

    log.info(f"[president] Parsed {len(new_trades)} new stock transactions total")
    return new_trades


# ----------------------------------------------------------------------
# HOUSE — disclosures-clerk.house.gov
# ----------------------------------------------------------------------

HOUSE_BASE = "https://disclosures-clerk.house.gov"
HOUSE_SEARCH_PAGE = f"{HOUSE_BASE}/FinancialDisclosure/ViewSearch"
HOUSE_RESULTS_URL = f"{HOUSE_BASE}/FinancialDisclosure/ViewMemberSearchResult"
HOUSE_PDF_BASE = f"{HOUSE_BASE}/"  # PDF links in results are relative, e.g. "public_disc/ptr-pdfs/2026/X.pdf"

HOUSE_TICKER_RE = re.compile(r'\(([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\)')  # naturally excludes CUSIPs (they contain digits)
HOUSE_ROW_LINK_RE = re.compile(r'href="([^"]+)"[^>]*>([^<]*)</a>')
HOUSE_TYPE_MAP = {"P": "Purchase", "S": "Sale", "E": "Exchange"}


def house_open_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    r = s.get(HOUSE_SEARCH_PAGE, timeout=20)
    r.raise_for_status()
    m = re.search(r'__RequestVerificationToken" type="hidden" value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("Couldn't find __RequestVerificationToken on the House search page — page layout may have changed")
    return s, m.group(1)


def house_fetch_filing_links(session, token, filing_year):
    """One blank member search (just a filing year) returns every
    filing for that year in one response — no pagination to walk."""
    resp = session.post(HOUSE_RESULTS_URL, data={
        "LastName": "", "FilingYear": str(filing_year), "State": "", "District": "",
        "__RequestVerificationToken": token,
    }, headers={"Referer": HOUSE_SEARCH_PAGE}, timeout=30)
    resp.raise_for_status()

    links = []
    for row_html in resp.text.split("<tr role=\"row\">")[1:]:
        if "PTR" not in row_html:
            continue  # skip Annual/Extension/Termination/New Filer — not trade data
        link_m = HOUSE_ROW_LINK_RE.search(row_html)
        if not link_m:
            continue
        href, name = link_m.groups()
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
        office = re.sub(r'<[^>]+>', '', cells[1]).strip() if len(cells) > 1 else ""
        links.append({
            "url": HOUSE_PDF_BASE + href.lstrip("/"),
            "name": " ".join(name.replace("Hon..", "").split()),
            "office": office,
            "filingYear": filing_year,
        })
    return links


def house_parse_pdf_transactions(pdf_bytes):
    """Uses pdfplumber's real table extraction (not free-text regex —
    House PTRs have genuine table structure) to pull transaction rows.
    Rows missing a valid date or amount (a detail/comment line that
    bled into the table) are skipped rather than guessed at."""
    if pdfplumber is None:
        return []
    import io
    date_re = re.compile(r'^\d{2}/\d{2}/\d{4}$')
    amount_re = re.compile(r'\$[\d,]+')
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if len(row) < 7 or not row[2] or not row[3] or not row[4] or not row[6]:
                        continue
                    date_str = row[4].strip()
                    if not date_re.match(date_str):
                        continue  # a spilled-over detail row, not a real transaction row
                    amount = amount_re.findall(row[6].replace("\n", " "))
                    if len(amount) < 2:
                        continue
                    asset_text = row[2].replace("\n", " ")
                    ticker_m = HOUSE_TICKER_RE.search(asset_text)
                    if not ticker_m:
                        continue  # bond/CUSIP or unrecognized format — skip, don't guess
                    type_code = row[3].strip().split()[0].rstrip(".")  # "S (partial)" -> "S"
                    out.append({
                        "ticker": ticker_m.group(1),
                        "type": HOUSE_TYPE_MAP.get(type_code, type_code),
                        "transactionDate": date_str,
                        "amountRange": f"{amount[0]} - {amount[1]}",
                    })
    return out


def scan_house(seen_ids, on_batch_done=None):
    """on_batch_done(batch_trades), if given, is called roughly every
    HOUSE_CHECKPOINT_EVERY filings — individual House filings are fast
    (no OCR needed for the vast majority), so checkpointing every
    single one would mean far too many git pushes; batching keeps the
    same crash-resilience benefit without the push spam."""
    if pdfplumber is None:
        log.warning("[house] pdfplumber not installed, skipping this source entirely")
        return []

    try:
        session, token = house_open_session()
    except Exception as e:
        log.error(f"[house] Couldn't reach disclosures-clerk.house.gov: {e}")
        return []

    this_year = datetime.now(timezone.utc).year
    all_links = []
    for year in (this_year, this_year - 1):
        try:
            all_links.extend(house_fetch_filing_links(session, token, year))
        except Exception as e:
            log.warning(f"[house] Couldn't fetch filing list for {year}: {e}")

    new_links = [l for l in all_links if l["url"] not in seen_ids]
    log.info(f"[house] Found {len(all_links)} PTR filings across {this_year-1}-{this_year}, {len(new_links)} not seen before")

    HOUSE_CHECKPOINT_EVERY = 25  # filings processed, not trades found — a filing can
                                  # contain zero or dozens of recognized trades
    new_trades = []
    batch = []
    filings_since_checkpoint = 0
    for i, link in enumerate(new_links):
        if i and i % 50 == 0:
            log.info(f"[house] Processed {i}/{len(new_links)} new filings so far")
        try:
            resp = requests.get(link["url"], headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            filing_trades_raw = house_parse_pdf_transactions(resp.content)
        except Exception as e:
            log.warning(f"[house] Couldn't process {link['url']}: {e}")
            continue

        filing_trades = [{
            "politician": link["name"],
            "office": link["office"],
            "transactionDate": tx["transactionDate"],
            "filedDate": None,  # not surfaced separately by this search; transactionDate is real here anyway
            "owner": None,
            "ticker": tx["ticker"],
            "assetName": None,
            "type": tx["type"],
            "amountRange": tx["amountRange"],
            "sourceId": link["url"],
            "sourceUrl": link["url"],
            "source": "house",
        } for tx in filing_trades_raw]

        new_trades.extend(filing_trades)
        batch.extend(filing_trades)
        seen_ids.add(link["url"])
        filings_since_checkpoint += 1
        time.sleep(0.5)  # be polite — this is a much larger set of filings than the other two sources

        if on_batch_done and filings_since_checkpoint >= HOUSE_CHECKPOINT_EVERY:
            on_batch_done(batch)
            batch = []
            filings_since_checkpoint = 0

    if on_batch_done and batch:
        on_batch_done(batch)

    log.info(f"[house] Parsed {len(new_trades)} new stock transactions total")
    return new_trades


# ----------------------------------------------------------------------
# Output shaping
# ----------------------------------------------------------------------

def _date_sort_key(date_str):
    """Congress data's dates are MM/DD/YYYY (Senate/House/whitehouse.gov
    filename convention), which is NOT lexicographically sortable as a
    plain string — "12/31/2024" and "12/31/2025" only happen to compare
    correctly because month and day match; "01/01/2026" vs "12/31/2020"
    would sort in the wrong order entirely, since a plain string
    comparison keys on month first, day second, year last. Converting
    to YYYY-MM-DD first makes string comparison equivalent to
    chronological comparison."""
    if not date_str:
        return ""
    try:
        mo, day, yr = date_str.split("/")
        return f"{yr}-{mo.zfill(2)}-{day.zfill(2)}"
    except (ValueError, AttributeError):
        return date_str


def build_politicians(trades):
    """Groups trades by politician -> ticker -> trade history. This is
    an observed buy/sell history, not a real position size — PTRs
    report dollar ranges, not exact holdings (see module docstring)."""
    politicians = {}
    for t in trades:
        p = politicians.setdefault(t["politician"], {"office": t["office"], "tickers": {}, "tradeCount": 0})
        p["tradeCount"] += 1
        tk = p["tickers"].setdefault(t["ticker"], {"trades": []})
        tk["trades"].append({
            "transactionDate": t["transactionDate"],
            "filedDate": t["filedDate"],
            "type": t["type"],
            "amountRange": t["amountRange"],
            "owner": t["owner"],
        })
    for p in politicians.values():
        for tk in p["tickers"].values():
            tk["trades"].sort(key=lambda x: _date_sort_key(x["transactionDate"] or x["filedDate"]), reverse=True)
            tk["lastAction"] = tk["trades"][0]["type"]
            tk["lastDate"] = tk["trades"][0]["transactionDate"] or tk["trades"][0]["filedDate"]
    return politicians


def load_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
        seen_ids = set(data.get("_meta", {}).get("seenIds", []))
        trades = data.get("trades", [])
        last_filed = data.get("_meta", {}).get("lastFiledDateSenate")
        log.info(f"Loaded {len(trades)} existing trades, {len(seen_ids)} known source ids from {path}")
        return trades, seen_ids, last_filed
    except Exception:
        return [], set(), None


def write_output(trades, seen_ids, path):
    trades = sorted(trades, key=lambda t: _date_sort_key(t.get("transactionDate") or t.get("filedDate")), reverse=True)
    trades = trades[:MAX_TRADES_KEPT]
    politicians = build_politicians(trades)
    last_filed_senate = max(
        (t["filedDate"] for t in trades if t.get("filedDate") and t["source"] == "senate"), default=None)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "U.S. Senate (efdsearch.senate.gov) + U.S. House (disclosures-clerk.house.gov) + President Trump's PTRs (whitehouse.gov)",
        "count": len(trades),
        "trades": trades,
        "politicians": politicians,
        "_meta": {"seenIds": sorted(seen_ids), "lastFiledDateSenate": last_filed_senate},
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"Wrote {len(trades)} trades across {len(politicians)} politicians to {path}")


GIT_SUBPROCESS_TIMEOUT = 60  # seconds — a hung `git push` (e.g. a stalled SSH
                              # connection to GitHub) would otherwise block the
                              # entire scan indefinitely, since a long OCR/House
                              # run may call this many times unattended


def git_commit_and_push(repo_dir, files):
    """Delegates to the shared, self-healing, lock-serialized publisher
    (scanner_git) so a race, crash, or dirty tree can't freeze the feed."""
    now = datetime.now(timezone.utc).isoformat()
    scanner_git.commit_and_push(repo_dir, files, f"congress trades update {now}")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def acquire_lock():
    """PID-file lock so an hourly-scheduled run doesn't overlap a
    previous run that's still deep in a long OCR pass (a 100+ page
    scanned filing can take 20-30 minutes). Not fancy — just enough for
    this single-Pi, single-cron use case. Returns True if the lock was
    acquired (safe to proceed), False if another run already holds it."""
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # raises OSError if that process isn't running
            return False
        except (OSError, ValueError):
            log.warning("Found a stale lock file (previous run no longer alive) — clearing it")
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        with open(LOCK_PATH) as f:
            if int(f.read().strip()) == os.getpid():
                os.remove(LOCK_PATH)
    except (OSError, ValueError):
        pass


def main():
    if not acquire_lock():
        log.info("Another run is still in progress (lock held by a live process) — skipping this trigger")
        return
    try:
        _run()
    finally:
        release_lock()


def _run():
    out_path = os.path.join(REPO_DIR, "congress.json") if REPO_DIR else "output/congress.json"
    if not REPO_DIR:
        log.warning("REPO_DIR not set — writing to ./output instead of a git repo")
        os.makedirs("output", exist_ok=True)

    existing_trades, seen_ids, last_filed_senate = load_existing(out_path)
    all_trades = list(existing_trades)
    last_pushed_count = len(all_trades)

    def checkpoint():
        # Always write locally — this persists seen_ids growth (e.g. a
        # filing that needed OCR but turned up zero recognized stocks
        # still gets marked seen) so a future run never redundantly
        # re-OCRs something already fully processed, even if this run
        # never pushes. But write_output's generatedAt timestamp changes
        # every call, which would make git see "a change" (and push/
        # rebuild Vercel) even when zero new trades were found — the
        # common case on an hourly schedule — so only push when the
        # trade count actually moved.
        nonlocal last_pushed_count
        write_output(all_trades, seen_ids, out_path)
        if len(all_trades) == last_pushed_count:
            log.info("No new trades since last push — wrote locally, skipping git push")
            return
        if REPO_DIR:
            git_commit_and_push(REPO_DIR, ["congress.json"])
        last_pushed_count = len(all_trades)

    senate_trades = scan_senate(seen_ids, last_filed_senate)
    all_trades.extend(senate_trades)
    checkpoint()  # Senate runs atomically already, but checkpoint here too so its
                  # results are safe before the much longer House and President scans start

    def on_house_batch_done(batch_trades):
        all_trades.extend(batch_trades)
        checkpoint()

    scan_house(seen_ids, on_batch_done=on_house_batch_done)

    def on_filing_done(filing_trades):
        all_trades.extend(filing_trades)
        checkpoint()

    scan_whitehouse_trump(seen_ids, on_filing_done=on_filing_done)

    checkpoint()  # final write — a harmless no-op most of the time since the last
                  # per-filing checkpoint already covers it (git diff check skips the commit)


if __name__ == "__main__":
    main()
