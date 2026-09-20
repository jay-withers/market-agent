-- Fake but plausible data for looking at the dashboard locally.
--
-- Deliberately NOT in sql/. Everything there is idempotent, production-safe and
-- run against Azure by `make sql` with no arguments; this file is destructive
-- and exists only so the local stack has something to draw. Keeping it out of
-- that directory is what stops it ever being applied by accident, and the guard
-- below is the second line of defence.
--
-- Everything is derived rather than typed out: prices are a deterministic
-- pseudo-random walk keyed on a hash of the ticker and the day, so the same
-- seed produces the same chart every time and a reviewer looking at a
-- screenshot sees what the next person will see. No randomness that varies per
-- run, and no dependence on the query plan.
--
-- Loaded by `make demo`. Run it again to reset to the same state.

-- ---------------------------------------------------------------------------
-- Refuse to run anywhere but the local compose database.
-- ---------------------------------------------------------------------------
DO $guard$
BEGIN
  IF current_database() <> 'marketagent' THEN
    RAISE EXCEPTION
      'demo/seed.sql refuses to run against "%". It deletes every transactional row and is for the local compose database (marketagent) only.',
      current_database();
  END IF;
END
$guard$;

BEGIN;

-- companies stays: the watchlist and the benchmark rows are real reference data
-- that migrations 003 and 004 own, and prices.ticker refers to them.
TRUNCATE weekly_reviews, daily_summaries, benchmarks, daily_performance,
         trades, ai_decisions, agent_runs, news_analysis, news, prices, positions
  RESTART IDENTITY;

UPDATE portfolio SET cash_gbp = initial_cash_gbp, updated_at = now() WHERE name = 'default';

-- ---------------------------------------------------------------------------
-- Prices: 90 calendar days of weekday bars, one random walk per ticker.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE demo_days ON COMMIT DROP AS
SELECT d::date AS bar_date,
       row_number() OVER (ORDER BY d) AS n
  FROM generate_series(current_date - 90, current_date - 1, '1 day') d
 WHERE EXTRACT(isodow FROM d) < 6;

CREATE TEMP TABLE demo_seeds (ticker text, start_usd numeric, drift numeric, vol numeric)
  ON COMMIT DROP;
INSERT INTO demo_seeds VALUES
  ('NVDA',  195.00, 0.00120, 0.030),
  ('AAPL',  310.00, 0.00040, 0.016),
  ('MSFT',  505.00, 0.00055, 0.017),
  ('GOOGL', 305.00, 0.00135, 0.021),
  ('AMZN',  240.00, 0.00050, 0.020),
  ('META',  600.00, 0.00110, 0.024),
  ('TSLA',  420.00,-0.00060, 0.038),
  ('AMD',   470.00, 0.00160, 0.034),
  ('AVGO',  330.00, 0.00090, 0.025),
  ('JPM',   295.00, 0.00035, 0.013),
  -- The benchmark proxies move like indices: less drift, much less noise.
  ('SPY',   660.00, 0.00045, 0.008),
  ('VT',    145.00, 0.00038, 0.007),
  ('EWU',    42.00, 0.00025, 0.009);

INSERT INTO prices (ticker, bar_date, open_usd, high_usd, low_usd, close_usd, volume, source)
WITH steps AS (
  SELECT s.ticker,
         d.bar_date,
         d.n,
         s.start_usd,
         -- Deterministic in [-0.5, 0.5): a hash of the ticker and the day
         -- index, not random(). Same output on every run and under any plan.
         s.drift + s.vol *
           ((('x' || substr(md5(s.ticker || ':' || d.n::text), 1, 8))::bit(32)::bigint
             % 1000) / 1000.0 - 0.5) AS step
    FROM demo_seeds s
    CROSS JOIN demo_days d
),
walk AS (
  SELECT ticker,
         bar_date,
         n,
         round((start_usd * exp(sum(step) OVER (PARTITION BY ticker ORDER BY n)))::numeric, 4)
           AS close_usd
    FROM steps
)
SELECT ticker,
       bar_date,
       -- An open near the previous close, and a high/low that actually bracket
       -- both: a bar whose close sits outside its own range looks wrong the
       -- moment anyone plots a candle.
       round(close_usd * 0.998, 4),
       round(close_usd * 1.009, 4),
       round(close_usd * 0.991, 4),
       close_usd,
       (2000000 + (('x' || substr(md5(ticker || bar_date::text), 1, 6))::bit(24)::bigint % 8000000)),
       'demo'
  FROM walk;

-- ---------------------------------------------------------------------------
-- Agent runs: one a weekday morning, with one failure and one abandoned row so
-- the runs table and the summary's alert path have something to show.
-- ---------------------------------------------------------------------------
INSERT INTO agent_runs (started_at, finished_at, status, trigger, dry_run, image_tag,
                        tickers_considered, news_fetched, news_relevant, decisions_made,
                        trades_executed, input_tokens, output_tokens, cost_usd, error)
SELECT d.bar_date + time '06:00',
       CASE WHEN d.n % 17 = 0 THEN NULL ELSE d.bar_date + time '06:04' END,
       CASE WHEN d.n % 17 = 0 THEN 'running'
            WHEN d.n % 23 = 0 THEN 'failed'
            ELSE 'succeeded' END,
       'schedule',
       true,
       'v0.8.1',
       10,
       70 + (d.n % 25),
       30 + (d.n % 18),
       CASE WHEN d.n % 23 = 0 THEN 0 ELSE 4 + (d.n % 6) END,
       CASE WHEN d.n BETWEEN 24 AND 34 AND d.n % 3 = 0 THEN 1 ELSE 0 END,
       80000 + (d.n * 137 % 20000),
       9000 + (d.n * 71 % 4000),
       round((0.17 + (d.n % 9) * 0.004)::numeric, 6),
       CASE WHEN d.n % 23 = 0 THEN 'alpaca news endpoint returned 502' ELSE NULL END
  FROM demo_days d;

-- ---------------------------------------------------------------------------
-- Six holdings, bought between day 24 and day 34 of the window.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE demo_buys (ticker text, buy_n int, notional_gbp numeric) ON COMMIT DROP;
INSERT INTO demo_buys VALUES
  ('AMD',   24, 50.0000),
  ('META',  27, 90.0000),
  ('NVDA',  29, 75.0000),
  ('GOOGL', 31, 70.0000),
  ('AVGO',  33, 99.0000),
  ('AAPL',  34, 16.7300);

-- One rate per day, drifting gently. Deterministic, and near enough to the real
-- 1.27–1.35 range that the pound figures look right.
CREATE TEMP TABLE demo_fx ON COMMIT DROP AS
SELECT d.bar_date,
       d.n,
       round((1.30 + 0.035 * sin(d.n / 9.0))::numeric, 6) AS rate
  FROM demo_days d;

CREATE TEMP TABLE demo_fills ON COMMIT DROP AS
SELECT b.ticker,
       b.buy_n,
       d.bar_date AS bought_on,
       b.notional_gbp,
       f.rate AS fx,
       p.close_usd AS price_usd,
       round((b.notional_gbp * f.rate / p.close_usd)::numeric, 6) AS quantity,
       round((b.notional_gbp * f.rate)::numeric, 4) AS notional_usd
  FROM demo_buys b
  JOIN demo_days d ON d.n = b.buy_n
  JOIN demo_fx f ON f.n = b.buy_n
  JOIN prices p ON p.ticker = b.ticker AND p.bar_date = d.bar_date;

INSERT INTO positions (portfolio_id, ticker, quantity, avg_cost_usd, avg_cost_gbp, opened_at, updated_at)
SELECT pf.id,
       fl.ticker,
       fl.quantity,
       fl.price_usd,
       round((fl.notional_gbp / fl.quantity)::numeric, 4),
       fl.bought_on + time '06:05',
       fl.bought_on + time '06:05'
  FROM demo_fills fl
  CROSS JOIN (SELECT id FROM portfolio WHERE name = 'default') pf;

UPDATE portfolio
   SET cash_gbp = initial_cash_gbp - (SELECT sum(notional_gbp) FROM demo_fills),
       updated_at = now()
 WHERE name = 'default';

-- ---------------------------------------------------------------------------
-- Decisions: one per holding on the day it was bought, plus a spread of HOLDs
-- and a couple the risk engine refused, so "bound by" has variety.
-- ---------------------------------------------------------------------------
INSERT INTO ai_decisions (run_id, decided_at, ticker, action, confidence, reasoning, risks,
                          model, prompt_version, recommended_amount_gbp, approved_amount_gbp,
                          portfolio_state, risk_verdict, input_tokens, output_tokens)
SELECT r.id,
       fl.bought_on + time '06:03',
       fl.ticker,
       'BUY',
       0.72 + (fl.buy_n % 5) * 0.03,
       'Demo data: sentiment across the day''s coverage was constructive and the '
         || fl.ticker || ' setup looked favourable against the sector.',
       'Demo data: concentration in a single sector, and a broad market pullback.',
       'claude-sonnet-5',
       'v3',
       fl.notional_gbp + 15,
       fl.notional_gbp,
       jsonb_build_object('cash_gbp', '500.0000', 'invested_gbp', '0.0000'),
       jsonb_build_object(
         'approved', true,
         'approved_amount_gbp', fl.notional_gbp::text,
         'binding_constraint', CASE WHEN fl.notional_gbp >= 90 THEN 'max_position_pct'
                                    ELSE 'recommended_amount' END,
         'reasons', jsonb_build_array(
           jsonb_build_object('constraint', 'max_trade_gbp', 'detail', 'within the per-trade cap'))),
       4200,
       520
  FROM demo_fills fl
  JOIN agent_runs r ON r.started_at::date = fl.bought_on;

-- Refusals and holds across the rest of the window.
INSERT INTO ai_decisions (run_id, decided_at, ticker, action, confidence, reasoning, risks,
                          model, prompt_version, recommended_amount_gbp, approved_amount_gbp,
                          portfolio_state, risk_verdict, input_tokens, output_tokens)
SELECT r.id,
       r.started_at + interval '3 minutes',
       s.ticker,
       CASE WHEN d.n % 4 = 0 THEN 'BUY' ELSE 'HOLD' END,
       0.55 + (d.n % 7) * 0.04,
       'Demo data: no decisive change in the coverage for ' || s.ticker || ' today.',
       'Demo data: the position would add to an already concentrated book.',
       'claude-sonnet-5',
       'v3',
       CASE WHEN d.n % 4 = 0 THEN 40.0000 ELSE NULL END,
       CASE WHEN d.n % 4 = 0 THEN 0.0000 ELSE NULL END,
       jsonb_build_object('cash_gbp', '99.2700'),
       CASE WHEN d.n % 4 = 0 THEN
              jsonb_build_object('approved', false, 'binding_constraint', 'daily_trade_limit',
                'reasons', jsonb_build_array(jsonb_build_object(
                  'constraint', 'daily_trade_limit', 'detail', 'already at the daily limit')))
            ELSE
              jsonb_build_object('approved', true, 'binding_constraint', NULL, 'reasons', '[]'::jsonb)
       END,
       3800,
       410
  FROM demo_days d
  JOIN agent_runs r ON r.started_at::date = d.bar_date AND r.status = 'succeeded'
  JOIN demo_seeds s ON s.ticker = CASE d.n % 4 WHEN 0 THEN 'TSLA' WHEN 1 THEN 'MSFT'
                                               WHEN 2 THEN 'AMZN' ELSE 'JPM' END
 WHERE d.n > 34;

INSERT INTO trades (portfolio_id, decision_id, ticker, side, quantity, price_usd, notional_usd,
                    notional_gbp, fx_rate_gbp_usd, broker, status, dry_run,
                    submitted_at, filled_at, created_at)
SELECT pf.id,
       dec.id,
       fl.ticker,
       'BUY',
       fl.quantity,
       fl.price_usd,
       fl.notional_usd,
       fl.notional_gbp,
       fl.fx,
       'alpaca',
       'simulated',
       true,
       fl.bought_on + time '06:05',
       fl.bought_on + time '06:05',
       fl.bought_on + time '06:05'
  FROM demo_fills fl
  CROSS JOIN (SELECT id FROM portfolio WHERE name = 'default') pf
  JOIN ai_decisions dec ON dec.ticker = fl.ticker AND dec.decided_at::date = fl.bought_on;

-- ---------------------------------------------------------------------------
-- Daily performance: every calendar day, valued at the most recent close on or
-- before it — so weekends carry Friday forward, exactly as the real series does.
-- ---------------------------------------------------------------------------
INSERT INTO daily_performance (portfolio_id, as_of, cash_gbp, positions_value_gbp,
                               total_value_gbp, pnl_gbp, pnl_pct, fx_rate_gbp_usd)
SELECT pf.id,
       cal.as_of,
       round(cash.gbp, 4),
       round(coalesce(held.gbp, 0), 4),
       round(cash.gbp + coalesce(held.gbp, 0), 4),
       round(cash.gbp + coalesce(held.gbp, 0) - pf.initial_cash_gbp, 4),
       round((cash.gbp + coalesce(held.gbp, 0) - pf.initial_cash_gbp)
             / pf.initial_cash_gbp * 100, 4),
       fx.rate
  FROM generate_series(current_date - 90, current_date - 1, '1 day') AS cal(as_of)
  CROSS JOIN (SELECT id, initial_cash_gbp FROM portfolio WHERE name = 'default') pf
  -- The rate in force: the most recent trading day's, carried across weekends.
  JOIN LATERAL (
    SELECT rate FROM demo_fx WHERE bar_date <= cal.as_of ORDER BY bar_date DESC LIMIT 1
  ) fx ON true
  JOIN LATERAL (
    SELECT pf.initial_cash_gbp
           - coalesce((SELECT sum(notional_gbp) FROM demo_fills
                        WHERE bought_on <= cal.as_of), 0) AS gbp
  ) cash ON true
  LEFT JOIN LATERAL (
    SELECT sum(fl.quantity * lc.close_usd / fx.rate) AS gbp
      FROM demo_fills fl
      JOIN LATERAL (
        SELECT close_usd FROM prices
         WHERE ticker = fl.ticker AND bar_date <= cal.as_of
         ORDER BY bar_date DESC LIMIT 1
      ) lc ON true
     WHERE fl.bought_on <= cal.as_of
  ) held ON true
 WHERE cal.as_of >= (SELECT min(bar_date) FROM demo_days);

-- ---------------------------------------------------------------------------
-- Benchmark arms. The index arms are a pure ratio against the close at
-- inception, so no FX appears in them; cash compounds daily.
-- ---------------------------------------------------------------------------
INSERT INTO benchmarks (symbol, as_of, close_usd, fx_rate_gbp_usd, value_gbp, source)
SELECT p.ticker,
       cal.as_of,
       lc.close_usd,
       fx.rate,
       round((500.0 * lc.close_usd / base.close_usd)::numeric, 4),
       'demo'
  FROM (VALUES ('SPY'), ('VT'), ('EWU')) AS p(ticker)
  CROSS JOIN generate_series(current_date - 90, current_date - 1, '1 day') AS cal(as_of)
  JOIN LATERAL (
    SELECT close_usd FROM prices WHERE ticker = p.ticker AND bar_date <= cal.as_of
     ORDER BY bar_date DESC LIMIT 1
  ) lc ON true
  JOIN LATERAL (
    SELECT close_usd FROM prices WHERE ticker = p.ticker ORDER BY bar_date LIMIT 1
  ) base ON true
  JOIN LATERAL (
    SELECT rate FROM demo_fx WHERE bar_date <= cal.as_of ORDER BY bar_date DESC LIMIT 1
  ) fx ON true
 WHERE cal.as_of >= (SELECT min(bar_date) FROM demo_days);

INSERT INTO benchmarks (symbol, as_of, close_usd, fx_rate_gbp_usd, value_gbp, source)
SELECT 'CASH5',
       cal.as_of,
       NULL,
       NULL,
       -- ::date on both sides: generate_series over dates yields timestamps,
       -- and timestamp minus date is an interval rather than a day count.
       round((500.0 * power(1 + 0.05 / 365.0,
              (cal.as_of::date - (SELECT min(bar_date) FROM demo_days))))::numeric, 4),
       'demo'
  FROM generate_series(current_date - 90, current_date - 1, '1 day') AS cal(as_of)
 WHERE cal.as_of >= (SELECT min(bar_date) FROM demo_days);

-- ---------------------------------------------------------------------------
-- The written artefacts: recent daily emails and one weekly review.
-- ---------------------------------------------------------------------------
INSERT INTO daily_summaries (as_of, subject, body_markdown, body_html, model, prompt_version,
                             email_status, sent_at, cost_usd)
SELECT dp.as_of,
       'MarketAgent ' || dp.as_of || ': £' || dp.total_value_gbp
         || ' (' || CASE WHEN dp.pnl_pct >= 0 THEN '+' ELSE '' END || dp.pnl_pct || '%)',
       '# MarketAgent — ' || dp.as_of || E'\n\nDemo data. The portfolio is worth £'
         || dp.total_value_gbp || ', against the £500 it started with.',
       '<h1>MarketAgent — ' || dp.as_of || '</h1><p>Demo data. The portfolio is worth £'
         || dp.total_value_gbp || ', against the £500 it started with.</p>',
       'claude-sonnet-5',
       'v3',
       'sent',
       dp.as_of + time '21:00',
       0.014500
  FROM daily_performance dp
 WHERE dp.as_of > current_date - 15;

INSERT INTO weekly_reviews (period_start, period_end, subject, assessment, body_markdown,
                            body_html, metrics, recommendations, model, prompt_version,
                            email_status, sent_at, cost_usd)
SELECT current_date - 8,
       current_date - 2,
       'MarketAgent week to ' || (current_date - 2),
       'Demo data. The machinery ran on every scheduled morning bar one, and the '
         || 'binding constraint on more than half of the approved trades was the per-position '
         || 'ceiling rather than the daily trade limit — which suggests the book is '
         || 'concentration-bound rather than cadence-bound. Two refusals were the daily limit '
         || 'doing its job on a day the model wanted four positions.',
       '## Week in review' || E'\n\nDemo data.',
       '<h2>Week in review</h2><p>Demo data.</p>',
       jsonb_build_object('runs', 5, 'decisions', 28, 'trades', 2),
       jsonb_build_array(
         jsonb_build_object(
           'area', 'risk_limits',
           'change', 'Raise RISK_MAX_POSITION_PCT from 20 to 25',
           'rationale', 'It bound three of the four approvals this week, so it — not the '
                        || 'model''s conviction — is setting position size.',
           'expected_effect', 'Fewer clamped approvals; more concentration risk.',
           'confidence', 0.62),
         jsonb_build_object(
           'area', 'watchlist',
           'change', 'Consider retiring TSLA from the watchlist',
           'rationale', 'Fifteen analyses, no approved trade, and the highest filter cost '
                        || 'of any name on the list.',
           'expected_effect', 'Slightly cheaper runs, one less distraction.',
           'confidence', 0.48)),
       'claude-sonnet-5',
       'v3',
       'sent',
       (current_date - 2) + time '22:00',
       0.021000;

COMMIT;

\echo ''
\echo 'Demo data loaded.'
SELECT (SELECT count(*) FROM prices) AS prices,
       (SELECT count(*) FROM positions) AS positions,
       (SELECT count(*) FROM daily_performance) AS perf_rows,
       (SELECT count(*) FROM ai_decisions) AS decisions,
       (SELECT count(*) FROM trades) AS trades,
       (SELECT count(*) FROM agent_runs) AS runs;
