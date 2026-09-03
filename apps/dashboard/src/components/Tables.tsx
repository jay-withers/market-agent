/* The tabular views: holdings, decisions, trades and runs.
 *
 * These are tables rather than charts on purpose. Each answers "what are the
 * values" for a handful of rows, which a table does better than any plot — and
 * the decisions table in particular is the audit trail, where the exact
 * constraint that bound a trade matters more than any visual impression of it.
 */

import type { Decision, Holding, Run, Trade } from "../api";
import { day, gbp, pct } from "../api";

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

export function DecisionsTable({ rows }: { rows: Decision[] }) {
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
            <tr key={row.id}>
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
              <td className="num">{row.news_count}</td>
            </tr>
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
