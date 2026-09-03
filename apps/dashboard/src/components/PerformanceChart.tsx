/* The comparison that is the whole point of the experiment: did the AI beat
 * just buying an index, or leaving the money in the bank?
 *
 * A multi-series line chart, because the job is change over time across
 * comparable entities. Every series is the value of the same notional £500, so
 * they share one axis — a second y-scale would be the single most common way to
 * make two series look related when they are not.
 *
 * The palette is the validated categorical order, assigned per entity so that
 * hiding a series never repaints the others. Light mode puts three of these
 * slots below 3:1 against the surface, which obliges relief: hence the table
 * view, which is a required accessibility channel here rather than a
 * convenience.
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

import type { Performance } from "../api";
import { gbp } from "../api";

/* Colour follows the entity, in the palette's fixed slot order. The portfolio
 * takes slot 1 because it is the subject; the benchmarks follow in a stable
 * order so a chart from last month is comparable with one from today. */
const SERIES = [
  { key: "portfolio", label: "Portfolio", color: "var(--series-1)" },
  { key: "SPY", label: "S&P 500 (SPY proxy)", color: "var(--series-2)" },
  { key: "VT", label: "World (VT proxy)", color: "var(--series-3)" },
  { key: "EWU", label: "UK (EWU proxy)", color: "var(--series-4)" },
  { key: "CASH5", label: "Savings at 5%", color: "var(--series-5)" },
] as const;

/* Keyed by the series, not by any string: an open index signature collides
 * with `as_of` and would let a typo'd series name through silently. */
type SeriesKey = (typeof SERIES)[number]["key"];
type Row = { as_of: string } & Partial<Record<SeriesKey, number>>;

function toRows(data: Performance): Row[] {
  const byDate = new Map<string, Row>();

  for (const point of data.portfolio) {
    byDate.set(point.as_of, { as_of: point.as_of, portfolio: point.total_value_gbp });
  }
  const known = new Set<string>(SERIES.map((s) => s.key));
  for (const point of data.benchmarks) {
    // A benchmark the dashboard does not know about is skipped rather than
    // plotted in a colour outside the validated palette.
    if (!known.has(point.symbol)) continue;
    const row: Row = byDate.get(point.as_of) ?? { as_of: point.as_of };
    row[point.symbol as SeriesKey] = point.value_gbp;
    byDate.set(point.as_of, row);
  }

  return [...byDate.values()].sort((a, b) => a.as_of.localeCompare(b.as_of));
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
            {SERIES.find((s) => s.key === entry.dataKey)?.label ?? entry.dataKey}
          </span>
          <span className="t-val">{gbp(entry.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function PerformanceChart({ data }: { data: Performance }) {
  const [asTable, setAsTable] = useState(false);
  const rows = useMemo(() => toRows(data), [data]);

  // Only plot series that actually have data, so a benchmark we have no price
  // for is absent rather than drawn flat at the notional — a flat line reads as
  // "the index did nothing", which is a different claim from "we have no data".
  const present = SERIES.filter((s) => rows.some((row) => row[s.key] !== undefined));

  if (rows.length === 0) {
    return (
      <div className="state">
        No performance history yet — the summary job writes a point each evening.
      </div>
    );
  }

  return (
    <>
      <div className="row-between">
        <div>
          <h2>Portfolio against the alternatives</h2>
          <p className="hint">
            What the same notional would be worth in each. Index arms are proxies, not
            the indices themselves.
          </p>
        </div>
        <button className="toggle" onClick={() => setAsTable((v) => !v)}>
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>

      {asTable ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                {present.map((s) => (
                  <th className="num" key={s.key}>
                    <span className="swatch" style={{ background: s.color }} />
                    {s.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.as_of}>
                  <td>{row.as_of}</td>
                  {present.map((s) => (
                    <td className="num" key={s.key}>
                      {row[s.key] === undefined ? "—" : gbp(row[s.key])}
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
              {/* Recessive chrome: horizontal rules only, no vertical clutter. */}
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
                tickFormatter={(v) => `£${Math.round(v)}`}
                // Not from zero: every series starts near £500 and a zero
                // baseline would compress the entire experiment into the top
                // few pixels. Honest here because these are indexed values
                // being compared with each other, not magnitudes.
                domain={["auto", "auto"]}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--axis)" }} />
              <Legend
                wrapperStyle={{ fontSize: 12, color: "var(--text-secondary)" }}
                iconType="plainline"
              />
              {present.map((s) => (
                <Line
                  key={s.key}
                  type="monotone"
                  dataKey={s.key}
                  name={s.label}
                  stroke={s.color}
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
