import { useEffect, useState } from "react";

import type { Decision, Holding, Overview, Performance, Run, Trade } from "./api";
import { gbp, get, pct } from "./api";
import { PerformanceChart } from "./components/PerformanceChart";
import {
  DecisionsTable,
  HoldingsTable,
  RunsTable,
  StatTile,
  TradesTable,
} from "./components/Tables";

type Data = {
  overview: Overview;
  performance: Performance;
  holdings: Holding[];
  decisions: Decision[];
  trades: Trade[];
  runs: Run[];
};

export default function App() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    // One round of requests in parallel. The API is read-only and every
    // endpoint is bounded, so there is nothing to paginate and nothing to
    // poll for — the data changes twice a day, on a schedule.
    Promise.all([
      get<Overview>("/api/overview"),
      get<Performance>("/api/performance"),
      get<Holding[]>("/api/holdings"),
      get<Decision[]>("/api/decisions?limit=50"),
      get<Trade[]>("/api/trades?limit=50"),
      get<Run[]>("/api/runs?limit=20"),
    ])
      .then(([overview, performance, holdings, decisions, trades, runs]) => {
        if (!cancelled) setData({ overview, performance, holdings, decisions, trades, runs });
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="app">
        <div className="state error">Could not reach the API: {error}</div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="app">
        <div className="state">Loading…</div>
      </div>
    );
  }

  const { overview } = data;
  const up = overview.pnl_gbp >= 0;

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>InvestAgent</h1>
          <div className="subtitle">
            An AI paper-trading experiment. No real money is ever connected.
          </div>
        </div>
        {overview.last_run && (
          <div className="subtitle">
            Last run {overview.last_run.started_at.slice(0, 16).replace("T", " ")} ·{" "}
            {overview.last_run.status}
            {overview.last_run.dry_run ? " · dry run" : ""}
          </div>
        )}
      </header>

      <div className="tiles">
        <StatTile label="Total value" value={gbp(overview.total_value_gbp)} />
        <StatTile
          label="Profit and loss"
          value={`${up ? "+" : ""}${gbp(overview.pnl_gbp)}`}
          note={pct(overview.pnl_pct)}
          tone={up ? "up" : "down"}
        />
        <StatTile label="Cash" value={gbp(overview.cash_gbp)} />
        <StatTile
          label="Positions"
          value={gbp(overview.positions_value_gbp)}
          note={`${overview.position_count} holding${overview.position_count === 1 ? "" : "s"}`}
        />
      </div>

      <section className="card">
        <PerformanceChart data={data.performance} />
      </section>

      <section className="card">
        <h2>Holdings</h2>
        <p className="hint">Valued at the most recent close we hold.</p>
        <HoldingsTable rows={data.holdings} />
      </section>

      <section className="card">
        <h2>Decisions</h2>
        <p className="hint">
          What the model recommended, and what the risk engine allowed. “Bound by” names
          the rule that decided the outcome.
        </p>
        <DecisionsTable rows={data.decisions} />
      </section>

      <section className="card">
        <h2>Trades</h2>
        <p className="hint">
          A scheduled run submits before the market opens, so an order can sit unfilled
          for hours.
        </p>
        <TradesTable rows={data.trades} />
      </section>

      <section className="card">
        <h2>Agent runs</h2>
        <p className="hint">One row per execution, opened before any work so a crash
          leaves evidence.</p>
        <RunsTable rows={data.runs} />
      </section>
    </div>
  );
}
