/* static-100 vs dynamic-500 — the reason this experiment runs two accounts
 * at all.
 *
 * A dedicated two-series chart rather than a third line added to
 * PerformanceChart: that chart already uses all five validated palette slots
 * for one account plus its four benchmarks, and light mode already puts
 * three of those below 3:1 contrast against the surface — adding a sixth
 * series there would need an unvalidated colour or force dropping a
 * benchmark. Here there are only ever two series and no benchmark clutter, so
 * it reuses two of the five already-validated slots instead of inventing a
 * new one. Same table-view fallback as PerformanceChart, for the same
 * accessibility reason.
 */

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { Account, Comparison } from "../api";
import { ACCOUNTS, displayMoney, pct } from "../api";

/* Reused from the validated five, not new colours — slot 1 already carries
 * "the account itself" in PerformanceChart, so static-100 keeps it here too;
 * dynamic-500 takes slot 2 rather than a colour nothing has validated. */
const SERIES: Record<Account, { label: string; color: string }> = {
  "static-100": { label: "static-100", color: "var(--series-1)" },
  "dynamic-500": { label: "dynamic-500", color: "var(--series-2)" },
};

type Row = { as_of: string } & Partial<Record<Account, number>>;

function toRows(data: Comparison): Row[] {
  const byDate = new Map<string, Row>();
  for (const account of ACCOUNTS) {
    for (const point of data[account]) {
      const row: Row = byDate.get(point.as_of) ?? { as_of: point.as_of };
      row[account] = point.total_value_usd;
      byDate.set(point.as_of, row);
    }
  }
  return [...byDate.values()].sort((a, b) => a.as_of.localeCompare(b.as_of));
}

/* Today's figures, for the one-line answer this view exists to give: which
 * account is ahead, and by how much. */
function Headline({ data }: { data: Comparison }) {
  const latest: Partial<Record<Account, number>> = {};
  const latestPct: Partial<Record<Account, number>> = {};
  for (const account of ACCOUNTS) {
    const points = data[account];
    const last = points.at(-1);
    if (last) {
      latest[account] = last.total_value_usd;
      latestPct[account] = last.pnl_pct;
    }
  }

  const [a, b] = ACCOUNTS;
  if (latest[a] === undefined || latest[b] === undefined) return null;

  const leader = latest[a] >= latest[b] ? a : b;
  const trailer = leader === a ? b : a;
  const gap = Math.abs(latest[a] - latest[b]);

  return (
    <p className="hint">
      <strong>{leader}</strong> is ahead of <strong>{trailer}</strong> by {displayMoney(gap)} as of
      the last recorded valuation ({SERIES[a].label} {pct(latestPct[a])}, {SERIES[b].label}{" "}
      {pct(latestPct[b])} since inception).
    </p>
  );
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip">
      <div className="t-date">{label}</div>
      {payload.map((entry: any) => (
        <div className="t-row" key={entry.dataKey}>
          <span>
            <span className="swatch" style={{ background: entry.color }} />
            {SERIES[entry.dataKey as Account]?.label ?? entry.dataKey}
          </span>
          <span className="t-val">{displayMoney(entry.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function Compare({ data }: { data: Comparison }) {
  const [asTable, setAsTable] = useState(false);
  const rows = useMemo(() => toRows(data), [data]);

  if (rows.length === 0) {
    return (
      <div className="state">
        No comparison history yet — the summary job writes a point for each account every
        evening.
      </div>
    );
  }

  return (
    <>
      <div className="row-between">
        <div>
          <h2>static-100 vs dynamic-500</h2>
          <p className="hint">
            The frozen S&amp;P 100 snapshot against the S&amp;P 500, refreshed monthly — same
            starting cash, same agent, different universes.
          </p>
        </div>
        <button className="toggle" onClick={() => setAsTable((v) => !v)}>
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>

      <Headline data={data} />

      {asTable ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                {ACCOUNTS.map((account) => (
                  <th className="num" key={account}>
                    <span className="swatch" style={{ background: SERIES[account].color }} />
                    {SERIES[account].label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.as_of}>
                  <td>{row.as_of}</td>
                  {ACCOUNTS.map((account) => (
                    <td className="num" key={account}>
                      {row[account] === undefined ? "—" : displayMoney(row[account])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div style={{ width: "100%", height: 320 }}>
          <ResponsiveContainer>
            <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis
                dataKey="as_of"
                tick={{ fill: "var(--text-muted)", fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: "var(--axis)" }}
                minTickGap={28}
              />
              <YAxis
                tick={{ fill: "var(--text-muted)", fontSize: 12 }}
                tickLine={false}
                axisLine={false}
                width={64}
                tickFormatter={(v) => `$${Math.round(v)}`}
                domain={["auto", "auto"]}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--axis)" }} />
              <Legend
                wrapperStyle={{ fontSize: 12, color: "var(--text-secondary)" }}
                iconType="plainline"
              />
              {ACCOUNTS.map((account) => (
                <Line
                  key={account}
                  type="monotone"
                  dataKey={account}
                  name={SERIES[account].label}
                  stroke={SERIES[account].color}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }}
                  connectNulls
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </>
  );
}
