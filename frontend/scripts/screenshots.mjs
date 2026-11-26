/**
 * Capture README screenshots from the running application.
 *
 * Development utility. Requires the backend on :8000 and the frontend on :3000.
 *
 *   node scripts/screenshots.mjs
 */

import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const BASE = process.env.PATCHPILOT_WEB_URL ?? "http://127.0.0.1:3000";
const API = process.env.PATCHPILOT_API_URL ?? "http://127.0.0.1:8000";
const OUT = "../docs/screenshots";

mkdirSync(OUT, { recursive: true });

async function shoot(page, name, path, prepare) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  if (prepare) await prepare(page);
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  console.log(`  ${name}.png  ${path}`);
}

const runs = await fetch(`${API}/api/runs?limit=20`).then((r) => r.json());
// Prefer the run that best demonstrates the loop: resolved, and with the most
// test runs (i.e. it reacted to a failure and tried again).
const resolved =
  runs.items
    .filter((r) => r.state === "RESOLVED")
    .sort((a, b) => (b.metrics?.test_runs ?? 0) - (a.metrics?.test_runs ?? 0))[0] ??
  runs.items[0];
if (!resolved) {
  console.error("No runs found. Seed some first:");
  console.error("  cd backend && python scripts/seed_demo_runs.py");
  process.exit(1);
}

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1560, height: 1000 },
  deviceScaleFactor: 2,
  colorScheme: "dark",
});

console.log("Capturing:");
await shoot(page, "01-landing", "/");
await shoot(page, "02-new-run", "/runs/new");
await shoot(page, "03-run-workspace", `/runs/${resolved.id}`);
await shoot(page, "04-run-trace", `/runs/${resolved.id}`, async (p) => {
  await p.getByRole("tab", { name: /agent trace/i }).click();
});
await shoot(page, "05-run-patch", `/runs/${resolved.id}`, async (p) => {
  await p.getByRole("tab", { name: /^patch/i }).click();
});
await shoot(page, "06-run-tests", `/runs/${resolved.id}`, async (p) => {
  await p.getByRole("tab", { name: /^tests/i }).click();
});
await shoot(page, "07-runs-list", "/runs");
await shoot(page, "08-results", "/results");
await shoot(page, "09-settings", "/settings");

await browser.close();
console.log(`\nWrote screenshots to ${OUT}`);
