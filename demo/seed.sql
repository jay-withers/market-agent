-- Fake USD paper-account data for the local dashboard. Destructive by design,
-- and guarded so it can never run against the Azure database.
--
-- Seeds every pot (tech, health, energy) rather than one: the dashboard's pot
-- tabs and Compare view need real data for each to preview meaningfully, and
-- the pots are deliberately given different trajectories (see the `rate`
-- column below) so the Compare view has three distinct lines to draw.
DO $$ BEGIN
  IF current_database() <> 'marketagent' THEN
    RAISE EXCEPTION 'demo seed only runs against the local marketagent database';
  END IF;
END $$;

BEGIN;
TRUNCATE weekly_reviews, daily_summaries, benchmarks, daily_performance, trades,
         ai_decisions, agent_runs, positions, news_analysis, news, prices RESTART IDENTITY;

UPDATE portfolio
   SET base_currency = 'USD', initial_cash_usd = 100000,
       cash_usd = CASE name WHEN 'tech' THEN 97350 WHEN 'health' THEN 98100 ELSE 98800 END,
       equity_usd = CASE name WHEN 'tech' THEN 102480 WHEN 'health' THEN 101690 ELSE 101150 END,
       buying_power_usd = 389400,
       broker_account_id = 'demo-paper-account-' || name,
       broker_synced_at = now(),
       display_gbp_usd = 1.340000, display_fx_as_of = current_date,
       created_at = now() - interval '90 days', updated_at = now()
 WHERE is_active;

-- Shared reference data, not portfolio-scoped: every pot prices its tickers
-- against the same market.
INSERT INTO prices (ticker, bar_date, open_usd, high_usd, low_usd, close_usd, volume)
SELECT c.ticker, d::date, round((base+n*slope)::numeric,4),
       round((base+n*slope+2)::numeric,4), round((base+n*slope-2)::numeric,4),
       round((base+n*slope+((n%5)-2)*0.55)::numeric,4), 1000000+n*12345
  FROM (VALUES ('AAPL',225.0,0.18),('AMD',160.0,0.12),('AVGO',320.0,0.28),
        ('NVDA',175.0,0.20),('LLY',780.0,0.40),('JNJ',160.0,0.04),('ABBV',190.0,0.09),
        ('UNH',330.0,-0.10),('XOM',115.0,0.03),('CVX',155.0,0.05),('NEE',75.0,0.06),
        ('COP',100.0,-0.02),('SPY',640.0,0.22),('VT',128.0,0.05),('EWU',40.0,0.01))
        c(ticker,base,slope)
 CROSS JOIN LATERAL generate_series(current_date-89,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-89))::int n) x;

INSERT INTO positions (portfolio_id,ticker,quantity,avg_cost_usd,market_value_usd,
                       opened_at,updated_at)
SELECT p.id,v.ticker,v.quantity,v.average,v.market_value,now()-interval '35 days',now()
  FROM portfolio p JOIN (VALUES
   ('tech','AAPL',4.250000000,220.00,980.00),('tech','AMD',5.500000000,155.00,925.00),
   ('tech','AVGO',3.100000000,315.00,1085.00),('tech','NVDA',1.650000000,170.00,300.00),
   ('health','LLY',1.200000000,760.00,960.00),('health','JNJ',6.000000000,158.00,975.00),
   ('health','ABBV',5.000000000,185.00,970.00),('health','UNH',2.500000000,340.00,810.00),
   ('energy','XOM',8.000000000,112.00,940.00),('energy','CVX',6.000000000,150.00,950.00),
   ('energy','NEE',12.000000000,72.00,915.00),('energy','COP',9.000000000,101.00,890.00)
  ) v(pot,ticker,quantity,average,market_value) ON v.pot = p.name;

INSERT INTO agent_runs (portfolio_id,started_at,finished_at,status,trigger,dry_run,image_tag,
                        tickers_considered,news_fetched,news_relevant,decisions_made,
                        trades_executed,input_tokens,output_tokens,cost_usd)
SELECT p.id,d+time '06:00',d+time '06:04','succeeded','schedule',false,'demo',
       50,72+n%18,20+n%12,4+n%5,n%3,28000+n*43,3800+n*11,0.14+n*0.0007
  FROM portfolio p
 CROSS JOIN LATERAL generate_series(current_date-29,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-29))::int n) x
 WHERE p.is_active;

INSERT INTO ai_decisions (portfolio_id,run_id,decided_at,ticker,action,confidence,reasoning,risks,
                          model,prompt_version,recommended_amount_usd,
                          approved_amount_usd,portfolio_state,risk_verdict,
                          input_tokens,output_tokens)
SELECT r.portfolio_id,r.id,r.started_at+interval '2 minutes',
       (SELECT ticker FROM positions WHERE portfolio_id = r.portfolio_id
         ORDER BY ticker OFFSET r.id%4 LIMIT 1),
       CASE WHEN r.id%4=0 THEN 'HOLD' ELSE 'BUY' END,0.61+(r.id%8)*0.04,
       'Demo decision based on improving earnings coverage and recent price momentum.',
       'The position could reverse if guidance weakens or the broad market sells off.',
       'deepseek-v4-pro','v5',
       CASE WHEN r.id%4=0 THEN NULL ELSE 3500+(r.id%4)*500 END,
       CASE WHEN r.id%4=0 THEN NULL ELSE 3000+(r.id%4)*400 END,
       jsonb_build_object('cash_usd','97350.0000','invested_usd','5130.0000'),
       jsonb_build_object('approved',r.id%4<>0,'binding_constraint',
         CASE WHEN r.id%4=0 THEN 'action_is_hold' ELSE 'recommended_amount' END,
         'reasons','[]'::jsonb),4200,520 FROM agent_runs r;

INSERT INTO trades (portfolio_id,decision_id,ticker,side,quantity,price_usd,
                    notional_usd,broker,broker_order_id,client_order_id,status,
                    dry_run,submitted_at,filled_at,created_at)
SELECT d.portfolio_id,d.id,d.ticker,'BUY',round((d.approved_amount_usd/200)::numeric,9),
       200,d.approved_amount_usd,'alpaca','demo-'||d.id,'ma-'||d.id,'filled',false,
       d.decided_at,d.decided_at+interval '8 hours',d.decided_at
  FROM ai_decisions d
 WHERE d.approved_amount_usd IS NOT NULL AND d.id%3=0;

-- Each pot grows at its own fixed rate — an arbitrary divergence, purely so
-- the Compare view has visibly different lines rather than identical ones
-- drawn on top of each other.
INSERT INTO daily_performance (portfolio_id,as_of,cash_usd,positions_value_usd,
                               total_value_usd,pnl_usd,pnl_pct)
SELECT p.id,d::date,round((100000-n*30*g.rate)::numeric,4),
       round((n*30*g.rate+n*27.5*g.rate)::numeric,4),
       round((100000+n*27.5*g.rate)::numeric,4),
       round((n*27.5*g.rate)::numeric,4),round((n*0.0275*g.rate)::numeric,4)
  FROM portfolio p
  JOIN (VALUES ('tech',1.0),('health',0.7),('energy',0.45)) g(name,rate) ON g.name = p.name
 CROSS JOIN LATERAL generate_series(current_date-89,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-89))::int n) x;

INSERT INTO benchmarks (portfolio_id,symbol,as_of,close_usd,value_usd,source)
SELECT p.id,b.symbol,d::date,NULL,round((100000+n*daily_gain)::numeric,4),'demo'
  FROM portfolio p
 CROSS JOIN (VALUES ('SPY',20.0),('VT',15.0),('EWU',8.0),('CASH5',13.7)) b(symbol,daily_gain)
 CROSS JOIN LATERAL generate_series(current_date-89,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-89))::int n) x
 WHERE p.is_active;

-- One combined row per day, covering every pot — matching the real
-- jobs/summary.py, which sends a single comparative email rather than one
-- per pot.
INSERT INTO daily_summaries (as_of,subject,body_markdown,body_html,model,
                             prompt_version,email_status,sent_at,cost_usd)
SELECT as_of,
       'MarketAgent '||as_of||': '||
         string_agg(name||' $'||total_value_usd, ', ' ORDER BY p.id),
       'Demo daily summary for the local dashboard, covering every pot.',
       '<p>Demo daily summary, covering every pot.</p>',
       'deepseek-v4-pro','v5','sent',as_of+time '21:00',0.014500
  FROM daily_performance dp JOIN portfolio p ON p.id = dp.portfolio_id
 WHERE as_of>current_date-15
 GROUP BY as_of;

-- Also one combined row — matching jobs/weekly.py's single comparative
-- review across every pot.
INSERT INTO weekly_reviews (period_start,period_end,subject,assessment,body_markdown,
                            body_html,metrics,recommendations,model,prompt_version,
                            email_status,sent_at,cost_usd)
VALUES (current_date-8,current_date-2,'MarketAgent demo weekly review',
        'All three pots gained in this demo window, with tech leading and energy '
        'trailing. Position limits constrained the largest recommendations in each '
        'while cash remained well above the reserve.',
        '## Demo week','<h2>Demo week</h2>','{"runs":14,"trades":6}',
        '[{"area":"risk_limits","change":"Keep the current exposure ceiling",'
        '"rationale":"It constrained concentration without preventing participation",'
        '"expected_effect":"Maintain cash reserves","confidence":0.72}]',
        'deepseek-v4-pro','v5','sent',current_date-2+time '22:00',0.021000);

COMMIT;

SELECT (SELECT count(*) FROM prices) prices,(SELECT count(*) FROM positions) positions,
       (SELECT count(*) FROM daily_performance) performance_rows,
       (SELECT count(*) FROM ai_decisions) decisions,(SELECT count(*) FROM trades) trades;
