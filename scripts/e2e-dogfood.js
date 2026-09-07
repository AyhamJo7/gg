#!/usr/bin/env node
/**
 * Phase 2B completion dogfood: manual + auto-planned missions through the UI.
 */
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const REPO = "/tmp/gg-ui-dogfood-" + Date.now();
const DB = "/tmp/gg-ui-dogfood.db";
const SCREENSHOTS = "/tmp/gg-ui-dogfood-shots";
const BASE = "/home/adam/projects/gg";

function waitForServer(url, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = () => {
      fetch(url).then((r) => { if (r.ok) resolve(); else setTimeout(check, 500); }).catch(() => {
        if (Date.now() - start > timeout) reject(new Error("timeout"));
        else setTimeout(check, 500);
      });
    };
    check();
  });
}

async function setupRepo() {
  fs.mkdirSync(REPO, { recursive: true });
  fs.writeFileSync(path.join(REPO, ".gitignore"), ".venv/\n__pycache__/\n*.pyc\n");
  fs.mkdirSync(path.join(REPO, "src"), { recursive: true });
  fs.mkdirSync(path.join(REPO, "tests"), { recursive: true });
  fs.writeFileSync(path.join(REPO, "pyproject.toml"), `[project]\nname = "dogfood"\nversion = "0.1.0"\nrequires-python = ">=3.10"\ndependencies = ["pytest"]\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n`);
  fs.writeFileSync(path.join(REPO, "src/__init__.py"), "");
  fs.writeFileSync(path.join(REPO, "tests/test_dogfood.py"), `from src.greeting import greet\nfrom src.farewell import farewell\n\ndef test_greet() -> None:\n    assert greet() == "hello"\n\ndef test_farewell() -> None:\n    assert farewell() == "goodbye"\n`);
  const { execSync } = require("child_process");
  execSync("git init && git config user.email 'dogfood@gg.local' && git config user.name 'Dogfood' && git add . && git commit -m 'initial'", { cwd: REPO });
  execSync("uv sync", { cwd: REPO });
  execSync("git add uv.lock && git commit -m 'add uv.lock'", { cwd: REPO });
}

async function takeScreenshot(page, name) {
  const p = path.join(SCREENSHOTS, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  console.log("Screenshot:", p);
}

async function getMissionState(missionId) {
  const r = await fetch(`http://127.0.0.1:8787/api/missions/${missionId}`);
  return r.json();
}

async function main() {
  fs.mkdirSync(SCREENSHOTS, { recursive: true });
  await setupRepo();
  fs.rmSync(DB, { force: true });

  // Start backend
  const backend = spawn("uv", ["run", "--directory", `${BASE}/backend`, "python", "-m", "orchestrator.server"], {
    env: { ...process.env, PYTHONPATH: `${BASE}/backend/src` },
    stdio: "pipe",
  });

  // Start frontend
  const frontend = spawn("npm", ["run", "dev", "--", "--port", "5173"], {
    cwd: `${BASE}/frontend`,
    stdio: "pipe",
  });

  try {
    await waitForServer("http://127.0.0.1:8787/api/health");
    await waitForServer("http://127.0.0.1:5173");

    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });

    // === 1. Create project via UI ===
    console.log("\n=== Creating project via UI ===");
    await page.goto("http://127.0.0.1:5173/#/projects");
    await page.waitForTimeout(1000);
    await page.fill('input[placeholder*="path"]', REPO);
    await page.click('button:has-text("Add Project")');
    await page.waitForTimeout(1500);
    await takeScreenshot(page, "01-project-created");

    // Get project ID from API
    const projects = await (await fetch("http://127.0.0.1:8787/api/projects")).json();
    const project = projects.find((p) => p.path === REPO);
    if (!project) throw new Error("Project not created");
    console.log("Project ID:", project.id);

    // === 2. Manual DAG mission ===
    console.log("\n=== Creating manual DAG mission ===");
    await page.goto("http://127.0.0.1:5173/#/new");
    await page.waitForTimeout(1000);

    await page.selectOption('select[data-testid="project-select"]', project.id);
    await page.fill('input[placeholder="Build invoice management SaaS"]', "Manual DAG Mission");
    await page.fill('textarea[data-testid="task-input"]', "Create greeting and farewell modules in parallel");
    await page.selectOption('select[data-testid="scheduling-mode"]', "PARALLEL_SAFE");
    await page.click('text=Manual DAG');
    await page.waitForTimeout(500);

    // Fill task 1
    const tasks = await page.locator('.card:has-text("Task 1")').all();
    await tasks[0].locator('input').first().fill("task-a");
    await tasks[0].locator('input').nth(1).fill("Create greeting");
    await tasks[0].locator('input').nth(2).fill("Create src/greeting.py with greet()");
    await tasks[0].locator('input').nth(4).fill("agy");
    await tasks[0].locator('input').nth(5).fill("src/greeting.py");

    // Fill task 2
    const tasks2 = await page.locator('.card:has-text("Task 2")').all();
    await tasks2[0].locator('input').first().fill("task-b");
    await tasks2[0].locator('input').nth(1).fill("Create farewell");
    await tasks2[0].locator('input').nth(2).fill("Create src/farewell.py with farewell()");
    await tasks2[0].locator('input').nth(4).fill("opencode");
    await tasks2[0].locator('input').nth(5).fill("src/farewell.py");

    await takeScreenshot(page, "02-manual-dag-filled");
    await page.click('button[data-testid="launch-mission"]');
    await page.waitForURL(/#\/$/, { timeout: 10000 });
    await page.waitForTimeout(2000);
    await takeScreenshot(page, "03-manual-mission-started");

    // Wait for manual mission to complete (or fail)
    let manualMissionId = null;
    for (let i = 0; i < 40; i++) {
      await page.waitForTimeout(5000);
      await page.reload();
      await page.waitForTimeout(1000);
      await takeScreenshot(page, `04-manual-${String(i).padStart(2, "0")}`);

      const missions = await (await fetch("http://127.0.0.1:8787/api/missions")).json();
      const manual = missions.find((m) => m.title === "Manual DAG Mission");
      if (manual) {
        manualMissionId = manual.id;
        if (["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED", "WAITING_FOR_HUMAN"].includes(manual.status)) {
          console.log("Manual mission final:", manual.status);
          break;
        }
      }
    }

    // === 3. Auto-planned mission ===
    console.log("\n=== Creating auto-planned mission ===");
    await page.goto("http://127.0.0.1:5173/#/new");
    await page.waitForTimeout(1000);

    await page.selectOption('select[data-testid="project-select"]', project.id);
    await page.fill('input[placeholder="Build invoice management SaaS"]', "Auto-Plan Mission");
    await page.fill('textarea[data-testid="task-input"]', "Create greeting and farewell modules");
    await page.selectOption('select[data-testid="scheduling-mode"]', "PARALLEL_SAFE");
    // Auto-plan is default
    await takeScreenshot(page, "05-auto-plan-filled");
    await page.click('button[data-testid="launch-mission"]');
    await page.waitForURL(/#\/$/, { timeout: 10000 });
    await page.waitForTimeout(2000);
    await takeScreenshot(page, "06-auto-plan-started");

    let autoMissionId = null;
    for (let i = 0; i < 40; i++) {
      await page.waitForTimeout(5000);
      await page.reload();
      await page.waitForTimeout(1000);
      await takeScreenshot(page, `07-auto-${String(i).padStart(2, "0")}`);

      const missions = await (await fetch("http://127.0.0.1:8787/api/missions")).json();
      const auto = missions.find((m) => m.title === "Auto-Plan Mission");
      if (auto) {
        autoMissionId = auto.id;
        if (["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED", "WAITING_FOR_HUMAN"].includes(auto.status)) {
          console.log("Auto mission final:", auto.status);
          break;
        }
      }
    }

    // Final state
    const manualState = manualMissionId ? await getMissionState(manualMissionId) : null;
    const autoState = autoMissionId ? await getMissionState(autoMissionId) : null;

    console.log("\n=== RESULTS ===");
    console.log("Manual mission:", manualMissionId, manualState?.status, manualState?.blocking_issue);
    console.log("Auto mission:", autoMissionId, autoState?.status, autoState?.blocking_issue);

    fs.writeFileSync(path.join(SCREENSHOTS, "results.json"), JSON.stringify({
      repo: REPO,
      manualMissionId,
      manualStatus: manualState?.status,
      manualBlockingIssue: manualState?.blocking_issue,
      autoMissionId,
      autoStatus: autoState?.status,
      autoBlockingIssue: autoState?.blocking_issue,
      screenshots: fs.readdirSync(SCREENSHOTS).filter((f) => f.endsWith(".png")),
    }, null, 2));

    await browser.close();
  } finally {
    backend.kill();
    frontend.kill();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
