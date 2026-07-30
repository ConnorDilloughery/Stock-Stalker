#!/usr/bin/env python3
"""
Undercurrent Sector History Scanner — runs on the Pi once a day, pulls
20+ years of daily price history for the 7 sector-representative ETFs,
plus a handful of sub-industry ETFs within each sector (e.g. Technology
→ Semiconductors, Software), from Nasdaq's public chart API (the same
source market_scanner.py already uses for its ticker universe — no API
key, no rate limits observed), and computes two things for each:

  1. Percent change over 1 day / 1 week / 1 month / 3 months / YTD /
     1 year / 5 years.
  2. Quarterly closing prices for the last 20 years, for an indexed
     (start = 100) performance chart.

Zero subscriptions, zero paid APIs.

WHAT IT DOES
Each of the app's 7 sector buckets (same taxonomy already used to tag
stocks in market_scanner.py / index.html) is represented by its SPDR
sector ETF — Technology→XLK, Healthcare→XLV, Industrials→XLI,
Energy→XLE, Consumer→XLY, Financials→XLF, Materials→XLB. Each sector
also gets 1-2 sub-industry ETFs (see SUBSECTOR_ETFS below), e.g.
Technology's Semiconductors (XSD) and Software (XSW), so the Sectors
tab can show trends within a sector, not just the sector as a whole.
For each ticker (sector or sub-industry), this fetches the full daily
close history, then:
  - Walks backward from today to find the closing price nearest each
    lookback window (trading days don't line up with calendar days —
    weekends/holidays mean "7 days ago" often isn't a trading day, so
    this uses the nearest prior trading day's close instead).
  - Buckets daily closes into quarters, keeping the last (most recent)
    close in each quarter — i.e. the quarter's actual closing price,
    not an average.
Writes sector_history.json and commits + pushes it, same pattern as
the other Pi scanners.

HONEST LIMITATIONS
  - "1 year ago" / "5 years ago" are simple day-count approximations
    (365 / 365*5), not calendar-exact anniversaries — close enough for
    a percent-change display, not appropriate for anything needing
    exact-date precision.
  - If Nasdaq's chart API changes shape or drops historical range, this
    logs a warning and skips that sector rather than guessing.
"""

import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta

import requests

REPO_DIR = os.environ.get("REPO_DIR", "").strip()
NASDAQ_CHART_URL = "https://api.nasdaq.com/api/quote/{symbol}/chart"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Same 7-bucket sector taxonomy already used for stock classification —
# see SECTOR_KEYWORDS in market_scanner.py / UNIVERSE in index.html.
SECTOR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Consumer": "XLY",
    "Financials": "XLF",
    "Materials": "XLB",
}

# Sub-industry ETFs within each broad sector, same "no paid data" Nasdaq
# chart API source — lets the Sectors tab show trends within a sector
# (e.g. Semiconductors vs Software within Technology), not just the
# broad sector itself.
SUBSECTOR_ETFS = {
    "Technology": {
        "Semiconductors": "XSD", "Software": "XSW", "Internet": "FDN",
        "Telecom": "XTL", "Cybersecurity": "CIBR", "Cloud Computing": "SKYY",
        "Robotics & AI": "BOTZ",
    },
    "Healthcare": {
        "Biotech": "XBI", "Pharmaceuticals": "XPH", "Health Care Services": "XHS",
        "Health Care Equipment": "XHE", "Medical Devices": "IHI",
        "Biotech (Large Cap)": "IBB", "Genomics": "ARKG",
    },
    "Financials": {
        "Regional Banks": "KRE", "Insurance": "KIE", "Capital Markets": "KCE",
        "Broker-Dealers": "IAI", "Banks": "KBWB", "Financial Services": "IYG",
        "FinTech": "FINX",
    },
    "Energy": {
        "Oil & Gas E&P": "XOP", "Oil & Gas Equipment & Services": "XES",
        "MLP / Midstream": "AMLP", "Uranium": "URA", "Solar": "TAN",
        "Clean Energy": "ICLN", "Natural Gas": "FCG",
    },
    "Industrials": {
        "Aerospace & Defense": "XAR", "Transportation": "XTN", "Airlines": "JETS",
        "Infrastructure": "PAVE", "Robotics & Automation": "ROBO",
    },
    "Consumer": {
        "Retail": "XRT", "Homebuilders": "XHB", "Online Retail": "IBUY",
        "Leisure & Entertainment": "PEJ", "Autos & Future Vehicles": "CARZ",
        "Sports Betting & iGaming": "BETZ",
    },
    "Materials": {
        "Metals & Mining": "XME", "Gold Miners": "GDX", "Steel": "SLX",
        "Lithium & Battery Tech": "LIT", "Copper Miners": "COPX",
        "Agribusiness": "MOO", "Timber": "WOOD",
    },
}

HISTORY_YEARS = 21  # comfortably covers the 20-year quarterly ask plus
                     # room for the 5-year change window's lookback
QUARTERLY_YEARS = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sector_history")


def fetch_history(ticker):
    today = datetime.now(timezone.utc).date()
    fromdate = today - timedelta(days=HISTORY_YEARS * 365)
    url = NASDAQ_CHART_URL.format(symbol=ticker)
    params = {"assetclass": "etf", "fromdate": fromdate.isoformat(), "todate": today.isoformat()}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    chart = (data.get("data") or {}).get("chart") or []
    points = []
    for c in chart:
        z = c.get("z") or {}
        dt_str = z.get("dateTime")
        close = z.get("close")
        if not dt_str or close is None:
            continue
        try:
            dt = datetime.strptime(dt_str, "%m/%d/%Y").date()
            points.append((dt, float(str(close).replace(",", ""))))
        except (ValueError, TypeError):
            continue
    points.sort(key=lambda p: p[0])
    return points


def nearest_on_or_before(points, target_date):
    """Trading days don't align with calendar days (weekends/holidays),
    so this walks to the nearest prior trading day's close instead of
    requiring an exact date match."""
    best = None
    for d, price in points:
        if d <= target_date:
            best = price
        else:
            break
    return best


def pct_change(latest, past):
    return round((latest - past) / past * 100, 2) if past else None


def compute_changes(points):
    if not points:
        return {}
    latest_date, latest_price = points[-1]
    windows = {"oneDay": 1, "oneWeek": 7, "oneMonth": 30, "threeMonth": 91, "oneYear": 365, "fiveYear": 365 * 5}
    changes = {name: pct_change(latest_price, nearest_on_or_before(points, latest_date - timedelta(days=d)))
               for name, d in windows.items()}
    ytd_target = latest_date.replace(month=1, day=1) - timedelta(days=1)
    changes["ytd"] = pct_change(latest_price, nearest_on_or_before(points, ytd_target))
    return changes


def compute_quarterly(points, years=QUARTERLY_YEARS):
    if not points:
        return []
    cutoff = points[-1][0] - timedelta(days=years * 366)
    by_quarter = {}
    for d, price in points:
        if d < cutoff:
            continue
        q = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
        by_quarter[q] = price  # points are date-ascending, so the last write per quarter
                                # is that quarter's actual closing price, not an average
    return [{"quarter": q, "close": round(p, 2)} for q, p in by_quarter.items()]


def git_commit_and_push(repo_dir, files):
    try:
        subprocess.run(["git", "-C", repo_dir, "add"] + files, check=True, timeout=60)
        diff = subprocess.run(["git", "-C", repo_dir, "diff", "--cached", "--quiet"], timeout=60)
        if diff.returncode == 0:
            log.info("No changes since last push — skipping commit")
            return
        now = datetime.now(timezone.utc).isoformat()
        subprocess.run(["git", "-C", repo_dir, "commit", "-m", f"sector history update {now}"], check=True, timeout=60)
        # Reconcile with anything pushed to the repo elsewhere before pushing,
        # so ours fast-forwards. Without this, one outside push would
        # non-fast-forward-reject every future push and silently freeze the
        # feed. On conflict, abort and retry next cycle rather than wedge the repo.
        pull = subprocess.run(["git", "-C", repo_dir, "pull", "--rebase", "--autostash", "origin", "main"],
                              capture_output=True, timeout=60)
        if pull.returncode != 0:
            subprocess.run(["git", "-C", repo_dir, "rebase", "--abort"], capture_output=True, timeout=60)
            log.error("git pull --rebase failed; skipping push this cycle: "
                      + (pull.stderr.decode(errors="replace")[:200] if pull.stderr else ""))
            return
        subprocess.run(["git", "-C", repo_dir, "push"], check=True, timeout=60)
        log.info("Pushed updated sector_history.json to GitHub — Vercel will redeploy shortly")
    except subprocess.CalledProcessError as e:
        log.error(f"git commit/push failed: {e}")
    except subprocess.TimeoutExpired as e:
        log.error(f"git command timed out (hung connection?): {e}")


def fetch_etf_entry(label, ticker):
    points = fetch_history(ticker)
    if not points:
        log.warning(f"No data returned for {label} ({ticker}) — skipping")
        return None
    entry = {
        "ticker": ticker,
        "latestPrice": points[-1][1],
        "latestDate": points[-1][0].isoformat(),
        "changes": compute_changes(points),
        "quarterly": compute_quarterly(points),
    }
    log.info(f"{label} ({ticker}): {len(points)} trading days, changes={entry['changes']}")
    return entry


def main():
    out_path = os.path.join(REPO_DIR, "sector_history.json") if REPO_DIR else "output/sector_history.json"
    if not REPO_DIR:
        log.warning("REPO_DIR not set — writing to ./output instead of a git repo")
        os.makedirs("output", exist_ok=True)

    sectors = {}
    for sector, ticker in SECTOR_ETFS.items():
        try:
            entry = fetch_etf_entry(sector, ticker)
            if entry:
                sectors[sector] = entry
        except Exception as e:
            log.warning(f"Failed to fetch {sector} ({ticker}): {e}")
        time.sleep(1)  # be polite

        subsectors = {}
        for subname, subticker in SUBSECTOR_ETFS.get(sector, {}).items():
            try:
                sub_entry = fetch_etf_entry(f"{sector} / {subname}", subticker)
                if sub_entry:
                    subsectors[subname] = sub_entry
            except Exception as e:
                log.warning(f"Failed to fetch {sector} / {subname} ({subticker}): {e}")
            time.sleep(1)  # be polite
        if subsectors and sector in sectors:
            sectors[sector]["subsectors"] = subsectors

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Nasdaq public chart API (api.nasdaq.com) — sector-representative SPDR ETFs, plus sub-industry SPDR ETFs within each sector",
        "sectors": sectors,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"Wrote sector history for {len(sectors)} sectors to {out_path}")

    if REPO_DIR:
        git_commit_and_push(REPO_DIR, ["sector_history.json"])


if __name__ == "__main__":
    main()
