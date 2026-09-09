#!/usr/bin/env node
/** Deterministic acceptance-integrity browser E2E (fake providers).
 * Backend needs GG_E2E_CUSTOM_PLAN=criterion + GG_E2E_SEED_APP=1.
 * Flow: create -> plan -> start -> criterion FAILS -> BLOCKED (no DELIVERED)
 * -> add repair phase via UI plan edit -> repair runs -> re-tested -> DELIVERED.
 * Env: API, UI, SHOTS.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const API = process.env.API || "http://127.0.0.1:8795";
const UI = process.env.UI || "http://127.0.0.1:5174";
const SHOTS = process.env.SHOTS || "/tmp/gg-accept-e2e/shots";

async function api(p, opts = {}, ms = 15000) {
  const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
  try {
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
async function waitState(pid, want, timeoutMs = 180000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const p = await api(`/api/product-projects/${pid}`);
    if (want.includes(p.state)) {
      const active = p.phases.some((ph) => ph.status === "RUNNING");
      if (!active || want.includes("RUNNING")) return p;
    }
    if (Date.now() > deadline) throw new Error(`state never in ${want}: ${p.state}`);
    await new Promise((r) => setTimeout(r, 2000));
  }
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
    await page.fill('[data-testid="lifecycle-name"]', "E2E Acceptance");
    await page.fill('[data-testid="lifecycle-idea"]', "validation service with executable checks");
    await page.click('[data-testid="lifecycle-create"]');
    await page.waitForSelector('[data-testid="lifecycle-plan"]', { timeout: 30000 });
    out.projectId = page.url().match(/lifecycle\/([0-9a-f]+)/)[1];
    console.log("project:", out.projectId);

    await page.click('[data-testid="lifecycle-plan"]');
    await page.waitForSelector('text=E2E Validation Service', { timeout: 60000 });
    await shot(page, "acc-01-plan");

    await page.click('[data-testid="lifecycle-start"]');
    // Criterion fails on the buggy seed: project must BLOCK, never DELIVER.
    let proj = await waitState(out.projectId, ["BLOCKED"]);
    out.blockedReason = proj.blocking_reason;
    console.log("blocked as required:", out.blockedReason);
    if ((proj.blocking_reason || "").includes("DELIVERED")) throw new Error("bad reason");
    await page.click('[data-testid="tab-delivery"]');
    await page.waitForTimeout(1500);
    await shot(page, "acc-02-blocked");
    if (await page.locator('[data-testid="delivery-sha"]').count()) {
      throw new Error("delivery SHA present while blocked — false DELIVERED");
    }

    // Add the repair phase through the UI plan editor (auditable revision).
    const current = await api(`/api/product-projects/${out.projectId}`);
    const plan = current.plan;
    plan.phases.push({
      key: "fix-validation",
      title: "Fix validation",
      goal: "enable strict blank-title rejection",
      deliverables: ["strict mode"],
      tasks: ["repair validation"],
      depends_on: ["foundation"],
      workspace_scopes: ["backend"],
      suggested_providers: [],
      acceptance: [{ id: "fix-validation-A1", description: "probe passes", verify: "npm test" }],
      requirement_ids: ["R1"],
      verify_commands: ["npm test"],
      human_prerequisites: [],
      effort: "S",
    });
    await page.click('[data-testid="tab-plan"]');
    await page.waitForTimeout(1000);
    await page.click('button:has-text("Edit JSON")');
    await page.fill('[data-testid="plan-json"]', JSON.stringify(plan, null, 2));
    await page.fill('[data-testid="plan-reason"]', "add repair phase after criterion failure");
    await page.click('[data-testid="plan-save"]');
    await page.waitForTimeout(2000);
    const revised = await api(`/api/product-projects/${out.projectId}`);
    if (revised.plan_revision !== 2) throw new Error("revision not persisted, got " + revised.plan_revision);
    console.log("revision 2 recorded");

    // Repair runs, criterion re-tested, fresh checkout, DELIVERED.
    proj = await waitState(out.projectId, ["DELIVERED"], 240000);
    out.sha = proj.delivery_sha;
    console.log("DELIVERED:", out.sha, proj.phases.map((p) => `${p.phase_key}=${p.status}`).join(","));
    const results = await api(`/api/product-projects/${out.projectId}`);
    out.criteria = results.criterion_results.map((c) => `${c.criterion_id}=${c.status}@${c.exit_code}`);
    console.log("criteria:", out.criteria.join(","));
    await page.click('[data-testid="tab-delivery"]');
    await page.waitForSelector('[data-testid="delivery-sha"]', { timeout: 30000 });
    await page.waitForTimeout(1500);
    await shot(page, "acc-03-delivered");
    const shaText = await page.textContent('[data-testid="delivery-sha"]');
    if ((shaText || "").trim() !== out.sha) throw new Error("SHA mismatch");
    console.log(JSON.stringify(out));
  } finally {
    await browser.close();
  }
}
main().catch((e) => { console.error("E2E FAILED:", e.message); process.exit(1); });
