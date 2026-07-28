#!/usr/bin/env python3
"""
Undercurrent Backtest Tracker — runs on the Pi once a day. Tracks
whether the app's own "Buy"/"Accumulate" conviction score actually
predicts anything, by logging the entry price the day a stock first
qualifies for a signal and checking its price again every day after.

Zero subscriptions, zero paid APIs — reuses the same Nasdaq public
chart API already validated for sector_history_scanner.py.

WHY THIS CAN ONLY TRACK FORWARD, NOT BACKTEST THE PAST
stocks.json is a rolling current-state snapshot that gets overwritten
every scan pass — there has never been a stored history of which
stocks had a Buy signal on a given past date. So this script starts
a real, honest track record from whenever it's first run onward; it
cannot retroactively reconstruct what the signal "would have said"
six months ago.

WHAT IT DOES
1. Reads stocks.json for tickers currently signaled BUY or ACCUMULATE.
2. For any such ticker not already being tracked as an active signal,
   logs a new entry: ticker, signal type, entry date, entry price,
   conviction score at the time. This only fires once per signal
   "episode" — a stock that stays BUY for three months doesn't create
   90 near-duplicate entries, just one, from the day it first
   qualified. If it later drops out and re-qualifies afterward, that's
   a genuinely new episode and gets its own entry.
3. Refreshes every existing entry's current price and computes its
   return since entry.
4. Computes aggregate stats (win rate, average return) bucketed by how
   long ago each entry started — a 3-day-old entry hasn't had time to
   prove anything, a 180-day-old one has.
5. Writes backtest.json and commits + pushes, same pattern as the
   other Pi scanners.

HONEST LIMITATIONS
  - Forward-only, as explained above — treat early results (the first
    few months) as too small a sample to mean much either way.
  - Uses each entry's most recent available close as "current price,"
    not a specific hold-to-date strategy — this measures "how did the
    stock do since the signal fired," not any particular buy/sell
    trading rule.
  - Entry/current prices come from Nasdaq's public chart API, the same
    source already used for sector history — real market closes, not
    the Finnhub quote stocks.json itself uses, so there can be small
    day-to-day discrepancies between the two.
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

TRACKED_SIGNALS = {"BUY", "ACCUMULATE"}
AGE_BUCKETS = [("30d", 30), ("90d", 90), ("180d", 180), ("365d", 365)]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("backtest_tracker")


def load_current_signals(repo_dir):
    path = os.path.join(repo_dir, "stocks.json") if repo_dir else "output/stocks.json"
    try:
        with open(path) as f:
            data = json.load(f)
        return {s["ticker"]: s for s in data.get("stocks", []) if s.get("signal") in TRACKED_SIGNALS}
    except Exception as e:
        log.warning(f"Couldn't load stocks.json ({e}) — no new signals will be logged this run")
        return {}


def fetch_latest_close(ticker):
    """A lightweight recent-window fetch (not the full multi-year
    history sector_history_scanner.py pulls) just to get the latest
    available close for one ticker."""
    today = datetime.now(timezone.utc).date()
    fromdate = today - timedelta(days=10)
    url = NASDAQ_CHART_URL.format(symbol=ticker)
    params = {"assetclass": "stocks", "fromdate": fromdate.isoformat(), "todate": today.isoformat()}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    chart = (data.get("data") or {}).get("chart") or []
    if not chart:
        return None, None
    last = chart[-1]
    z = last.get("z") or {}
    close = z.get("close")
    dt_str = z.get("dateTime")
    if close is None or not dt_str:
        return None, None
    try:
        return float(str(close).replace(",", "")), datetime.strptime(dt_str, "%m/%d/%Y").date().isoformat()
    except (ValueError, TypeError):
        return None, None


def load_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("entries", []), set(data.get("_meta", {}).get("activeTickers", []))
    except Exception:
        return [], set()


def compute_stats(entries):
    now = datetime.now(timezone.utc).date()
    stats = {}
    for label, min_days in AGE_BUCKETS:
        cohort = [e for e in entries if e.get("returnPct") is not None
                  and (now - datetime.fromisoformat(e["entryDate"]).date()).days >= min_days]
        if not cohort:
            stats[label] = {"count": 0, "winRate": None, "avgReturn": None}
            continue
        wins = sum(1 for e in cohort if e["returnPct"] > 0)
        stats[label] = {
            "count": len(cohort),
            "winRate": round(wins / len(cohort) * 100, 1),
            "avgReturn": round(sum(e["returnPct"] for e in cohort) / len(cohort), 2),
        }
    return stats


def git_commit_and_push(repo_dir, files):
    try:
        subprocess.run(["git", "-C", repo_dir, "add"] + files, check=True, timeout=60)
        diff = subprocess.run(["git", "-C", repo_dir, "diff", "--cached", "--quiet"], timeout=60)
        if diff.returncode == 0:
            log.info("No changes since last push — skipping commit")
            return
        now = datetime.now(timezone.utc).isoformat()
        subprocess.run(["git", "-C", repo_dir, "commit", "-m", f"backtest tracker update {now}"], check=True, timeout=60)
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
        log.info("Pushed updated backtest.json to GitHub — Vercel will redeploy shortly")
    except subprocess.CalledProcessError as e:
        log.error(f"git commit/push failed: {e}")
    except subprocess.TimeoutExpired as e:
        log.error(f"git command timed out (hung connection?): {e}")


def main():
    out_path = os.path.join(REPO_DIR, "backtest.json") if REPO_DIR else "output/backtest.json"
    if not REPO_DIR:
        log.warning("REPO_DIR not set — writing to ./output instead of a git repo")
        os.makedirs("output", exist_ok=True)

    entries, active_tickers = load_existing(out_path)
    current_signals = load_current_signals(REPO_DIR)
    today = datetime.now(timezone.utc).date().isoformat()

    # New signal episodes: currently signaled, not already being tracked as active
    new_count = 0
    for ticker, s in current_signals.items():
        if ticker in active_tickers:
            continue
        price = s.get("price")
        if not price:
            continue
        entries.append({
            "ticker": ticker, "signal": s["signal"], "entryDate": today, "entryPrice": price,
            "convictionScore": s.get("score"), "currentPrice": price, "currentDate": today, "returnPct": 0.0,
        })
        active_tickers.add(ticker)
        new_count += 1
    log.info(f"Logged {new_count} new signal episodes")

    # Episodes that dropped out of the signal — stop tracking as "active" (they
    # keep their permanent entry, just won't re-trigger until they re-qualify)
    dropped = active_tickers - set(current_signals.keys())
    active_tickers -= dropped
    if dropped:
        log.info(f"{len(dropped)} tickers dropped out of signal status: {sorted(dropped)}")

    # Refresh current price + return for every entry
    unique_tickers = sorted({e["ticker"] for e in entries})
    prices = {}
    for i, ticker in enumerate(unique_tickers):
        if i and i % 30 == 0:
            log.info(f"Refreshed prices for {i}/{len(unique_tickers)} tracked tickers so far")
        try:
            price, date = fetch_latest_close(ticker)
            if price:
                prices[ticker] = (price, date)
        except Exception as e:
            log.warning(f"Couldn't refresh price for {ticker}: {e}")
        time.sleep(0.3)

    for e in entries:
        if e["ticker"] in prices:
            price, date = prices[e["ticker"]]
            e["currentPrice"] = price
            e["currentDate"] = date
            e["returnPct"] = round((price - e["entryPrice"]) / e["entryPrice"] * 100, 2)

    stats = compute_stats(entries)
    log.info(f"Stats: {stats}")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Undercurrent's own conviction score, tracked forward from each signal's first day — see module docstring for why this can't be retroactive",
        "count": len(entries),
        "entries": entries,
        "stats": stats,
        "_meta": {"activeTickers": sorted(active_tickers)},
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"Wrote {len(entries)} tracked signal episodes to {out_path}")

    if REPO_DIR:
        git_commit_and_push(REPO_DIR, ["backtest.json"])


if __name__ == "__main__":
    main()
