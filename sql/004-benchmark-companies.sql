-- Lets benchmark symbols have prices without becoming tradeable.
--
-- `prices.ticker` references `companies`, which is right for the watchlist and
-- wrong for the comparison arms: the summary job stores closes for SPY, VT and
-- EWU, and the insert failed with a foreign key violation because a benchmark
-- is not a company we follow.
--
-- Marking them rather than dropping the constraint keeps the referential
-- integrity that stops a typo'd ticker silently accumulating prices. A separate
-- flag rather than reusing is_active = false, which means "retired from the
-- watchlist" — a benchmark was never on it, and the dashboard has to tell the
-- two apart to label a proxy as a proxy.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS is_benchmark boolean NOT NULL DEFAULT false;

-- EWU is a UK equity ETF used as a UK proxy. It is **not** the FTSE 100, and
-- saying so is the difference between a proxy and a wrong number.
INSERT INTO companies (ticker, name, exchange, sector, is_active, is_benchmark) VALUES
  ('SPY', 'SPDR S&P 500 ETF Trust',            'NYSE', 'Index proxy', false, true),
  ('VT',  'Vanguard Total World Stock ETF',    'NYSE', 'Index proxy', false, true),
  ('EWU', 'iShares MSCI United Kingdom ETF',   'NYSE', 'Index proxy', false, true)
ON CONFLICT (ticker) DO UPDATE
  SET name = EXCLUDED.name,
      sector = EXCLUDED.sector,
      is_active = false,
      is_benchmark = true;

INSERT INTO schema_migrations (filename) VALUES ('004-benchmark-companies.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> watchlist vs benchmarks:'
SELECT count(*) FILTER (WHERE is_active AND NOT is_benchmark) AS watchlist,
       count(*) FILTER (WHERE is_benchmark) AS benchmarks
FROM companies;
