/* The tabular views: holdings, decisions, trades and runs.
 *
 * These are tables rather than charts on purpose. Each answers "what are the
 * values" for a handful of rows, which a table does better than any plot — and
 * the decisions table in particular is the audit trail, where the exact
 * constraint that bound a trade matters more than any visual impression of it.
 */

import { Fragment, useState } from "react";

import type { Article, Decision, DecisionDetail, Holding, Run, Trade } from "../api";
import { day, gbp, get, pct } from "../api";

export function HoldingsTable({ rows }: { rows: Holding[] }) {
  if (rows.length === 0) return <div className="state">Nothing held yet.</div>;

  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Name</th>
            <th className="num">Quantity</th>
            <th className="num">Avg cost</th>
            <th className="num">Last close</th>
            <th>As of</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.ticker}>
              <td>{row.ticker}</td>
              <td>{row.name}</td>
              <td className="num">{row.quantity.toFixed(6)}</td>
              <td className="num">{gbp(row.avg_cost_gbp)}</td>
              <td className="num">
                {row.last_close_usd === null ? "—" : `$${row.last_close_usd.toFixed(2)}`}
              </td>
              <td>{row.last_close_date ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* The articles behind one decision, fetched on demand.
 *
 * `/api/decisions` carries a count and nothing else, so the count alone is a
 * dead end: the audit question is *which* articles the model was shown, and
 * `/api/decisions/{id}` — the endpoint that exists for exactly that — answers
 * it. Fetching per row rather than eagerly keeps the table's own request small
 * on a cold start, and most rows are never expanded.
 */
function Articles({
  articles,
  error,
  loading,
}: {
  articles: Article[] | undefined;
  error: string | undefined;
  loading: boolean;
}) {
  if (loading) return <div className="articles-state">Loading articles…</div>;
  if (error !== undefined) {
    return <div className="articles-state error">Could not load the articles: {error}</div>;
  }
  // Reachable in principle rather than in practice: news rows are never
  // deleted, so a stored id always resolves.
  if (articles === undefined || articles.length === 0) {
    return <div className="articles-state">No articles recorded against this decision.</div>;
  }

  return (
    <ul className="articles">
      {articles.map((article) => (
        <li key={article.id}>
          <div className="headline">
            {article.url === null ? (
              article.headline
            ) : (
              // noreferrer as well as noopener: these are third-party news
              // links and the dashboard's URL is not theirs to log.
              <a href={article.url} target="_blank" rel="noopener noreferrer">
                {article.headline}
              </a>
            )}
          </div>
          <div className="meta">
            {[article.source, day(article.published_at)].filter(Boolean).join(" · ")}
          </div>
          {article.summary !== null && article.summary !== "" && (
            <p className="summary">{article.summary}</p>
          )}
        </li>
      ))}
    </ul>
  );
}

export function DecisionsTable({ rows }: { rows: Decision[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);
  // Keyed by decision id and never evicted: a decision is immutable once
  // written, so a re-expand has nothing new to fetch.
  const [articles, setArticles] = useState<Record<number, Article[]>>({});
  const [errors, setErrors] = useState<Record<number, string>>({});
  // Keyed by id rather than a single id: expanding a second row while the
  // first is still in flight would otherwise clear the flag for both, and the
  // second would render its empty state while its own fetch was still running.
  const [loading, setLoading] = useState<Record<number, boolean>>({});

  async function toggle(id: number) {
    if (expanded === id) {
      setExpanded(null);
      return;
    }
    setExpanded(id);
    if (articles[id] !== undefined || loading[id]) return;

    setLoading((previous) => ({ ...previous, [id]: true }));
    try {
      const detail = await get<DecisionDetail>(`/api/decisions/${id}`);
      setArticles((previous) => ({ ...previous, [id]: detail.news ?? [] }));
      setErrors(({ [id]: _dropped, ...rest }) => rest);
    } catch (failure) {
      // A failed fetch must not read as "this decision had no articles", which
      // is a claim about the audit trail rather than about the network.
      setErrors((previous) => ({
        ...previous,
        [id]: failure instanceof Error ? failure.message : String(failure),
      }));
    } finally {
      setLoading(({ [id]: _done, ...rest }) => rest);
    }
  }

  if (rows.length === 0) return <div className="state">No decisions recorded yet.</div>;

  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Ticker</th>
            <th>Action</th>
            <th className="num">Confidence</th>
            <th className="num">Asked</th>
            <th className="num">Approved</th>
            <th>Bound by</th>
            <th className="num">Articles</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Fragment key={row.id}>
              <tr>
                <td>{day(row.decided_at)}</td>
                <td>{row.ticker}</td>
                <td>{row.action}</td>
                <td className="num">
                  {row.confidence === null ? "—" : row.confidence.toFixed(2)}
                </td>
                <td className="num">{gbp(row.recommended_amount_gbp)}</td>
                {/* A refusal shows a dash, not £0.00: "approved nothing" and
                    "approved zero" are different facts. */}
                <td className="num">{gbp(row.approved_amount_gbp)}</td>
                <td>
                  <span className="pill">{row.binding_constraint ?? "—"}</span>
                </td>
                <td className="num">
                  {/* Plain text at zero: a control that opens an empty panel
                      is worse than no control. */}
                  {row.news_count === 0 ? (
                    0
                  ) : (
                    <button
                      type="button"
                      className="toggle count"
                      aria-expanded={expanded === row.id}
                      aria-controls={`articles-${row.id}`}
                      onClick={() => void toggle(row.id)}
                    >
                      {row.news_count}
                    </button>
                  )}
                </td>
              </tr>
              {expanded === row.id && (
                <tr className="detail">
                  <td colSpan={8} id={`articles-${row.id}`}>
                    <Articles
                      articles={articles[row.id]}
                      error={errors[row.id]}
                      loading={loading[row.id] === true && articles[row.id] === undefined}
                    />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TradesTable({ rows }: { rows: Trade[] }) {
  if (rows.length === 0) return <div className="state">No trades yet.</div>;

  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Ticker</th>
            <th>Side</th>
            <th>Status</th>
            <th className="num">Amount</th>
            <th className="num">Quantity</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>{day(row.created_at)}</td>
              <td>{row.ticker}</td>
              <td>{row.side}</td>
              <td>
                <span className="pill">{row.status}</span>
                {row.dry_run && <span className="pill" style={{ marginLeft: 6 }}>dry run</span>}
              </td>
              <td className="num">{gbp(row.notional_gbp)}</td>
              {/* A notional order names no quantity until it fills, and a blank
                  cell would read as zero shares. */}
              <td className="num">
                {row.quantity === null ? "not yet filled" : row.quantity.toFixed(6)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RunsTable({ rows }: { rows: Run[] }) {
  if (rows.length === 0) return <div className="state">The agent has not run yet.</div>;

  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Started</th>
            <th>Status</th>
            <th className="num">Decisions</th>
            <th className="num">Trades</th>
            <th className="num">Cost</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>{row.started_at.slice(0, 16).replace("T", " ")}</td>
              <td>
                {/* Status is a word, never a colour alone. `stale` is computed
                    by the API: a run killed outright cannot close its own row,
                    so one still "running" past the job timeout is not alive. */}
                <span className={row.status === "failed" ? "down" : undefined}>
                  {row.stale ? "abandoned" : row.status}
                </span>
                {row.dry_run && <span className="pill" style={{ marginLeft: 6 }}>dry run</span>}
              </td>
              <td className="num">{row.decisions_made ?? "—"}</td>
              <td className="num">{row.trades_executed ?? "—"}</td>
              <td className="num">
                {row.cost_usd === null ? "—" : `$${row.cost_usd.toFixed(4)}`}
              </td>
              <td style={{ maxWidth: 320, whiteSpace: "nowrap", overflow: "hidden",
                           textOverflow: "ellipsis" }} title={row.error ?? ""}>
                {row.error ?? ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StatTile({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: "up" | "down";
}) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      {/* The delta cue carries a sign in the text as well as the colour. */}
      <div className={`value ${tone ?? ""}`}>{value}</div>
      {note && <div className="note">{note}</div>}
    </div>
  );
}

export { gbp, pct };
