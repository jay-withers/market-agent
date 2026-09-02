-- Grants the workload's managed identity access to the database.
--
-- Self-contained: it names the identity and database itself and switches
-- databases where it has to, so `psql --file` needs nothing passed in. Both
-- names are quoted because they contain hyphens, otherwise an operator.
--
-- The \connect dance is not optional. pgaadauth is installed only in the
-- `postgres` maintenance database — the application database has plpgsql and
-- nothing else — so pgaadauth_create_principal has to run there or it fails
-- with "function does not exist". Roles are cluster-wide, so the principal is
-- then visible to the GRANTs, which must themselves run against the
-- application database.
--
-- These are the dev names. Another environment needs its own copy of this file.
--
-- Safe to re-run: the principal is created only if absent, and GRANTs are
-- idempotent.

\connect postgres

SELECT pgaadauth_create_principal('uai-marketagent-dev', false, false)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'uai-marketagent-dev');

\connect psqldb-marketagent-dev

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
