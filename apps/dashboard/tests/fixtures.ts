/* The API, stubbed.
 *
 * These tests are about layout, not data, so they must not need Postgres, the
 * API image or a network — CI has none of them, and a layout test that only
 * runs when a database happens to be up is a layout test nobody runs.
 *
 * The shapes match what `queries.py` actually returns. They are deliberately
 * *awkward* rather than tidy: the longest company name on the watchlist, a
 * holding with no price at all, and a benchmark arm missing from the series,
 * because a layout only breaks on the content that does not fit.
 */

export const CONFIG = { apiOrigin: "", apiToken: "" };

const HOLDING_TICKERS = ["AAPL", "AMD", "AVGO", "GOOGL", "META", "NVDA"] as const;

export const OVERVIEW = {
  portfolio: { name: "default", initial_cash_gbp: 500 },
  cash_gbp: 99.27,
  positions_value_gbp: 415.4,
  total_value_gbp: 514.67,
  position_count: 6,
  pnl_gbp: 14.67,
  pnl_pct: 2.9338,
  last_run: {
    id: 16,
    started_at: "2026-09-19T06:00:30Z",
    status: "succeeded",
    trigger: "schedule",
    dry_run: true,
    decisions_made: 6,
    trades_executed: 0,
    cost_usd: 0.19,
  },
};

function series(days: number, start: number, step: number) {
  return Array.from({ length: days }, (_, i) => ({
    as_of: new Date(Date.UTC(2026, 5, 22 + i)).toISOString().slice(0, 10),
    value: start + i * step,
  }));
}

export const PERFORMANCE = {
  portfolio: series(90, 500, 0.16).map((p) => ({
    as_of: p.as_of,
    total_value_gbp: p.value,
    cash_gbp: 99.27,
    positions_value_gbp: p.value - 99.27,
    pnl_gbp: p.value - 500,
    pnl_pct: ((p.value - 500) / 500) * 100,
  })),
  // EWU is deliberately absent: a benchmark with no data is omitted rather
  // than drawn flat, so the legend has to cope with four arms, not five.
  benchmarks: ["SPY", "VT", "CASH5"].flatMap((symbol, n) =>
    series(90, 500, 0.1 + n * 0.05).map((p) => ({
      symbol,
      as_of: p.as_of,
      value_gbp: p.value,
      close_usd: 100 + n,
    })),
  ),
};

export const HOLDINGS = [
  {
    ticker: "AAPL",
    name: "Apple Inc.",
    sector: "Technology",
    quantity: 0.069234,
    avg_cost_usd: 326.57,
    avg_cost_gbp: 241.63,
    last_close_usd: 336.13,
    last_close_date: "2026-09-18",
  },
  {
    ticker: "AMD",
    // The longest name on the watchlist. A shorter one would not prove the
    // panel header truncates rather than pushing the panel wider.
    name: "Advanced Micro Devices, Inc.",
    sector: "Technology",
    quantity: 0.130149,
    avg_cost_usd: 521.1,
    avg_cost_gbp: 384.18,
    last_close_usd: 559.82,
    last_close_date: "2026-09-18",
  },
  {
    ticker: "AVGO",
    name: "Broadcom Inc.",
    sector: "Technology",
    quantity: 0.367254,
    avg_cost_usd: 363.86,
    avg_cost_gbp: 269.57,
    last_close_usd: 357.61,
    last_close_date: "2026-09-18",
  },
  {
    ticker: "GOOGL",
    name: "Alphabet Inc.",
    sector: "Communication Services",
    quantity: 0.278273,
    avg_cost_usd: 339.31,
    avg_cost_gbp: 251.55,
    last_close_usd: 349.54,
    last_close_date: "2026-09-18",
  },
  {
    ticker: "META",
    name: "Meta Platforms, Inc.",
    sector: "Communication Services",
    quantity: 0.197775,
    avg_cost_usd: 614.79,
    avg_cost_gbp: 455.06,
    last_close_usd: 665.75,
    last_close_date: "2026-09-18",
  },
  {
    // No price at all. `build_state` drops an unpriced holding from the
    // valuation, and the panel has to render the state rather than a line
    // through nothing.
    ticker: "NVDA",
    name: "NVIDIA Corporation",
    sector: "Technology",
    quantity: 0.440483,
    avg_cost_usd: 230.36,
    avg_cost_gbp: 170.27,
    last_close_usd: null,
    last_close_date: null,
  },
];

export const PRICES = HOLDING_TICKERS.filter((t) => t !== "NVDA").flatMap((ticker, n) =>
  Array.from({ length: 64 }, (_, i) => ({
    ticker,
    bar_date: new Date(Date.UTC(2026, 5, 22 + i)).toISOString().slice(0, 10),
    close_usd: 200 + n * 80 + Math.sin(i / 4) * 12,
  })),
);

export const DECISIONS = Array.from({ length: 12 }, (_, i) => ({
  id: i + 1,
  decided_at: "2026-09-19T06:03:00Z",
  ticker: HOLDING_TICKERS[i % HOLDING_TICKERS.length],
  action: i % 3 === 0 ? "BUY" : "HOLD",
  confidence: 0.72,
  recommended_amount_gbp: i % 3 === 0 ? 55 : null,
  approved_amount_gbp: i % 3 === 0 ? 50 : null,
  binding_constraint: i % 3 === 0 ? "max_position_pct" : null,
  approved: i % 3 === 0,
  news_count: i % 4,
}));

export const TRADES = [
  {
    id: 12,
    ticker: "AAPL",
    side: "BUY",
    status: "simulated",
    dry_run: true,
    quantity: 0.069234,
    price_usd: 326.57,
    notional_gbp: 16.73,
    created_at: "2026-09-11T06:03:18Z",
  },
];

export const RUNS = Array.from({ length: 8 }, (_, i) => ({
  id: 16 - i,
  started_at: `2026-09-${19 - i}T06:00:30Z`,
  finished_at: `2026-09-${19 - i}T06:03:38Z`,
  status: i === 2 ? "failed" : "succeeded",
  trigger: "schedule",
  dry_run: true,
  decisions_made: 6,
  trades_executed: 0,
  cost_usd: 0.19,
  // A long error, because the runs table truncates it and a short one would
  // not prove that.
  error: i === 2 ? "alpaca news endpoint returned 502 after three attempts" : null,
  stale: false,
}));

export const REVIEW = {
  id: 1,
  period_start: "2026-09-13",
  period_end: "2026-09-19",
  subject: "MarketAgent week to 2026-09-19",
  assessment:
    "Demo assessment. The machinery ran on every scheduled morning bar one, and the " +
    "binding constraint on more than half of the approved trades was the per-position " +
    "ceiling rather than the daily trade limit.",
  recommendations: [
    {
      area: "risk_limits",
      change: "Raise RISK_MAX_POSITION_PCT from 20 to 25",
      rationale: "It bound three of the four approvals this week.",
      expected_effect: "Fewer clamped approvals; more concentration risk.",
      confidence: 0.62,
    },
  ],
  model: "claude-sonnet-5",
  email_status: "sent",
  sent_at: "2026-09-19T22:00:00Z",
};

export const ROUTES: Record<string, unknown> = {
  "/config.json": CONFIG,
  "/api/overview": OVERVIEW,
  "/api/performance": PERFORMANCE,
  "/api/holdings": HOLDINGS,
  "/api/prices": PRICES,
  "/api/decisions": DECISIONS,
  "/api/trades": TRADES,
  "/api/runs": RUNS,
  "/api/reviews/latest": REVIEW,
};
