#!/usr/bin/env node
/**
 * Phase 2B completion dogfood: manual + auto-planned missions through the UI.
 * Reuses already-running backend (:8787) and frontend (:5173). Never spawns
 * or kills servers. All API calls are timeout-bounded; results.json is always
 * written. Real browser UI drives project + mission creation.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const API = "http://127.0.0.1:8787";
const UI = "http://127.0.0.1:5173";
const REPO = process.env.REPO_PATH || process.argv[2];
const RUN_TAG = process.env.RUN_TAG || String(Date.now());
const SCREENSHOTS = process.env.SHOTS || "/tmp/gg-phase2b-shots";
const MANUAL_TITLE = `Manual DAG ${RUN_TAG}`;
const AUTO_TITLE = `Auto-Plan ${RUN_TAG}`;
const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED", "WAITING_FOR_HUMAN"];

if (!REPO) {
  console.error("Usage: REPO_PATH=/tmp/repo node scripts/e2e-dogfood.js");
  process.exit(2);
}

async function api(p, opts = {}, timeoutMs = 15000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${API}${p}`, { ...opts, signal: ctl.signal });
    if (!r.ok) throw new Error(`${r.status} ${p}: ${(await r.text()).slice(0, 200)}`);
    return r.json();
  } finally {
    clearTimeout(t);
  }
}

async function takeScreenshot(page, name) {
  const p = path.join(SCREENSHOTS, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  console.log("shot:", p);
}

async function newestByTitle(title) {
  const missions = await api("/api/missions");
  const matches = missions.filter((m) => m.title === title)
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  return matches[0] || null;
}

async function pollMission(page, id, label, maxPolls = 90, pollMs = 10000) {
  for (let i = 0; i < maxPolls; i++) {
    await page.waitForTimeout(pollMs);
    let m = null;
    try {
      m = await api(`/api/missions/${id}`);
    } catch (e) {
      console.log(`${label} poll ${i}: api error: ${e.message}`);
      continue;
    }
    if (i % 6 === 0 || TERMINAL.includes(m.status)) {
      try {
        await page.reload();
        await page.waitForTimeout(1500);
        await takeScreenshot(page, `${label}-${String(i).padStart(2, "0")}-${m.status}`);
      } catch (e) {
        console.log(`${label} screenshot failed: ${e.message}`);
      }
    }
    console.log(`${label} poll ${i}: ${m.status}`);
    if (TERMINAL.includes(m.status)) return m;
  }
  return api(`/api/missions/${id}`).catch((e) => ({ status: `POLL_ERROR: ${e.message}` }));
}

async function main() {
  fs.mkdirSync(SCREENSHOTS, { recursive: true });
  await api("/api/health");
  console.log("backend ok; repo:", REPO, "tag:", RUN_TAG);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 900 });
  const out = { repo: REPO, runTag: RUN_TAG };

  try {
    // === 1. Project via UI ===
    console.log("\n=== project via UI ===");
    await page.goto(`${UI}/#/projects`);
    await page.waitForTimeout(1200);
    await page.fill('input[placeholder*="path"]', REPO);
    await page.click('button:has-text("Add Project")');
    await page.waitForTimeout(1500);
    await takeScreenshot(page, "01-project-created");
    const projects = await api("/api/projects");
    const project = projects.find((p) => p.path === REPO);
    if (!project) throw new Error("project not created via UI");
    console.log("project:", project.id);

    // === 2. Manual DAG mission via UI ===
    console.log("\n=== manual DAG via UI ===");
    await page.goto(`${UI}/#/new`);
    await page.waitForTimeout(1200);
    await page.selectOption('select[data-testid="project-select"]', project.id);
    await page.fill('input[placeholder="Build invoice management SaaS"]', MANUAL_TITLE);
    await page.fill('textarea[data-testid="task-input"]', "Create greeting and farewell modules in parallel");
    await page.selectOption('select[data-testid="scheduling-mode"]', "PARALLEL_SAFE");
    await page.click("text=Manual DAG");
    await page.waitForTimeout(600);

    const task1 = page.locator('[data-testid="dag-task-0"]');
    await task1.locator("input").nth(0).fill("task-a");
    await task1.locator("input").nth(1).fill("Create greeting");
    await task1.locator("input").nth(2).fill("Create src/greeting.py with greet() greeting function");
    await task1.locator("input").nth(4).fill("agy");
    await task1.locator("input").nth(5).fill("src/greeting.py");
    const task2 = page.locator('[data-testid="dag-task-1"]');
    await task2.locator("input").nth(0).fill("task-b");
    await task2.locator("input").nth(1).fill("Create farewell");
    await task2.locator("input").nth(2).fill("Create src/farewell.py with farewell() farewell function");
    await task2.locator("input").nth(4).fill("opencode");
    await task2.locator("input").nth(5).fill("src/farewell.py");
    await takeScreenshot(page, "02-manual-dag-filled");
    await page.click('button[data-testid="launch-mission"]');
    await page.waitForURL(/#\/$/, { timeout: 15000 });
    await page.waitForTimeout(2500);
    await takeScreenshot(page, "03-manual-mission-started");

    const manual = await newestByTitle(MANUAL_TITLE);
    if (!manual) throw new Error("manual mission not found after UI launch");
    out.manualMissionId = manual.id;
    console.log("manual mission:", manual.id);
    const manualFinal = await pollMission(page, manual.id, "04-manual");
    out.manualStatus = manualFinal.status;
    out.manualBlockingIssue = manualFinal.blocking_issue || null;
    try {
      out.manualDetail = await api(`/api/missions/${manual.id}`);
    } catch (e) {
      out.manualDetailError = e.message;
    }

    // Per-task log panel evidence: click DAG node for the first task (dynamic id)
    try {
      const detail = await api(`/api/missions/${manual.id}`);
      const firstTaskId = detail.tasks?.[0]?.id;
      out.manualFirstTaskId = firstTaskId || null;
      await page.reload();
      await page.waitForTimeout(1500);
      if (firstTaskId) {
        const node = page.locator(`[data-testid="dag-node-${firstTaskId}"]`);
        if (await node.count()) {
          await node.first().click();
          await page.waitForTimeout(1500);
          await takeScreenshot(page, "05-manual-task-logs");
          out.taskLogShot = true;
        }
        try {
          out.taskALogs = await api(`/api/missions/${manual.id}/tasks/${firstTaskId}/logs`);
          out.taskALogStdoutChars = (out.taskALogs.stdout || "").length;
          out.taskALogProvider = out.taskALogs.run?.provider || null;
        } catch (e) {
          out.taskALogsError = e.message;
        }
      }
    } catch (e) {
      out.taskLogShotError = e.message;
    }

    // === 3. Auto-planned mission via UI ===
    console.log("\n=== auto-plan via UI ===");
    await page.goto(`${UI}/#/new`);
    await page.waitForTimeout(1200);
    await page.selectOption('select[data-testid="project-select"]', project.id);
    await page.fill('input[placeholder="Build invoice management SaaS"]', AUTO_TITLE);
    await page.fill('textarea[data-testid="task-input"]', "Create greeting and farewell modules");
    await page.selectOption('select[data-testid="scheduling-mode"]', "PARALLEL_SAFE");
    await takeScreenshot(page, "06-auto-plan-filled");
    await page.click('button[data-testid="launch-mission"]');
    await page.waitForURL(/#\/$/, { timeout: 15000 });
    await page.waitForTimeout(2500);
    await takeScreenshot(page, "07-auto-plan-started");

    const auto = await newestByTitle(AUTO_TITLE);
    if (!auto) throw new Error("auto mission not found after UI launch");
    out.autoMissionId = auto.id;
    console.log("auto mission:", auto.id);
    const autoFinal = await pollMission(page, auto.id, "08-auto");
    out.autoStatus = autoFinal.status;
    out.autoBlockingIssue = autoFinal.blocking_issue || null;
    try {
      out.autoDetail = await api(`/api/missions/${auto.id}`);
    } catch (e) {
      out.autoDetailError = e.message;
    }

    console.log("\n=== RESULTS ===");
    console.log("manual:", out.manualMissionId, out.manualStatus, out.manualBlockingIssue);
    console.log("auto:", out.autoMissionId, out.autoStatus, out.autoBlockingIssue);
  } catch (e) {
    out.scriptError = e.message;
    console.error("SCRIPT ERROR:", e);
  } finally {
    try {
      out.screenshots = fs.readdirSync(SCREENSHOTS).filter((f) => f.endsWith(".png"));
    } catch { /* ignore */ }
    fs.writeFileSync(path.join(SCREENSHOTS, "results.json"), JSON.stringify(out, null, 2));
    console.log("results.json written");
    await browser.close().catch(() => {});
  }
  if (out.scriptError) process.exit(1);
}

main();
