#!/usr/bin/env node
/**
 * Phase 2C dogfood: full parallel mission + injected merge conflict via UI.
 * Reuses running backend (:8787) and frontend (:5173). Never spawns/kills.
 * Part 1: manual Parallel Safe mission -> tasks -> live logs -> COMPLETED.
 * Part 2: same-scope mission -> genuine MERGE_CONFLICT -> ConflictCard ->
 *         git resolve (operator path) -> Resume button -> COMPLETED.
 */
const { chromium } = require("playwright");
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const API = "http://127.0.0.1:8787";
const UI = "http://127.0.0.1:5173";
const REPO = process.env.REPO_PATH || process.argv[2];
const RUN_TAG = process.env.RUN_TAG || String(Date.now());
const SCREENSHOTS = process.env.SHOTS || "/tmp/gg-phase2c-shots";
const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED", "WAITING_FOR_HUMAN"];

if (!REPO) { console.error("Usage: REPO_PATH=/tmp/repo node scripts/e2e-2c-dogfood.js"); process.exit(2); }

async function api(p, opts = {}, timeoutMs = 15000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${API}${p}`, { ...opts, signal: ctl.signal });
    if (!r.ok) throw new Error(`${r.status} ${p}: ${(await r.text()).slice(0, 200)}`);
    return r.json();
  } finally { clearTimeout(t); }
}

async function shot(page, name) {
  const p = path.join(SCREENSHOTS, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  console.log("shot:", p);
}

async function newestByTitle(title) {
  const missions = await api("/api/missions");
  return missions.filter((m) => m.title === title)
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0] || null;
}

async function pollUntil(page, id, label, stop, maxPolls = 100, pollMs = 10000) {
  for (let i = 0; i < maxPolls; i++) {
    await page.waitForTimeout(pollMs);
    let m;
    try { m = await api(`/api/missions/${id}`); }
    catch (e) { console.log(`${label} poll ${i}: api error ${e.message}`); continue; }
    if (i % 6 === 0 || stop.includes(m.status)) {
      try {
        await page.reload(); await page.waitForTimeout(1500);
        await shot(page, `${label}-${String(i).padStart(2, "0")}-${m.status}`);
      } catch (e) { console.log(`${label} shot failed: ${e.message}`); }
    }
    console.log(`${label} poll ${i}: ${m.status}`);
    if (stop.includes(m.status)) return m;
  }
  return api(`/api/missions/${id}`).catch((e) => ({ status: `POLL_ERROR: ${e.message}` }));
}

async function selectMission(page, id) {
  await page.selectOption('select[data-testid="mission-select"]', id);
  await page.waitForTimeout(1500);
}

async function createMissionViaUI(page, projectId, title, taskText, dagTasks) {
  await page.goto(`${UI}/#/new`);
  await page.waitForTimeout(1200);
  await page.selectOption('select[data-testid="project-select"]', projectId);
  await page.fill('input[placeholder="Build invoice management SaaS"]', title);
  await page.fill('textarea[data-testid="task-input"]', taskText);
  await page.selectOption('select[data-testid="scheduling-mode"]', "PARALLEL_SAFE");
  await page.click("text=Manual DAG");
  await page.waitForTimeout(600);
  for (let i = 0; i < dagTasks.length; i++) {
    const t = page.locator(`[data-testid="dag-task-${i}"]`);
    await t.locator("input").nth(0).fill(dagTasks[i].id);
    await t.locator("input").nth(1).fill(dagTasks[i].title);
    await t.locator("input").nth(2).fill(dagTasks[i].desc);
    await t.locator("input").nth(4).fill(dagTasks[i].providers);
    await t.locator("input").nth(5).fill(dagTasks[i].scope);
  }
  await shot(page, `new-${title.replace(/\W+/g, "-")}`);
  await page.click('button[data-testid="launch-mission"]');
  await page.waitForURL(/#\/$/, { timeout: 20000 });
  await page.waitForTimeout(2500);
  const m = await newestByTitle(title);
  if (!m) throw new Error(`mission not found: ${title}`);
  return m;
}

async function main() {
  fs.mkdirSync(SCREENSHOTS, { recursive: true });
  await api("/api/health");
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 900 });
  const out = { repo: REPO, runTag: RUN_TAG };

  try {
    await page.goto(`${UI}/#/projects`);
    await page.waitForTimeout(1200);
    await page.fill('input[placeholder*="path"]', REPO);
    await page.click('button:has-text("Add Project")');
    await page.waitForTimeout(1500);
    await shot(page, "01-project");
    const project = (await api("/api/projects")).find((p) => p.path === REPO);
    if (!project) throw new Error("project not created");
    console.log("project:", project.id);

    // ---- Part 1: normal parallel mission ----
    const m1 = await createMissionViaUI(page, project.id, `2C Flow ${RUN_TAG}`,
      "Create greeting module and farewell module in parallel", [
        { id: "task-a", title: "Create greeting", desc: "Create src/greeting.py with greet() returning hello", providers: "agy", scope: "src/greeting.py" },
        { id: "task-b", title: "Create farewell", desc: "Create src/farewell.py with farewell() returning goodbye", providers: "opencode", scope: "src/farewell.py" },
      ]);
    out.flowMissionId = m1.id;
    await selectMission(page, m1.id);
    // open live log mid-run once a task is running
    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(10000);
      const d = await api(`/api/missions/${m1.id}`);
      const running = (d.tasks || []).find((t) => ["RUNNING", "CLAIMED"].includes(t.status));
      if (running) {
        await page.reload(); await page.waitForTimeout(1500);
        await selectMission(page, m1.id);
        const node = page.locator(`[data-testid="dag-node-${running.id}"]`);
        if (await node.count()) {
          await node.first().click(); await page.waitForTimeout(4000);
          await shot(page, "02-live-log");
          out.liveLogTask = running.id;
          const logs = await api(`/api/missions/${m1.id}/tasks/${running.id}/logs?tail_bytes=65536`);
          out.liveLogKeys = Object.keys(logs);
          break;
        }
      }
      const cur = await api(`/api/missions/${m1.id}`);
      if (TERMINAL.includes(cur.status)) break;
    }
    const m1f = await pollUntil(page, m1.id, "03-flow", TERMINAL);
    out.flowStatus = m1f.status; out.flowBlocking = m1f.blocking_issue || null;
    await selectMission(page, m1.id);
    await shot(page, "04-flow-final");

    // ---- Part 2: injected conflict mission ----
    const m2 = await createMissionViaUI(page, project.id, `2C Conflict ${RUN_TAG}`,
      "Edit shared.txt twice with different content", [
        { id: "side-a", title: "Side A", desc: "Set src/shared.txt first line to SHARED = alpha (keep file valid python)", providers: "agy", scope: "src/shared.txt" },
        { id: "side-b", title: "Side B", desc: "Set src/shared.txt first line to SHARED = beta (keep file valid python)", providers: "opencode", scope: "src/shared.txt" },
      ]);
    out.conflictMissionId = m2.id;
    const m2w = await pollUntil(page, m2.id, "05-conflict", ["WAITING_FOR_HUMAN", "COMPLETED", "FAILED"]);
    out.conflictWaitStatus = m2w.status;
    await selectMission(page, m2.id);
    await shot(page, "06-conflict-card");
    out.conflictCardVisible = await page.locator('[data-testid="conflict-card"]').count() > 0;

    if (m2w.status === "WAITING_FOR_HUMAN") {
      // operator resolution in the repo (same steps the card documents)
      const integ = (await api(`/api/missions/${m2.id}`)).integrations?.[0];
      const branches = JSON.parse(integ?.branch_names || "[]");
      const merged = execSync("git branch --merged HEAD --list", { cwd: REPO }).toString();
      for (const b of branches) {
        if (!b || merged.includes(b)) continue;
        const r = (() => { try { execSync(`git merge --no-commit --no-ff ${b}`, { cwd: REPO }); return 0; } catch { return 1; } })();
        if (r !== 0) {
          execSync("git checkout --theirs -- src/shared.txt", { cwd: REPO });
          execSync("git add src/shared.txt", { cwd: REPO });
        }
        execSync(`git commit -m "operator: resolve ${b}" --allow-empty`, { cwd: REPO });
      }
      out.resolvedFile = require("fs").readFileSync(`${REPO}/src/shared.txt`, "utf8").slice(0, 80);
      // resume through the UI button on the conflict card
      await page.reload(); await page.waitForTimeout(1500);
      await selectMission(page, m2.id);
      await page.click('[data-testid="conflict-resume"]');
      await page.waitForTimeout(3000);
      const m2f = await pollUntil(page, m2.id, "07-resumed", TERMINAL);
      out.conflictFinalStatus = m2f.status;
      await selectMission(page, m2.id);
      await shot(page, "08-conflict-final");
    }
    console.log("\n=== RESULTS ===");
    console.log("flow:", out.flowMissionId, out.flowStatus);
    console.log("conflict:", out.conflictMissionId, out.conflictWaitStatus, "->", out.conflictFinalStatus);
  } catch (e) {
    out.scriptError = String(e && e.message || e);
    console.error("SCRIPT ERROR:", e);
  } finally {
    try { out.screenshots = fs.readdirSync(SCREENSHOTS).filter((f) => f.endsWith(".png")); } catch {}
    fs.writeFileSync(path.join(SCREENSHOTS, "results.json"), JSON.stringify(out, null, 2));
    await browser.close().catch(() => {});
  }
  if (out.scriptError) process.exit(1);
}
main();
