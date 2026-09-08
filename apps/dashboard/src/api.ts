/* Talking to the API, and finding out where it is.
 *
 * The address is learned at *runtime*, not baked in at build time: nginx
 * renders config.json from $API_ORIGIN when the container starts, and this
 * fetches it on boot. That is what lets one image serve every environment —
 * the alternative, a VITE_API_URL compiled in, means a rebuild per environment
 * and an image that is only correct where it was built.
 */

export type Overview = {
  portfolio: { name: string; initial_cash_gbp: number } | null;
  cash_gbp: number;
  positions_value_gbp: number;
  total_value_gbp: number;
  position_count: number;
  pnl_gbp: number;
  pnl_pct: number;
  last_run: {
    id: number;
    started_at: string;
    status: string;
    trigger: string;
    dry_run: boolean;
    decisions_made: number | null;
    trades_executed: number | null;
    cost_usd: number | null;
  } | null;
};

export type PerformancePoint = {
  as_of: string;
  total_value_gbp: number;
  cash_gbp: number;
  positions_value_gbp: number;
  pnl_gbp: number;
  pnl_pct: number;
};

export type BenchmarkPoint = {
  symbol: string;
  as_of: string;
  value_gbp: number;
  close_usd: number | null;
};

export type Performance = {
  portfolio: PerformancePoint[];
  benchmarks: BenchmarkPoint[];
};

export type Holding = {
  ticker: string;
  name: string;
  sector: string | null;
  quantity: number;
  avg_cost_usd: number;
  avg_cost_gbp: number;
  last_close_usd: number | null;
  last_close_date: string | null;
};

export type Decision = {
  id: number;
  decided_at: string;
  ticker: string;
  action: "BUY" | "SELL" | "HOLD";
  confidence: number | null;
  recommended_amount_gbp: number | null;
  approved_amount_gbp: number | null;
  binding_constraint: string | null;
  approved: boolean | null;
  news_count: number;
};

/* One article as `/api/decisions/{id}` returns it. The list endpoint carries
 * only `news_count`; these are what that count is counting. */
export type Article = {
  id: number;
  headline: string;
  summary: string | null;
  url: string | null;
  source: string | null;
  published_at: string;
  tickers: string[];
};

/* The detail endpoint returns the whole `ai_decisions` row plus its trades as
 * well. Only the articles are typed here, because only they are rendered —
 * everything else in the table above already comes from the list endpoint. */
export type DecisionDetail = {
  id: number;
  news: Article[];
};

export type Trade = {
  id: number;
  ticker: string;
  side: string;
  status: string;
  dry_run: boolean;
  quantity: number | null;
  price_usd: number | null;
  notional_gbp: number;
  created_at: string;
};

export type Run = {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  // 'schedule' or 'manual', per the CHECK constraint on agent_runs.trigger. A
  // run started by hand and a run the cron fired are otherwise indistinguishable
  // in this table, and they are read very differently.
  trigger: string;
  dry_run: boolean;
  decisions_made: number | null;
  trades_executed: number | null;
  cost_usd: number | null;
  error: string | null;
  stale: boolean;
};

let origin: string | null = null;

async function apiOrigin(): Promise<string> {
  if (origin !== null) return origin;
  try {
    const response = await fetch("/config.json", { cache: "no-store" });
    const config = (await response.json()) as { apiOrigin?: string };
    // An unsubstituted template still contains the placeholder; treating that
    // as an origin produces a confusing CORS error rather than an obvious
    // misconfiguration.
    const value = config.apiOrigin ?? "";
    origin = value && !value.includes("${") ? value.replace(/\/$/, "") : "";
  } catch {
    // Same-origin is the right fallback for `vite dev` behind a proxy and for
    // any deployment where the API is served from the same host.
    origin = "";
  }
  return origin;
}

export async function get<T>(path: string): Promise<T> {
  const base = await apiOrigin();
  const response = await fetch(`${base}${path}`, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} from ${path}`);
  }
  return (await response.json()) as T;
}

export const gbp = (value: number | null | undefined): string =>
  value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("en-GB", {
        style: "currency",
        currency: "GBP",
        maximumFractionDigits: 2,
      }).format(value);

export const pct = (value: number | null | undefined): string =>
  value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

export const day = (iso: string): string => iso.slice(0, 10);

// Sliced rather than parsed through Date, for the same reason day() is: the
// zone is forced to UTC and said out loud. Everything these timestamps get read
// against is UTC — the 06:00 agent cron, the 14:30 market open, the job logs —
// so rendering them in the browser's zone would shift them an hour under BST
// and make a 06:00 run look like it started at 07:00.
export const when = (iso: string): string => `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;

/* The weekly review: the model's prose, and the changes it proposes.
 *
 * `body_html` is deliberately not part of this type and the API does not send
 * it. That column is Markdown-rendered *model output*, and Python-Markdown
 * passes raw HTML straight through, so putting it in the DOM would let the
 * model inject markup. The figures it contains are ones this dashboard already
 * renders itself from the same tables. */
export type ProposedChange = {
  area: string;
  change: string;
  rationale: string;
  expected_effect: string;
  confidence: number;
};

export type Review = {
  id: number;
  period_start: string;
  period_end: string;
  subject: string;
  assessment: string | null;
  /* jsonb, so null is possible on a row written before this column meant
   * anything — and an empty list is a real answer: a week that supported no
   * change. */
  recommendations: ProposedChange[] | null;
  model: string | null;
  email_status: string;
  sent_at: string | null;
};

/* A GET whose 404 is an expected state rather than an error.
 *
 * The weekly review does not exist until the first Sunday run, and the
 * dashboard's other requests share one Promise.all — a rejection here would
 * blank the whole page over a section that is merely not ready yet. Anything
 * other than a 404 still throws. */
export async function getOptional<T>(path: string): Promise<T | null> {
  const base = await apiOrigin();
  const response = await fetch(`${base}${path}`, {
    headers: { accept: "application/json" },
  });
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} from ${path}`);
  }
  return (await response.json()) as T;
}

/* Markdown emphasis stripped rather than rendered.
 *
 * The model is asked for Markdown because the email is Markdown, and this is
 * the same text in a browser. Rendering it properly would mean either a
 * Markdown library or injecting the server's rendered HTML, and the second of
 * those is the XSS vector described above — so the inline markers are removed
 * and the words are shown as words. Block structure survives as paragraphs. */
export const plain = (text: string): string =>
  text
    .replace(/`{1,3}([^`]*)`{1,3}/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/(^|\s)[*_]([^*_]+)[*_]/g, "$1$2")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .trim();
