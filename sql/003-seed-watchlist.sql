-- Seeds ten reference tickers into companies.
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
--
-- No longer seeds a portfolio row: 009-two-accounts.sql replaced the single
-- 'default' portfolio with 'static-100'/'dynamic-500', and neither trades
-- these ten names (see 010-seed-sp100-static.sql for static-100's actual
-- watchlist). These rows exist purely as harmless companies reference data
-- now — nothing marks them active on either account's watchlist.

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

INSERT INTO schema_migrations (filename) VALUES ('003-seed-watchlist.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> reference tickers seeded:'
SELECT count(*) AS companies FROM companies
WHERE ticker IN ('NVDA','AAPL','MSFT','GOOGL','AMZN','META','TSLA','AMD','AVGO','JPM');
