-- Grants the workload's managed identity access to the database.
--
-- Self-contained: the identity and database are named literally, so this runs
-- as-is under plain `psql --file` with nothing to pass in. Both are quoted
-- because the names contain hyphens, which are otherwise an operator.
--
-- These are the dev names. Another environment needs its own copy of this file.
--
-- Safe to re-run: the principal is created only if absent, and GRANTs are
-- idempotent.

SELECT pgaadauth_create_principal('uai-marketagent-dev', false, false)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'uai-marketagent-dev');

GRANT CONNECT ON DATABASE "psqldb-marketagent-dev" TO "uai-marketagent-dev";
GRANT USAGE, CREATE ON SCHEMA public TO "uai-marketagent-dev";

-- Default privileges rather than a one-off GRANT ON ALL TABLES: the latter
-- covers only tables that exist right now, so anything a later migration
-- creates would be invisible to the workload. Migrations run as this identity
-- and so own their tables outright, but this keeps a migration run by hand as
-- the administrator from silently locking the app out.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "uai-marketagent-dev";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO "uai-marketagent-dev";

\echo '==> principal:'
SELECT rolname FROM pg_roles WHERE rolname = 'uai-marketagent-dev';
