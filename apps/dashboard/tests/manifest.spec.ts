/* The web app manifest that lets Android Chrome install the dashboard.
 *
 * Runs against `vite preview`, so it proves the built artefact ships the
 * manifest and every icon it names. Keeping them outside the passcode gate is
 * nginx's job (nginx/default.conf), which this server does not run.
 */

import { expect, test } from "@playwright/test";

type Icon = { src: string; sizes: string; purpose: string; type: string };

test("the manifest names Android icons that are all served", async ({ request }) => {
  const response = await request.get("/manifest.webmanifest");
  expect(response.ok()).toBe(true);
  const manifest = await response.json();
  expect(manifest.display).toBe("standalone");

  const icons: Icon[] = manifest.icons;
  // Android Chrome installs from a raster icon of at least 192px, and wants a
  // separate maskable one for the launcher's own shape.
  expect(icons.map((i) => `${i.sizes} ${i.purpose}`).sort()).toEqual([
    "192x192 any",
    "512x512 any",
    "512x512 maskable",
  ]);
  for (const icon of icons) {
    const png = await request.get(icon.src);
    expect(png.ok(), icon.src).toBe(true);
    expect(png.headers()["content-type"]).toBe("image/png");
  }
});

test("both pages link the manifest", async ({ request }) => {
  const link = '<link rel="manifest" href="/manifest.webmanifest" />';
  expect(await (await request.get("/")).text()).toContain(link);
  // login.html is nginx's 401 body, so it is the page a fresh install reads.
  expect(await (await request.get("/login.html")).text()).toContain(link);
});
