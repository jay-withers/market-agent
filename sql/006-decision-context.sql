-- Records what the analysis model was shown beyond the portfolio itself.
--
-- `portfolio_state` holds the picture the risk engine judged, and the claim
-- attached to it is that a decision can be replayed later against exactly what
-- the model saw. The prompt now also carries the agent's own recent decisions
-- on the ticker, which is an input that no column recorded — so the claim was
-- about to become false. This is where the rest of the prompt's context goes.
--
-- jsonb rather than columns, for the reason `portfolio_state` and
-- `risk_verdict` are: the shape will change as the prompt does, and a
-- historical row must keep the shape it was written with. Structured rather
-- than the rendered prompt text so that "has it recommended the same thing
-- three days running?" stays a query.
--
-- Nullable with no default: rows written before this migration were taken
-- without that context, and an empty object would claim the model was shown an
-- empty history rather than none at all.
ALTER TABLE ai_decisions ADD COLUMN IF NOT EXISTS prompt_context jsonb;

INSERT INTO schema_migrations (filename) VALUES ('006-decision-context.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> ai_decisions context columns:'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'ai_decisions'
  AND column_name IN ('portfolio_state', 'risk_verdict', 'prompt_context')
ORDER BY column_name;
