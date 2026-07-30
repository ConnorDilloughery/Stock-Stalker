#!/usr/bin/env python3
"""
Undercurrent Radar — the "buyer resource" ranker. Runs on the Pi (own
timer), reads what the other scanners already publish, and produces one
ranked shortlist of names where MULTIPLE independent bullish signals
converge — the thing no single existing tab does.

WHY THESE SIGNALS (grounded in the research)
  * Insider CLUSTER buying — 3+ distinct insiders buying on the open
    market in a compressed window — has repeatedly beaten solitary
    insider buys (~2x the excess return) and the market by ~5-6%/yr
    (Lakonishok-Lee; Cohen-Malloy-Pomorski). No existing tab detects
    clusters; this is the core new edge.
  * Proximity to the 52-week high is a dominant forward-return predictor
    (George & Hwang) that subsumes plain momentum.
  * Convergence — a name lit up by several INDEPENDENT signals at once
    (insider + value + congress + momentum + analysts) — is stronger
    than any one factor alone.

WHAT IT DOES
  1. Loads stocks.json (value signal/score/analysts), universe.json
     (price/discount for the full market), insider_trades.json, and
     congress.json — all already published by the other scanners.
  2. Detects insider clusters (distinct open-market buyers per ticker in
     a recent window) and recent congressional buys.
  3. Scores every candidate by how many independent signals converge,
     ranks them, and for the top N fetches real 1-day / 1-week / 1-month
     returns from Nasdaq's public chart API (same source the sector and
     backtest scanners already use).
  4. Publishes radar.json via the shared, self-healing scanner_git
     publisher.

HONEST LIMITATIONS
  - It ranks by convergence of signals, NOT by any proven alpha model —
    it's a research shortlist, not advice. Insider/congress filings lag
    the actual trade (up to ~45 days), so "recent" means recently
    disclosed, not necessarily recently traded.
  - Returns are point-to-point price changes (no dividends), from
    Nasdaq daily closes — a quick read, not a backtest.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import requests

import scanner_git

REPO_DIR = os.environ.get("REPO_DIR", "").strip()
NASDAQ_CHART_URL = "https://api.nasdaq.com/api/quote/{symbol}/chart"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

INSIDER_WINDOW_DAYS = 45   # Form 4 filings lag the trade; window for a "recent" cluster
CONGRESS_WINDOW_DAYS = 60  # STOCK Act allows up to 45 days to file; a bit more slack
TOP_N = 40                 # how many top picks to fetch returns for + publish

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("radar_scanner")


def _load(name):
    path = os.path.join(REPO_DIR, name) if REPO_DIR else os.path.join("output", name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Couldn't load {name} ({e}) — its signals will be skipped this run")
        return {}


def _parse_date(s):
    """Handles both the insider ISO dates (2026-07-29) and the congress
    US dates (07/29/2026)."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def build_insider_clusters(insider, today):
    """ticker -> {count of distinct open-market buyers, names, lastDate}
    within the recent window."""
    cutoff = today - timedelta(days=INSIDER_WINDOW_DAYS)
    by = {}
    for t in insider.get("trades", []):
        if (t.get("type") or "").lower() != "purchase":
            continue
        d = _parse_date(t.get("transactionDate"))
        if not d or d < cutoff:
            continue
        tk = t.get("ticker")
        if not tk:
            continue
        e = by.setdefault(tk, {"buyers": set(), "last": None})
        e["buyers"].add(t.get("insiderName") or "?")
        if e["last"] is None or d > e["last"]:
            e["last"] = d
    return {tk: {"count": len(v["buyers"]), "names": sorted(v["buyers"])[:6],
                 "lastDate": v["last"].isoformat() if v["last"] else None}
            for tk, v in by.items()}


def build_congress_buys(congress, today):
    cutoff = today - timedelta(days=CONGRESS_WINDOW_DAYS)
    by = {}
    for t in congress.get("trades", []):
        if "purchase" not in (t.get("type") or "").lower():
            continue
        d = _parse_date(t.get("transactionDate")) or _parse_date(t.get("filedDate"))
        if not d or d < cutoff:
            continue
        tk = t.get("ticker")
        if not tk:
            continue
        e = by.setdefault(tk, {"pols": set(), "last": None})
        e["pols"].add(t.get("politician") or "?")
        if e["last"] is None or d > e["last"]:
            e["last"] = d
    return {tk: {"count": len(v["pols"]),
                 "lastDate": v["last"].isoformat() if v["last"] else None}
            for tk, v in by.items()}


def fetch_returns(ticker):
    """1-day / 1-week / 1-month price returns from Nasdaq daily closes.
    Returns None on failure rather than guessing."""
    today = datetime.now(timezone.utc).date()
    frm = today - timedelta(days=45)
    try:
        resp = requests.get(
            NASDAQ_CHART_URL.format(symbol=ticker),
            params={"assetclass": "stocks", "fromdate": frm.isoformat(), "todate": today.isoformat()},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        chart = (resp.json().get("data") or {}).get("chart") or []
        closes = []
        for pt in chart:
            z = pt.get("z") or {}
            c = z.get("close")
            if c is None:
                continue
            try:
                closes.append(float(str(c).replace(",", "")))
            except (ValueError, TypeError):
                pass
        if len(closes) < 2:
            return None
        last = closes[-1]

        def ret(n):
            if len(closes) > n and closes[-1 - n]:
                return round((last / closes[-1 - n] - 1) * 100, 2)
            return None

        return {"ret1d": ret(1), "ret1w": ret(5), "ret1m": ret(21), "returnsAsOf": today.isoformat()}
    except Exception as e:
        log.warning(f"Returns fetch failed for {ticker} ({e})")
        return None


def score_and_rank(stocks, universe, clusters, congress_buys):
    """Convergence score per candidate. Weights are simple and auditable;
    the app shows which signals fired, so the ranking is explainable."""
    sidx = {s["ticker"]: s for s in stocks.get("stocks", []) if s.get("ticker")}
    uidx = {s["ticker"]: s for s in universe.get("stocks", []) if s.get("ticker")}

    # Candidate pool: any name with a real conviction signal (insider buy,
    # value Buy/Accumulate, or congressional buy). Momentum & analysts are
    # scoring boosts, not pool sources, so the list stays conviction-driven.
    pool = set(clusters) | set(congress_buys)
    pool |= {t for t, s in sidx.items() if s.get("signal") in ("BUY", "ACCUMULATE")}

    picks = []
    for tk in pool:
        s = sidx.get(tk) or uidx.get(tk) or {}
        score = 0.0
        signals = []

        # Each signal contributes a BOUNDED amount, so no single one can
        # crown a pick on its own. The real driver is BREADTH — a name lit
        # up by several INDEPENDENT signals ranks far above one big signal.
        cl = clusters.get(tk)
        if cl:
            n = cl["count"]
            # A cluster (3+) is the strongest single signal, but capped ~30 so
            # even a huge 16-buyer cluster can't outweigh broad convergence.
            score += (20 + min(n - 3, 10)) if n >= 3 else 14 if n == 2 else 8
            label = f"{n} insider buyer{'s' if n != 1 else ''}" + (" — cluster" if n >= 3 else "")
            signals.append({"kind": "insider", "label": label})

        vsig = s.get("signal") if s.get("signal") in ("BUY", "ACCUMULATE") else None
        if vsig == "BUY":
            score += 20
            signals.append({"kind": "value", "label": "Buy signal"})
        elif vsig == "ACCUMULATE":
            score += 12
            signals.append({"kind": "value", "label": "Accumulate"})

        conv = s.get("score")
        if isinstance(conv, (int, float)):
            score += conv / 100 * 10  # a modifier on the value thesis, not its own signal

        cg = congress_buys.get(tk)
        if cg:
            score += min(12 + 3 * (cg["count"] - 1), 18)
            signals.append({"kind": "congress",
                            "label": f"{cg['count']} congress buy{'s' if cg['count'] != 1 else ''}"})

        disc = (uidx.get(tk) or s).get("discount")
        if isinstance(disc, (int, float)):
            offpct = round(disc * 100)
            if disc <= 0.05:
                score += 12
                signals.append({"kind": "momentum", "label": f"{offpct}% below 52-wk high"})
            elif disc <= 0.15:
                score += 7
                signals.append({"kind": "momentum", "label": f"{offpct}% below 52-wk high"})

        # Analyst lean only counts as an INDEPENDENT signal when there's no
        # value Buy/Accumulate — that signal is itself computed from positive
        # analyst sentiment, so counting both would double-count and inflate
        # every value name's breadth.
        if not vsig:
            rec = s.get("recTotal") or 0
            if rec:
                buys = (s.get("strongBuy") or 0) + (s.get("buy") or 0)
                sells = (s.get("sell") or 0) + (s.get("strongSell") or 0)
                if buys - sells > 0:
                    score += 8
                    signals.append({"kind": "analyst", "label": f"{buys} buy / {sells} sell"})

        # Breadth bonus: the more DISTINCT signals converge, the bigger the
        # boost. This is what makes a 4-signal name beat a lone big cluster.
        score += max(0, len(signals) - 1) * 18

        picks.append({
            "ticker": tk,
            "sector": s.get("sector"),
            "price": s.get("price"),
            "pe": s.get("pe"),
            "cap": s.get("cap"),
            "radarScore": round(score, 1),
            "signalCount": len(signals),
            "signals": signals,
            "insiderCluster": cl,
            "congress": cg,
            "valueSignal": vsig,
            "convictionScore": conv if isinstance(conv, (int, float)) else None,
            "discount": disc if isinstance(disc, (int, float)) else None,
        })

    picks.sort(key=lambda p: (p["radarScore"], p["signalCount"]), reverse=True)
    return picks


def main():
    if not REPO_DIR:
        log.warning("REPO_DIR not set — reading/writing ./output")

    today = datetime.now(timezone.utc).date()
    stocks = _load("stocks.json")
    universe = _load("universe.json")
    insider = _load("insider_trades.json")
    congress = _load("congress.json")

    clusters = build_insider_clusters(insider, today)
    congress_buys = build_congress_buys(congress, today)
    log.info(f"Insider clusters: {sum(1 for c in clusters.values() if c['count'] >= 3)} names with 3+ buyers, "
             f"{len(clusters)} with any recent buy; congress buys on {len(congress_buys)} names")

    picks = score_and_rank(stocks, universe, clusters, congress_buys)
    top = picks[:TOP_N]
    log.info(f"Radar: {len(picks)} candidates; fetching returns for top {len(top)}")
    for p in top:
        r = fetch_returns(p["ticker"])
        if r:
            p.update(r)
        time.sleep(0.4)  # be polite to the free Nasdaq endpoint

    payload = {"generatedAt": datetime.now(timezone.utc).isoformat(), "count": len(top), "picks": top}
    path = os.path.join(REPO_DIR, "radar.json") if REPO_DIR else os.path.join("output", "radar.json")
    if not REPO_DIR:
        os.makedirs("output", exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    log.info(f"Wrote radar.json — {len(top)} picks")

    if REPO_DIR:
        scanner_git.commit_and_push(REPO_DIR, ["radar.json"],
                                    f"radar update {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
