-- Records what the summary and weekly jobs spend with the model.
--
-- `agent_runs.cost_usd` has always held the agent's spend, accumulated per
-- call. The other two jobs each make one Sonnet call and neither recorded what
-- it cost: the weekly review logged the figure and threw it away, the daily
-- summary never looked. So the only answer the database could give to "what
-- has this experiment spent" was missing a call a day and a call a week — low
-- by a small amount that grows without bound.
--
-- Six decimal places, matching `agent_runs.cost_usd`, for the reason given
-- there: a single call can cost a fraction of a cent and the point of the
-- figure is to notice when one suddenly doesn't.
ALTER TABLE daily_summaries ADD COLUMN IF NOT EXISTS cost_usd numeric(18, 6);
ALTER TABLE weekly_reviews ADD COLUMN IF NOT EXISTS cost_usd numeric(18, 6);

INSERT INTO schema_migrations (filename) VALUES ('007-job-costs.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> cost columns:'
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name = 'cost_usd'
ORDER BY table_name;
