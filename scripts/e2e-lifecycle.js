#!/usr/bin/env node
/** Deterministic lifecycle browser E2E against the fake-provider backend.
 * Exercises: create project -> generate plan -> review -> start -> execute
 * multiple phases -> delivery. Gated variant covers the human-gate loop.
 * Env: API=http://127.0.0.1:8789 UI=http://127.0.0.1:5174 SHOTS=/tmp/..
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const AUTH_TOKEN = (() => {
  try { return fs.readFileSync(path.join(__dirname, "..", "backend", ".orchestrator", "auth_token"), "utf8").trim(); }
  catch { return null; }
})();

const API = process.env.API || "http://127.0.0.1:8789";
const UI = process.env.UI || "http://127.0.0.1:5174";
const SHOTS = process.env.SHOTS || "/tmp/gg-lifecycle-shots";
const GATED = process.env.GATED === "1";

async function api(p, opts = {}, ms = 15000) {
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
  console.log("shot:", f);
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  await api("/api/health");
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 900 });
  const out = { gated: GATED };
  try {
    // 1. New project through the UI — never raw API for creation.
    await page.goto(`${UI}/#/lifecycle`);
    await page.waitForSelector('[data-testid="lifecycle-name"]', { timeout: 20000 });
    await shot(page, "01-list");
    await page.fill('[data-testid="lifecycle-name"]', GATED ? "E2E Gated" : "E2E Product");
    await page.fill('[data-testid="lifecycle-idea"]', "A local counter service with tests");
    await page.click('[data-testid="lifecycle-create"]');
    await page.waitForSelector('[data-testid="lifecycle-plan"]', { timeout: 20000 });
    const m = page.url().match(/lifecycle\/([0-9a-f]+)/);
    if (!m) throw new Error("did not land on detail page: " + page.url());
    out.projectId = m[1];
    console.log("project:", out.projectId);

    // 2. Generate plan in browser (first attempt malformed -> bounded repair
    //    when GG_E2E_MALFORMED_FIRST=1 on the backend).
    await page.click('[data-testid="lifecycle-plan"]');
    await page.waitForSelector('text=Test Product', { timeout: 60000 });
    await shot(page, "02-plan");

    // 3. Review roadmap tab.
    await page.click('[data-testid="tab-roadmap"]');
    await page.waitForTimeout(800);
    await shot(page, "03-roadmap");

    // 4. Start project in browser.
    await page.click('[data-testid="lifecycle-start"]');
    await page.waitForTimeout(1500);
    await page.click('[data-testid="tab-execution"]');
    await shot(page, "04-execution");

    if (GATED) {
      // 5a. Gate appears; independent foundation phase completes first.
      await page.click('[data-testid="tab-gates"]');
      await page.waitForSelector('[data-testid="gate-card"]', { timeout: 60000 });
      await shot(page, "05-gate");
      const proj = await api(`/api/product-projects/${out.projectId}`);
      const gate = proj.gates.find((g) => g.status === "open");
      if (!gate) throw new Error("no open gate");
      out.gateId = gate.id;
      // Provide the prerequisite like a human would (values stay in the repo).
      fs.appendFileSync(path.join(proj.target_repo_path, ".env"), "TEST_TOKEN=e2e-fake\n");
      await page.click('[data-testid="gate-resolve"]');
      await page.waitForTimeout(2000);
    }

    // 6. Wait for terminal state via API polling (UI keeps polling too).
    const deadline = Date.now() + 240000;
    let final = null;
    for (;;) {
      final = await api(`/api/product-projects/${out.projectId}`);
      if (["DELIVERED", "FAILED", "CANCELLED", "BLOCKED"].includes(final.state)) break;
      if (Date.now() > deadline) throw new Error("project did not settle: " + final.state);
      await new Promise((r) => setTimeout(r, 2000));
    }
    out.state = final.state;
    out.phases = final.phases.map((p) => `${p.phase_key}=${p.status}`);
    console.log("final:", out.state, out.phases.join(","));

    // 7. Delivery tab evidence in browser.
    await page.click('[data-testid="tab-delivery"]');
    await page.waitForTimeout(1000);
    await shot(page, "06-delivery");
    if (final.state === "DELIVERED") {
      await page.waitForSelector('[data-testid="delivery-sha"]', { timeout: 15000 });
      out.sha = await page.textContent('[data-testid="delivery-sha"]');
    }
    if (final.state !== "DELIVERED") throw new Error("expected DELIVERED, got " + final.state);
    console.log(JSON.stringify(out));
  } finally {
    await browser.close();
  }
}

main().catch((e) => { console.error("E2E FAILED:", e.message); process.exit(1); });
