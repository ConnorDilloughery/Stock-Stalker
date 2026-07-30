#!/usr/bin/env python3
"""
Undercurrent Market Scanner — runs on a Pi, scans the active US stock market
and a curated ETF list continuously on Finnhub's free tier, and pushes the
results into the same GitHub repo the web app is deployed from.

Zero subscriptions, zero paid APIs. Everything here respects Finnhub's free
60-calls/minute limit with margin to spare.

WHAT IT DOES
1. Pulls the list of actively-traded US common stocks (NYSE + NASDAQ +
   AMEX) from NASDAQ's free public screener API, with a fallback to a
   smaller built-in list if that download ever fails or moves.
2. Loops through every ticker at a safe, rate-limited pace, computing the
   exact same metrics and formulas as the web app: P/E, discount from
   52-week high, financial-health checks, peer P/E medians, beta, signal,
   desk price levels, and conviction score.
3. Separately scans a curated list of liquid ETFs with fund-appropriate
   metrics only (expense ratio, AUM, premium/discount to NAV, beta,
   dividend yield) — no fake P/E-based score, since that math doesn't
   apply to a basket of holdings.
4. Writes stocks.json and etfs.json, then commits and pushes them into
   your existing GitHub repo so Vercel redeploys them as static files the
   web app can just fetch — no new hosting needed.
5. Repeats forever, in rolling passes, all day.

SETUP (one-time)
  1. pip install requests --break-system-packages
  2. Set these two environment variables (e.g. in ~/.bashrc or a systemd
     EnvironmentFile — see undercurrent-scanner.service):
       FINNHUB_API_KEY   your free Finnhub API key
       REPO_DIR           local path to a git clone of your Stock-Stalker
                           repo, with push access already configured
                           (SSH key or a stored HTTPS credential — same
                           git setup you'd use for any normal push)
  3. Run: python3 market_scanner.py

HONEST LIMITATIONS
  - Finnhub's free tier doesn't reliably expose expense ratio / AUM for
    ETFs via API, so those two fields come from a small hand-maintained
    table below (EFT_STATIC_INFO). They're stable numbers that rarely
    change, but double check them occasionally against the fund
    provider's page if you're being precise.
  - Dividend yield (both ETFs and regular stocks) is computed from real
    trailing-12-month distribution amounts (see
    fetch_trailing_dividend_yield), not a vendor-supplied yield field.
    Finnhub's free tier returns zero dividend fields for ETFs at all,
    and its per-stock TTM yield field has been observed off by several
    points versus actual distributions (e.g. WHF: Finnhub said 24.3%,
    real trailing total worked out to ~18.2%). A ticker that's simply
    too new to have a year of distribution history, or that pays no
    dividend at all, shows n/a rather than an incomplete/misleading
    partial-year number.
  - The active-universe list comes from NASDAQ's public screener API.
    It's a stabler source than a fund provider's bulk holdings download
    (no session/cookie requirements), but any free public API can still
    change shape over time. If it starts failing, the script logs a
    warning and falls back to the smaller built-in list so scanning
    never just stops — but you'll want to check what changed.
  - This machine needs to stay on and connected. If it reboots, the
    systemd service (see the companion .service file) restarts the
    script automatically.
"""

import os
import sys
import json
import time
import math
import statistics
import subprocess

import scanner_git
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
REPO_DIR = os.environ.get("REPO_DIR", "").strip()
API_BASE = "https://finnhub.io/api/v1"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Stay comfortably under Finnhub's free 60 calls/minute limit.
CALL_DELAY_SECONDS = 1.1          # ~54 calls/min sustained pace
PEER_SAMPLE_SIZE = 8              # tickers sampled per sector for P/E medians
FINALIST_EXTRA_CALLS = True       # pull analyst ratings + earnings for passers only
CHECKPOINT_EVERY_N = 400          # push partial results every N tickers scanned

# Fallback if the live download ever breaks — keeps the scanner running
# on a smaller but still-diversified list rather than dying outright.
FALLBACK_TICKERS = [
    "AAPL","MSFT","GOOGL","AVGO","ORCL","CSCO","QCOM","TXN","AMAT","MU","ADI","IBM",
    "INTU","ADBE","CRM","NOW","INTC","AMD","PANW","SNPS","CDNS","FTNT","ANET","MSI",
    "LLY","UNH","JNJ","ABBV","MRK","PFE","TMO","AMGN","GILD","CVS","CI","ELV",
    "CAT","HON","UNP","DE","GE","LMT","RTX","UPS","ETN","ADP","CSX","EMR",
    "XOM","CVX","COP","SLB","EOG","MPC","PSX","VLO","OXY","WMB","KMI","OKE",
    "WMT","HD","PG","KO","PEP","COST","MCD","NKE","SBUX","TJX","LOW","TGT",
    "JPM","BAC","WFC","GS","MS","SCHW","AXP","C","BLK","PNC","USB","TFC",
    "LIN","SHW","APD","FCX","NUE","DOW","NEM","CTVA","VMC","MLM","PPG","IP",
    "SMTC","DIOD","POWI","CRUS","SLAB","ONTO","CROX","ASO","MUR","CIVI","JXN","OMF",
]

SECTOR_KEYWORDS = {
    "Technology": ["technology", "software", "semiconductor", "hardware", "it services"],
    "Healthcare": ["health", "pharma", "biotech", "medical"],
    "Industrials": ["industrial", "aerospace", "defense", "machinery", "transport"],
    "Energy": ["energy", "oil", "gas", "petroleum"],
    "Consumer": ["consumer", "retail", "restaurant", "apparel"],
    "Financials": ["financ", "bank", "insurance", "capital markets"],
    "Materials": ["material", "chemical", "mining", "metal"],
}

# Curated liquid ETF universe with hand-maintained expense ratio / category
# data (see the module docstring for why this isn't pulled from an API).
ETF_STATIC_INFO = {
    "SPY":  {"name": "SPDR S&P 500 ETF Trust",              "category": "US Broad Market",     "expense": 0.0945},
    "VOO":  {"name": "Vanguard S&P 500 ETF",                "category": "US Broad Market",     "expense": 0.03},
    "IVV":  {"name": "iShares Core S&P 500 ETF",            "category": "US Broad Market",     "expense": 0.03},
    "QQQ":  {"name": "Invesco QQQ Trust",                   "category": "US Large Growth",     "expense": 0.20},
    "VTI":  {"name": "Vanguard Total Stock Market ETF",     "category": "US Total Market",     "expense": 0.03},
    "IWM":  {"name": "iShares Russell 2000 ETF",            "category": "US Small Cap",        "expense": 0.19},
    "IWV":  {"name": "iShares Russell 3000 ETF",            "category": "US Total Market",     "expense": 0.20},
    "DIA":  {"name": "SPDR Dow Jones Industrial Avg ETF",   "category": "US Large Cap",        "expense": 0.16},
    "VEA":  {"name": "Vanguard FTSE Developed Markets ETF", "category": "International Developed", "expense": 0.06},
    "VWO":  {"name": "Vanguard FTSE Emerging Markets ETF",  "category": "Emerging Markets",    "expense": 0.08},
    "EFA":  {"name": "iShares MSCI EAFE ETF",               "category": "International Developed", "expense": 0.32},
    "EEM":  {"name": "iShares MSCI Emerging Markets ETF",   "category": "Emerging Markets",    "expense": 0.68},
    "AGG":  {"name": "iShares Core US Aggregate Bond ETF",  "category": "US Bonds",            "expense": 0.03},
    "BND":  {"name": "Vanguard Total Bond Market ETF",      "category": "US Bonds",            "expense": 0.03},
    "TLT":  {"name": "iShares 20+ Year Treasury Bond ETF",  "category": "Long-Term Treasuries", "expense": 0.15},
    "SHY":  {"name": "iShares 1-3 Year Treasury Bond ETF",  "category": "Short-Term Treasuries", "expense": 0.15},
    "LQD":  {"name": "iShares iBoxx Investment Grade Corp Bond ETF", "category": "Corporate Bonds", "expense": 0.14},
    "HYG":  {"name": "iShares iBoxx High Yield Corp Bond ETF", "category": "High-Yield Bonds", "expense": 0.48},
    "XLK":  {"name": "Technology Select Sector SPDR",       "category": "Sector — Technology",  "expense": 0.09},
    "XLF":  {"name": "Financial Select Sector SPDR",        "category": "Sector — Financials",  "expense": 0.09},
    "XLE":  {"name": "Energy Select Sector SPDR",           "category": "Sector — Energy",      "expense": 0.09},
    "XLV":  {"name": "Health Care Select Sector SPDR",      "category": "Sector — Healthcare",  "expense": 0.09},
    "XLI":  {"name": "Industrial Select Sector SPDR",       "category": "Sector — Industrials", "expense": 0.09},
    "XLY":  {"name": "Consumer Discretionary Select SPDR",  "category": "Sector — Consumer",    "expense": 0.09},
    "XLP":  {"name": "Consumer Staples Select SPDR",        "category": "Sector — Consumer",    "expense": 0.09},
    "XLB":  {"name": "Materials Select Sector SPDR",        "category": "Sector — Materials",   "expense": 0.09},
    "XLU":  {"name": "Utilities Select Sector SPDR",        "category": "Sector — Utilities",   "expense": 0.09},
    "XLC":  {"name": "Communication Services Select SPDR",  "category": "Sector — Communications", "expense": 0.09},
    "XLRE": {"name": "Real Estate Select Sector SPDR",      "category": "Sector — Real Estate", "expense": 0.09},
    "SMH":  {"name": "VanEck Semiconductor ETF",            "category": "Sector — Semiconductors", "expense": 0.35},
    "SOXX": {"name": "iShares Semiconductor ETF",           "category": "Sector — Semiconductors", "expense": 0.35},
    "ARKK": {"name": "ARK Innovation ETF",                  "category": "Thematic — Innovation", "expense": 0.75},
    "VNQ":  {"name": "Vanguard Real Estate ETF",            "category": "Real Estate",          "expense": 0.13},
    "GLD":  {"name": "SPDR Gold Shares",                    "category": "Commodities — Gold",   "expense": 0.40},
    "SLV":  {"name": "iShares Silver Trust",                "category": "Commodities — Silver", "expense": 0.50},
    "USO":  {"name": "United States Oil Fund",              "category": "Commodities — Oil",    "expense": 0.60},
    "VYM":  {"name": "Vanguard High Dividend Yield ETF",    "category": "Dividend",             "expense": 0.06},
    "SCHD": {"name": "Schwab US Dividend Equity ETF",       "category": "Dividend",             "expense": 0.06},
    "DVY":  {"name": "iShares Select Dividend ETF",         "category": "Dividend",             "expense": 0.38},
    "VUG":  {"name": "Vanguard Growth ETF",                 "category": "US Large Growth",      "expense": 0.04},
    "VTV":  {"name": "Vanguard Value ETF",                  "category": "US Large Value",       "expense": 0.04},
    "IJH":  {"name": "iShares Core S&P Mid-Cap ETF",        "category": "US Mid Cap",           "expense": 0.05},
    "IJR":  {"name": "iShares Core S&P Small-Cap ETF",      "category": "US Small Cap",         "expense": 0.06},
    "MDY":  {"name": "SPDR S&P MidCap 400 ETF",             "category": "US Mid Cap",           "expense": 0.23},
    "VXUS": {"name": "Vanguard Total International Stock ETF", "category": "International Total", "expense": 0.05},
    "BNDX": {"name": "Vanguard Total International Bond ETF", "category": "International Bonds", "expense": 0.07},
    "TIP":  {"name": "iShares TIPS Bond ETF",               "category": "Inflation-Protected Bonds", "expense": 0.19},
    "JEPI": {"name": "JPMorgan Equity Premium Income ETF",  "category": "Covered Call / Income", "expense": 0.35},
    "SCHB": {"name": "Schwab US Broad Market ETF",          "category": "US Broad Market",      "expense": 0.03},
    "SCHX": {"name": "Schwab US Large-Cap ETF",             "category": "US Large Cap",         "expense": 0.03},
    "RSP":  {"name": "Invesco S&P 500 Equal Weight ETF",    "category": "US Large Cap — Equal Weight", "expense": 0.20},
    "MTUM": {"name": "iShares MSCI USA Momentum Factor ETF", "category": "Factor — Momentum",   "expense": 0.15},
    "QUAL": {"name": "iShares MSCI USA Quality Factor ETF", "category": "Factor — Quality",     "expense": 0.15},
    "USMV": {"name": "iShares MSCI USA Min Vol Factor ETF", "category": "Factor — Low Volatility", "expense": 0.15},
    "IEMG": {"name": "iShares Core MSCI Emerging Markets ETF", "category": "Emerging Markets",  "expense": 0.09},
    "ACWI": {"name": "iShares MSCI ACWI ETF",               "category": "Global Total Market",  "expense": 0.32},
    "IYR":  {"name": "iShares US Real Estate ETF",          "category": "Real Estate",          "expense": 0.39},
    "KRE":  {"name": "SPDR S&P Regional Banking ETF",       "category": "Sector — Regional Banks", "expense": 0.35},
    "XBI":  {"name": "SPDR S&P Biotech ETF",                "category": "Sector — Biotech",     "expense": 0.35},
    "IBB":  {"name": "iShares Biotechnology ETF",           "category": "Sector — Biotech",     "expense": 0.44},
    "GDX":  {"name": "VanEck Gold Miners ETF",              "category": "Sector — Gold Mining", "expense": 0.51},
    "URA":  {"name": "Global X Uranium ETF",                "category": "Sector — Uranium",     "expense": 0.69},
}

# ETFs with genuinely deep, weekly-expiration options markets — hand-
# maintained, since Finnhub's free tier doesn't expose options-chain
# listings at all. This is a starting filter, not a guarantee: always
# confirm weeklies and current liquidity in your broker's options chain
# before trading, since availability can change.
WEEKLY_OPTIONS_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "EEM", "EFA", "GLD", "SLV",
    "XLF", "XLE", "XLK", "XLI", "XLV", "XLY", "XLP", "XLU", "XLC", "XLRE",
    "TLT", "HYG", "LQD", "SMH", "SOXX", "ARKK", "KRE", "XBI", "IBB", "GDX",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scanner")


# ----------------------------------------------------------------------
# Finnhub helpers
# ----------------------------------------------------------------------

def fh_get(path, params):
    """Rate-limited Finnhub GET. Retries once on a 429 after waiting."""
    q = dict(params)
    q["token"] = API_KEY
    url = f"{API_BASE}{path}?{urlencode(q)}"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 429:
        log.warning("Rate limited, waiting 20s...")
        time.sleep(20)
        resp = requests.get(url, timeout=15)
    if resp.status_code in (401, 403):
        raise RuntimeError("BADKEY")
    resp.raise_for_status()
    time.sleep(CALL_DELAY_SECONDS)
    return resp.json()


def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# A sustained real-world dividend yield above ~25% essentially never
# happens (it would mean a company pays out nearly its whole share price
# in dividends every year). Finnhub's free tier occasionally reports
# garbage values for thinly-covered small/micro-cap tickers — rather than
# display a false number, treat anything implausible as "we don't trust
# this" and fall back to None (shown as n/a) instead of a fake precise
# figure.
YIELD_SANITY_CEILING = 25.0

def sanitize_yield(y):
    if y is None:
        return None
    return y if 0 <= y <= YIELD_SANITY_CEILING else None


# ----------------------------------------------------------------------
# Ticker universe
# ----------------------------------------------------------------------

def fetch_active_universe():
    """Downloads the list of actively-traded US common stocks from NASDAQ's
    public screener API (nasdaq.com), which returns real JSON with no login
    or session cookie required — unlike iShares' holdings CSV, which now
    serves an HTML disclaimer page instead of the raw file to plain
    requests. Falls back to a smaller built-in list on failure.

    This pulls NYSE + NASDAQ + AMEX common stock listings and filters out
    ETFs, warrants, units, and other non-common-stock tickers, which is
    roughly equivalent in spirit to "the Russell 3000" — the standard
    actively-traded universe — without depending on a fund provider's
    fragile bulk-holdings download.
    """
    url = "https://api.nasdaq.com/api/screener/stocks"
    params = {"tableonly": "true", "limit": "10000", "download": "true"}
    headers = {
        # NASDAQ's API rejects requests without a browser-like User-Agent
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = (data.get("data") or {}).get("rows") or []
        tickers = []
        for row in rows:
            t = (row.get("symbol") or "").strip()
            # Skip warrants, units, preferreds, and other non-common-stock
            # tickers, which usually contain a separator character.
            if t and t.isalpha() and 1 <= len(t) <= 5:
                tickers.append(t)
        if len(tickers) < 500:
            raise ValueError(f"Only parsed {len(tickers)} tickers — API response shape may have changed")
        log.info(f"Loaded {len(tickers)} tickers from NASDAQ's screener API")
        return sorted(set(tickers))
    except Exception as e:
        log.warning(f"Active-universe download failed ({e}); using fallback list of {len(FALLBACK_TICKERS)} tickers")
        return FALLBACK_TICKERS


# Kept as an alias so any external references to the old name still work.
fetch_russell_3000 = fetch_active_universe


def guess_sector(finnhub_industry):
    ind = (finnhub_industry or "").lower()
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(k in ind for k in keywords):
            return sector
    return "Other"


# ----------------------------------------------------------------------
# Scoring — ported 1:1 from the web app's JavaScript so both surfaces
# agree on what "cheap," "healthy," and "in the buy zone" mean.
# ----------------------------------------------------------------------

def extract_quality(m, pe):
    de = num(m.get("totalDebt/totalEquityQuarterly")) or num(m.get("totalDebt/totalEquityAnnual"))
    if de is not None and de > 10:
        de = de / 100
    cr = num(m.get("currentRatioQuarterly")) or num(m.get("currentRatioAnnual"))
    roe = num(m.get("roeTTM")) or num(m.get("roeRfy"))
    growth = num(m.get("epsGrowth5Y")) or num(m.get("epsGrowthTTMYoy"))
    peg = (pe / growth) if (pe and growth and growth > 0) else None
    ps = num(m.get("psTTM"))
    pb = num(m.get("pbQuarterly")) or num(m.get("pbAnnual"))
    div_yield = sanitize_yield(num(m.get("currentDividendYieldTTM")) or num(m.get("dividendYieldIndicatedAnnual")))
    beta = num(m.get("beta"))
    checks = [
        None if de is None else de < 1.0,
        None if cr is None else cr > 1.5,
        None if roe is None else roe > 15,
        None if peg is None else peg < 1.2,
    ]
    valid = [c for c in checks if c is not None]
    q_max = len(valid)
    q_pass = sum(1 for c in valid if c)
    return {"de": de, "cr": cr, "roe": roe, "peg": peg, "ps": ps, "pb": pb,
            "divYield": div_yield, "beta": beta, "qPass": q_pass, "qMax": q_max}


def median(values):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def compute_signal(s):
    net = ((2 * s["strongBuy"] + s["buy"] - s["sell"] - 2 * s["strongSell"]) / s["recTotal"]) if s["recTotal"] > 0 else 0
    q = (s["qPass"] / s["qMax"]) if s["qMax"] > 0 else 0.5
    if s["discount"] >= 0.20 and net >= 0.5 and s["pe"] <= 20 and q >= 0.5:
        return "BUY"
    if s["discount"] >= 0.12 and net >= 0.25 and q >= 0.5:
        return "ACCUMULATE"
    return "WATCH"


def compute_verdict(s):
    pe_score = max(0, min(1, (s["pe"] - 6) / 19))
    near_high = 1 - max(0, min(1, s["discount"] / 0.4))
    return round(pe_score * 50 + near_high * 50)


def compute_bands(s):
    eps = s["price"] / s["pe"]
    target_pe = 13 + 1.5 * (s.get("qPass") or 0)
    fair = eps * target_pe
    return {"fairValue": fair, "buyBelow": fair * 0.85, "avoidAbove": eps * 25}


def compute_score(s):
    value = min((s.get("discount") or 0) / 0.35, 1) * 30
    pe_score = (1 - max(0, min(1, ((s.get("pe") or 15) - 6) / 19))) * 15
    health = (s["qPass"] / s["qMax"]) * 25 if s.get("qMax") else 12.5
    net = ((2 * s["strongBuy"] + s["buy"] - s["sell"] - 2 * s["strongSell"]) / s["recTotal"]) if s.get("recTotal", 0) > 0 else 0.4
    analysts = max(0, min(net, 1)) * 20
    zone_pts = 5
    if s.get("buyBelow") and s.get("avoidAbove"):
        if s["price"] <= s["buyBelow"]:
            zone_pts = 10
        elif s["price"] >= s["avoidAbove"]:
            zone_pts = 0
        else:
            zone_pts = ((s["avoidAbove"] - s["price"]) / (s["avoidAbove"] - s["buyBelow"])) * 10
    return round(value + pe_score + health + analysts + zone_pts)


# ----------------------------------------------------------------------
# Stock scan
# ----------------------------------------------------------------------

def finalize_stocks(passed, enrich_n=150):
    """Takes the full set of currently-known passing stocks (which may
    include tickers found in earlier passes that haven't been re-visited
    yet this pass), ranks everything, and pulls the pricier extra calls
    (analyst ratings, earnings date) only for the top slice — but returns
    the FULL list, not just that slice, so previously-found good stocks
    are never silently dropped from the output due to a cutoff."""
    if not passed:
        return []
    sector_pes = {}
    for sec in set(s["sector"] for s in passed):
        sector_pes[sec] = median([s["pe"] for s in passed if s["sector"] == sec])
    for s in passed:
        s["sectorMedianPE"] = sector_pes.get(s["sector"])

    ranked = sorted(passed, key=lambda s: (s["discount"] + 0.04 * s["qPass"]), reverse=True)
    to_enrich = ranked[:enrich_n]

    if FINALIST_EXTRA_CALLS:
        for s in to_enrich:
            try:
                recs = fh_get("/stock/recommendation", {"symbol": s["ticker"]})
                if recs:
                    r = recs[0]
                    s.update({
                        "strongBuy": r.get("strongBuy", 0), "buy": r.get("buy", 0),
                        "hold": r.get("hold", 0), "sell": r.get("sell", 0),
                        "strongSell": r.get("strongSell", 0),
                    })
                    s["recTotal"] = s["strongBuy"] + s["buy"] + s["hold"] + s["sell"] + s["strongSell"]
                today = datetime.now(timezone.utc).date()
                earn = fh_get("/calendar/earnings", {
                    "symbol": s["ticker"],
                    "from": today.isoformat(),
                    "to": (today + timedelta(days=45)).isoformat(),
                })
                events = sorted((e for e in earn.get("earningsCalendar", []) if e.get("date")), key=lambda e: e["date"])
                if events:
                    s["earningsDate"] = events[0]["date"]
            except RuntimeError as e:
                if str(e) == "BADKEY":
                    raise
            except Exception:
                pass

    for s in ranked:
        s["signal"] = compute_signal(s)
        s["verdict"] = compute_verdict(s)
        s.update(compute_bands(s))
        s["score"] = compute_score(s)
    ranked.sort(key=lambda s: s["score"], reverse=True)
    return ranked


def load_existing_stocks():
    """Loads whatever stocks.json already has, keyed by ticker, so a
    restart (or the start of a new pass) updates known-good picks in
    place instead of wiping everything until they're re-scanned."""
    path = os.path.join(REPO_DIR, "stocks.json") if REPO_DIR else "output/stocks.json"
    try:
        with open(path) as f:
            data = json.load(f)
        known = {s["ticker"]: s for s in data.get("stocks", []) if "ticker" in s}
        log.info(f"Loaded {len(known)} previously-known stocks from {path} to update in place")
        return known
    except Exception:
        return {}


def load_existing_universe():
    """Same load-in-place idea as load_existing_stocks(), but for
    universe.json — the UNFILTERED list of every scanned name (used by the
    web app's Legends/strategy screeners). Unlike stocks.json this keeps
    expensive and near-52-week-high names, which the value screen drops but
    growth/momentum strategies need."""
    path = os.path.join(REPO_DIR, "universe.json") if REPO_DIR else "output/universe.json"
    try:
        with open(path) as f:
            data = json.load(f)
        u = {s["ticker"]: s for s in data.get("stocks", []) if "ticker" in s}
        log.info(f"Loaded {len(u)} previously-known universe names from {path}")
        return u
    except Exception:
        return {}


def scan_stocks(tickers, known, universe=None, on_checkpoint=None):
    """Scans every ticker, updating `known` (a ticker -> stock dict, kept
    by the caller across passes and restarts) in place: tickers that
    still pass get their entry refreshed, tickers that no longer pass
    get removed — but only once they've actually been re-checked this
    pass, never just because this pass hasn't reached them yet. Calls
    on_checkpoint(current_full_list) every CHECKPOINT_EVERY_N tickers so
    long passes push fresh data well before the whole thing finishes."""
    total = len(tickers)
    for i, tk in enumerate(tickers):
        if i % 100 == 0:
            log.info(f"Stock scan progress: {i}/{total} ({tk})")
        if on_checkpoint and i > 0 and i % CHECKPOINT_EVERY_N == 0:
            log.info(f"Checkpoint at {i}/{total} — {len(known)} known-good stocks so far this pass")
            try:
                snapshot = finalize_stocks(list(known.values()), enrich_n=40)
                on_checkpoint(snapshot, i, total)
            except RuntimeError as e:
                if str(e) == "BADKEY":
                    raise
            except Exception as e:
                log.warning(f"Checkpoint push failed, continuing scan: {e}")
        try:
            quote = fh_get("/quote", {"symbol": tk})
            metric = fh_get("/stock/metric", {"symbol": tk, "metric": "all"})
            profile = fh_get("/stock/profile2", {"symbol": tk})
        except RuntimeError as e:
            if str(e) == "BADKEY":
                raise
            continue
        except Exception:
            continue

        price = quote.get("c")
        m = metric.get("metric") or {}
        pe = m.get("peBasicExclExtraTTM") or m.get("peTTM") or m.get("peNormalizedAnnual")
        cap = m.get("marketCapitalization")
        high = m.get("52WeekHigh")
        low = m.get("52WeekLow")

        # ---- Full universe (universe.json): a lightweight record for EVERY
        # name with basic data, with NO value gate — this is the pool the
        # Legends screeners read, so it must include expensive and near-high
        # names that the stocks.json value screen below throws out. No extra
        # API calls: reuses the same quote/metric/profile, vendor dividend
        # yield only (skips the pricey trailing-yield call).
        if universe is not None:
            if price and cap and high:
                uq = extract_quality(m, pe)
                universe[tk] = {
                    "ticker": tk, "sector": guess_sector(profile.get("finnhubIndustry")),
                    "price": price, "pe": pe, "cap": cap, "high": high, "low": low,
                    "discount": (high - price) / high,
                    "de": uq["de"], "cr": uq["cr"], "roe": uq["roe"], "peg": uq["peg"],
                    "pb": uq["pb"], "divYield": uq["divYield"], "beta": uq["beta"],
                    "scannedAt": datetime.now(timezone.utc).isoformat(),
                }
            else:
                universe.pop(tk, None)  # re-checked and no longer has basic data

        if not price or not pe or pe <= 0 or not cap or not high:
            known.pop(tk, None)  # re-checked and no longer has valid data — remove
            continue
        if pe < 6 or pe > 25:
            known.pop(tk, None)  # re-checked and no longer in the healthy P/E band
            continue

        discount = (high - price) / high
        if discount < 0.08:
            known.pop(tk, None)  # re-checked and no longer discounted enough
            continue

        sector = guess_sector(profile.get("finnhubIndustry"))
        s = {
            "ticker": tk, "sector": sector, "price": price, "pe": pe, "cap": cap,
            "high": high, "low": low, "discount": discount,
            "avgVolumeM": num(m.get("10DayAverageTradingVolume")),  # real, live liquidity signal
            "strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0, "recTotal": 0,
            "scannedAt": datetime.now(timezone.utc).isoformat(),
        }
        s.update(extract_quality(m, pe))
        # Finnhub's TTM dividend yield field is frequently stale or off by
        # several points (e.g. WHF: Finnhub said 24.3%, actual trailing
        # distributions work out to ~18.2%) — same fix as the ETF scan:
        # trust a real sum of the last 12 months of per-share distributions
        # over the vendor-supplied number. None means genuinely no
        # dividend events found, not "unknown."
        s["divYield"] = fetch_trailing_dividend_yield(tk, price)
        time.sleep(0.3)  # be polite to a free public endpoint
        known[tk] = s  # upsert — refreshes if already present, adds if new

    return finalize_stocks(list(known.values()), enrich_n=150)


# ----------------------------------------------------------------------
# ETF dividend yield — Finnhub's free tier returns zero dividend fields
# for ETFs at all (confirmed empty across every ETF tested), so this
# computes a real trailing-12-month yield instead, from actual
# per-share distribution amounts pulled from Yahoo Finance's public
# chart endpoint (query1.finance.yahoo.com) — the same basic, no-auth,
# no-crumb-required endpoint used across the industry for exactly this,
# not the newer quoteSummary endpoint (which does require an auth
# "crumb" token). Works regardless of listing exchange, unlike Nasdaq's
# own dividends endpoint, which only covers Nasdaq-listed symbols
# (confirmed: real data for QQQ/BND/TLT, but "N/A" for the majority of
# our list — SPY, the sector SPDRs, iShares, Vanguard funds, etc., all
# of which are NYSE Arca-listed).
# ----------------------------------------------------------------------

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def fetch_trailing_dividend_yield(ticker, price):
    """Sums the last 12 months of actual per-share distributions and
    divides by price — a real computed yield, not a vendor-supplied
    number. Returns None for funds with no dividend events at all
    (e.g. gold/commodity ETFs like GLD genuinely pay no distributions)
    — that's an honest reflection of the fund, not a gap to guess at."""
    if not price:
        return None
    try:
        url = YAHOO_CHART_URL.format(symbol=ticker)
        resp = requests.get(url, params={"interval": "1d", "range": "1y", "events": "div"},
                             headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code != 200:
            return None
        result = (resp.json().get("chart") or {}).get("result") or []
        if not result:
            return None
        events = (result[0].get("events") or {}).get("dividends") or {}
        if not events:
            return None
        total = sum(num(v.get("amount")) or 0 for v in events.values())
        if total <= 0:
            return None
        return sanitize_yield((total / price) * 100)
    except Exception:
        return None


# ----------------------------------------------------------------------
# ETF scan — fund-appropriate metrics only, no fake per-share score
# ----------------------------------------------------------------------

def scan_etfs():
    results = []
    for tk, info in ETF_STATIC_INFO.items():
        try:
            quote = fh_get("/quote", {"symbol": tk})
            metric = fh_get("/stock/metric", {"symbol": tk, "metric": "all"})
        except RuntimeError as e:
            if str(e) == "BADKEY":
                raise
            continue
        except Exception:
            continue

        # Finnhub's basic metrics endpoint frequently leaves marketCap and
        # dividend yield blank for ETFs (it's really built for equities).
        # profile2 sometimes has shares outstanding, which lets us estimate
        # AUM ourselves — a real fallback instead of just showing n/a.
        profile = {}
        try:
            profile = fh_get("/stock/profile2", {"symbol": tk}) or {}
        except RuntimeError as e:
            if str(e) == "BADKEY":
                raise
        except Exception:
            pass

        price = quote.get("c")
        if not price:
            continue
        m = metric.get("metric") or {}
        high = m.get("52WeekHigh")
        low = m.get("52WeekLow")
        range_position = None
        range_width_pct = None
        if high and low and high > low:
            range_position = max(0, min(100, ((price - low) / (high - low)) * 100))
            range_width_pct = ((high - low) / price) * 100 if price else None
        avg_volume = num(m.get("10DayAverageTradingVolume"))  # Finnhub reports this in millions of shares

        # AUM: try the metrics field first, then estimate from profile
        # shares outstanding × price, then profile's own market cap field.
        aum = num(m.get("marketCapitalization"))
        if aum is None:
            shares_out = num(profile.get("shareOutstanding"))  # profile reports this in millions
            if shares_out:
                aum = shares_out * price
        if aum is None:
            aum = num(profile.get("marketCapitalization"))

        # Dividend yield: Finnhub's free tier has no dividend fields for
        # ETFs at all (confirmed empty), so this is computed directly
        # from real trailing-12-month distributions instead — see
        # fetch_trailing_dividend_yield's docstring for the source.
        div_yield = fetch_trailing_dividend_yield(tk, price)
        time.sleep(0.3)  # be polite to a free public endpoint

        results.append({
            "ticker": tk,
            "name": info["name"],
            "category": info["category"],
            "expenseRatio": info["expense"],
            "price": price,
            "high": high,
            "low": low,
            "rangePosition": range_position,
            "rangeWidthPct": range_width_pct,  # rough "how much it moves" proxy — NOT options IV
            "avgVolumeM": avg_volume,
            "weeklyOptions": tk in WEEKLY_OPTIONS_ETFS,
            "beta": num(m.get("beta")),
            "divYield": div_yield,
            "aum": aum,
            "scannedAt": datetime.now(timezone.utc).isoformat(),
        })

    # Category peer comparison for expense ratio — same idea as the stock
    # peer-P/E comp: is this fund cheap or pricey relative to others in
    # the same category, not just in isolation. Skip categories with
    # fewer than 2 members since a "peer group" of one isn't meaningful.
    by_category = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r["expenseRatio"])
    for cat, ratios in by_category.items():
        if len(ratios) >= 2:
            med = median(ratios)
            for r in results:
                if r["category"] == cat:
                    r["categoryMedianExpense"] = med

    return results


# ----------------------------------------------------------------------
# Output + git push
# ----------------------------------------------------------------------

def write_output(stocks, etfs, stocks_only=False, etfs_only=False, partial=False, universe=None):
    now = datetime.now(timezone.utc).isoformat()
    stock_payload = {"generatedAt": now, "count": len(stocks), "stocks": stocks, "partial": partial}
    etf_payload = {"generatedAt": now, "count": len(etfs), "etfs": etfs}

    if not REPO_DIR:
        log.warning("REPO_DIR not set — writing to ./output instead of a git repo")
        os.makedirs("output", exist_ok=True)
        stock_path, etf_path = "output/stocks.json", "output/etfs.json"
        universe_path = "output/universe.json"
    else:
        stock_path = os.path.join(REPO_DIR, "stocks.json")
        etf_path = os.path.join(REPO_DIR, "etfs.json")
        universe_path = os.path.join(REPO_DIR, "universe.json")

    files_to_add = []
    if not etfs_only:
        with open(stock_path, "w") as f:
            json.dump(stock_payload, f, indent=2)
        files_to_add.append("stocks.json")
    if not stocks_only:
        with open(etf_path, "w") as f:
            json.dump(etf_payload, f, indent=2)
        files_to_add.append("etfs.json")
    # Full universe (unfiltered) for the Legends screeners. Written compact
    # (no indent) since it's ~6× bigger than stocks.json and only read by code.
    if universe is not None and not etfs_only:
        universe_payload = {"generatedAt": now, "count": len(universe), "stocks": universe, "partial": partial}
        with open(universe_path, "w") as f:
            json.dump(universe_payload, f, separators=(",", ":"))
        files_to_add.append("universe.json")
    log.info(f"Wrote {'' if etfs_only else str(len(stocks)) + ' stocks '}{'' if stocks_only else 'and ' + str(len(etfs)) + ' ETFs '}to {os.path.dirname(stock_path) or '.'}")

    if REPO_DIR:
        # Shared, self-healing, lock-serialized publisher — a race, crash, or
        # stray dirty file can't freeze the feed (see scanner_git.py).
        label = "checkpoint" if partial else "scan update"
        scanner_git.commit_and_push(REPO_DIR, files_to_add, f"{label} {now}")


# ----------------------------------------------------------------------
# Main loop — rolling passes, all day, forever
# ----------------------------------------------------------------------

def main():
    if not API_KEY:
        log.error("FINNHUB_API_KEY is not set. Export it and try again.")
        sys.exit(1)

    tickers = fetch_russell_3000()
    known = load_existing_stocks()  # seed from whatever's already published, so a
                                     # restart updates in place instead of wiping
    universe = load_existing_universe()  # unfiltered full-market pool for Legends
    pass_num = 0
    while True:
        pass_num += 1
        started = time.time()
        log.info(f"=== Starting pass #{pass_num} over {len(tickers)} stocks ({len(known)} known-good carried over) ===")
        try:
            # ETFs first — only ~60 tickers, done in a couple minutes, so
            # etfs.json stays fresh even while the much longer stock scan
            # is still working through thousands of tickers.
            etfs = scan_etfs()
            write_output([], etfs, stocks_only=False, etfs_only=True)

            def on_checkpoint(current_stocks, done, total):
                write_output(current_stocks, etfs, stocks_only=True, partial=True,
                             universe=list(universe.values()))

            stocks = scan_stocks(tickers, known, universe, on_checkpoint=on_checkpoint)
            write_output(stocks, etfs, universe=list(universe.values()))
        except RuntimeError as e:
            if str(e) == "BADKEY":
                log.error("Finnhub rejected the API key. Check FINNHUB_API_KEY and restart.")
                sys.exit(1)
            log.error(f"Unexpected error mid-scan: {e}")
        except Exception as e:
            log.error(f"Unexpected error mid-scan: {e}")

        elapsed = time.time() - started
        log.info(f"Pass #{pass_num} finished in {elapsed/60:.1f} minutes")
        # Re-fetch the ticker list once a day in case constituents changed;
        # otherwise just loop straight into the next pass.
        if pass_num % 12 == 0:
            tickers = fetch_russell_3000()


if __name__ == "__main__":
    main()
