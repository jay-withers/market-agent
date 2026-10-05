-- Narrow the tech pot from about 50 names to 10.
--
-- The 2026-10-05 weekly review found 46 of tech's 50 names produced no
-- decision all week, from only 134 articles, while health and energy each
-- decided on 16-17 names. The silent names still cost a news fetch and a
-- filter pass every day. The ten kept are the four that had decisions
-- (AMD, MSFT, QCOM, SNPS) plus six large caps for sector coverage.
--
-- Rows are deactivated, not deleted, so history and the dashboard's past
-- views keep working. A name the pot currently holds stays active: the agent
-- only considers a SELL for tickers on the active watchlist, so deactivating a
-- holding would strand it. It can be deactivated once it is sold.
--
-- The guard keeps a replay from undoing a later manual re-activation, the
-- same discipline 011 applies to its own deactivation.

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = '012-narrow-tech-pot.sql') THEN
  UPDATE portfolio_watchlist w SET is_active = false
  FROM portfolio p
  WHERE p.id = w.portfolio_id AND p.name = 'tech' AND w.is_active
    AND w.ticker NOT IN ('AMD', 'MSFT', 'QCOM', 'SNPS', 'AAPL', 'AVGO', 'CRM', 'NOW', 'NVDA', 'ORCL')
    AND NOT EXISTS (
      SELECT 1 FROM positions pos
      WHERE pos.portfolio_id = w.portfolio_id AND pos.ticker = w.ticker AND pos.quantity > 0
    );
 END IF;
END $$;

INSERT INTO schema_migrations (filename) VALUES ('012-narrow-tech-pot.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> tech watchlist:'
SELECT w.ticker, w.is_active
FROM portfolio_watchlist w JOIN portfolio p ON p.id = w.portfolio_id
WHERE p.name = 'tech' AND w.is_active
ORDER BY w.ticker;
