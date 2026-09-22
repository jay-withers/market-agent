-- Splits the single 'default' experiment into two independent Alpaca paper
-- accounts, run side by side so they can be compared: 'static-100' trades a
-- frozen S&P 100 snapshot (sql/010), 'dynamic-500' trades the S&P 500,
-- refreshed periodically by the rebalance job as membership changes.
--
-- This is a RESET, not a migration of existing data: every trading fact
-- table is truncated and the single 'default' portfolio is replaced by the
-- two new accounts. Matches the precedent in 008-usd-broker-account.sql,
-- which refused to run over live data during the GBP-to-USD change — this
-- file inverts that guard into a deliberate one-time wipe instead of a
-- refusal. Back up the database (pg_dump) before applying this to anything
-- that has been trading; there is no way back from it. Guarded so a second
-- `make sql`/Invoke-DbSql.ps1 run, which replays the whole sql/ directory
-- every time, does not truncate a second time and destroy real history that
-- has since accumulated under the two new accounts.
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename='009-two-accounts.sql') THEN
  TRUNCATE trades, positions, ai_decisions, agent_runs, daily_performance,
    daily_summaries, weekly_reviews, benchmarks, portfolio
    RESTART IDENTITY CASCADE;

  -- Per-account facts that were previously implicit (there was only ever one
  -- portfolio). NOT NULL with no default is safe here only because the
  -- TRUNCATE above just emptied both tables in this same transaction.
  EXECUTE 'ALTER TABLE agent_runs ADD COLUMN portfolio_id bigint NOT NULL REFERENCES portfolio(id)';
  EXECUTE 'ALTER TABLE ai_decisions ADD COLUMN portfolio_id bigint NOT NULL REFERENCES portfolio(id)';

  -- benchmarks mixed a global fact (close_usd, the raw market price) with a
  -- portfolio-dependent one (value_usd: what this account's notional would be
  -- worth) under a key that assumed only one account could ever ask the
  -- question. Two accounts calling save_benchmarks for the same symbol/day
  -- would otherwise silently overwrite each other's value_usd.
  EXECUTE 'ALTER TABLE benchmarks DROP CONSTRAINT benchmarks_pkey';
  EXECUTE 'ALTER TABLE benchmarks ADD COLUMN portfolio_id bigint NOT NULL REFERENCES portfolio(id)';
  EXECUTE 'ALTER TABLE benchmarks ADD PRIMARY KEY (portfolio_id, symbol, as_of)';

  -- Same starting capital for both, so the comparison isolates universe and
  -- strategy effects rather than starting-capital effects. $500, matching
  -- both new Alpaca paper accounts' real starting balance — not the $100,000
  -- Alpaca gives by default, which risklimits.py's own defaults are sized
  -- for (see its docstring). Only a bootstrap placeholder either way:
  -- save_broker_snapshot() overwrites initial_cash_usd/cash_usd from the real
  -- account on each portfolio's first sync, so this is what a dashboard would
  -- show for the brief window before that first sync runs, not a figure
  -- anything downstream depends on.
  EXECUTE $ins$
    INSERT INTO portfolio (name, base_currency, initial_cash_usd, cash_usd) VALUES
      ('static-100',  'USD', 500, 500),
      ('dynamic-500', 'USD', 500, 500)
  $ins$;

  INSERT INTO schema_migrations (filename) VALUES ('009-two-accounts.sql');
 END IF;
END $$;
COMMIT;

-- Which portfolio actually trades a ticker, replacing the global
-- companies.is_active flag that only ever worked for one account.
-- companies stays global reference data (name/sector/exchange/is_benchmark);
-- this is the per-account layer on top of it. is_active=false (not a row
-- delete) is how the rebalance job drops an S&P 500 name that falls out of
-- the index without touching an existing position in it — same precedent
-- save_broker_snapshot already uses for a manually-held ticker.
CREATE TABLE IF NOT EXISTS portfolio_watchlist (
  portfolio_id bigint NOT NULL REFERENCES portfolio (id) ON DELETE CASCADE,
  ticker       text NOT NULL REFERENCES companies (ticker),
  is_active    boolean NOT NULL DEFAULT true,
  source       text NOT NULL DEFAULT 'manual'
                 CHECK (source IN ('sp100_snapshot', 'sp500_index', 'manual')),
  added_at     timestamptz NOT NULL DEFAULT now(),
  removed_at   timestamptz,
  PRIMARY KEY (portfolio_id, ticker)
);

CREATE INDEX IF NOT EXISTS agent_runs_portfolio_started_idx
  ON agent_runs (portfolio_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ai_decisions_portfolio_ticker_idx
  ON ai_decisions (portfolio_id, ticker, decided_at DESC);
CREATE INDEX IF NOT EXISTS benchmarks_portfolio_as_of_idx
  ON benchmarks (portfolio_id, as_of DESC);

\echo '==> two accounts:'
SELECT name, initial_cash_usd, cash_usd FROM portfolio ORDER BY name;
