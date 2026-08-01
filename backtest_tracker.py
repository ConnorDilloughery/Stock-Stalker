#!/usr/bin/env python3
"""
Undercurrent Backtest Tracker — an HONEST forward track record for the app's
signals. Runs daily on the Pi.

The only question that matters for any of these signals is: does it beat just
buying the index? So this doesn't report "% that went up" (meaningless if the
whole market went up) — it reports, per signal and per holding horizon, the
EXCESS return over SPY and the share of picks that actually BEAT SPY.

WHAT IT TRACKS (each as its own signal, tagged so we can compare them)
  * value      — stocks.json names currently rated BUY / ACCUMULATE
  * radar      — radar.json convergence picks
  * earlymover — early_movers.json momentum-emergence picks

FORWARD-ONLY (not retroactive)
  These files are rolling current-state snapshots with no stored past, so this
  can only start a real record from the day it first runs each signal forward.
  Treat the first few months as too small a sample to mean anything.

HOW THE MATH WORKS
  * One episode per (ticker, signal): logged the first day it qualifies, with
    entry price and SPY's close that day. It re-logs only if it drops out and
    later re-qualifies.
  * As each entry ages past a horizon (7/30/90/180/365 days), its return AT
    that horizon is captured once, alongside SPY's return over the same window,
    and the excess (stock − SPY) is frozen in. Point-in-time, no look-ahead.
  * Stats per signal × horizon: count, % that beat SPY, average excess return,
    plus the raw average stock and SPY returns for context.

DATA
  Prices and the SPY benchmark come from Nasdaq's public daily chart API (same
  source the technicals and sector scanners use) — real closes, one free feed.
"""

import os
import sys
import json
import time
import bisect
import logging
from datetime import datetime, timezone, timedelta

import requests

import scanner_git

REPO_DIR = os.environ.get("REPO_DIR", "").strip()
NASDAQ_CHART_URL = "https://api.nasdaq.com/api/quote/{symbol}/chart"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TRACKED_VALUE = {"BUY", "ACCUMULATE"}
HORIZONS = [7, 30, 90, 180, 365]      # trading-forward days to measure at
BENCHMARK = "SPY"
FETCH_DELAY = 0.35

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("backtest_tracker")


def _load(name):
    path = os.path.join(REPO_DIR, name) if REPO_DIR else os.path.join("output", name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Couldn't load {name} ({e})")
        return {}


def _key(ticker, signal_type):
    return ticker + "|" + signal_type


def load_current_signals():
    """{(ticker, signalType): {price, score, detail}} across all three feeds."""
    out = {}
    for s in _load("stocks.json").get("stocks", []):
        tk, price = s.get("ticker"), s.get("price")
        if tk and price and s.get("signal") in TRACKED_VALUE:
            out[(tk, "value")] = {"price": price, "score": s.get("score"), "detail": s["signal"]}
    for p in _load("radar.json").get("picks", []):
        tk, price = p.get("ticker"), p.get("price")
        if tk and price:
            out[(tk, "radar")] = {"price": price, "score": p.get("radarScore"),
                                  "detail": f"{p.get('signalCount', '?')} signals"}
    for m in _load("early_movers.json").get("movers", []):
        tk, price = m.get("ticker"), m.get("price")
        if tk and price:
            out[(tk, "earlymover")] = {"price": price, "score": m.get("emScore"),
                                       "detail": f"{m.get('signalCount', '?')} signals"}
    return out


def fetch_history(symbol, assetclass="stocks"):
    """{isodate: close} of daily closes, or {} on failure."""
    today = datetime.now(timezone.utc).date()
    frm = today - timedelta(days=420)
    try:
        resp = requests.get(NASDAQ_CHART_URL.format(symbol=symbol),
                            params={"assetclass": assetclass, "fromdate": frm.isoformat(), "todate": today.isoformat()},
                            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=20)
        resp.raise_for_status()
        chart = (resp.json().get("data") or {}).get("chart") or []
        out = {}
        for pt in chart:
            z = pt.get("z") or {}
            c, d = z.get("close"), z.get("dateTime")
            if c is None or not d:
                continue
            try:
                iso = datetime.strptime(d, "%m/%d/%Y").date().isoformat()
                out[iso] = float(str(c).replace(",", ""))
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        log.warning(f"History fetch failed for {symbol} ({e})")
        return {}


def price_asof(by_date, sorted_dates, target_iso):
    """Latest close on or before target_iso (nearest prior trading day)."""
    if not sorted_dates:
        return None
    i = bisect.bisect_right(sorted_dates, target_iso) - 1
    if i < 0:
        return None
    return by_date.get(sorted_dates[i])


def latest(by_date, sorted_dates):
    return (sorted_dates[-1], by_date[sorted_dates[-1]]) if sorted_dates else (None, None)


def compute_stats(entries):
    """Per signalType × horizon: count, % beat SPY, avg excess, avg stock ret,
    avg SPY ret — the honest 'did it beat the index' table."""
    stats = {}
    for e in entries:
        st = e.get("signalType", "value")
        for h in HORIZONS:
            rec = (e.get("horizons") or {}).get(str(h))
            if not rec or rec.get("excess") is None:
                continue
            b = stats.setdefault(st, {}).setdefault(str(h),
                                                    {"n": 0, "beat": 0, "sumEx": 0.0, "sumRet": 0.0, "sumSpy": 0.0})
            b["n"] += 1
            b["beat"] += 1 if rec["excess"] > 0 else 0
            b["sumEx"] += rec["excess"]
            b["sumRet"] += rec.get("stockRet", 0.0)
            b["sumSpy"] += rec.get("spyRet", 0.0)
    out = {}
    for st, hs in stats.items():
        out[st] = {}
        for h, b in hs.items():
            n = b["n"]
            out[st][h] = {
                "count": n,
                "beatRate": round(b["beat"] / n * 100, 1),
                "avgExcess": round(b["sumEx"] / n, 2),
                "avgReturn": round(b["sumRet"] / n, 2),
                "avgSpy": round(b["sumSpy"] / n, 2),
            }
    return out


def main():
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    out_path = os.path.join(REPO_DIR, "backtest.json") if REPO_DIR else "output/backtest.json"
    if not REPO_DIR:
        os.makedirs("output", exist_ok=True)

    existing = _load("backtest.json") if REPO_DIR else {}
    entries = existing.get("entries", [])
    active = set(existing.get("_meta", {}).get("activeKeys", []))
    # Back-compat: older entries had no signalType — they were the value signal.
    for e in entries:
        e.setdefault("signalType", "value")
        e.setdefault("horizons", {})

    # SPY benchmark history (one fetch).
    spy = fetch_history(BENCHMARK, assetclass="etf")
    spy_dates = sorted(spy.keys())
    _, spy_now = latest(spy, spy_dates)
    if not spy:
        log.warning("No SPY history — excess returns can't be computed this run")

    # New episodes across all three feeds.
    current = load_current_signals()
    new_count = 0
    for (tk, st), sig in current.items():
        if _key(tk, st) in active:
            continue
        entries.append({
            "ticker": tk, "signalType": st, "entryDate": today_iso,
            "entryPrice": sig["price"], "spyEntry": spy_now,
            "score": sig.get("score"), "detail": sig.get("detail"),
            "currentPrice": sig["price"], "currentDate": today_iso, "returnPct": 0.0,
            "horizons": {},
        })
        active.add(_key(tk, st))
        new_count += 1
    log.info(f"Logged {new_count} new episodes ({len(current)} live signals across value/radar/earlymover)")

    # Episodes no longer signaled stop being 'active' (keep their record).
    still = {_key(tk, st) for (tk, st) in current}
    dropped = active - still
    active -= dropped
    if dropped:
        log.info(f"{len(dropped)} episodes dropped out of signal status")

    # Fetch each tracked ticker's history once, then refresh returns + fill any
    # horizons that have newly matured.
    tickers = sorted({e["ticker"] for e in entries})
    log.info(f"Refreshing {len(tickers)} tracked tickers across {len(entries)} episodes")
    hist = {}
    for i, tk in enumerate(tickers):
        if i and i % 50 == 0:
            log.info(f"  {i}/{len(tickers)} histories fetched")
        h = fetch_history(tk)
        if h:
            hist[tk] = (h, sorted(h.keys()))
        time.sleep(FETCH_DELAY)

    for e in entries:
        pack = hist.get(e["ticker"])
        if not pack:
            continue
        by_date, sdates = pack
        cur_date, cur_price = latest(by_date, sdates)
        if cur_price:
            e["currentPrice"] = cur_price
            e["currentDate"] = cur_date
            if e.get("entryPrice"):
                e["returnPct"] = round((cur_price - e["entryPrice"]) / e["entryPrice"] * 100, 2)
        # Backfill SPY entry price for older entries.
        if not e.get("spyEntry") and spy:
            e["spyEntry"] = price_asof(spy, spy_dates, e["entryDate"])
        # Freeze in each horizon as it matures (measured once).
        entry_d = datetime.fromisoformat(e["entryDate"]).date()
        age = (today - entry_d).days
        for h in HORIZONS:
            hk = str(h)
            if hk in (e.get("horizons") or {}) or age < h:
                continue
            target = (entry_d + timedelta(days=h)).isoformat()
            sp = price_asof(by_date, sdates, target)
            spy_e, spy_h = e.get("spyEntry"), price_asof(spy, spy_dates, target)
            if sp and e.get("entryPrice") and spy_e and spy_h:
                stock_ret = round((sp / e["entryPrice"] - 1) * 100, 2)
                spy_ret = round((spy_h / spy_e - 1) * 100, 2)
                e.setdefault("horizons", {})[hk] = {
                    "stockRet": stock_ret, "spyRet": spy_ret, "excess": round(stock_ret - spy_ret, 2),
                }

    stats = compute_stats(entries)
    log.info(f"Stats by signal × horizon: {json.dumps(stats)}")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "benchmark": BENCHMARK,
        "horizons": HORIZONS,
        "source": "Undercurrent's own signals (value / radar / early-mover), tracked forward vs SPY from each signal's first day — forward-only, not retroactive.",
        "count": len(entries),
        "entries": entries,
        "stats": stats,
        "_meta": {"activeKeys": sorted(active)},
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, out_path)
    log.info(f"Wrote {len(entries)} episodes to {out_path}")

    if REPO_DIR:
        scanner_git.commit_and_push(REPO_DIR, ["backtest.json"],
                                    f"backtest tracker update {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
