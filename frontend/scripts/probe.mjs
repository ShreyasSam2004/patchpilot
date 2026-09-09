import { chromium } from "playwright";
const base = "http://127.0.0.1:3000";
const routes = process.argv.slice(2).map(r => (r === "root" ? "/" : "/" + r));
const browser = await chromium.launch();
for (const path of routes) {
  const page = await browser.newPage();
  const errors = [], bad = [];
  page.on("pageerror", e => errors.push(e.stack || e.message));
  page.on("response", r => { if (r.status() >= 400) bad.push(`${r.status()} ${r.url().replace(base, "")}`); });
  await page.goto(base + path, { waitUntil: "networkidle" }).catch(() => {});
  await page.waitForTimeout(2000);
  const body = (await page.innerText("body").catch(() => "")).slice(0, 200).replace(/\s+/g, " ");
  const crashed = body.includes("Application error");
  console.log(`${crashed ? "CRASH" : "ok   "}  ${path}`);
  for (const b of bad) console.log("        HTTP " + b);
  for (const e of errors) console.log("        ERR  " + e.split("\n").slice(0, 3).join(" | "));
  await page.close();
}
await browser.close();
