import { chromium } from "playwright";
const b = await chromium.launch();
const p = await b.newPage();
const t0 = Date.now();
await p.goto("http://127.0.0.1:3000/runs/new", { waitUntil: "networkidle" });
console.log(`  page ready in ${Date.now() - t0}ms`);
await p.waitForTimeout(800);

const box = p.locator("textarea");
const prefilled = (await box.inputValue()).slice(0, 45);
console.log(`  fixture tab   : "${prefilled}..." (${(await box.inputValue()).length} chars)`);

await p.getByRole("button", { name: "Repository URL" }).click();
await p.waitForTimeout(300);
const afterSwitch = await box.inputValue();
console.log(`  after switch  : ${afterSwitch === "" ? "EMPTY ✓" : `STILL FILLED ✗ (${afterSwitch.length} chars)`}`);

await box.fill("My own bug report about a thing.");
await p.getByRole("button", { name: "Built-in fixture" }).click();
await p.waitForTimeout(300);
const kept = await box.inputValue();
console.log(`  user text kept: ${kept === "My own bug report about a thing." ? "YES ✓" : `NO ✗ ("${kept.slice(0,30)}")`}`);
await b.close();
