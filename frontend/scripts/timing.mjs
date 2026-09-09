import { chromium } from "playwright";
const b = await chromium.launch();
const routes = ["/", "/runs", "/runs/new", "/benchmarks", "/settings"];
for (const pass of ["cold", "warm"]) {
  console.log(`  --- ${pass} ---`);
  for (const r of routes) {
    const p = await b.newPage();
    const t = Date.now();
    await p.goto("http://127.0.0.1:3000" + r, { waitUntil: "domcontentloaded" });
    const dom = Date.now() - t;
    await p.waitForSelector("h1, table, section", { timeout: 15000 }).catch(() => {});
    console.log(`    ${r.padEnd(12)} dom ${String(dom).padStart(5)}ms   content ${String(Date.now()-t).padStart(5)}ms`);
    await p.close();
  }
}
await b.close();
