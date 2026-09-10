#!/usr/bin/env node
/** Poll a product project to a terminal state with periodic screenshots.
 * Env: API, UI, SHOTS, PROJECT, BUDGET_MS (default 120 min).
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const API = process.env.API || "http://127.0.0.1:8793";
const UI = process.env.UI || "http://127.0.0.1:5174";
const SHOTS = process.env.SHOTS || "/tmp/gg-dogfood/shots";
const PROJECT = process.env.PROJECT;
const BUDGET_MS = parseInt(process.env.BUDGET_MS || "7200000", 10);
if (!PROJECT) { console.error("need PROJECT"); process.exit(2); }

const AUTH_TOKEN = (() => {
  try { return fs.readFileSync(path.join(__dirname, "..", "backend", ".orchestrator", "auth_token"), "utf8").trim(); }
  catch { return null; }
})();

async function api(p, ms = 30000) {
  const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
  try {
    const headers = AUTH_TOKEN ? { Authorization: `Bearer ${AUTH_TOKEN}` } : {};
    const r = await fetch(`${API}${p}`, { headers, signal: c.signal });
    if (!r.ok) throw new Error(`${r.status} ${p}`);
    return r.json();
  } finally { clearTimeout(t); }
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 900 });
  try {
    await page.goto(`${UI}/#/lifecycle/${PROJECT}`);
    await page.waitForTimeout(3000);
    const deadline = Date.now() + BUDGET_MS;
    let n = 0, final = null;
    for (;;) {
      final = await api(`/api/product-projects/${PROJECT}`);
      const line = `${new Date().toISOString()} ${final.state} ` +
        final.phases.map((p) => `${p.phase_key}=${p.status}`).join(",");
      const gates = final.gates.filter((g) => g.status === "open").map((g) => g.title).join(";");
      console.log(line + (gates ? ` GATES: ${gates}` : ""));
      const active = final.phases.some((p) => p.status === "RUNNING");
      if (["DELIVERED", "FAILED", "CANCELLED", "BLOCKED", "WAITING_FOR_HUMAN"].includes(final.state) && !active) break;
      if (Date.now() > deadline) { console.log("BUDGET EXCEEDED"); break; }
      await new Promise((r) => setTimeout(r, 60000));
      if (++n % 5 === 0) {
        await page.reload();
        await page.waitForTimeout(3000);
        await page.screenshot({ path: path.join(SHOTS, `monitor-${n}.png`), fullPage: true });
      }
    }
    await page.reload();
    await page.waitForTimeout(3000);
    await page.click('[data-testid="tab-delivery"]');
    await page.waitForTimeout(2000);
    await page.screenshot({ path: path.join(SHOTS, "monitor-final.png"), fullPage: true });
    fs.writeFileSync(path.join(SHOTS, "monitor-final.json"), JSON.stringify(final, null, 2));
    console.log("MONITOR DONE:", final.state);
  } finally {
    await browser.close();
  }
}
main().catch((e) => { console.error("MONITOR FAILED:", e.message); process.exit(1); });
