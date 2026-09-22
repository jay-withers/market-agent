/* Layout regression tests.
 *
 * These exist because the only thing that caught the dashboard collapsing to a
 * two-word column on a phone was somebody opening it on a phone. Nothing in CI
 * renders a page, so every CSS change shipped unverified — and the fault that
 * prompted these was introduced by a `flex-shrink: 0` that looked obviously
 * correct in the diff.
 *
 * Deliberately narrow in scope. They assert that nothing overflows its
 * viewport and that the things which have actually broken stay unbroken. They
 * are not screenshot tests: a pixel baseline would fail on every font tweak
 * and get deleted within a month.
 */

import { expect, test, type Page } from "@playwright/test";

import { ROUTES } from "./fixtures";

const TABS = ["/", "/holdings", "/activity", "/review", "/compare"] as const;

// 360 is the narrowest mainstream Android; 390 an iPhone; 768 a tablet, where
// a two-column grid first appears and is the likeliest place for a row to
// break; 1280 the desktop the layout was designed at.
const WIDTHS = [360, 390, 768, 1280] as const;

async function stubApi(page: Page) {
  // Registered first, and that ordering is load-bearing: Playwright matches
  // handlers in *reverse* registration order, so a catch-all added last wins
  // over every specific route and the whole page renders its error state.
  //
  // Anything still reaching it is a request this suite does not know about. It
  // fails loudly rather than 404ing, because getOptional swallows a 404 as "no
  // data" — a new endpoint should break this file, not quietly render an empty
  // page that satisfies every assertion below.
  await page.route("**/api/**", (route) => route.abort("failed"));

  for (const [path, body] of Object.entries(ROUTES)) {
    // Exact-match on the pathname so `/api/decisions` does not also answer
    // `/api/decisions/3`, which the articles panel fetches separately.
    await page.route(
      (url) => url.pathname === path,
      (route) => route.fulfill({ json: body as object }),
    );
  }
}

/** Horizontal overflow of the page itself, in pixels. */
const pageOverflow = (page: Page) =>
  page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );

/** Elements sticking out past the viewport, ignoring self-scrolling containers.
 *
 * A wide table inside `.scroll`, or an overflowing tab inside `.tabs`, is
 * allowed to exceed the viewport — both carry `overflow-x: auto` precisely so
 * *they* scroll instead of the page, which is what `pageOverflow` above
 * checks separately. `.tabs` growing a fifth entry (Compare) is what first
 * exercised this at 360-390px; it was already designed to cope, the check
 * just had not been asked to look past `.scroll` yet. Anything else clipping
 * is still a bug.
 */
const clippedOutside = (page: Page) =>
  page.evaluate(() => {
    const limit = document.documentElement.clientWidth + 1;
    return [...document.querySelectorAll<HTMLElement>("body *")]
      .filter(
        (el) =>
          !el.closest(".scroll") && !el.closest(".tabs") && el.getBoundingClientRect().right > limit,
      )
      .map((el) => `${el.tagName.toLowerCase()}.${el.className}`)
      .slice(0, 5);
  });

for (const width of WIDTHS) {
  test.describe(`at ${width}px`, () => {
    test.use({ viewport: { width, height: 900 } });

    for (const path of TABS) {
      test(`${path} fits the viewport`, async ({ page }) => {
        await stubApi(page);
        await page.goto(path);
        await expect(page.locator(".tabs")).toBeVisible();

        expect(await pageOverflow(page)).toBe(0);
        expect(await clippedOutside(page)).toEqual([]);
      });
    }
  });
}

test.describe("the section header", () => {
  test.use({ viewport: { width: 360, height: 900 } });

  test("keeps its width instead of being squeezed by the controls beside it", async ({
    page,
  }) => {
    // The exact regression: `.controls` cannot shrink, so without a flex-basis
    // on the heading block every missing pixel came out of the text and the
    // hint rendered about two words wide.
    await stubApi(page);
    await page.goto("/holdings");

    const hint = await page.locator(".card .hint").first().boundingBox();
    const card = await page.locator(".card").first().boundingBox();
    expect(hint).not.toBeNull();
    expect(card).not.toBeNull();

    // Most of the card's inner width, rather than a magic number: the point is
    // that the text is not competing with the buttons for the same row.
    expect(hint!.width).toBeGreaterThan(card!.width * 0.75);
  });

  test("drops the controls onto their own row rather than beside the text", async ({
    page,
  }) => {
    await stubApi(page);
    await page.goto("/holdings");

    const hint = await page.locator(".card .hint").first().boundingBox();
    const controls = await page.locator(".controls").first().boundingBox();

    // Below the hint's *bottom*, not merely its top. The row is centred, so a
    // squeezed hint is a tall narrow column whose top sits above the controls
    // anyway — asserting `controls.y > hint.y` passed even with the bug
    // present, which is how this assertion was caught being worthless.
    expect(controls!.y).toBeGreaterThanOrEqual(hint!.y + hint!.height);
  });
});

test.describe("the stat tiles", () => {
  test.use({ viewport: { width: 360, height: 900 } });

  test("fit two to a row on the narrowest phone", async ({ page }) => {
    // At a 160px minimum they needed 332px and a 360px phone has 320, so all
    // four stacked and pushed every bit of content below the fold.
    await stubApi(page);
    await page.goto("/");

    const tiles = page.locator(".tile");
    await expect(tiles).toHaveCount(4);
    const first = await tiles.nth(0).boundingBox();
    const second = await tiles.nth(1).boundingBox();
    expect(second!.y).toBe(first!.y);
  });
});

test.describe("the holdings panels", () => {
  test.use({ viewport: { width: 1280, height: 900 } });

  test("render one per holding, and say so when a holding has no price", async ({
    page,
  }) => {
    await stubApi(page);
    await page.goto("/holdings");

    await expect(page.locator(".trend")).toHaveCount(6);
    // NVDA has no bars in the fixture. A panel with nothing to plot must say
    // that rather than draw a flat line, which reads as "it did not move".
    await expect(page.locator(".trend", { hasText: "NVDA" })).toContainText(
      "No closes in this window",
    );
  });

  test("the range picker narrows the series without reloading", async ({ page }) => {
    await stubApi(page);
    await page.goto("/holdings");

    await page.getByRole("button", { name: "1W", exact: true }).click();
    await expect(page.getByRole("button", { name: "1W", exact: true })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(await pageOverflow(page)).toBe(0);
  });
});

test.describe("navigation", () => {
  test("each tab is reachable by URL and marks itself current", async ({ page }) => {
    await stubApi(page);
    for (const [path, label] of [
      ["/", "Overview"],
      ["/holdings", "Holdings"],
      ["/activity", "Activity"],
      ["/review", "Review"],
      ["/compare", "Compare"],
    ] as const) {
      await page.goto(path);
      await expect(page.locator(".tab.current")).toHaveText(label);
    }
  });
});

test.describe("account toggle", () => {
  test("switches the selected account and remembers the choice", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await expect(page.getByRole("button", { name: "static-100" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await page.getByRole("button", { name: "dynamic-500" }).click();
    await expect(page.getByRole("button", { name: "dynamic-500" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(await pageOverflow(page)).toBe(0);

    await page.reload();
    await expect(page.getByRole("button", { name: "dynamic-500" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

test.describe("display currency", () => {
  test("converts account values to GBP and remembers the choice", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await expect(page.getByText("$102,480.00", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "£ GBP" }).click();
    await expect(page.getByText("£76,477.61", { exact: true })).toBeVisible();
    await expect(page.getByText(/Trading and accounting remain in USD/)).toBeVisible();

    await page.reload();
    await expect(page.getByRole("button", { name: "£ GBP" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByText("£76,477.61", { exact: true })).toBeVisible();
  });
});
