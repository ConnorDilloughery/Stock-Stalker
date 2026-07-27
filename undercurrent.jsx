import { useState, useEffect, useRef } from "react";

/* ---------- constants ---------- */

const SECTORS = [
  "Technology", "Healthcare", "Industrials", "Energy",
  "Consumer", "Financials", "Materials", "Any sector",
];

const CAPS = [
  { label: "Under $2B — small companies", value: "$2 billion" },
  { label: "Under $10B — mid-size", value: "$10 billion" },
  { label: "Under $50B — larger, steadier", value: "$50 billion" },
];

const SIGNAL_STYLE = {
  BUY:        { bg: "#E3F1EA", fg: "#1E7A5A", label: "Buy signal" },
  ACCUMULATE: { bg: "#E3F1EA", fg: "#1E7A5A", label: "Accumulate" },
  WATCH:      { bg: "#F6EEDD", fg: "#9A6B1B", label: "Watch" },
  HOLD:       { bg: "#F6EEDD", fg: "#9A6B1B", label: "Hold" },
  TRIM:       { bg: "#F7E6E2", fg: "#C94F3D", label: "Trim" },
  AVOID:      { bg: "#F7E6E2", fg: "#C94F3D", label: "Avoid" },
};

const IMPACT_STYLE = {
  GOOD:    { bg: "#E3F1EA", fg: "#1E7A5A" },
  BAD:     { bg: "#F7E6E2", fg: "#C94F3D" },
  NEUTRAL: { bg: "#EAEEF1", fg: "#4A5A66" },
};

const GLOSSARY = [
  ["P/E ratio", "Price divided by yearly earnings. Roughly: how many dollars you pay for $1 of the company's annual profit. Around 10–25 is generally 'healthy' — the company is profitable and not wildly expensive. Very high P/E means big expectations are baked in; negative means the company loses money."],
  ["Market cap", "The total price tag of the whole company (share price × number of shares). Small caps (under ~$2B) can grow faster but swing harder. Mid caps are the middle ground. Mega caps like Apple are steadier but rarely double."],
  ["Undervalued", "The market price is below what the business seems to be worth based on its profits, assets, and growth. The bet: eventually other investors notice and the price catches up."],
  ["Upside", "How far below analysts' average price target the stock trades. '+18% upside' means analysts think fair value is about 18% above today's price. Targets are educated guesses, not promises."],
  ["Buy / Watch / Trim signals", "Plain-English summaries of the setup. 'Buy' = value case looks strong now. 'Watch' = interesting but wait for something (a price, an earnings report). 'Trim' = consider taking profits. These are study aids, not orders — always read the 'why'."],
  ["IPO", "Initial Public Offering — a private company selling shares to the public for the first time. Exciting but risky: no trading history, and prices often spike then slump in the first months. Many pros wait 6–12 months after an IPO."],
  ["Diversification", "Don't put everything in one stock or one sector. Spreading across several sectors means one bad surprise can't sink you. It's the closest thing investing has to a free lunch."],
  ["The Legends tabs", "Four extra screens run a famous investor's full checklist through live web research. Graham = defensive deep value; Lynch = growth at a reasonable price; O'Neil = CAN SLIM growth leaders; Murphy = pure price-trend momentum. Each criterion is marked pass, fail, or unknown."],
  ["Why some checks say “unknown”", "Some criteria depend on data that's hard to verify reliably in a quick research pass — exact 10–20 year EPS and dividend histories, share float, live RSI/MACD readings, or chart-pattern reads. Rather than guess, those are marked “unknown” and never counted as a pass, so a card's score reflects only what was actually confirmed."],
];

/* Legend screeners: each applies a famous investor's checklist via live
   research. Criteria strings drive both the prompt and the on-card labels. */
const STRATEGIES = [
  {
    id: "graham", label: "Graham", cat: "Value",
    title: "Benjamin Graham — Defensive Value",
    blurb: "From The Intelligent Investor: only financially bulletproof businesses bought at a strict discount, chosen to protect your downside first.",
    criteria: [
      "Current ratio > 2.0 (financial solvency)",
      "Long-term debt below net current assets (working capital)",
      "Positive EPS every year for the past 10 years",
      "Uninterrupted dividends for at least 20 years",
      "EPS up at least one-third over the past decade",
      "P/E × P/B ≤ 22.5 (strict valuation cap)",
    ],
  },
  {
    id: "lynch", label: "Lynch", cat: "Value",
    title: "Peter Lynch — Growth at a Reasonable Price",
    blurb: "From One Up on Wall Street: understandable companies growing steadily, cheap relative to that growth, spotted before Wall Street piles in.",
    criteria: [
      "PEG ≤ 1 (P/E at or below the growth rate)",
      "A boring, dull, or unglamorous name or industry",
      "Low analyst coverage and minimal institutional ownership",
      "Recent insider buying by executives or the board",
      "Sustainable ~20–25% growth (not a frantic 50–100%)",
      "High cash reserves paired with very low debt",
    ],
  },
  {
    id: "oneil", label: "O'Neil", cat: "Growth",
    title: "William O'Neil — CAN SLIM Growth",
    blurb: "From How to Make Money in Stocks: fast-growing earnings leaders breaking out on strength, bought only when the overall market is rising.",
    criteria: [
      "C — Quarterly EPS up ≥ 25% year-over-year",
      "A — Annual EPS growth ≥ 25% over 3–5 years",
      "N — Something new: product, management, or a new price high",
      "S — Low float with a volume spike on breakout days",
      "L — Relative Strength rating ≥ 80 (a market leader)",
      "I — Owned by 3–5 top-performing institutional funds",
      "M — Overall market in a confirmed uptrend",
      "Breaking out of a cup-and-handle, double-bottom, or flat base",
    ],
  },
  {
    id: "murphy", label: "Murphy", cat: "Technical",
    title: "John Murphy — Trend & Momentum",
    blurb: "From Technical Analysis of the Financial Markets: ignore the financial statements and follow price, trend, and volume.",
    criteria: [
      "Trend aligned with the macro trend (50-day above 200-day MA)",
      "Breaking resistance on heavy volume, or bouncing off support",
      "Rising volume confirming the price move",
      "RSI / MACD bullish but not overextended",
    ],
  },
];

/* ---------- Claude API helper ---------- */

async function askClaude(prompt) {
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-6",
      max_tokens: 1000,
      messages: [{ role: "user", content: prompt }],
      tools: [{ type: "web_search_20250305", name: "web_search" }],
    }),
  });
  const data = await res.json();
  if (data.error) throw new Error(data.error.message || "API error");
  const text = (data.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("\n");
  const clean = text.replace(/```json|```/g, "").trim();
  const first = clean.indexOf("{");
  if (first === -1) throw new Error("No JSON in response");
  const body = clean.slice(first);
  try {
    return JSON.parse(body.slice(0, body.lastIndexOf("}") + 1));
  } catch {
    return salvage(body); // response was cut off — recover the complete entries
  }
}

/* Recovers complete objects from a truncated {"key":[{...},{...},{trunc */
function salvage(s) {
  const m = s.match(/"(\w+)"\s*:\s*\[/);
  if (!m) throw new Error("Response was cut off and couldn't be recovered");
  const key = m[1];
  const items = [];
  let depth = 0, start = -1, inStr = false, esc = false;
  for (let j = s.indexOf("[", m.index) + 1; j < s.length; j++) {
    const c = s[j];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') { inStr = true; continue; }
    if (c === "{") { if (depth === 0) start = j; depth++; }
    else if (c === "}") {
      depth--;
      if (depth === 0 && start >= 0) {
        try { items.push(JSON.parse(s.slice(start, j + 1))); } catch { /* skip broken item */ }
        start = -1;
      }
    } else if (c === "]" && depth === 0) break;
  }
  if (items.length === 0) throw new Error("Response was cut off and couldn't be recovered");
  return { [key]: items };
}

const TODAY = new Date().toLocaleDateString("en-US", {
  year: "numeric", month: "long", day: "numeric",
});

function scanPrompt(sector, cap) {
  const sectorLine = sector === "Any sector"
    ? "across a MIX of different sectors"
    : `in the ${sector} sector`;
  return `You are a careful value-stock screener. Today is ${TODAY}. Use web search to find CURRENT data — do not rely on memory.

Find exactly 3 US-listed stocks ${sectorLine} that meet ALL of these:
- Profitable, with a P/E ratio roughly between 6 and 25
- Market cap under ${cap}
- Appears undervalued: trades meaningfully below analyst consensus price targets or reasonable fair-value estimates
- At least 10% upside to the average analyst price target

Respond ONLY with compact valid JSON (no markdown fences, no commentary, no extra whitespace):
{"stocks":[{"ticker":"","name":"","price":"$0.00","pe":"0.0","marketCap":"$0.0B","sector":"","upside":"+0%","signal":"BUY or ACCUMULATE or WATCH","verdict":25,"why":"why it looks cheap, plain English","risk":"biggest risk","sellIf":"what would make you sell"}]}
"verdict" is 0-100 where 0 = deep bargain, 50 = fairly priced, 100 = expensive. CRITICAL: keep every string under 12 words so the full JSON fits in your response.`;
}

function strategyPrompt(strat, sector) {
  const sectorLine = sector === "Any sector"
    ? "across a mix of different sectors"
    : `in the ${sector} sector`;
  return `You are a disciplined stock screener applying ${strat.title}'s exact checklist. Today is ${TODAY}. Use web search to find CURRENT data — do not rely on memory.

Find up to 3 US-listed stocks ${sectorLine} that best fit this checklist. For EACH stock, evaluate every criterion and mark it "pass", "fail", or "unknown". Use "unknown" honestly whenever reliable public data isn't readily verifiable (for example exact 10–20 year EPS or dividend histories, share float, live RSI/MACD readings, or chart-pattern reads) — never guess a "pass". Prefer stocks that genuinely pass the most criteria.

Checklist (return one entry per item, in this order):
${strat.criteria.map((c, i) => `${i + 1}. ${c}`).join("\n")}

Respond ONLY with compact valid JSON (no markdown fences, no commentary, no extra whitespace):
{"stocks":[{"ticker":"","name":"","price":"$0.00","marketCap":"$0.0B","sector":"","summary":"one-line plain-English take","checks":[{"label":"short criterion label","status":"pass or fail or unknown","detail":"brief evidence under 10 words"}]}]}
CRITICAL: keep every string short so the full JSON fits in your response. If nothing fits well, return {"stocks":[]}.`;
}

function newsPrompt(ticker, name) {
  return `Today is ${TODAY}. Use web search to find the most important news from the last 2-3 weeks about the stock ${ticker} (${name}) — earnings, guidance, deals, downgrades, anything that moves the price.

Respond ONLY with compact valid JSON (no fences, no commentary):
{"items":[{"headline":"short headline","date":"","impact":"GOOD or BAD or NEUTRAL","meaning":"what it means for shareholders, plain English"}]}
Max 3 items, most important first. Keep every string under 12 words. If nothing notable happened: {"items":[]}`;
}

function ipoPrompt() {
  return `Today is ${TODAY}. Use web search to find notable companies planning to IPO (go public) in the next 6-12 months, or that recently filed to IPO.

Respond ONLY with compact valid JSON (no fences, no commentary, no extra whitespace):
{"ipos":[{"company":"","what":"what they do, plain English","timing":"expected timing","valuation":"rough valuation or 'TBD'","whyWatch":"why it's interesting","caution":"one caution"}]}
Exactly 3 companies, most notable first. Keep every string under 12 words so the full JSON fits in your response.`;
}

/* ---------- persistent storage ---------- */

async function loadWatchlist() {
  try {
    const r = await window.storage.get("undercurrent:watchlist");
    return r ? JSON.parse(r.value) : [];
  } catch { return []; }
}
async function saveWatchlist(list) {
  try { await window.storage.set("undercurrent:watchlist", JSON.stringify(list)); }
  catch { /* in-memory only this session */ }
}

/* ---------- small components ---------- */

function Gauge({ value }) {
  const v = Math.max(0, Math.min(100, Number(value) || 50));
  return (
    <div className="uc-gauge">
      <div className="uc-gauge-labels">
        <span>Deep value</span><span>Fairly priced</span><span>Pricey</span>
      </div>
      <div className="uc-gauge-track">
        <div className="uc-gauge-marker" style={{ left: `${v}%` }} />
      </div>
    </div>
  );
}

function SignalChip({ signal }) {
  const s = SIGNAL_STYLE[signal] || SIGNAL_STYLE.WATCH;
  return (
    <span className="uc-chip" style={{ background: s.bg, color: s.fg }}>
      {s.label}
    </span>
  );
}

function LoadingBlock({ lines }) {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setI((x) => (x + 1) % lines.length), 2600);
    return () => clearInterval(t);
  }, [lines.length]);
  return (
    <div className="uc-loading">
      <div className="uc-spinner" />
      <div>
        <div className="uc-loading-line">{lines[i]}</div>
        <div className="uc-loading-sub">Live research usually takes 20–40 seconds</div>
      </div>
    </div>
  );
}

function NewsList({ items }) {
  if (!items) return null;
  if (items.length === 0)
    return <div className="uc-news-empty">No major news in the last few weeks — quiet is often fine.</div>;
  return (
    <div className="uc-news">
      {items.map((n, i) => {
        const st = IMPACT_STYLE[n.impact] || IMPACT_STYLE.NEUTRAL;
        return (
          <div key={i} className="uc-news-item">
            <div className="uc-news-top">
              <span className="uc-chip" style={{ background: st.bg, color: st.fg }}>
                {(n.impact || "NEUTRAL").toLowerCase()}
              </span>
              <span className="uc-news-date">{n.date}</span>
            </div>
            <div className="uc-news-headline">{n.headline}</div>
            <div className="uc-news-meaning">{n.meaning}</div>
          </div>
        );
      })}
    </div>
  );
}

function StockCard({ stock, inWatchlist, onToggleWatch, news, newsLoading, onCheckNews }) {
  return (
    <div className="uc-card">
      <div className="uc-card-head">
        <div>
          <div className="uc-ticker-row">
            <span className="uc-ticker">{stock.ticker}</span>
            <SignalChip signal={stock.signal} />
          </div>
          <div className="uc-name">{stock.name}</div>
        </div>
        <div className="uc-price-block">
          <div className="uc-price">{stock.price}</div>
          <div className="uc-upside">{stock.upside} upside</div>
        </div>
      </div>

      <div className="uc-stats">
        <div className="uc-stat"><span>P/E</span><strong>{stock.pe}</strong></div>
        <div className="uc-stat"><span>Market cap</span><strong>{stock.marketCap}</strong></div>
        <div className="uc-stat"><span>Sector</span><strong>{stock.sector}</strong></div>
      </div>

      <Gauge value={stock.verdict} />

      <div className="uc-notes">
        <p><strong className="uc-k">Why it made the cut.</strong> {stock.why}</p>
        <p><strong className="uc-k uc-k-risk">Main risk.</strong> {stock.risk}</p>
        <p><strong className="uc-k uc-k-sell">When to rethink.</strong> {stock.sellIf}</p>
      </div>

      <div className="uc-card-actions">
        <button className={inWatchlist ? "uc-btn uc-btn-quiet" : "uc-btn"} onClick={onToggleWatch}>
          {inWatchlist ? "Remove from watchlist" : "+ Add to watchlist"}
        </button>
        <button className="uc-btn uc-btn-quiet" onClick={onCheckNews} disabled={newsLoading}>
          {newsLoading ? "Checking news…" : "Check news"}
        </button>
      </div>
      {newsLoading && <LoadingBlock lines={["Searching recent headlines…", "Reading what moved the price…"]} />}
      <NewsList items={news} />
    </div>
  );
}

/* ---------- tabs ---------- */

function ScoutTab({ watchlist, toggleWatch }) {
  const [sector, setSector] = useState("Any sector");
  const [cap, setCap] = useState(CAPS[1].value);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [stocks, setStocks] = useState(null);
  const [news, setNews] = useState({});
  const [newsLoading, setNewsLoading] = useState({});

  const runScan = async () => {
    setLoading(true); setError(null); setStocks(null); setNews({});
    try {
      const data = await askClaude(scanPrompt(sector, cap));
      setStocks(data.stocks || []);
    } catch (e) {
      setError("The scan hit a snag: " + e.message + ". Try again — live research occasionally times out.");
    }
    setLoading(false);
  };

  const checkNews = async (s) => {
    setNewsLoading((p) => ({ ...p, [s.ticker]: true }));
    try {
      const data = await askClaude(newsPrompt(s.ticker, s.name));
      setNews((p) => ({ ...p, [s.ticker]: data.items || [] }));
    } catch {
      setNews((p) => ({ ...p, [s.ticker]: [] }));
    }
    setNewsLoading((p) => ({ ...p, [s.ticker]: false }));
  };

  return (
    <div>
      <div className="uc-panel">
        <div className="uc-panel-title">Set your screen</div>
        <div className="uc-field-label">Sector</div>
        <div className="uc-chips">
          {SECTORS.map((s) => (
            <button key={s} className={s === sector ? "uc-pick uc-pick-on" : "uc-pick"} onClick={() => setSector(s)}>
              {s}
            </button>
          ))}
        </div>
        <div className="uc-field-label">Company size</div>
        <div className="uc-chips">
          {CAPS.map((c) => (
            <button key={c.value} className={c.value === cap ? "uc-pick uc-pick-on" : "uc-pick"} onClick={() => setCap(c.value)}>
              {c.label}
            </button>
          ))}
        </div>
        <div className="uc-baked">
          Always applied: healthy P/E (≈6–25) · profitable · trading below fair value · 10%+ analyst upside
        </div>
        <button className="uc-btn uc-btn-big" onClick={runScan} disabled={loading}>
          {loading ? "Scanning…" : "Run live scan"}
        </button>
      </div>

      {loading && (
        <LoadingBlock lines={[
          "Searching current market data…",
          "Filtering for healthy P/E ratios…",
          "Comparing prices to fair-value estimates…",
          "Writing plain-English verdicts…",
        ]} />
      )}
      {error && <div className="uc-error">{error}</div>}
      {stocks && stocks.length === 0 && !loading && (
        <div className="uc-error">Nothing passed the screen this time — try a different sector or a larger size cap.</div>
      )}
      {stocks && stocks.map((s) => (
        <StockCard
          key={s.ticker}
          stock={s}
          inWatchlist={watchlist.some((w) => w.ticker === s.ticker)}
          onToggleWatch={() => toggleWatch(s)}
          news={news[s.ticker]}
          newsLoading={!!newsLoading[s.ticker]}
          onCheckNews={() => checkNews(s)}
        />
      ))}
    </div>
  );
}

function WatchTab({ watchlist, toggleWatch }) {
  const [news, setNews] = useState({});
  const [newsLoading, setNewsLoading] = useState({});

  const checkNews = async (s) => {
    setNewsLoading((p) => ({ ...p, [s.ticker]: true }));
    try {
      const data = await askClaude(newsPrompt(s.ticker, s.name));
      setNews((p) => ({ ...p, [s.ticker]: data.items || [] }));
    } catch {
      setNews((p) => ({ ...p, [s.ticker]: [] }));
    }
    setNewsLoading((p) => ({ ...p, [s.ticker]: false }));
  };

  if (watchlist.length === 0)
    return (
      <div className="uc-empty">
        <div className="uc-empty-title">Your watchlist is empty</div>
        <p>Run a scan on the Scout tab and add the stocks you want to keep an eye on. They'll be saved here, and you can pull fresh news on each one any time.</p>
      </div>
    );

  return (
    <div>
      <div className="uc-watch-hint">Open the app and hit “Check news” whenever you want a fresh read — each check searches the last few weeks of headlines.</div>
      {watchlist.map((s) => (
        <StockCard
          key={s.ticker}
          stock={s}
          inWatchlist={true}
          onToggleWatch={() => toggleWatch(s)}
          news={news[s.ticker]}
          newsLoading={!!newsLoading[s.ticker]}
          onCheckNews={() => checkNews(s)}
        />
      ))}
    </div>
  );
}

function IpoTab() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [ipos, setIpos] = useState(null);

  const scan = async () => {
    setLoading(true); setError(null);
    try {
      const data = await askClaude(ipoPrompt());
      setIpos(data.ipos || []);
    } catch (e) {
      setError("IPO scan hit a snag: " + e.message + ". Give it another try.");
    }
    setLoading(false);
  };

  return (
    <div>
      <div className="uc-panel">
        <div className="uc-panel-title">IPO radar</div>
        <p className="uc-panel-copy">
          Companies planning to go public in the next 6–12 months. Heads up: IPOs are the riskiest corner of this app — no trading history, and prices often spike then slump. Many value investors wait for the first few earnings reports.
        </p>
        <button className="uc-btn uc-btn-big" onClick={scan} disabled={loading}>
          {loading ? "Scanning…" : "Scan upcoming IPOs"}
        </button>
      </div>

      {loading && <LoadingBlock lines={["Searching recent IPO filings…", "Checking expected timing and valuations…"]} />}
      {error && <div className="uc-error">{error}</div>}
      {ipos && ipos.map((p, i) => (
        <div className="uc-card" key={i}>
          <div className="uc-ticker-row">
            <span className="uc-ticker">{p.company}</span>
            <span className="uc-chip" style={{ background: "#EAEEF1", color: "#4A5A66" }}>{p.timing}</span>
          </div>
          <div className="uc-stats" style={{ marginTop: 10 }}>
            <div className="uc-stat"><span>Expected valuation</span><strong>{p.valuation}</strong></div>
          </div>
          <div className="uc-notes">
            <p><strong className="uc-k">What they do.</strong> {p.what}</p>
            <p><strong className="uc-k">Why watch.</strong> {p.whyWatch}</p>
            <p><strong className="uc-k uc-k-risk">Caution.</strong> {p.caution}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

const CHECK_ICON = {
  pass:    { c: "var(--kelp)",  s: "✓" },
  fail:    { c: "var(--coral)", s: "✗" },
  unknown: { c: "var(--dim)",   s: "—" },
};

function StrategyCard({ stock }) {
  const checks = stock.checks || [];
  const scored = checks.filter((c) => c.status !== "unknown");
  const passed = checks.filter((c) => c.status === "pass").length;
  const pct = scored.length ? Math.round((passed / scored.length) * 100) : 0;
  const bg = pct >= 75 ? "#E3F1EA" : pct >= 50 ? "#F6EEDD" : "#F7E6E2";
  const fg = pct >= 75 ? "#1E7A5A" : pct >= 50 ? "#9A6B1B" : "#C94F3D";
  const meta = [stock.sector, stock.marketCap, stock.price].filter(Boolean).join(" · ");
  return (
    <div className="uc-card">
      <div className="uc-card-head">
        <div>
          <div className="uc-ticker-row">
            <span className="uc-ticker">{stock.ticker}</span>
            <span className="uc-chip" style={{ background: bg, color: fg }}>
              {passed}/{scored.length} checks · {pct}%
            </span>
          </div>
          <div className="uc-name">{stock.name}{meta ? " · " + meta : ""}</div>
        </div>
      </div>
      {stock.summary && (
        <div className="uc-notes"><p><strong className="uc-k">Read.</strong> {stock.summary}</p></div>
      )}
      <div className="uc-checklist">
        {checks.map((c, i) => {
          const ic = CHECK_ICON[c.status] || CHECK_ICON.unknown;
          return (
            <div className="uc-check-row" key={i}>
              <span className="uc-check-ic" style={{ color: ic.c }}>{ic.s}</span>
              <div>
                <div className="uc-check-label">{c.label}</div>
                {c.detail && <div className="uc-check-detail">{c.detail}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StrategyTab({ strat }) {
  const [sector, setSector] = useState("Any sector");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [stocks, setStocks] = useState(null);

  const run = async () => {
    setLoading(true); setError(null); setStocks(null);
    try {
      const data = await askClaude(strategyPrompt(strat, sector));
      setStocks(data.stocks || []);
    } catch (e) {
      setError("The screen hit a snag: " + e.message + ". Try again — live research occasionally times out.");
    }
    setLoading(false);
  };

  return (
    <div>
      <div className="uc-panel">
        <div className="uc-panel-title">{strat.title}</div>
        <p className="uc-panel-copy">{strat.blurb}</p>
        <div className="uc-field-label">Sector</div>
        <div className="uc-chips">
          {SECTORS.map((s) => (
            <button key={s} className={s === sector ? "uc-pick uc-pick-on" : "uc-pick"} onClick={() => setSector(s)}>
              {s}
            </button>
          ))}
        </div>
        <div className="uc-baked">
          <strong>Honest labels.</strong> Every criterion is marked pass, fail, or <em>unknown</em>. “Unknown” means live research couldn't reliably confirm it (multi-decade histories, float, RSI/MACD, chart patterns) — it's never counted as a pass, and each card's score reflects only the checks actually confirmed.
        </div>
        <button className="uc-btn uc-btn-big" onClick={run} disabled={loading}>
          {loading ? "Screening…" : `Run the ${strat.label} screen`}
        </button>
      </div>

      {loading && (
        <LoadingBlock lines={[
          `Applying ${strat.label}'s checklist…`,
          "Searching current fundamentals and price action…",
          "Marking each criterion pass, fail, or unknown…",
        ]} />
      )}
      {error && <div className="uc-error">{error}</div>}
      {stocks && stocks.length === 0 && !loading && (
        <div className="uc-error">Nothing cleared {strat.label}'s checklist this time — try a different sector.</div>
      )}
      {stocks && stocks.map((s, i) => <StrategyCard key={s.ticker || i} stock={s} />)}
    </div>
  );
}

function LearnTab() {
  return (
    <div>
      <div className="uc-panel">
        <div className="uc-panel-title">The five-minute vocabulary</div>
        <p className="uc-panel-copy">Everything this app measures, in plain English. Read this once and the stock cards will make total sense.</p>
      </div>
      {GLOSSARY.map(([term, def]) => (
        <div className="uc-card uc-learn-card" key={term}>
          <div className="uc-learn-term">{term}</div>
          <div className="uc-learn-def">{def}</div>
        </div>
      ))}
    </div>
  );
}

/* ---------- app ---------- */

const TABS = [
  { id: "scout", label: "Scout" },
  ...STRATEGIES.map((s) => ({ id: s.id, label: s.label })),
  { id: "watch", label: "Watchlist" },
  { id: "ipo", label: "IPO radar" },
  { id: "learn", label: "Learn" },
];

export default function Undercurrent() {
  const [tab, setTab] = useState("scout");
  const [watchlist, setWatchlist] = useState([]);
  const loaded = useRef(false);

  useEffect(() => {
    loadWatchlist().then((w) => { setWatchlist(w); loaded.current = true; });
  }, []);

  const toggleWatch = (stock) => {
    setWatchlist((prev) => {
      const next = prev.some((w) => w.ticker === stock.ticker)
        ? prev.filter((w) => w.ticker !== stock.ticker)
        : [...prev, stock];
      saveWatchlist(next);
      return next;
    });
  };

  return (
    <div className="uc-root">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Zilla+Slab:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600&display=swap');

        .uc-root {
          --ink: #16232E; --mist: #F2F5F6; --card: #FFFFFF;
          --kelp: #1E7A5A; --coral: #C94F3D; --amber: #9A6B1B;
          --tide: #2E5E7E; --line: #DCE3E7; --dim: #5C6B76;
          font-family: 'IBM Plex Sans', system-ui, sans-serif;
          background: var(--mist); color: var(--ink);
          min-height: 100vh; padding: 0 0 48px;
        }
        .uc-shell { max-width: 780px; margin: 0 auto; padding: 0 16px; }
        .uc-header { padding: 28px 0 6px; }
        .uc-logo { font-family: 'Zilla Slab', Georgia, serif; font-weight: 700; font-size: 30px; letter-spacing: -0.5px; }
        .uc-logo em { font-style: normal; color: var(--tide); }
        .uc-tagline { color: var(--dim); font-size: 14px; margin-top: 2px; }
        .uc-disclaimer { font-size: 12px; color: var(--dim); background: #E9EEF1; border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; margin: 14px 0 4px; }
        .uc-tabs { display: flex; flex-wrap: wrap; gap: 4px; margin: 16px 0 20px; border-bottom: 2px solid var(--line); }
        .uc-tab { background: none; border: none; font: 600 15px 'IBM Plex Sans', sans-serif; color: var(--dim); padding: 10px 14px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; }
        .uc-tab-on { color: var(--ink); border-bottom-color: var(--tide); }
        .uc-tab:focus-visible, .uc-btn:focus-visible, .uc-pick:focus-visible { outline: 2px solid var(--tide); outline-offset: 2px; }

        .uc-panel { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 20px; margin-bottom: 18px; }
        .uc-panel-title { font-family: 'Zilla Slab', Georgia, serif; font-weight: 700; font-size: 20px; margin-bottom: 6px; }
        .uc-panel-copy { color: var(--dim); font-size: 14px; line-height: 1.5; margin: 4px 0 14px; }
        .uc-field-label { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--dim); margin: 14px 0 8px; }
        .uc-chips { display: flex; flex-wrap: wrap; gap: 8px; }
        .uc-pick { border: 1px solid var(--line); background: var(--mist); border-radius: 999px; padding: 7px 14px; font: 500 13px 'IBM Plex Sans', sans-serif; color: var(--ink); cursor: pointer; }
        .uc-pick-on { background: var(--tide); border-color: var(--tide); color: #fff; }
        .uc-baked { font-size: 13px; color: var(--dim); margin: 16px 0; padding: 10px 12px; background: var(--mist); border-radius: 8px; }
        .uc-btn { border: none; border-radius: 8px; background: var(--tide); color: #fff; font: 600 14px 'IBM Plex Sans', sans-serif; padding: 10px 16px; cursor: pointer; }
        .uc-btn:disabled { opacity: 0.6; cursor: default; }
        .uc-btn-big { width: 100%; padding: 13px; font-size: 15px; }
        .uc-btn-quiet { background: var(--mist); color: var(--ink); border: 1px solid var(--line); }

        .uc-card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
        .uc-card-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
        .uc-ticker-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .uc-ticker { font-family: 'IBM Plex Mono', monospace; font-weight: 600; font-size: 20px; }
        .uc-name { color: var(--dim); font-size: 14px; margin-top: 2px; }
        .uc-chip { font-size: 12px; font-weight: 600; border-radius: 999px; padding: 3px 10px; white-space: nowrap; }
        .uc-price-block { text-align: right; }
        .uc-price { font-family: 'IBM Plex Mono', monospace; font-weight: 600; font-size: 20px; }
        .uc-upside { color: var(--kelp); font-weight: 600; font-size: 13px; margin-top: 2px; }

        .uc-stats { display: flex; gap: 10px; margin: 14px 0; flex-wrap: wrap; }
        .uc-stat { flex: 1; min-width: 100px; background: var(--mist); border-radius: 8px; padding: 8px 12px; }
        .uc-stat span { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--dim); }
        .uc-stat strong { font-family: 'IBM Plex Mono', monospace; font-size: 15px; }

        .uc-gauge { margin: 14px 0 6px; }
        .uc-gauge-labels { display: flex; justify-content: space-between; font-size: 11px; color: var(--dim); margin-bottom: 5px; }
        .uc-gauge-track { position: relative; height: 8px; border-radius: 999px; background: linear-gradient(90deg, #1E7A5A 0%, #D8B45A 55%, #C94F3D 100%); opacity: 0.95; }
        .uc-gauge-marker { position: absolute; top: -4px; width: 4px; height: 16px; border-radius: 2px; background: var(--ink); transform: translateX(-50%); box-shadow: 0 0 0 2px var(--card); }

        .uc-notes { margin-top: 14px; font-size: 14px; line-height: 1.55; }
        .uc-notes p { margin: 6px 0; }
        .uc-k { color: var(--kelp); }
        .uc-k-risk { color: var(--coral); }
        .uc-k-sell { color: var(--amber); }

        .uc-card-actions { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }

        .uc-news { margin-top: 14px; border-top: 1px dashed var(--line); padding-top: 12px; }
        .uc-news-item { padding: 10px 0; border-bottom: 1px solid var(--mist); }
        .uc-news-item:last-child { border-bottom: none; }
        .uc-news-top { display: flex; gap: 10px; align-items: center; }
        .uc-news-date { font-size: 12px; color: var(--dim); }
        .uc-news-headline { font-weight: 600; font-size: 14px; margin-top: 5px; }
        .uc-news-meaning { font-size: 13px; color: var(--dim); margin-top: 3px; line-height: 1.5; }
        .uc-news-empty { margin-top: 14px; font-size: 13px; color: var(--dim); border-top: 1px dashed var(--line); padding-top: 12px; }

        .uc-loading { display: flex; gap: 14px; align-items: center; background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; }
        .uc-spinner { width: 22px; height: 22px; border-radius: 50%; border: 3px solid var(--line); border-top-color: var(--tide); animation: uc-spin 0.9s linear infinite; flex-shrink: 0; }
        @keyframes uc-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) { .uc-spinner { animation: none; } }
        .uc-loading-line { font-weight: 600; font-size: 14px; }
        .uc-loading-sub { font-size: 12px; color: var(--dim); margin-top: 2px; }

        .uc-error { background: #F7E6E2; color: #7C3225; border: 1px solid #E8C4BC; border-radius: 10px; padding: 12px 16px; font-size: 14px; margin-bottom: 16px; }
        .uc-empty { text-align: center; padding: 48px 24px; color: var(--dim); }
        .uc-empty-title { font-family: 'Zilla Slab', Georgia, serif; font-weight: 700; font-size: 20px; color: var(--ink); margin-bottom: 8px; }
        .uc-watch-hint { font-size: 13px; color: var(--dim); margin-bottom: 14px; }

        .uc-checklist { margin-top: 14px; border-top: 1px dashed var(--line); padding-top: 4px; }
        .uc-check-row { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; border-bottom: 1px solid var(--mist); }
        .uc-check-row:last-child { border-bottom: none; }
        .uc-check-ic { font-family: 'IBM Plex Mono', monospace; font-weight: 700; font-size: 15px; width: 16px; text-align: center; flex-shrink: 0; line-height: 1.4; }
        .uc-check-label { font-size: 14px; font-weight: 500; line-height: 1.35; }
        .uc-check-detail { font-size: 12.5px; color: var(--dim); margin-top: 2px; line-height: 1.4; }

        .uc-learn-card { padding: 16px 20px; }
        .uc-learn-term { font-family: 'Zilla Slab', Georgia, serif; font-weight: 700; font-size: 17px; margin-bottom: 4px; }
        .uc-learn-def { font-size: 14px; line-height: 1.6; color: #3A4852; }

        @media (max-width: 480px) {
          .uc-logo { font-size: 24px; }
          .uc-tab { padding: 10px 8px; font-size: 14px; }
          .uc-card, .uc-panel { padding: 16px; }
        }
      `}</style>

      <div className="uc-shell">
        <header className="uc-header">
          <div className="uc-logo">Under<em>current</em></div>
          <div className="uc-tagline">Finds value the market hasn't priced in yet — explained in plain English.</div>
          <div className="uc-disclaimer">
            Educational research tool, not financial advice. Data comes from live web research and can contain errors — verify numbers before trading, and never invest money you can't afford to lose.
          </div>
        </header>

        <nav className="uc-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? "uc-tab uc-tab-on" : "uc-tab"} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </nav>

        {tab === "scout" && <ScoutTab watchlist={watchlist} toggleWatch={toggleWatch} />}
        {STRATEGIES.map((s) => (tab === s.id ? <StrategyTab key={s.id} strat={s} /> : null))}
        {tab === "watch" && <WatchTab watchlist={watchlist} toggleWatch={toggleWatch} />}
        {tab === "ipo" && <IpoTab />}
        {tab === "learn" && <LearnTab />}
      </div>
    </div>
  );
}
