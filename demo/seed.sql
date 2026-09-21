-- Fake USD paper-account data for the local dashboard. Destructive by design,
-- and guarded so it can never run against the Azure database.
DO $$ BEGIN
  IF current_database() <> 'marketagent' THEN
    RAISE EXCEPTION 'demo seed only runs against the local marketagent database';
  END IF;
END $$;

BEGIN;
TRUNCATE weekly_reviews, daily_summaries, benchmarks, daily_performance, trades,
         ai_decisions, agent_runs, positions, news_analysis, news, prices RESTART IDENTITY;

UPDATE portfolio
   SET base_currency = 'USD', initial_cash_usd = 100000, cash_usd = 97350,
       equity_usd = 102480, buying_power_usd = 389400,
       broker_account_id = 'demo-paper-account', broker_synced_at = now(),
       display_gbp_usd = 1.340000, display_fx_as_of = current_date,
       created_at = now() - interval '90 days', updated_at = now()
 WHERE name = 'default';

INSERT INTO prices (ticker, bar_date, open_usd, high_usd, low_usd, close_usd, volume)
SELECT c.ticker, d::date, round((base+n*slope)::numeric,4),
       round((base+n*slope+2)::numeric,4), round((base+n*slope-2)::numeric,4),
       round((base+n*slope+((n%5)-2)*0.55)::numeric,4), 1000000+n*12345
  FROM (VALUES ('AAPL',225.0,0.18),('AMD',160.0,0.12),('AVGO',320.0,0.28),
        ('GOOGL',190.0,0.15),('META',560.0,0.35),('NVDA',175.0,0.20),
        ('SPY',640.0,0.22),('VT',128.0,0.05),('EWU',40.0,0.01)) c(ticker,base,slope)
 CROSS JOIN LATERAL generate_series(current_date-89,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-89))::int n) x;

INSERT INTO positions (portfolio_id,ticker,quantity,avg_cost_usd,market_value_usd,
                       opened_at,updated_at)
SELECT p.id,v.ticker,v.quantity,v.average,v.market_value,now()-interval '35 days',now()
  FROM portfolio p CROSS JOIN (VALUES
   ('AAPL',4.250000000,220.00,980.00),('AMD',5.500000000,155.00,925.00),
   ('AVGO',3.100000000,315.00,1085.00),('GOOGL',4.800000000,185.00,990.00),
   ('META',1.400000000,545.00,850.00),('NVDA',1.650000000,170.00,300.00)
  ) v(ticker,quantity,average,market_value) WHERE p.name='default';

INSERT INTO agent_runs (started_at,finished_at,status,trigger,dry_run,image_tag,
                        tickers_considered,news_fetched,news_relevant,decisions_made,
                        trades_executed,input_tokens,output_tokens,cost_usd)
SELECT d+time '06:00',d+time '06:04','succeeded','schedule',false,'demo',
       10,72+n%18,20+n%12,4+n%5,n%3,28000+n*43,3800+n*11,0.14+n*0.0007
  FROM generate_series(current_date-29,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-29))::int n) x;

INSERT INTO ai_decisions (run_id,decided_at,ticker,action,confidence,reasoning,risks,
                          model,prompt_version,recommended_amount_usd,
                          approved_amount_usd,portfolio_state,risk_verdict,
                          input_tokens,output_tokens)
SELECT r.id,r.started_at+interval '2 minutes',
       CASE r.id%5 WHEN 0 THEN 'AAPL' WHEN 1 THEN 'AMD' WHEN 2 THEN 'AVGO'
                   WHEN 3 THEN 'GOOGL' ELSE 'META' END,
       CASE WHEN r.id%4=0 THEN 'HOLD' ELSE 'BUY' END,0.61+(r.id%8)*0.04,
       'Demo decision based on improving earnings coverage and recent price momentum.',
       'The position could reverse if guidance weakens or the broad market sells off.',
       'claude-sonnet-5','v4',
       CASE WHEN r.id%4=0 THEN NULL ELSE 3500+(r.id%4)*500 END,
       CASE WHEN r.id%4=0 THEN NULL ELSE 3000+(r.id%4)*400 END,
       jsonb_build_object('cash_usd','97350.0000','invested_usd','5130.0000'),
       jsonb_build_object('approved',r.id%4<>0,'binding_constraint',
         CASE WHEN r.id%4=0 THEN 'action_is_hold' ELSE 'recommended_amount' END,
         'reasons','[]'::jsonb),4200,520 FROM agent_runs r;

INSERT INTO trades (portfolio_id,decision_id,ticker,side,quantity,price_usd,
                    notional_usd,broker,broker_order_id,client_order_id,status,
                    dry_run,submitted_at,filled_at,created_at)
SELECT p.id,d.id,d.ticker,'BUY',round((d.approved_amount_usd/200)::numeric,9),
       200,d.approved_amount_usd,'alpaca','demo-'||d.id,'ma-'||d.id,'filled',false,
       d.decided_at,d.decided_at+interval '8 hours',d.decided_at
  FROM ai_decisions d CROSS JOIN portfolio p
 WHERE d.approved_amount_usd IS NOT NULL AND d.id%3=0 AND p.name='default';

INSERT INTO daily_performance (portfolio_id,as_of,cash_usd,positions_value_usd,
                               total_value_usd,pnl_usd,pnl_pct)
SELECT p.id,d::date,round((100000-n*30)::numeric,4),
       round((n*30+n*27.5)::numeric,4),round((100000+n*27.5)::numeric,4),
       round((n*27.5)::numeric,4),round((n*0.0275)::numeric,4)
  FROM portfolio p
 CROSS JOIN LATERAL generate_series(current_date-89,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-89))::int n) x
 WHERE p.name='default';

INSERT INTO benchmarks (symbol,as_of,close_usd,value_usd,source)
SELECT symbol,d::date,NULL,round((100000+n*daily_gain)::numeric,4),'demo'
  FROM (VALUES ('SPY',20.0),('VT',15.0),('EWU',8.0),('CASH5',13.7)) b(symbol,daily_gain)
 CROSS JOIN LATERAL generate_series(current_date-89,current_date-1,'1 day') d
 CROSS JOIN LATERAL (SELECT (d::date-(current_date-89))::int n) x;

INSERT INTO daily_summaries (as_of,subject,body_markdown,body_html,model,
                             prompt_version,email_status,sent_at,cost_usd)
SELECT as_of,'MarketAgent '||as_of||': $'||total_value_usd,
       'Demo daily summary for the local dashboard.','<p>Demo daily summary.</p>',
       'claude-sonnet-5','v4','sent',as_of+time '21:00',0.014500
  FROM daily_performance WHERE as_of>current_date-15;

INSERT INTO weekly_reviews (period_start,period_end,subject,assessment,body_markdown,
                            body_html,metrics,recommendations,model,prompt_version,
                            email_status,sent_at,cost_usd)
VALUES (current_date-8,current_date-2,'MarketAgent demo weekly review',
        'The account gained steadily in this demo window. Position limits constrained the '
        'largest recommendations while cash remained well above the reserve.',
        '## Demo week','<h2>Demo week</h2>','{"runs":7,"trades":3}',
        '[{"area":"risk_limits","change":"Keep the current exposure ceiling",'
        '"rationale":"It constrained concentration without preventing participation",'
        '"expected_effect":"Maintain cash reserves","confidence":0.72}]',
        'claude-sonnet-5','v4','sent',current_date-2+time '22:00',0.021000);

COMMIT;

SELECT (SELECT count(*) FROM prices) prices,(SELECT count(*) FROM positions) positions,
       (SELECT count(*) FROM daily_performance) performance_rows,
       (SELECT count(*) FROM ai_decisions) decisions,(SELECT count(*) FROM trades) trades;
