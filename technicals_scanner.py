#!/usr/bin/env python3
"""
Undercurrent Technicals — daily price-history indicators for the Legends
screens (O'Neil / Murphy) that fundamentals alone can't answer.

WHY
  The Full Market scan (market_scanner.py) publishes fundamentals only, so the
  Legends technical checks — 50/200-day moving-average trend, RSI/MACD, volume
  confirmation, and true Relative Strength — had to show "—". This scanner
  fetches ~1 year of daily OHLCV from Nasdaq's public chart API (the same
  source radar_scanner uses for returns), computes those indicators, and
  publishes technicals.json for the app to merge into the Legends by ticker.

SCOPE
  Computing indicators for all ~6,000 universe names every run would hammer a
  free endpoint, so we prioritise the names the technical screens actually care
  about: those nearest their 52-week high (momentum leaders), capped at
  MAX_TECH. Names outside the set keep showing "—" — honest, unchanged.

HONEST LIMITATIONS
  - Point-in-time daily closes from one free source; not survivorship-adjusted,
    no intraday, no splits/dividends beyond what Nasdaq already reflects.
  - Relative Strength is a percentile rank of a blended 3/6/12-month return
    across the fetched set — a good IBD-style approximation, not the exact RS
    rating. Chart patterns and share float still aren't derivable here.
"""

import os
import sys
import json
import time
import tempfile
import logging
from datetime import datetime, timezone, timedelta

import requests

import scanner_git

REPO_DIR = os.environ.get("REPO_DIR", "").strip()
NASDAQ_CHART_URL = "https://api.nasdaq.com/api/quote/{symbol}/chart"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_TECH = int(os.environ.get("MAX_TECH", "800"))  # how many names to fetch history for
FETCH_DELAY = 0.4                                    # be polite to the free endpoint
HISTORY_DAYS = 420                                   # calendar days requested (~280 trading days)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("technicals_scanner")


def _load(name):
    path = os.path.join(REPO_DIR, name) if REPO_DIR else os.path.join("output", name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Couldn't load {name} ({e})")
        return {}


def _atomic_dump(path, payload):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    """Wilder's RSI over the last n periods."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def macd_state(closes):
    """MACD(12/26/9) → (macd_line, signal_line), or (None, None) if too short."""
    if len(closes) < 35:
        return None, None
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    signal = ema_series(macd, 9)
    return macd[-1], signal[-1]


def ret(closes, n):
    if len(closes) > n and closes[-1 - n]:
        return closes[-1] / closes[-1 - n] - 1
    return None


def vol_trend_up(vols):
    """Recent 10-day average volume above the 50-day average — rising interest."""
    v10 = sma(vols, 10)
    v50 = sma(vols, 50)
    if v10 is None or v50 is None or v50 == 0:
        return None
    return v10 > v50


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_history(symbol, assetclass="stocks"):
    """Chronological (closes, volumes) from Nasdaq daily candles, or None.
    ETFs (e.g. the SPY market proxy) need assetclass="etf"."""
    today = datetime.now(timezone.utc).date()
    frm = today - timedelta(days=HISTORY_DAYS)
    try:
        resp = requests.get(
            NASDAQ_CHART_URL.format(symbol=symbol),
            params={"assetclass": assetclass, "fromdate": frm.isoformat(), "todate": today.isoformat()},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        chart = (resp.json().get("data") or {}).get("chart") or []
        closes, vols = [], []
        for pt in chart:
            z = pt.get("z") or {}
            c, v = z.get("close"), z.get("volume")
            try:
                cf = float(str(c).replace(",", ""))
            except (ValueError, TypeError):
                continue
            try:
                vf = float(str(v).replace(",", ""))
            except (ValueError, TypeError):
                vf = 0.0
            closes.append(cf)
            vols.append(vf)
        return (closes, vols) if len(closes) >= 30 else None
    except Exception as e:
        log.warning(f"History fetch failed for {symbol} ({e})")
        return None


def indicators_for(closes, vols):
    """The per-name technical bundle plus the raw blended return used for RS."""
    s50, s200 = sma(closes, 50), sma(closes, 200)
    ml, sig = macd_state(closes)
    # Bullish momentum = MACD line above its signal AND above zero (12-EMA over
    # 26-EMA), which stays bearish through a steady downtrend (histogram alone
    # wouldn't — it only measures acceleration).
    macd_bull = (ml > sig and ml > 0) if ml is not None else None
    # IBD-style blended relative-strength input: weight recent more heavily.
    parts = [(ret(closes, 63), 0.4), (ret(closes, 126), 0.2),
             (ret(closes, 189), 0.2), (ret(closes, 252), 0.2)]
    avail = [(r, w) for r, w in parts if r is not None]
    rs_raw = sum(r * w for r, w in avail) / sum(w for _, w in avail) if avail else None
    return {
        "price": round(closes[-1], 2),
        "sma50": round(s50, 2) if s50 is not None else None,
        "sma200": round(s200, 2) if s200 is not None else None,
        "rsi": rsi(closes),
        "macdBull": macd_bull,
        "volTrendUp": vol_trend_up(vols),
        "asOf": datetime.now(timezone.utc).date().isoformat(),
    }, rs_raw


def pick_candidates(universe, stocks):
    """Momentum leaders first (nearest their 52-week high), which is what the
    technical screens care about, then fill with value names; capped."""
    uni = [s for s in universe.get("stocks", []) if s.get("ticker")]
    # nearest-high first (smallest discount). None discounts sort last.
    uni.sort(key=lambda s: (s.get("discount") if isinstance(s.get("discount"), (int, float)) else 1.0))
    order = [s["ticker"] for s in uni]
    seen = set(order)
    for s in stocks.get("stocks", []):
        tk = s.get("ticker")
        if tk and tk not in seen:
            order.append(tk)
            seen.add(tk)
    return order[:MAX_TECH]


def main():
    if not REPO_DIR:
        log.warning("REPO_DIR not set — reading/writing ./output")

    universe = _load("universe.json")
    stocks = _load("stocks.json")
    candidates = pick_candidates(universe, stocks)
    log.info(f"Technicals: {len(candidates)} candidates (cap {MAX_TECH})")

    # Overall market trend from SPY — answers O'Neil's "M" (market in uptrend).
    market = {}
    spy = fetch_history("SPY", assetclass="etf")
    if spy and len(spy[0]) >= 200:
        c = spy[0]
        s50, s200 = sma(c, 50), sma(c, 200)
        market = {"uptrend": bool(s50 and s200 and s50 > s200),
                  "spySma50": round(s50, 2) if s50 else None,
                  "spySma200": round(s200, 2) if s200 else None}
        log.info(f"Market (SPY): 50d {market.get('spySma50')} vs 200d {market.get('spySma200')} -> uptrend={market.get('uptrend')}")
    time.sleep(FETCH_DELAY)

    by_ticker, rs_inputs = {}, {}
    for i, tk in enumerate(candidates):
        if i and i % 100 == 0:
            log.info(f"  {i}/{len(candidates)} fetched")
        hist = fetch_history(tk)
        if hist:
            bundle, rs_raw = indicators_for(*hist)
            by_ticker[tk] = bundle
            if rs_raw is not None:
                rs_inputs[tk] = rs_raw
        time.sleep(FETCH_DELAY)

    # Relative Strength: percentile rank (1..99) of the blended return across
    # everyone we fetched — a market-relative "leader vs laggard" score.
    if rs_inputs:
        ranked = sorted(rs_inputs.items(), key=lambda kv: kv[1])
        n = len(ranked)
        for idx, (tk, _) in enumerate(ranked):
            by_ticker[tk]["rsRank"] = max(1, min(99, round((idx + 1) / n * 99)))

    payload = {"generatedAt": datetime.now(timezone.utc).isoformat(),
               "count": len(by_ticker), "market": market, "byTicker": by_ticker}
    path = os.path.join(REPO_DIR, "technicals.json") if REPO_DIR else os.path.join("output", "technicals.json")
    if not REPO_DIR:
        os.makedirs("output", exist_ok=True)
    _atomic_dump(path, payload)
    log.info(f"Wrote technicals.json — {len(by_ticker)} names with indicators")

    if REPO_DIR:
        scanner_git.commit_and_push(REPO_DIR, ["technicals.json"],
                                    f"technicals update {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
