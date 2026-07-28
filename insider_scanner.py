#!/usr/bin/env python3
"""
Undercurrent Insider Trading Scanner — runs on the Pi hourly, polls SEC
EDGAR's real-time Form 4 feed for open-market insider buy/sell activity
in tickers Undercurrent tracks at all (the full actively-traded US
stock universe — not the literal entire market, see below), and
pushes the results into the same GitHub repo the web app is deployed
from.

Zero subscriptions, zero paid APIs. SEC EDGAR requires only a proper
User-Agent header identifying the requester (their own documented
fair-access policy — not a workaround, an explicit requirement) —
https://www.sec.gov/os/webmaster-faq#developers

WHY SCOPED TO THE ACTIVE UNIVERSE, NOT THE LITERAL WHOLE MARKET
Every public company files Form 4s constantly — thousands a week,
mostly routine compensation events (RSU vests, tax-withholding
dispositions, option exercises), not discretionary buy/sell decisions.
The transaction-type filter (P/S only, see below) already cuts that
noise down a lot on its own. Originally this was scoped further, to
only stocks.json's current passing list (~1,000 tickers) — but that
made real matches rare enough (that list is a small slice of the
market, and it changes) that the tab would sit empty for long
stretches. It now uses the full NASDAQ/NYSE/AMEX active-trading
universe (thousands of tickers, the same free screener endpoint
market_scanner.py already uses for its own ticker list) — still not
literally every filer on EDGAR (foreign private issuers, OTC/pink
sheets, and thinly-traded names are excluded by that screener), but
broad enough that matches show up regularly instead of only when a
signal-passing stock happens to have insider activity too.

WHAT IT DOES
1. Loads the current relevant-ticker set — the full active-trading
   universe from NASDAQ's screener, falling back to stocks.json's
   narrower passing list only if that fetch ever fails.
2. Polls SEC EDGAR's "current filings" Atom feed for Form 4s (each
   filing appears twice in the feed — once tagged Issuer, once tagged
   Reporting — deduped by accession number).
3. For each new filing, fetches its XML and keeps only nonDerivative
   transactions with a real open-market code (P = purchase, S = sale —
   the discretionary trades, not grants/vests/option exercises/gifts),
   where the issuer's ticker is in the relevant set.
4. Writes insider_trades.json and commits + pushes, same pattern as
   the other Pi scanners.

HONEST LIMITATIONS
  - Only P (purchase) and S (sale) transaction codes are kept — grants,
    option exercises, tax-withholding dispositions, and gifts (codes A,
    M, F, G, and others) are real Form 4 events but don't represent a
    discretionary market decision the way a P or S does, so they're
    left out rather than diluting the signal.
  - Scoped to NASDAQ's active-trading screener universe — foreign
    private issuers, OTC/pink sheets, and very thinly-traded names it
    excludes won't show up here even if they file a Form 4. That's an
    intentional, honest boundary (see above), not a bug.
  - The SEC feed caps at 100 entries (~50 unique filings). Observed
    coverage is comfortably wider than the hourly poll interval even
    on a slow day, but a very high-volume period (e.g. a mass 10-K
    filing deadline day) could theoretically outrun it — if that ever
    happens, filings older than the feed's current window are simply
    never seen, not guessed at.
"""

import os
import re
import sys
import json
import time
import logging
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser
from xml.etree import ElementTree

import requests

REPO_DIR = os.environ.get("REPO_DIR", "").strip()

# SEC's own fair-access policy requires a descriptive User-Agent
# identifying the requester — see module docstring.
USER_AGENT = "Undercurrent (personal research project) contact@example.com"

EDGAR_CURRENT_FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
TRANSACTION_TYPE_MAP = {"P": "Purchase", "S": "Sale"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("insider_scanner")


NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"


def fetch_active_universe():
    """The full actively-traded US common stock universe (NYSE + NASDAQ
    + AMEX, thousands of tickers) — same free, no-auth NASDAQ screener
    endpoint market_scanner.py already uses reliably for its own ticker
    universe. This is the primary relevant-ticker source now (broader
    than just stocks.json's current passing list, so matches show up
    far more often), with stocks.json as a fallback if this ever fails."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    }
    resp = requests.get(NASDAQ_SCREENER_URL, params={"tableonly": "true", "limit": "10000", "download": "true"},
                         headers=headers, timeout=30)
    resp.raise_for_status()
    rows = (resp.json().get("data") or {}).get("rows") or []
    tickers = {(row.get("symbol") or "").strip() for row in rows}
    tickers = {t for t in tickers if t and t.isalpha() and 1 <= len(t) <= 5}
    if len(tickers) < 500:
        raise ValueError(f"Only parsed {len(tickers)} tickers — API response shape may have changed")
    return tickers


def load_relevant_tickers(repo_dir):
    try:
        tickers = fetch_active_universe()
        log.info(f"Loaded {len(tickers)} relevant tickers from NASDAQ's active-universe screener")
        return tickers
    except Exception as e:
        log.warning(f"Couldn't fetch the active universe ({e}) — falling back to stocks.json's narrower passing list")

    path = os.path.join(repo_dir, "stocks.json") if repo_dir else "output/stocks.json"
    try:
        with open(path) as f:
            data = json.load(f)
        tickers = {s["ticker"] for s in data.get("stocks", []) if s.get("ticker")}
        log.info(f"Loaded {len(tickers)} relevant tickers from {path} (fallback)")
        return tickers
    except Exception as e:
        log.warning(f"Couldn't load stocks.json either ({e}) — insider scan will find nothing this run")
        return set()


def fetch_current_form4_accessions():
    """Returns a list of (accession_no, index_url) for the most recent
    Form 4 filings, deduped (each filing appears twice in the feed)."""
    params = {"action": "getcurrent", "type": "4", "company": "", "dateb": "",
              "owner": "include", "count": "100", "output": "atom"}
    resp = requests.get(EDGAR_CURRENT_FEED, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    seen_accessions = {}
    for entry in root.findall("a:entry", ns):
        link_el = entry.find("a:link", ns)
        href = link_el.get("href") if link_el is not None else None
        id_el = entry.find("a:id", ns)
        acc_match = re.search(r"accession-number=([\d-]+)", id_el.text) if id_el is not None else None
        if not href or not acc_match:
            continue
        seen_accessions.setdefault(acc_match.group(1), href)
    return list(seen_accessions.items())


class _XmlLinkFinder(HTMLParser):
    """Finds the primary Form 4 XML document link on a filing's index page."""
    def __init__(self):
        super().__init__()
        self.xml_links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href.endswith(".xml") and "xsl" not in href:
                self.xml_links.append(href)


def fetch_filing_xml_url(index_url):
    resp = requests.get(index_url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    parser = _XmlLinkFinder()
    parser.feed(resp.text)
    if not parser.xml_links:
        return None
    href = parser.xml_links[0]
    return href if href.startswith("http") else f"https://www.sec.gov{href}"


def parse_form4(xml_bytes):
    """Returns (ticker, list of transaction dicts) or (None, []) if this
    filing has no ticker or no qualifying open-market transactions."""
    root = ElementTree.fromstring(xml_bytes)

    def text(el, path, default=None):
        found = el.find(path)
        return found.text.strip() if found is not None and found.text else default

    ticker = text(root, "issuer/issuerTradingSymbol")
    if not ticker or ticker.upper() == "NONE":
        return None, []
    issuer_name = text(root, "issuer/issuerName", "")

    owner = root.find("reportingOwner")
    owner_name = text(owner, "reportingOwnerId/rptOwnerName", "") if owner is not None else ""
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    title_bits = []
    if rel is not None:
        if text(rel, "isDirector") == "1":
            title_bits.append("Director")
        if text(rel, "isOfficer") == "1":
            title_bits.append(text(rel, "officerTitle", "Officer") or "Officer")
        if text(rel, "isTenPercentOwner") == "1":
            title_bits.append("10% Owner")
    title = ", ".join(title_bits) or "Reporting Person"

    transactions = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = text(tx, "transactionCoding/transactionCode")
        if code not in TRANSACTION_TYPE_MAP:
            continue  # skip grants/exercises/tax-withholding/gifts — see module docstring
        shares = text(tx, "transactionAmounts/transactionShares/value")
        price = text(tx, "transactionAmounts/transactionPricePerShare/value")
        date = text(tx, "transactionDate/value")
        if not shares or not date:
            continue
        transactions.append({
            "ticker": ticker,
            "issuerName": issuer_name,
            "insiderName": owner_name,
            "title": title,
            "type": TRANSACTION_TYPE_MAP[code],
            "shares": float(shares),
            "pricePerShare": float(price) if price else None,
            "transactionDate": date,
        })
    return ticker, transactions


def git_commit_and_push(repo_dir, files):
    try:
        subprocess.run(["git", "-C", repo_dir, "add"] + files, check=True, timeout=60)
        diff = subprocess.run(["git", "-C", repo_dir, "diff", "--cached", "--quiet"], timeout=60)
        if diff.returncode == 0:
            log.info("No changes since last push — skipping commit")
            return
        now = datetime.now(timezone.utc).isoformat()
        subprocess.run(["git", "-C", repo_dir, "commit", "-m", f"insider trades update {now}"], check=True, timeout=60)
        # Reconcile with anything pushed to the repo elsewhere before pushing,
        # so ours fast-forwards. Without this, one outside push would
        # non-fast-forward-reject every future push and silently freeze the
        # feed. On conflict, abort and retry next cycle rather than wedge the repo.
        pull = subprocess.run(["git", "-C", repo_dir, "pull", "--rebase", "origin", "main"],
                              capture_output=True, timeout=60)
        if pull.returncode != 0:
            subprocess.run(["git", "-C", repo_dir, "rebase", "--abort"], capture_output=True, timeout=60)
            log.error("git pull --rebase failed; skipping push this cycle: "
                      + (pull.stderr.decode(errors="replace")[:200] if pull.stderr else ""))
            return
        subprocess.run(["git", "-C", repo_dir, "push"], check=True, timeout=60)
        log.info("Pushed updated insider_trades.json to GitHub — Vercel will redeploy shortly")
    except subprocess.CalledProcessError as e:
        log.error(f"git commit/push failed: {e}")
    except subprocess.TimeoutExpired as e:
        log.error(f"git command timed out (hung connection?): {e}")


def build_by_ticker(trades):
    by_ticker = {}
    for t in trades:
        tk = by_ticker.setdefault(t["ticker"], {"issuerName": t["issuerName"], "trades": []})
        tk["trades"].append(t)
    for tk in by_ticker.values():
        tk["trades"].sort(key=lambda x: x["transactionDate"], reverse=True)
    return by_ticker


def load_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
        seen = set(data.get("_meta", {}).get("seenAccessions", []))
        trades = data.get("trades", [])
        log.info(f"Loaded {len(trades)} existing insider trades, {len(seen)} known accessions from {path}")
        return trades, seen
    except Exception:
        return [], set()


def write_output(trades, seen_accessions, path):
    trades.sort(key=lambda t: t.get("transactionDate") or "", reverse=True)
    trades = trades[:3000]
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "SEC EDGAR Form 4 filings (sec.gov) — scoped to tickers currently passing Undercurrent's Full Market screen",
        "count": len(trades),
        "trades": trades,
        "byTicker": build_by_ticker(trades),
        "_meta": {"seenAccessions": sorted(seen_accessions)},
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"Wrote {len(trades)} insider trades across {len(payload['byTicker'])} tickers to {path}")


def main():
    out_path = os.path.join(REPO_DIR, "insider_trades.json") if REPO_DIR else "output/insider_trades.json"
    if not REPO_DIR:
        log.warning("REPO_DIR not set — writing to ./output instead of a git repo")
        os.makedirs("output", exist_ok=True)

    relevant_tickers = load_relevant_tickers(REPO_DIR)
    existing_trades, seen_accessions = load_existing(out_path)

    try:
        accessions = fetch_current_form4_accessions()
    except Exception as e:
        log.error(f"Couldn't reach SEC EDGAR: {e}")
        sys.exit(1)

    new_accessions = [(acc, url) for acc, url in accessions if acc not in seen_accessions]
    log.info(f"Found {len(accessions)} Form 4 filings in the current feed, {len(new_accessions)} not seen before")

    new_trades = []
    kept_tickers = 0
    for i, (acc, index_url) in enumerate(new_accessions):
        if i and i % 20 == 0:
            log.info(f"Processed {i}/{len(new_accessions)} new filings so far")
        try:
            xml_url = fetch_filing_xml_url(index_url)
            if not xml_url:
                seen_accessions.add(acc)
                continue
            resp = requests.get(xml_url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            ticker, transactions = parse_form4(resp.content)
            seen_accessions.add(acc)
            if ticker and ticker in relevant_tickers and transactions:
                new_trades.extend(transactions)
                kept_tickers += 1
        except Exception as e:
            log.warning(f"Couldn't process filing {acc}: {e}")
        time.sleep(0.3)  # SEC's fair-access policy: stay well under 10 req/sec

    log.info(f"Kept {len(new_trades)} open-market transactions from {kept_tickers} filings matching relevant tickers")

    all_trades = existing_trades + new_trades
    write_output(all_trades, seen_accessions, out_path)

    if REPO_DIR:
        git_commit_and_push(REPO_DIR, ["insider_trades.json"])


if __name__ == "__main__":
    main()
