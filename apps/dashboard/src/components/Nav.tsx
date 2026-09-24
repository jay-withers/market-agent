/* Tabbed areas with real URLs, and a router small enough not to be a dependency.
 *
 * Real paths rather than a hash because nginx already falls back unknown paths
 * to index.html inside the authenticated `location /`, so deep links cost
 * nothing and need no server change. Six static areas, one of them split into
 * three sub-pages, with no parameters do not justify react-router; this is
 * the whole of what it would be used for.
 * The account being viewed is state, not a route — every area shows one
 * account's data at a time, so it lives beside the currency toggle instead of
 * in the URL.
 *
 * An unknown path renders the overview rather than a not-found page: every
 * route here is a view of the same dataset, and a stale bookmark should show
 * the dashboard instead of an error.
 */

import { useCallback, useEffect, useState } from "react";

export const TABS = [
  { path: "/", label: "Overview" },
  { path: "/holdings", label: "Holdings" },
  { path: "/watchlist", label: "Watchlist" },
  { path: "/activity", label: "Activity" },
  { path: "/review", label: "Review" },
  { path: "/compare", label: "Compare" },
] as const;

/* Activity's sub-pages, each its own URL. Decisions run to fifty rows, so on
 * one page they pushed trades and runs far below the fold on a phone. */
export const ACTIVITY_SECTIONS = [
  { path: "/activity/decisions", label: "Decisions" },
  { path: "/activity/trades", label: "Trades" },
  { path: "/activity/runs", label: "Agent runs" },
] as const;

export type TabPath = (typeof TABS)[number]["path"];
export type ActivityPath = (typeof ACTIVITY_SECTIONS)[number]["path"];
export type Route = Exclude<TabPath, "/activity"> | ActivityPath;

type Item = { path: string; label: string };

function normalise(pathname: string): Route {
  const trimmed = pathname.replace(/\/+$/, "") || "/";
  // Bare /activity, which a bookmark from before the split still points at,
  // opens its first sub-page.
  if (trimmed === "/activity") return ACTIVITY_SECTIONS[0].path;
  const known: readonly Item[] = [...TABS, ...ACTIVITY_SECTIONS];
  return (known.find((t) => t.path === trimmed && t.path !== "/activity")?.path ?? "/") as Route;
}

/* The top-level tab a route belongs to, which is what the main nav marks. */
export const tabOf = (route: Route): TabPath =>
  route.startsWith("/activity/") ? "/activity" : (route as TabPath);

export function useRoute(): [Route, (to: Route | TabPath) => void] {
  const [path, setPath] = useState<Route>(() => normalise(window.location.pathname));

  // Back and forward have to move the view too, or the URL and the page
  // disagree — the usual failure of a hand-rolled router.
  useEffect(() => {
    const onPop = () => setPath(normalise(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: Route | TabPath) => {
    const route = normalise(to);
    if (window.location.pathname !== route) {
      window.history.pushState(null, "", route);
    }
    setPath(route);
  }, []);

  return [path, navigate];
}

export function Nav({
  current,
  onNavigate,
  items = TABS,
  label = "Dashboard areas",
  className = "tabs",
}: {
  current: string;
  onNavigate: (to: Route | TabPath) => void;
  items?: readonly Item[];
  label?: string;
  className?: string;
}) {
  return (
    <nav className={className} aria-label={label}>
      {items.map((tab) => (
        /* A real anchor, so middle-click, copy-link and open-in-new-tab all
           behave. The click handler only takes over the plain left click. */
        <a
          key={tab.path}
          href={tab.path}
          className={`tab${current === tab.path ? " current" : ""}`}
          aria-current={current === tab.path ? "page" : undefined}
          onClick={(event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
            event.preventDefault();
            onNavigate(tab.path as Route | TabPath);
          }}
        >
          {tab.label}
        </a>
      ))}
    </nav>
  );
}
