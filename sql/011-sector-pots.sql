-- Three fixed sector pots replace static-100 and dynamic-500.
--
-- Each pot trades about 50 tickers from one S&P 500 sector, with its own
-- Alpaca paper account and its own DeepSeek API key, so cost and results can
-- be compared per pot. The lists are a point-in-time snapshot of the largest
-- names in each sector, committed as literal data for the same reason 010
-- was: the list lives in a file, not behind a job that could change it. The
-- energy pot is Energy plus Utilities because Energy alone has only about 22
-- S&P 500 names.
--
-- Nothing is deleted. The two old accounts are marked inactive, which stops
-- every job and the dashboard iterating them, and their history stays in the
-- tables for anyone who wants to read it.
--
-- Idempotent like every file here. The one statement that would undo a
-- manual change on replay, the deactivation, only runs the first time.

ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS description text;
-- The effective model and risk settings a run decided under. Pots can override
-- them per job, and ai_decisions records the model but not the effort level.
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS config jsonb;

-- The pots' lists are their own kind of source, so the dashboard can say what
-- a ticker is there for. Dropped and re-added so a replay converges.
ALTER TABLE portfolio_watchlist DROP CONSTRAINT IF EXISTS portfolio_watchlist_source_check;
ALTER TABLE portfolio_watchlist ADD CONSTRAINT portfolio_watchlist_source_check
  CHECK (source IN ('sp100_snapshot', 'sp500_index', 'manual', 'sector_snapshot'));

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = '011-sector-pots.sql') THEN
  UPDATE portfolio SET is_active = false WHERE name IN ('static-100', 'dynamic-500');
 END IF;
END $$;

-- $500 each, matching the new paper accounts; save_broker_snapshot() replaces
-- these with the real balances on each pot's first sync.
INSERT INTO portfolio (name, base_currency, initial_cash_usd, cash_usd, description) VALUES
  ('tech', 'USD', 500, 500, 'S&P 500 Information Technology'),
  ('health', 'USD', 500, 500, 'S&P 500 Health Care'),
  ('energy', 'USD', 500, 500, 'S&P 500 Energy and Utilities')
ON CONFLICT (name) DO NOTHING;

INSERT INTO companies (ticker, name, sector) VALUES
  ('NVDA', 'NVIDIA', 'Information Technology'),
  ('MSFT', 'Microsoft', 'Information Technology'),
  ('AAPL', 'Apple Inc.', 'Information Technology'),
  ('AVGO', 'Broadcom', 'Information Technology'),
  ('ORCL', 'Oracle', 'Information Technology'),
  ('PLTR', 'Palantir Technologies', 'Information Technology'),
  ('AMD', 'Advanced Micro Devices', 'Information Technology'),
  ('CSCO', 'Cisco', 'Information Technology'),
  ('IBM', 'IBM', 'Information Technology'),
  ('CRM', 'Salesforce', 'Information Technology'),
  ('MU', 'Micron Technology', 'Information Technology'),
  ('INTU', 'Intuit', 'Information Technology'),
  ('NOW', 'ServiceNow', 'Information Technology'),
  ('TXN', 'Texas Instruments', 'Information Technology'),
  ('QCOM', 'Qualcomm', 'Information Technology'),
  ('LRCX', 'Lam Research', 'Information Technology'),
  ('AMAT', 'Applied Materials', 'Information Technology'),
  ('ACN', 'Accenture', 'Information Technology'),
  ('ADBE', 'Adobe Inc.', 'Information Technology'),
  ('KLAC', 'KLA Corporation', 'Information Technology'),
  ('ANET', 'Arista Networks', 'Information Technology'),
  ('APH', 'Amphenol', 'Information Technology'),
  ('INTC', 'Intel', 'Information Technology'),
  ('PANW', 'Palo Alto Networks', 'Information Technology'),
  ('ADI', 'Analog Devices', 'Information Technology'),
  ('CRWD', 'CrowdStrike', 'Information Technology'),
  ('SNPS', 'Synopsys', 'Information Technology'),
  ('CDNS', 'Cadence Design Systems', 'Information Technology'),
  ('MSI', 'Motorola Solutions', 'Information Technology'),
  ('ADSK', 'Autodesk', 'Information Technology'),
  ('DELL', 'Dell Technologies', 'Information Technology'),
  ('APP', 'AppLovin', 'Information Technology'),
  ('FTNT', 'Fortinet', 'Information Technology'),
  ('WDAY', 'Workday', 'Information Technology'),
  ('ROP', 'Roper Technologies', 'Information Technology'),
  ('NXPI', 'NXP Semiconductors', 'Information Technology'),
  ('TEL', 'TE Connectivity', 'Information Technology'),
  ('GLW', 'Corning Inc.', 'Information Technology'),
  ('MCHP', 'Microchip Technology', 'Information Technology'),
  ('MPWR', 'Monolithic Power Systems', 'Information Technology'),
  ('SNDK', 'Sandisk', 'Information Technology'),
  ('WDC', 'Western Digital', 'Information Technology'),
  ('STX', 'Seagate Technology', 'Information Technology'),
  ('DDOG', 'Datadog', 'Information Technology'),
  ('HPE', 'Hewlett Packard Enterprise', 'Information Technology'),
  ('CTSH', 'Cognizant', 'Information Technology'),
  ('IT', 'Gartner', 'Information Technology'),
  ('FICO', 'Fair Isaac', 'Information Technology'),
  ('KEYS', 'Keysight Technologies', 'Information Technology'),
  ('HPQ', 'HP Inc.', 'Information Technology'),
  ('LLY', 'Eli Lilly', 'Health Care'),
  ('JNJ', 'Johnson & Johnson', 'Health Care'),
  ('ABBV', 'AbbVie', 'Health Care'),
  ('UNH', 'UnitedHealth Group', 'Health Care'),
  ('MRK', 'Merck & Co.', 'Health Care'),
  ('ABT', 'Abbott Laboratories', 'Health Care'),
  ('TMO', 'Thermo Fisher Scientific', 'Health Care'),
  ('ISRG', 'Intuitive Surgical', 'Health Care'),
  ('AMGN', 'Amgen', 'Health Care'),
  ('BSX', 'Boston Scientific', 'Health Care'),
  ('DHR', 'Danaher', 'Health Care'),
  ('PFE', 'Pfizer', 'Health Care'),
  ('GILD', 'Gilead Sciences', 'Health Care'),
  ('SYK', 'Stryker', 'Health Care'),
  ('VRTX', 'Vertex Pharmaceuticals', 'Health Care'),
  ('MDT', 'Medtronic', 'Health Care'),
  ('BMY', 'Bristol Myers Squibb', 'Health Care'),
  ('CVS', 'CVS Health', 'Health Care'),
  ('MCK', 'McKesson', 'Health Care'),
  ('CI', 'Cigna', 'Health Care'),
  ('ELV', 'Elevance Health', 'Health Care'),
  ('HCA', 'HCA Healthcare', 'Health Care'),
  ('ZTS', 'Zoetis', 'Health Care'),
  ('REGN', 'Regeneron Pharmaceuticals', 'Health Care'),
  ('BDX', 'Becton Dickinson', 'Health Care'),
  ('COR', 'Cencora', 'Health Care'),
  ('EW', 'Edwards Lifesciences', 'Health Care'),
  ('IDXX', 'Idexx Laboratories', 'Health Care'),
  ('A', 'Agilent Technologies', 'Health Care'),
  ('GEHC', 'GE HealthCare', 'Health Care'),
  ('RMD', 'ResMed', 'Health Care'),
  ('IQV', 'IQVIA', 'Health Care'),
  ('DXCM', 'Dexcom', 'Health Care'),
  ('CAH', 'Cardinal Health', 'Health Care'),
  ('HUM', 'Humana', 'Health Care'),
  ('CNC', 'Centene', 'Health Care'),
  ('MTD', 'Mettler Toledo', 'Health Care'),
  ('STE', 'Steris', 'Health Care'),
  ('WAT', 'Waters Corporation', 'Health Care'),
  ('ZBH', 'Zimmer Biomet', 'Health Care'),
  ('LH', 'Labcorp', 'Health Care'),
  ('DGX', 'Quest Diagnostics', 'Health Care'),
  ('PODD', 'Insulet', 'Health Care'),
  ('WST', 'West Pharmaceutical Services', 'Health Care'),
  ('BIIB', 'Biogen', 'Health Care'),
  ('INCY', 'Incyte', 'Health Care'),
  ('HOLX', 'Hologic', 'Health Care'),
  ('COO', 'Cooper Companies', 'Health Care'),
  ('BAX', 'Baxter International', 'Health Care'),
  ('ALGN', 'Align Technology', 'Health Care'),
  ('XOM', 'ExxonMobil', 'Energy'),
  ('CVX', 'Chevron', 'Energy'),
  ('COP', 'ConocoPhillips', 'Energy'),
  ('WMB', 'Williams Companies', 'Energy'),
  ('EOG', 'EOG Resources', 'Energy'),
  ('KMI', 'Kinder Morgan', 'Energy'),
  ('OKE', 'Oneok', 'Energy'),
  ('MPC', 'Marathon Petroleum', 'Energy'),
  ('PSX', 'Phillips 66', 'Energy'),
  ('SLB', 'Schlumberger', 'Energy'),
  ('VLO', 'Valero Energy', 'Energy'),
  ('OXY', 'Occidental Petroleum', 'Energy'),
  ('BKR', 'Baker Hughes', 'Energy'),
  ('FANG', 'Diamondback Energy', 'Energy'),
  ('TRGP', 'Targa Resources', 'Energy'),
  ('EQT', 'EQT Corporation', 'Energy'),
  ('HAL', 'Halliburton', 'Energy'),
  ('DVN', 'Devon Energy', 'Energy'),
  ('CTRA', 'Coterra', 'Energy'),
  ('TPL', 'Texas Pacific Land', 'Energy'),
  ('EXE', 'Expand Energy', 'Energy'),
  ('APA', 'APA Corporation', 'Energy'),
  ('NEE', 'NextEra Energy', 'Utilities'),
  ('SO', 'Southern Company', 'Utilities'),
  ('DUK', 'Duke Energy', 'Utilities'),
  ('CEG', 'Constellation Energy', 'Utilities'),
  ('VST', 'Vistra', 'Utilities'),
  ('AEP', 'American Electric Power', 'Utilities'),
  ('SRE', 'Sempra', 'Utilities'),
  ('D', 'Dominion Energy', 'Utilities'),
  ('EXC', 'Exelon', 'Utilities'),
  ('XEL', 'Xcel Energy', 'Utilities'),
  ('PCG', 'PG&E', 'Utilities'),
  ('ETR', 'Entergy', 'Utilities'),
  ('PEG', 'Public Service Enterprise Group', 'Utilities'),
  ('ED', 'Consolidated Edison', 'Utilities'),
  ('WEC', 'WEC Energy Group', 'Utilities'),
  ('NRG', 'NRG Energy', 'Utilities'),
  ('EIX', 'Edison International', 'Utilities'),
  ('DTE', 'DTE Energy', 'Utilities'),
  ('AEE', 'Ameren', 'Utilities'),
  ('PPL', 'PPL Corporation', 'Utilities'),
  ('ATO', 'Atmos Energy', 'Utilities'),
  ('CNP', 'CenterPoint Energy', 'Utilities'),
  ('ES', 'Eversource Energy', 'Utilities'),
  ('FE', 'FirstEnergy', 'Utilities'),
  ('CMS', 'CMS Energy', 'Utilities'),
  ('NI', 'NiSource', 'Utilities'),
  ('AWK', 'American Water Works', 'Utilities'),
  ('EVRG', 'Evergy', 'Utilities')
ON CONFLICT (ticker) DO NOTHING;

INSERT INTO portfolio_watchlist (portfolio_id, ticker, source)
SELECT p.id, t.ticker, 'sector_snapshot' FROM portfolio p, (VALUES
  ('NVDA'),
  ('MSFT'),
  ('AAPL'),
  ('AVGO'),
  ('ORCL'),
  ('PLTR'),
  ('AMD'),
  ('CSCO'),
  ('IBM'),
  ('CRM'),
  ('MU'),
  ('INTU'),
  ('NOW'),
  ('TXN'),
  ('QCOM'),
  ('LRCX'),
  ('AMAT'),
  ('ACN'),
  ('ADBE'),
  ('KLAC'),
  ('ANET'),
  ('APH'),
  ('INTC'),
  ('PANW'),
  ('ADI'),
  ('CRWD'),
  ('SNPS'),
  ('CDNS'),
  ('MSI'),
  ('ADSK'),
  ('DELL'),
  ('APP'),
  ('FTNT'),
  ('WDAY'),
  ('ROP'),
  ('NXPI'),
  ('TEL'),
  ('GLW'),
  ('MCHP'),
  ('MPWR'),
  ('SNDK'),
  ('WDC'),
  ('STX'),
  ('DDOG'),
  ('HPE'),
  ('CTSH'),
  ('IT'),
  ('FICO'),
  ('KEYS'),
  ('HPQ')
) AS t(ticker)
WHERE p.name = 'tech'
ON CONFLICT (portfolio_id, ticker) DO NOTHING;

INSERT INTO portfolio_watchlist (portfolio_id, ticker, source)
SELECT p.id, t.ticker, 'sector_snapshot' FROM portfolio p, (VALUES
  ('LLY'),
  ('JNJ'),
  ('ABBV'),
  ('UNH'),
  ('MRK'),
  ('ABT'),
  ('TMO'),
  ('ISRG'),
  ('AMGN'),
  ('BSX'),
  ('DHR'),
  ('PFE'),
  ('GILD'),
  ('SYK'),
  ('VRTX'),
  ('MDT'),
  ('BMY'),
  ('CVS'),
  ('MCK'),
  ('CI'),
  ('ELV'),
  ('HCA'),
  ('ZTS'),
  ('REGN'),
  ('BDX'),
  ('COR'),
  ('EW'),
  ('IDXX'),
  ('A'),
  ('GEHC'),
  ('RMD'),
  ('IQV'),
  ('DXCM'),
  ('CAH'),
  ('HUM'),
  ('CNC'),
  ('MTD'),
  ('STE'),
  ('WAT'),
  ('ZBH'),
  ('LH'),
  ('DGX'),
  ('PODD'),
  ('WST'),
  ('BIIB'),
  ('INCY'),
  ('HOLX'),
  ('COO'),
  ('BAX'),
  ('ALGN')
) AS t(ticker)
WHERE p.name = 'health'
ON CONFLICT (portfolio_id, ticker) DO NOTHING;

INSERT INTO portfolio_watchlist (portfolio_id, ticker, source)
SELECT p.id, t.ticker, 'sector_snapshot' FROM portfolio p, (VALUES
  ('XOM'),
  ('CVX'),
  ('COP'),
  ('WMB'),
  ('EOG'),
  ('KMI'),
  ('OKE'),
  ('MPC'),
  ('PSX'),
  ('SLB'),
  ('VLO'),
  ('OXY'),
  ('BKR'),
  ('FANG'),
  ('TRGP'),
  ('EQT'),
  ('HAL'),
  ('DVN'),
  ('CTRA'),
  ('TPL'),
  ('EXE'),
  ('APA'),
  ('NEE'),
  ('SO'),
  ('DUK'),
  ('CEG'),
  ('VST'),
  ('AEP'),
  ('SRE'),
  ('D'),
  ('EXC'),
  ('XEL'),
  ('PCG'),
  ('ETR'),
  ('PEG'),
  ('ED'),
  ('WEC'),
  ('NRG'),
  ('EIX'),
  ('DTE'),
  ('AEE'),
  ('PPL'),
  ('ATO'),
  ('CNP'),
  ('ES'),
  ('FE'),
  ('CMS'),
  ('NI'),
  ('AWK'),
  ('EVRG')
) AS t(ticker)
WHERE p.name = 'energy'
ON CONFLICT (portfolio_id, ticker) DO NOTHING;

INSERT INTO schema_migrations (filename) VALUES ('011-sector-pots.sql')
ON CONFLICT (filename) DO NOTHING;

\echo '==> active pots:'
SELECT p.name, p.description, count(w.ticker) AS tickers
FROM portfolio p LEFT JOIN portfolio_watchlist w ON w.portfolio_id = p.id AND w.is_active
WHERE p.is_active
GROUP BY p.id, p.name, p.description
ORDER BY p.id;
