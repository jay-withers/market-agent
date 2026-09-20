import { useEffect, useState } from "react";

import type { Decision, Holding, Overview, Performance, PricePoint, Review, Run, Trade } from "./api";
import { gbp, get, getOptional, pct, when } from "./api";
import { Nav, useRoute } from "./components/Nav";
import { PerformanceChart } from "./components/PerformanceChart";
import { PriceTrends } from "./components/PriceTrends";
import { WeeklyReview } from "./components/Review";
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
  prices: PricePoint[];
  decisions: Decision[];
  trades: Trade[];
  runs: Run[];
  // Null until the first Sunday run, which is a state and not a failure.
  review: Review | null;
};

export default function App() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, navigate] = useRoute();

  useEffect(() => {
    let cancelled = false;

    // Still one round of requests on boot rather than one per tab: the whole
    // dataset is small, it changes twice a day, and this app sits behind
    // min_replicas = 0 — paying the cold start once beats paying a spinner on
    // every tab switch.
    Promise.all([
      get<Overview>("/api/overview"),
      get<Performance>("/api/performance"),
      get<Holding[]>("/api/holdings"),
      // A year, filtered to the chosen window on the client: a few hundred rows
      // either way, and switching range then costs nothing.
      get<PricePoint[]>("/api/prices?days=365"),
      get<Decision[]>("/api/decisions?limit=50"),
      get<Trade[]>("/api/trades?limit=50"),
      get<Run[]>("/api/runs?limit=20"),
      // getOptional, because /api/reviews/latest is a 404 until the first
      // weekly run and one missing section must not blank the page.
      getOptional<Review>("/api/reviews/latest"),
    ])
      .then(([overview, performance, holdings, prices, decisions, trades, runs, review]) => {
        if (!cancelled)
          setData({ overview, performance, holdings, prices, decisions, trades, runs, review });
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
          <h1>MarketAgent</h1>
          <div className="subtitle">
            An AI paper-trading experiment. No real money is ever connected.
          </div>
        </div>
        {overview.last_run && (
          <div className="subtitle">
            Last run {when(overview.last_run.started_at)} ·{" "}
            {overview.last_run.status}
            {/* Guarded because the API and the dashboard are separate container
                apps whose revisions roll independently: a dashboard live a
                moment before the API that supplies `trigger` would otherwise
                render a dangling separator. */}
            {overview.last_run.trigger ? ` · ${overview.last_run.trigger}` : ""}
            {overview.last_run.dry_run ? " · dry run" : ""}
          </div>
        )}
      </header>

      {/* Above the tabs, not inside one: these four are the answer to "how is it
          doing", and they should not depend on which area happens to be open. */}
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

      <Nav current={tab} onNavigate={navigate} />

      {tab === "/" && (
        <section className="card">
          <PerformanceChart data={data.performance} />
        </section>
      )}

      {tab === "/holdings" && (
        <>
          <section className="card">
            <PriceTrends holdings={data.holdings} prices={data.prices} />
          </section>

          <section className="card">
            <h2>Holdings</h2>
            <p className="hint">Valued at the most recent close we hold.</p>
            <HoldingsTable rows={data.holdings} />
          </section>
        </>
      )}

      {tab === "/activity" && (
        <>
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
        </>
      )}

      {tab === "/review" && (
        <section className="card">
          <h2>Weekly review</h2>
          <p className="hint">
            Written on Sunday from the week&rsquo;s stored figures, with the changes it
            proposes to the experiment.
          </p>
          <WeeklyReview review={data.review} />
        </section>
      )}
    </div>
  );
}
