-- Corrects `trades` for how order submission actually works.
--
-- The agent runs at 06:00 UTC and the US market opens at 14:30, so a market
-- order placed by the scheduled job sits `accepted` for eight hours. "Submitted,
-- outcome unknown" is the normal case, not an error — and the original schema
-- could not represent it.
--
-- Idempotent, like every file here: DROP NOT NULL on a nullable column is a
-- no-op, ADD COLUMN uses IF NOT EXISTS, and the uniqueness is a CREATE UNIQUE
-- INDEX IF NOT EXISTS rather than ADD CONSTRAINT, which has no such guard.

-- A notional order never names a quantity — Alpaca derives it from the dollar
-- amount — so the share count is unknown at submission even in principle. The
-- notional columns carry the size until a fill is reconciled.
ALTER TABLE trades ALTER COLUMN quantity DROP NOT NULL;

-- Our own deterministic id for the order, derived from the decision it
-- implements. Two jobs: it makes a resubmission idempotent at the broker, and
-- it is the handle the summary job uses to look up a fill that happened hours
-- after the run that caused it. Distinct from broker_order_id, which is
-- Alpaca's and is absent for a dry run.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS client_order_id text;
CREATE UNIQUE INDEX IF NOT EXISTS trades_client_order_id_key
  ON trades (client_order_id);

-- Which day's rate was used. ECB publishes on weekdays, so a Saturday run
-- converts at Friday's rate; without this, a weekend row looks like it used
-- the wrong rate rather than the only one that existed.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS fx_rate_as_of date;
ALTER TABLE daily_performance ADD COLUMN IF NOT EXISTS fx_rate_as_of date;

INSERT INTO schema_migrations (filename) VALUES ('002-trade-submission-fields.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> trades columns:'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'trades' AND column_name IN
  ('quantity', 'client_order_id', 'fx_rate_as_of', 'broker_order_id', 'notional_gbp')
ORDER BY column_name;
