#!/usr/bin/env node
/** Real-provider lifecycle dogfood: issue tracker through the browser.
 * Env: API=http://127.0.0.1:8793 UI=http://127.0.0.1:5174 SHOTS=/tmp/..
 * Creates the project via UI, generates a real plan, starts execution,
 * polls to a terminal state, and dumps evidence. Human gates (if any)
 * are reported, not auto-resolved.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const AUTH_TOKEN = (() => {
  try { return fs.readFileSync(path.join(__dirname, "..", "backend", ".orchestrator", "auth_token"), "utf8").trim(); }
  catch { return null; }
})();

const API = process.env.API || "http://127.0.0.1:8793";
const UI = process.env.UI || "http://127.0.0.1:5174";
const SHOTS = process.env.SHOTS || "/tmp/gg-dogfood/shots";
const BUDGET_MS = parseInt(process.env.BUDGET_MS || "5400000", 10); // 90 min

const IDEA = `Local issue-tracking web application for a single user.
Core journeys: create an issue (title, description, status), view the issue
list, view one issue, edit it, close/reopen it, delete it, filter by status
and search by text. Data must persist across restarts (local file/SQLite).
Include automated tests covering CRUD, validation, filtering, and persistence.
Local-first: no accounts, no external services, no credentials needed.`;
const CONSTRAINTS = `Keep the scope tiny: at most 4 implementation phases, minimal
dependencies, one install step and one test command. Prefer Node.js with
SQLite (better-sqlite3 or node:sqlite) or Python stdlib+sqlite3. No Docker,
no cloud, no paid services, no frontend framework build step unless trivial.`;

async function api(p, opts = {}, ms = 30000) {
  const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
  try {
    if (AUTH_TOKEN) opts = { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${AUTH_TOKEN}` } };
    const r = await fetch(`${API}${p}`, { ...opts, signal: c.signal });
    if (!r.ok) throw new Error(`${r.status} ${p}: ${(await r.text()).slice(0, 300)}`);
    return r.json();
  } finally { clearTimeout(t); }
}
async function shot(page, n) {
  const f = path.join(SHOTS, `${n}.png`);
  await page.screenshot({ path: f, fullPage: true });
  console.log("shot:", f, new Date().toISOString());
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  await api("/api/health");
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 900 });
  const out = {};
  try {
    await page.goto(`${UI}/#/lifecycle`);
    await page.waitForSelector('[data-testid="lifecycle-name"]', { timeout: 30000 });
    await page.fill('[data-testid="lifecycle-name"]', "Issue Tracker");
    await page.fill('[data-testid="lifecycle-idea"]', IDEA);
    await page.fill('[data-testid="lifecycle-constraints"]', CONSTRAINTS);
    await page.click('[data-testid="lifecycle-create"]');
    await page.waitForSelector('[data-testid="lifecycle-plan"]', { timeout: 30000 });
    out.projectId = page.url().match(/lifecycle\/([0-9a-f]+)/)[1];
    console.log("project:", out.projectId);

    await page.click('[data-testid="lifecycle-plan"]');
    await page.waitForSelector('text=Requirements', { timeout: 600000 });
    await page.waitForTimeout(2000);
    await shot(page, "dogfood-plan");
    const proj = await api(`/api/product-projects/${out.projectId}`);
    out.planRevision = proj.plan_revision;
    out.phases = proj.plan.phases.map((p) => p.key);
    out.stack = proj.plan.architecture;
    console.log("plan rev", out.planRevision, "phases:", out.phases.join(","));
    fs.writeFileSync(path.join(SHOTS, "plan.json"), JSON.stringify(proj.plan, null, 2));

    await page.click('[data-testid="lifecycle-start"]');
    await page.waitForTimeout(3000);
    await page.click('[data-testid="tab-execution"]');
    await shot(page, "dogfood-execution-start");

    const deadline = Date.now() + BUDGET_MS;
    let final = null, shots = 0;
    for (;;) {
      final = await api(`/api/product-projects/${out.projectId}`);
      if (["DELIVERED", "FAILED", "CANCELLED", "BLOCKED", "WAITING_FOR_HUMAN"].includes(final.state)) {
        const active = final.phases.some((p) => p.status === "RUNNING");
        if (!active) break;
      }
      if (Date.now() > deadline) break;
      await new Promise((r) => setTimeout(r, 30000));
      if (++shots % 4 === 0) {
        await shot(page, `dogfood-progress-${shots}`);
        console.log(new Date().toISOString(), final.state, final.phases.map((p) => `${p.phase_key}=${p.status}`).join(","));
      }
    }
    out.state = final.state;
    out.phaseStatus = final.phases.map((p) => `${p.phase_key}=${p.status}@${p.mission_id || "none"}`);
    out.openGates = final.gates.filter((g) => g.status === "open").map((g) => g.title);
    out.sha = final.delivery_sha;
    console.log("FINAL:", out.state, "|", out.phaseStatus.join(" | "), "| gates:", out.openGates.join("; ") || "none");
    await shot(page, "dogfood-final");
    fs.writeFileSync(path.join(SHOTS, "final.json"), JSON.stringify({ out, final }, null, 2));
    console.log(JSON.stringify(out));
  } finally {
    await browser.close();
  }
}

main().catch((e) => { console.error("DOGFOOD FAILED:", e.message); process.exit(1); });
