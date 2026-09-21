-- USD account mirror. Legacy GBP columns remain for historical compatibility;
-- their values are never relabelled as USD. Migrate only a fresh experiment.
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename='008-usd-broker-account.sql') THEN
  IF EXISTS (SELECT 1 FROM trades) OR EXISTS (SELECT 1 FROM positions)
     OR EXISTS (SELECT 1 FROM daily_performance) OR EXISTS (SELECT 1 FROM ai_decisions)
     OR EXISTS (SELECT 1 FROM benchmarks) OR EXISTS (SELECT 1 FROM daily_summaries)
     OR EXISTS (SELECT 1 FROM weekly_reviews) THEN
   RAISE EXCEPTION 'Back up and reset the GBP experiment before migrating to the USD broker account';
  END IF;
 END IF;
END $$;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS initial_cash_usd numeric(18,4) NOT NULL DEFAULT 100000;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS cash_usd numeric(18,4) NOT NULL DEFAULT 100000;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS equity_usd numeric(18,4);
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS buying_power_usd numeric(18,4);
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS broker_account_id text;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS broker_synced_at timestamptz;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS market_value_usd numeric(18,4);
ALTER TABLE ai_decisions ADD COLUMN IF NOT EXISTS recommended_amount_usd numeric(18,4);
ALTER TABLE ai_decisions ADD COLUMN IF NOT EXISTS approved_amount_usd numeric(18,4);
ALTER TABLE daily_performance ADD COLUMN IF NOT EXISTS cash_usd numeric(18,4);
ALTER TABLE daily_performance ADD COLUMN IF NOT EXISTS positions_value_usd numeric(18,4);
ALTER TABLE daily_performance ADD COLUMN IF NOT EXISTS total_value_usd numeric(18,4);
ALTER TABLE daily_performance ADD COLUMN IF NOT EXISTS pnl_usd numeric(18,4);
ALTER TABLE benchmarks ADD COLUMN IF NOT EXISTS value_usd numeric(18,4);
ALTER TABLE portfolio ALTER COLUMN initial_cash_gbp DROP NOT NULL;
ALTER TABLE portfolio ALTER COLUMN cash_gbp DROP NOT NULL;
ALTER TABLE positions ALTER COLUMN avg_cost_gbp DROP NOT NULL;
ALTER TABLE trades ALTER COLUMN fx_rate_gbp_usd DROP NOT NULL;
ALTER TABLE daily_performance ALTER COLUMN cash_gbp DROP NOT NULL;
ALTER TABLE daily_performance ALTER COLUMN positions_value_gbp DROP NOT NULL;
ALTER TABLE daily_performance ALTER COLUMN total_value_gbp DROP NOT NULL;
ALTER TABLE daily_performance ALTER COLUMN pnl_gbp DROP NOT NULL;
ALTER TABLE daily_performance ALTER COLUMN fx_rate_gbp_usd DROP NOT NULL;
ALTER TABLE benchmarks ALTER COLUMN value_gbp DROP NOT NULL;
ALTER TABLE positions ALTER COLUMN quantity TYPE numeric(24,9);
ALTER TABLE positions ALTER COLUMN avg_cost_usd TYPE numeric(24,9);
ALTER TABLE trades ALTER COLUMN quantity TYPE numeric(24,9);
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS display_gbp_usd numeric(18,6);
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS display_fx_as_of date;
ALTER TABLE portfolio ALTER COLUMN base_currency SET DEFAULT 'USD';
UPDATE portfolio SET base_currency='USD' WHERE base_currency='GBP';
INSERT INTO schema_migrations(filename) VALUES ('008-usd-broker-account.sql') ON CONFLICT DO NOTHING;
COMMIT;
