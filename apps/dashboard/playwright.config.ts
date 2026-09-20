/* Playwright against the *built* app, not the dev server.
 *
 * `vite preview` serves `dist`, which is what the nginx image ships. A dev
 * server would test a bundle nobody deploys — and the CSS that broke on a
 * phone is emitted identically either way, so testing the real artefact costs
 * nothing extra beyond the build.
 *
 * Chromium only. These assert box geometry against the viewport, which is not
 * where engines disagree, and three browsers would triple the CI download for
 * no additional signal.
 */

import { defineConfig, devices } from "@playwright/test";

const PORT = 4173;

export default defineConfig({
  testDir: "./tests",
  // Geometry assertions are deterministic; a retry would only ever hide a
  // genuine flake in the fixtures or the server startup.
  retries: 0,
  fullyParallel: true,
  // Fails the run rather than passing it, if a .only is left behind.
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // `--strictPort` so a busy port fails loudly instead of serving the suite
    // from somewhere baseURL does not point.
    command: `npm run build && npx vite preview --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
