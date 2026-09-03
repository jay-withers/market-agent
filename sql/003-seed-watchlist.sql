-- Seeds the watchlist and the single portfolio row.
--
-- Data rather than schema, but it lives here for the same reason the grants do:
-- it has to exist before the agent can run, and ai_decisions.ticker has a
-- foreign key to companies, so an unseeded database fails every decision.
--
-- Idempotent via ON CONFLICT. Editing a name here and re-running updates it;
-- removing a ticker from this file does *not* remove it from the table, since
-- dropping a company would orphan its prices, news and decisions. Retire one
-- by setting is_active = false instead.
--
-- The ten names are large, liquid, heavily covered US listings — chosen so the
-- news feed actually has something to say about them most days, which is what
-- the experiment needs to test. Not a considered portfolio, and cheap to change.

INSERT INTO companies (ticker, name, exchange, sector) VALUES
  ('NVDA',  'NVIDIA Corporation',          'NASDAQ', 'Technology'),
  ('AAPL',  'Apple Inc.',                  'NASDAQ', 'Technology'),
  ('MSFT',  'Microsoft Corporation',       'NASDAQ', 'Technology'),
  ('GOOGL', 'Alphabet Inc.',               'NASDAQ', 'Communication Services'),
  ('AMZN',  'Amazon.com, Inc.',            'NASDAQ', 'Consumer Discretionary'),
  ('META',  'Meta Platforms, Inc.',        'NASDAQ', 'Communication Services'),
  ('TSLA',  'Tesla, Inc.',                 'NASDAQ', 'Consumer Discretionary'),
  ('AMD',   'Advanced Micro Devices, Inc.','NASDAQ', 'Technology'),
  ('AVGO',  'Broadcom Inc.',               'NASDAQ', 'Technology'),
  ('JPM',   'JPMorgan Chase & Co.',        'NYSE',   'Financials')
ON CONFLICT (ticker) DO UPDATE
  SET name = EXCLUDED.name,
      exchange = EXCLUDED.exchange,
      sector = EXCLUDED.sector;

-- The notional GBP 500 the whole experiment is about. initial_cash_gbp is never
-- updated, so total return stays computable after any number of trades; only
-- cash_gbp moves. DO NOTHING rather than DO UPDATE: re-running this file must
-- not reset a portfolio that has been trading.
INSERT INTO portfolio (name, base_currency, initial_cash_gbp, cash_gbp)
VALUES ('default', 'GBP', 500.0000, 500.0000)
ON CONFLICT (name) DO NOTHING;

INSERT INTO schema_migrations (filename) VALUES ('003-seed-watchlist.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> watchlist and portfolio:'
SELECT (SELECT count(*) FROM companies WHERE is_active) AS active_tickers,
       (SELECT count(*) FROM portfolio) AS portfolios,
       (SELECT cash_gbp FROM portfolio WHERE name = 'default') AS cash_gbp;
