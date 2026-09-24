/* The pots side by side — the reason this experiment runs several at all.
 *
 * A dedicated chart rather than more lines added to PerformanceChart: that
 * chart already uses all five validated palette slots for one pot plus its
 * four benchmarks. Here there is one series per pot and no benchmark clutter,
 * so the pots reuse the validated slots in order — which caps this view at
 * five pots before it would need an unvalidated colour. Light mode puts three
 * slots below 3:1 contrast against the surface, so the table view is a
 * required accessibility channel, not a convenience, exactly as in
 * PerformanceChart.
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

import type { AccountInfo, Comparison } from "../api";
import { displayMoney, pct } from "../api";

/* The slot follows the pot's position in the API's id-ordered list, so a pot
 * keeps its colour however the chart is filtered. The palette's documented
 * slot order is the colourblind-safety mechanism; never reorder or cycle it. */
const color = (index: number) => `var(--series-${index + 1})`;

/* One date's row, keyed by pot name — flat, because recharts reads each line's
 * value by `dataKey`. */
type Row = { as_of: string; [pot: string]: string | number };

function toRows(names: string[], data: Comparison): Row[] {
  const byDate = new Map<string, Row>();
  for (const account of names) {
    for (const point of data[account] ?? []) {
      const row: Row = byDate.get(point.as_of) ?? { as_of: point.as_of };
      row[account] = point.total_value_usd;
      byDate.set(point.as_of, row);
    }
  }
  return [...byDate.values()].sort((a, b) => a.as_of.localeCompare(b.as_of));
}

/* Today's figures, for the one-line answer this view exists to give: which
 * pot leads, which trails, and by how much. */
function Headline({ names, data }: { names: string[]; data: Comparison }) {
  const latest = names
    .map((name) => ({ name, last: data[name]?.at(-1) }))
    .filter((entry): entry is { name: string; last: NonNullable<typeof entry.last> } =>
      Boolean(entry.last),
    )
    .sort((x, y) => y.last.total_value_usd - x.last.total_value_usd);

  if (latest.length < 2) return null;

  const leader = latest[0];
  const trailer = latest[latest.length - 1];
  const gap = leader.last.total_value_usd - trailer.last.total_value_usd;

  return (
    <p className="hint">
      <strong>{leader.name}</strong> leads and <strong>{trailer.name}</strong> trails,{" "}
      {displayMoney(gap)} apart as of the last recorded valuation (
      {latest.map((entry) => `${entry.name} ${pct(entry.last.pnl_pct)}`).join(", ")} since
      inception).
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
            {entry.dataKey}
          </span>
          <span className="t-val">{displayMoney(entry.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function Compare({ accounts, data }: { accounts: AccountInfo[]; data: Comparison }) {
  const [asTable, setAsTable] = useState(false);
  const names = useMemo(() => accounts.map((a) => a.name), [accounts]);
  // A pot with no valuation yet is left out rather than drawn as an empty
  // series, the same rule the benchmarks follow. Its colour slot stays its
  // own, so a pot never changes colour when another one first appears.
  const charted = useMemo(
    () => names.filter((name) => (data[name]?.length ?? 0) > 0),
    [names, data],
  );
  const rows = useMemo(() => toRows(charted, data), [charted, data]);

  if (rows.length === 0) {
    return (
      <div className="state">
        No comparison history yet — the summary job writes a point for each pot every evening.
      </div>
    );
  }

  return (
    <>
      <div className="row-between">
        <div>
          <h2>The pots compared</h2>
          <p className="hint">
            {accounts.map((a) => (a.description ? `${a.name}: ${a.description}` : a.name)).join(" · ")}
            {" "}— same starting cash, same agent, different sectors.
          </p>
        </div>
        <button className="toggle" onClick={() => setAsTable((v) => !v)}>
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>

      <Headline names={names} data={data} />

      {asTable ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                {charted.map((account) => (
                  <th className="num" key={account}>
                    <span className="swatch" style={{ background: color(names.indexOf(account)) }} />
                    {account}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.as_of}>
                  <td>{row.as_of}</td>
                  {charted.map((account) => (
                    <td className="num" key={account}>
                      {typeof row[account] === "number" ? displayMoney(row[account]) : "—"}
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
              {charted.map((account) => (
                <Line
                  key={account}
                  type="monotone"
                  dataKey={account}
                  name={account}
                  stroke={color(names.indexOf(account))}
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
