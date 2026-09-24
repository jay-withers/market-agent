import { useEffect, useState } from "react";

import type {
  Account,
  AccountInfo,
  Comparison,
  Decision,
  DisplayCurrency,
  Holding,
  Overview,
  Performance,
  PricePoint,
  Review,
  Run,
  Trade,
  WatchlistTicker,
} from "./api";
import {
  configureDisplayCurrency,
  displayMoney,
  get,
  getOptional,
  getVersion,
  pct,
  when,
  withAccount,
} from "./api";
import { Compare } from "./components/Compare";
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
  WatchlistTable,
} from "./components/Tables";

type AccountData = {
  overview: Overview;
  performance: Performance;
  holdings: Holding[];
  watchlist: WatchlistTicker[];
  prices: PricePoint[];
  decisions: Decision[];
  trades: Trade[];
  runs: Run[];
};

// Neither account-scoped nor tab-scoped: the daily summary and weekly review
// already cover both accounts in one combined report, and the comparison is
// the whole reason the two exist side by side — none of this changes when
// the account toggle moves.
type SharedData = {
  // Null until the first Sunday run, which is a state and not a failure.
  review: Review | null;
  comparison: Comparison;
  // The dashboard's own image tag — empty locally (docker-entrypoint.sh's
  // IMAGE_TAG default there is "dev") or if config.json could not be read.
  version: string;
};

export default function App() {
  // Null until /api/accounts answers: which pots exist is the API's to say.
  const [accounts, setAccounts] = useState<AccountInfo[] | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [data, setData] = useState<AccountData | null>(null);
  const [shared, setShared] = useState<SharedData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, navigate] = useRoute();
  const [currency, setCurrency] = useState<DisplayCurrency>(() =>
    localStorage.getItem("marketagent-currency") === "GBP" ? "GBP" : "USD",
  );

  useEffect(() => {
    let cancelled = false;

    get<AccountInfo[]>("/api/accounts")
      .then((list) => {
        if (cancelled) return;
        if (list.length === 0) {
          setError("the API reports no active pots");
          return;
        }
        // A remembered pot that has since been retired falls back to the first.
        const stored = localStorage.getItem("marketagent-account");
        setAccounts(list);
        setAccount(list.find((a) => a.name === stored)?.name ?? list[0].name);
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (account === null) return;
    let cancelled = false;
    setData(null);

    // One round of requests per account switch rather than one per tab: the
    // whole per-account dataset is small, it changes twice a day, and this
    // app sits behind min_replicas = 0 — paying a cold start once beats
    // paying a spinner on every tab switch.
    Promise.all([
      get<Overview>(withAccount("/api/overview", account)),
      get<Performance>(withAccount("/api/performance", account)),
      get<Holding[]>(withAccount("/api/holdings", account)),
      get<WatchlistTicker[]>(withAccount("/api/watchlist", account)),
      // A year, filtered to the chosen window on the client: a few hundred rows
      // either way, and switching range then costs nothing.
      get<PricePoint[]>(withAccount("/api/prices?days=365", account)),
      get<Decision[]>(withAccount("/api/decisions?limit=50", account)),
      get<Trade[]>(withAccount("/api/trades?limit=50", account)),
      get<Run[]>(withAccount("/api/runs?limit=20", account)),
    ])
      .then(([overview, performance, holdings, watchlist, prices, decisions, trades, runs]) => {
        if (!cancelled)
          setData({ overview, performance, holdings, watchlist, prices, decisions, trades, runs });
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });

    return () => {
      cancelled = true;
    };
  }, [account]);

  useEffect(() => {
    let cancelled = false;

    Promise.all([
      // getOptional, because /api/reviews/latest is a 404 until the first
      // weekly run and one missing section must not blank the page.
      getOptional<Review>("/api/reviews/latest"),
      get<Comparison>("/api/comparison"),
      getVersion(),
    ])
      .then(([review, comparison, version]) => {
        if (!cancelled) setShared({ review, comparison, version });
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  function chooseAccount(next: Account) {
    setAccount(next);
    localStorage.setItem("marketagent-account", next);
  }

  if (error) {
    return (
      <div className="app">
        <div className="state error">Could not reach the API: {error}</div>
      </div>
    );
  }

  if (!data || !shared || !accounts) {
    return (
      <div className="app">
        <div className="state">Loading…</div>
      </div>
    );
  }

  const { overview } = data;
  const gbpUsd = overview.display_gbp_usd ?? null;
  const effectiveCurrency = currency === "GBP" && gbpUsd ? "GBP" : "USD";
  configureDisplayCurrency(effectiveCurrency, gbpUsd);
  const up = overview.pnl_usd >= 0;

  function chooseCurrency(next: DisplayCurrency) {
    setCurrency(next);
    localStorage.setItem("marketagent-currency", next);
  }

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>
            MarketAgent
            {/* Empty in local dev (vite serves no config.json) and on any
                deploy that predates this — absence reads as "unknown", not
                as a broken build. */}
            {shared.version && <span className="version">{shared.version}</span>}
          </h1>
          <div className="subtitle">
            An AI paper-trading experiment. No real money is ever connected.
          </div>
        </div>
        <div className="masthead-actions">
          <div className="account-toggle" role="group" aria-label="Account">
            {accounts.map(({ name, description }) => (
              <button key={name} className={`toggle ${account === name ? "current" : ""}`}
                title={description ?? undefined}
                aria-pressed={account === name}
                onClick={() => chooseAccount(name)}>
                {name}
              </button>
            ))}
          </div>
          <div className="currency-toggle" role="group" aria-label="Display currency">
            {(["USD", "GBP"] as const).map((code) => (
              <button key={code} className={`toggle ${effectiveCurrency === code ? "current" : ""}`}
                aria-pressed={effectiveCurrency === code} disabled={code === "GBP" && !gbpUsd}
                onClick={() => chooseCurrency(code)}>
                {code === "USD" ? "$ USD" : "£ GBP"}
              </button>
            ))}
          </div>
          {overview.last_run && <div className="subtitle">
            Last run {when(overview.last_run.started_at)} ·{" "}
            {overview.last_run.status}
            {/* Guarded because the API and the dashboard are separate container
                apps whose revisions roll independently: a dashboard live a
                moment before the API that supplies `trigger` would otherwise
                render a dangling separator. */}
            {overview.last_run.trigger ? ` · ${overview.last_run.trigger}` : ""}
            {overview.last_run.dry_run ? " · dry run" : ""}
          </div>}
        </div>
      </header>

      {/* Above the tabs, not inside one: these four are the answer to "how is it
          doing", and they should not depend on which area happens to be open. */}
      <div className="tiles">
        <StatTile label="Total value" value={displayMoney(overview.total_value_usd)} />
        <StatTile
          label="Profit and loss"
          value={`${up ? "+" : ""}${displayMoney(overview.pnl_usd)}`}
          note={pct(overview.pnl_pct)}
          tone={up ? "up" : "down"}
        />
        <StatTile label="Cash" value={displayMoney(overview.cash_usd)}
          note={overview.buying_power_usd == null ? undefined : `Alpaca buying power ${displayMoney(overview.buying_power_usd)}`} />
        <StatTile
          label="Positions"
          value={displayMoney(overview.positions_value_usd)}
          note={`${overview.position_count} holding${overview.position_count === 1 ? "" : "s"}`}
        />
      </div>

      <p className="hint">
        {overview.broker_synced_at
          ? `Alpaca paper account · last synchronized ${when(overview.broker_synced_at)}`
          : "Awaiting the first Alpaca account synchronization"}
      </p>
      {effectiveCurrency === "GBP" && gbpUsd && (
        <p className="hint currency-note">
          Display converted at £1 = ${gbpUsd.toFixed(4)}
          {overview.display_fx_as_of ? ` (${overview.display_fx_as_of})` : ""}. Trading and accounting remain in USD.
        </p>
      )}

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
            <p className="hint">Holdings and cost basis from the latest Alpaca snapshot.</p>
            <HoldingsTable rows={data.holdings} />
          </section>
        </>
      )}

      {tab === "/watchlist" && (
        <section className="card">
          <h2>Watchlist</h2>
          <p className="hint">
            The names this account considers each day, whether or not any is currently held.
          </p>
          <WatchlistTable rows={data.watchlist} />
        </section>
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
            Written on Sunday from the week&rsquo;s stored figures, covering both accounts, with
            the changes it proposes to the experiment.
          </p>
          <WeeklyReview review={shared.review} />
        </section>
      )}

      {tab === "/compare" && (
        <section className="card">
          <Compare accounts={accounts} data={shared.comparison} />
        </section>
      )}
    </div>
  );
}
