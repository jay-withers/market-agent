-- MarketAgent's schema, for DuckDB.
--
-- The end state of the Postgres migrations sql/001 to sql/012, collapsed into
-- one file when the database moved from a Flexible Server to a DuckDB file in
-- blob storage. Columns keep the Postgres order, so `marketagent import-pg`
-- copies each table column for column.
--
-- Applied by db.py when it opens a database with no row for this file in
-- schema_migrations. A later change is a new numbered file next to this one,
-- which inserts its own row at the end, as the Postgres files did.
--
-- Conventions, carried over unchanged:
--
--   * Money is DECIMAL(18,4), never float. DuckDB keeps DECIMAL exact through
--     sum() and arithmetic, which is why it replaced Postgres rather than
--     SQLite, whose NUMERIC affinity would store a REAL.
--   * Quantities are DECIMAL(24,9): Alpaca fills fractional shares.
--   * Every timestamp is TIMESTAMPTZ, because the cron schedules are UTC.
--   * Statuses are TEXT + CHECK rather than enum types.
--
-- **No foreign keys.** DuckDB has no ON DELETE CASCADE or SET NULL, and it
-- refuses an upsert on any row another table references (an upsert is a
-- delete and insert inside its index). That would break save_broker_snapshot's
-- portfolio update on the first run. Nothing here deletes a referenced row, and
-- only one writer holds the blob lease at a time, so the application keeps the
-- references it always wrote.
--
-- No secondary indexes either. Every table is small enough to scan, and DuckDB
-- ART indexes only add constraint-checking cost to updates. Primary keys and
-- UNIQUE stay: ON CONFLICT needs them.

CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Identity columns as sequences: DuckDB has no GENERATED ... AS IDENTITY.
-- import-pg restarts each one past the highest id it copied.
CREATE SEQUENCE IF NOT EXISTS news_id_seq;
CREATE SEQUENCE IF NOT EXISTS news_analysis_id_seq;
CREATE SEQUENCE IF NOT EXISTS portfolio_id_seq;
CREATE SEQUENCE IF NOT EXISTS positions_id_seq;
CREATE SEQUENCE IF NOT EXISTS agent_runs_id_seq;
CREATE SEQUENCE IF NOT EXISTS ai_decisions_id_seq;
CREATE SEQUENCE IF NOT EXISTS trades_id_seq;
CREATE SEQUENCE IF NOT EXISTS daily_summaries_id_seq;
CREATE SEQUENCE IF NOT EXISTS weekly_reviews_id_seq;

-- ---------------------------------------------------------------------------
-- Reference data: the watchlist and the market data fetched for it
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS companies (
  ticker       TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  exchange     TEXT,
  sector       TEXT,
  currency     TEXT NOT NULL DEFAULT 'USD',
  -- Retirement is a flag rather than a DELETE, so prices, news and decisions
  -- never lose their company.
  is_active    BOOLEAN NOT NULL DEFAULT true,
  added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The benchmark ETFs (SPY, VT, EWU): priced like companies, never traded.
  is_benchmark BOOLEAN NOT NULL DEFAULT false
);

-- Daily bars. The composite key makes the fetch an upsert.
CREATE TABLE IF NOT EXISTS prices (
  ticker     TEXT NOT NULL,
  bar_date   DATE NOT NULL,
  open_usd   DECIMAL(18, 4),
  high_usd   DECIMAL(18, 4),
  low_usd    DECIMAL(18, 4),
  close_usd  DECIMAL(18, 4) NOT NULL,
  volume     BIGINT,
  source     TEXT NOT NULL DEFAULT 'alpaca',
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (ticker, bar_date)
);

-- ---------------------------------------------------------------------------
-- News and its analysis
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS news (
  id           BIGINT PRIMARY KEY DEFAULT nextval('news_id_seq'),
  -- UNIQUE is the deduplication: consecutive runs see overlapping windows.
  external_id  TEXT NOT NULL UNIQUE,
  source       TEXT NOT NULL DEFAULT 'alpaca',
  tickers      TEXT[] NOT NULL DEFAULT [],
  headline     TEXT NOT NULL,
  summary      TEXT,
  author       TEXT,
  url          TEXT,
  published_at TIMESTAMPTZ NOT NULL,
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news_analysis (
  id              BIGINT PRIMARY KEY DEFAULT nextval('news_analysis_id_seq'),
  news_id         BIGINT NOT NULL,
  ticker          TEXT NOT NULL,
  relevant        BOOLEAN NOT NULL,
  sentiment       TEXT CHECK (sentiment IN ('positive', 'neutral', 'negative')),
  sentiment_score DECIMAL(6, 4) CHECK (sentiment_score BETWEEN -1 AND 1),
  rationale       TEXT,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  analysed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (news_id, ticker, model, prompt_version)
);

-- ---------------------------------------------------------------------------
-- The pots. These tables mirror Alpaca's paper accounts, which are the ledger.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS portfolio (
  id                BIGINT PRIMARY KEY DEFAULT nextval('portfolio_id_seq'),
  name              TEXT NOT NULL UNIQUE,
  base_currency     TEXT NOT NULL DEFAULT 'USD',
  -- The GBP columns are the pre-USD experiment's, kept nullable for history.
  initial_cash_gbp  DECIMAL(18, 4),
  cash_gbp          DECIMAL(18, 4),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  initial_cash_usd  DECIMAL(18, 4) NOT NULL DEFAULT 100000,
  cash_usd          DECIMAL(18, 4) NOT NULL DEFAULT 100000,
  equity_usd        DECIMAL(18, 4),
  buying_power_usd  DECIMAL(18, 4),
  broker_account_id TEXT,
  broker_synced_at  TIMESTAMPTZ,
  display_gbp_usd   DECIMAL(18, 6),
  display_fx_as_of  DATE,
  is_active         BOOLEAN NOT NULL DEFAULT true,
  description       TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_watchlist (
  portfolio_id BIGINT NOT NULL,
  ticker       TEXT NOT NULL,
  is_active    BOOLEAN NOT NULL DEFAULT true,
  source       TEXT NOT NULL DEFAULT 'manual'
                 CHECK (source IN ('sp100_snapshot', 'sp500_index', 'manual', 'sector_snapshot')),
  added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  removed_at   TIMESTAMPTZ,
  PRIMARY KEY (portfolio_id, ticker)
);

CREATE TABLE IF NOT EXISTS positions (
  id               BIGINT PRIMARY KEY DEFAULT nextval('positions_id_seq'),
  portfolio_id     BIGINT NOT NULL,
  ticker           TEXT NOT NULL,
  quantity         DECIMAL(24, 9) NOT NULL,
  avg_cost_usd     DECIMAL(24, 9) NOT NULL,
  avg_cost_gbp     DECIMAL(18, 4),
  opened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  market_value_usd DECIMAL(18, 4),
  UNIQUE (portfolio_id, ticker)
);

-- ---------------------------------------------------------------------------
-- The audit trail
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_runs (
  id                 BIGINT PRIMARY KEY DEFAULT nextval('agent_runs_id_seq'),
  started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at        TIMESTAMPTZ,
  status             TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'succeeded', 'failed')),
  trigger            TEXT NOT NULL DEFAULT 'schedule'
                       CHECK (trigger IN ('schedule', 'manual')),
  dry_run            BOOLEAN NOT NULL DEFAULT false,
  image_tag          TEXT,
  tickers_considered INTEGER,
  news_fetched       INTEGER,
  news_relevant      INTEGER,
  decisions_made     INTEGER,
  trades_executed    INTEGER,
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cost_usd           DECIMAL(18, 6),
  error              TEXT,
  portfolio_id       BIGINT NOT NULL,
  config             JSON
);

CREATE TABLE IF NOT EXISTS ai_decisions (
  id                     BIGINT PRIMARY KEY DEFAULT nextval('ai_decisions_id_seq'),
  run_id                 BIGINT,
  decided_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  ticker                 TEXT NOT NULL,
  action                 TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD')),
  confidence             DECIMAL(5, 4) CHECK (confidence BETWEEN 0 AND 1),
  reasoning              TEXT,
  risks                  TEXT,
  model                  TEXT NOT NULL,
  prompt_version         TEXT NOT NULL,
  news_ids               BIGINT[] NOT NULL DEFAULT [],
  recommended_amount_gbp DECIMAL(18, 4),
  approved_amount_gbp    DECIMAL(18, 4),
  -- JSON rather than columns: a historical row keeps the shape it was written
  -- with.
  portfolio_state        JSON,
  risk_verdict           JSON,
  input_tokens           INTEGER,
  output_tokens          INTEGER,
  prompt_context         JSON,
  recommended_amount_usd DECIMAL(18, 4),
  approved_amount_usd    DECIMAL(18, 4),
  portfolio_id           BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  id              BIGINT PRIMARY KEY DEFAULT nextval('trades_id_seq'),
  portfolio_id    BIGINT NOT NULL,
  decision_id     BIGINT,
  ticker          TEXT NOT NULL,
  side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  quantity        DECIMAL(24, 9),
  price_usd       DECIMAL(18, 4),
  notional_usd    DECIMAL(18, 4),
  notional_gbp    DECIMAL(18, 4),
  fx_rate_gbp_usd DECIMAL(18, 6),
  broker          TEXT NOT NULL DEFAULT 'alpaca',
  broker_order_id TEXT UNIQUE,
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'submitted', 'filled',
                                      'partially_filled', 'cancelled',
                                      'rejected', 'simulated')),
  dry_run         BOOLEAN NOT NULL DEFAULT false,
  submitted_at    TIMESTAMPTZ,
  filled_at       TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Deterministic per decision, so an order POST is never retried.
  client_order_id TEXT UNIQUE,
  fx_rate_as_of   DATE
);

-- ---------------------------------------------------------------------------
-- Daily and weekly rollups
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS daily_performance (
  portfolio_id        BIGINT NOT NULL,
  as_of               DATE NOT NULL,
  cash_gbp            DECIMAL(18, 4),
  positions_value_gbp DECIMAL(18, 4),
  total_value_gbp     DECIMAL(18, 4),
  pnl_gbp             DECIMAL(18, 4),
  -- Percent, not a fraction: 12.3400 means +12.34%.
  pnl_pct             DECIMAL(10, 4) NOT NULL,
  fx_rate_gbp_usd     DECIMAL(18, 6),
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  fx_rate_as_of       DATE,
  cash_usd            DECIMAL(18, 4),
  positions_value_usd DECIMAL(18, 4),
  total_value_usd     DECIMAL(18, 4),
  pnl_usd             DECIMAL(18, 4),
  PRIMARY KEY (portfolio_id, as_of)
);

CREATE TABLE IF NOT EXISTS benchmarks (
  symbol          TEXT NOT NULL,
  as_of           DATE NOT NULL,
  close_usd       DECIMAL(18, 4),
  fx_rate_gbp_usd DECIMAL(18, 6),
  value_gbp       DECIMAL(18, 4),
  source          TEXT,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  value_usd       DECIMAL(18, 4),
  portfolio_id    BIGINT NOT NULL,
  PRIMARY KEY (portfolio_id, symbol, as_of)
);

CREATE TABLE IF NOT EXISTS daily_summaries (
  id                BIGINT PRIMARY KEY DEFAULT nextval('daily_summaries_id_seq'),
  as_of             DATE NOT NULL UNIQUE,
  subject           TEXT NOT NULL,
  body_markdown     TEXT,
  body_html         TEXT,
  model             TEXT,
  prompt_version    TEXT,
  email_status      TEXT NOT NULL DEFAULT 'pending'
                      CHECK (email_status IN ('pending', 'sent', 'failed', 'skipped')),
  email_provider_id TEXT,
  email_error       TEXT,
  sent_at           TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  cost_usd          DECIMAL(18, 6)
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
  id                BIGINT PRIMARY KEY DEFAULT nextval('weekly_reviews_id_seq'),
  period_start      DATE NOT NULL,
  period_end        DATE NOT NULL UNIQUE,
  subject           TEXT NOT NULL,
  assessment        TEXT,
  body_markdown     TEXT,
  body_html         TEXT,
  metrics           JSON,
  recommendations   JSON,
  model             TEXT,
  prompt_version    TEXT,
  email_status      TEXT NOT NULL DEFAULT 'pending'
                      CHECK (email_status IN ('pending', 'sent', 'failed', 'skipped')),
  email_provider_id TEXT,
  email_error       TEXT,
  sent_at           TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  cost_usd          DECIMAL(18, 6),
  CHECK (period_start <= period_end)
);

INSERT INTO schema_migrations (filename) VALUES ('001-schema.sql')
ON CONFLICT (filename) DO NOTHING;
