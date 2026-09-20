/* Per-holding price trend, as small multiples.
 *
 * A grid of one panel per holding rather than one chart with every holding on
 * it, and the reason is the palette: the validated categorical order has five
 * slots and its order *is* the colourblind-safety mechanism, so six holdings on
 * shared axes would mean either cycling it or extending it, and both give up
 * the guarantee. A panel per entity needs one colour in total.
 *
 * It is also the better read. Closes here span roughly $220 to $670, so a
 * shared y-axis would flatten the cheaper names into each other; each panel
 * scaling to its own range shows every trend at full amplitude. The cost of
 * that — and it is a real one — is that panels are not comparable by eye, so
 * the figure under each is the percentage against its own cost basis, which is
 * comparable across panels in a way the lines deliberately are not.
 *
 * Quoted in USD against the USD cost basis, not converted: this answers "what
 * did the stock do", and mixing in the GBP rate would fold a currency move into
 * a figure read as the company's. The portfolio's own GBP P&L is on the
 * overview, where the FX belongs.
 */

import { useMemo, useState } from "react";
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { Holding, PricePoint } from "../api";

const usd = (value: number): string => `$${value.toFixed(2)}`;

function PanelTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip">
      <div className="t-date">{label}</div>
      <div className="t-row">
        <span>Close</span>
        <span className="t-val">{usd(payload[0].value)}</span>
      </div>
    </div>
  );
}

function Panel({ holding, points }: { holding: Holding; points: PricePoint[] }) {
  const cost = holding.avg_cost_usd;
  const last = points.length > 0 ? points[points.length - 1].close_usd : holding.last_close_usd;
  // Against the cost basis rather than the start of the window, so the figure
  // means the same thing on every panel however much history each one has.
  const change = last !== null && cost > 0 ? (last / cost - 1) * 100 : null;

  // The cost line is part of the picture, so the scale has to include it —
  // recharts will not widen a domain to fit a ReferenceLine on its own.
  const domain = useMemo<[number, number]>(() => {
    const values = points.map((p) => p.close_usd);
    if (values.length === 0) return [0, 1];
    const lo = Math.min(...values, cost);
    const hi = Math.max(...values, cost);
    const pad = (hi - lo) * 0.08 || Math.max(hi * 0.01, 0.5);
    return [lo - pad, hi + pad];
  }, [points, cost]);

  return (
    <div className="trend">
      <div className="trend-head">
        <span className="trend-ticker">{holding.ticker}</span>
        <span className="trend-name">{holding.name}</span>
      </div>

      {points.length < 2 ? (
        /* One bar cannot be a line, and a single dot drawn across a panel reads
           as a flat trend rather than as one observation. Worded against the
           window rather than the holding: with a short range selected, "no
           history" would be a claim about the data that is not true. */
        <div className="trend-empty">
          {points.length === 0 ? "No closes in this window." : "One close in this window."}
        </div>
      ) : (
        <div className="trend-plot">
          <ResponsiveContainer>
            <LineChart data={points} margin={{ top: 4, right: 2, bottom: 0, left: 2 }}>
              <XAxis dataKey="bar_date" hide />
              <YAxis hide domain={domain} />
              {/* Where the position was bought: the line being above or below
                  it is the whole question, and it saves reading the number. */}
              <ReferenceLine y={cost} stroke="var(--axis)" strokeDasharray="3 3" />
              <Tooltip content={<PanelTooltip />} cursor={{ stroke: "var(--axis)" }} />
              <Line
                type="monotone"
                dataKey="close_usd"
                stroke="var(--series-1)"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 2, stroke: "var(--surface)" }}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="trend-foot">
        <span className="trend-close">{last === null ? "—" : usd(last)}</span>
        {/* Sign in the text as well as the colour, and the basis named: this is
            the stock against what we paid, not the holding's GBP return. */}
        <span className={change === null ? undefined : change >= 0 ? "up" : "down"}>
          {change === null ? "—" : `${change >= 0 ? "+" : ""}${change.toFixed(1)}% vs cost`}
        </span>
      </div>
    </div>
  );
}

/* The windows offered. Filtered on the client rather than refetched: the whole
 * year is a few hundred rows, it already arrives in the page's one round of
 * requests, and switching window should not cost a cold-start round trip to an
 * app that scales to zero. */
const RANGES = [
  { key: "1W", label: "1W", days: 7 },
  { key: "1M", label: "1M", days: 30 },
  { key: "3M", label: "3M", days: 90 },
  { key: "ALL", label: "All", days: null },
] as const;

type RangeKey = (typeof RANGES)[number]["key"];

/* ISO day arithmetic on strings, matching how the rest of the client handles
 * these: bar_date is a plain YYYY-MM-DD and lexical order is chronological, so
 * comparing strings avoids parsing a date into the browser's zone and shifting
 * it across a boundary. */
function cutoffFor(days: number | null): string | null {
  if (days === null) return null;
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

export function PriceTrends({ holdings, prices }: { holdings: Holding[]; prices: PricePoint[] }) {
  const [asTable, setAsTable] = useState(false);
  // Three months by default: long enough to show a trend, short enough that a
  // year of accumulated history does not flatten the recent weeks.
  const [range, setRange] = useState<RangeKey>("3M");

  const visible = useMemo(() => {
    const cutoff = cutoffFor(RANGES.find((r) => r.key === range)?.days ?? null);
    return cutoff === null ? prices : prices.filter((p) => p.bar_date >= cutoff);
  }, [prices, range]);

  const byTicker = useMemo(() => {
    const grouped = new Map<string, PricePoint[]>();
    for (const point of visible) {
      const series = grouped.get(point.ticker);
      if (series) series.push(point);
      else grouped.set(point.ticker, [point]);
    }
    return grouped;
  }, [visible]);

  // Every date any holding has a bar for, so the table has one row per day even
  // where a ticker is missing that day.
  const dates = useMemo(
    () => [...new Set(visible.map((p) => p.bar_date))].sort(),
    [visible],
  );

  if (holdings.length === 0) return <div className="state">Nothing held yet.</div>;

  return (
    <>
      <div className="row-between">
        <div>
          <h2>How each holding has moved</h2>
          <p className="hint">
            Daily closes in dollars against the dashed line we paid, so this is the
            company&rsquo;s move rather than the holding&rsquo;s return in pounds. Each panel
            has its own scale — compare the percentages, not the shapes.
          </p>
        </div>
        <div className="controls">
          {/* A pressed-state group rather than a select: four options, and the
              current window should be readable without opening anything. */}
          <div className="segmented" role="group" aria-label="Time range">
            {RANGES.map((r) => (
              <button
                key={r.key}
                type="button"
                className={`seg${range === r.key ? " current" : ""}`}
                aria-pressed={range === r.key}
                onClick={() => setRange(r.key)}
              >
                {r.label}
              </button>
            ))}
          </div>
          <button className="toggle" onClick={() => setAsTable((v) => !v)}>
            {asTable ? "Show charts" : "Show table"}
          </button>
        </div>
      </div>

      {asTable ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                {holdings.map((h) => (
                  <th className="num" key={h.ticker}>
                    {h.ticker}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dates.map((date) => (
                <tr key={date}>
                  <td>{date}</td>
                  {holdings.map((h) => {
                    const point = byTicker.get(h.ticker)?.find((p) => p.bar_date === date);
                    return (
                      <td className="num" key={h.ticker}>
                        {point === undefined ? "—" : usd(point.close_usd)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="trends">
          {holdings.map((h) => (
            <Panel key={h.ticker} holding={h} points={byTicker.get(h.ticker) ?? []} />
          ))}
        </div>
      )}
    </>
  );
}
