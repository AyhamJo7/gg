#!/usr/bin/env node
/** Scale dogfood: 6-task mixed DAG via UI, real providers, bounded window. */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const API = "http://127.0.0.1:8787";
const UI = "http://127.0.0.1:5173";
const REPO = process.env.REPO_PATH || process.argv[2];
const RUN_TAG = process.env.RUN_TAG || String(Date.now());
const SHOTS = process.env.SHOTS || "/tmp/gg-scale-shots";
const TITLE = `Scale textstats ${RUN_TAG}`;
const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED", "WAITING_FOR_HUMAN"];
if (!REPO) { console.error("need REPO_PATH"); process.exit(2); }

const RULES = "Type-annotated Python, ruff-clean (no unused imports, no print in library code). ";
const TASKS = [
  { id: "core-counter", title: "Counter module", role: "implementation", prov: "agy", scope: "src/textstats/counter.py",
    desc: RULES + "Create src/textstats/counter.py with count_chars(text)->int, count_words(text)->int, count_lines(text)->int." },
  { id: "core-tokens", title: "Tokenizer module", role: "implementation", prov: "opencode", scope: "src/textstats/tokens.py",
    desc: RULES + "Create src/textstats/tokens.py with tokenize(text)->list[str] (lowercase alphanumeric words)." },
  { id: "core-stats", title: "Stats helpers", role: "implementation", prov: "agy", scope: "src/textstats/stats.py",
    desc: RULES + "Create src/textstats/stats.py with mean(xs)->float, median(xs)->float raising ValueError on empty." },
  { id: "core-report", title: "Report builder", role: "implementation", prov: "opencode", scope: "src/textstats/report.py",
    desc: RULES + "Create src/textstats/report.py with summary(text)->dict using counter, tokens, stats modules (from textstats.counter import ...)." },
  { id: "core-cli", title: "CLI entrypoint", role: "implementation", prov: "agy", scope: "src/textstats/cli.py",
    desc: RULES + "Create src/textstats/cli.py with main(argv)->int reading a file path argument and printing the report as JSON." },
  { id: "test-suite", title: "Pytest suite", role: "testing", prov: "opencode", scope: "tests/test_textstats.py",
    desc: RULES + "Create tests/test_textstats.py with pytest tests covering counter, tokens, stats, report, cli (import from src.textstats... with pythonpath set so use 'from textstats...')." },
];
const DEPS = [
  ["core-counter", "core-report"], ["core-tokens", "core-report"], ["core-stats", "core-report"],
  ["core-report", "core-cli"],
  ["core-counter", "test-suite"], ["core-tokens", "test-suite"], ["core-stats", "test-suite"],
  ["core-report", "test-suite"], ["core-cli", "test-suite"],
];

async function api(p, opts = {}, ms = 15000) {
  const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
  try {
    const r = await fetch(`${API}${p}`, { ...opts, signal: c.signal });
    if (!r.ok) throw new Error(`${r.status} ${p}: ${(await r.text()).slice(0, 200)}`);
    return r.json();
  } finally { clearTimeout(t); }
}
async function shot(page, n) { const p = path.join(SHOTS, `${n}.png`); await page.screenshot({ path: p, fullPage: true }); console.log("shot:", p); }

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  await api("/api/health");
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 900 });
  const out = { repo: REPO, runTag: RUN_TAG, title: TITLE };
  try {
    await page.goto(`${UI}/#/projects`); await page.waitForTimeout(1200);
    await page.fill('input[placeholder*="path"]', REPO);
    await page.click('button:has-text("Add Project")'); await page.waitForTimeout(1500);
    const project = (await api("/api/projects")).find((p) => p.path === REPO);
    if (!project) throw new Error("no project"); out.projectId = project.id;

    await page.goto(`${UI}/#/new`); await page.waitForTimeout(1200);
    await page.selectOption('select[data-testid="project-select"]', project.id);
    await page.fill('input[placeholder="Build invoice management SaaS"]', TITLE);
    await page.fill('textarea[data-testid="task-input"]', "Build textstats package: counter, tokens, stats, report, cli, tests");
    await page.selectOption('select[data-testid="scheduling-mode"]', "PARALLEL_SAFE");
    await page.click("text=Manual DAG"); await page.waitForTimeout(600);
    for (let k = 0; k < 4; k++) { await page.click("text=+ Add Task"); await page.waitForTimeout(300); }
    for (let i = 0; i < TASKS.length; i++) {
      const t = page.locator(`[data-testid="dag-task-${i}"]`);
      await t.locator("input").nth(0).fill(TASKS[i].id);
      await t.locator("input").nth(1).fill(TASKS[i].title);
      await t.locator("input").nth(2).fill(TASKS[i].desc);
      await t.locator("select").selectOption(TASKS[i].role);
      await t.locator("input").nth(4).fill(TASKS[i].prov);
      await t.locator("input").nth(5).fill(TASKS[i].scope);
    }
    for (const [a, b] of DEPS) {
      await page.evaluate(([x, y]) => {
        const labels = [...document.querySelectorAll("label")];
        const lab = labels.find((l) => l.textContent.replace(/\s+/g, " ").trim() === `${x} → ${y}`);
        if (!lab) throw new Error(`dep checkbox missing ${x}->${y}`);
        lab.querySelector("input").click();
      }, [a, b]);
    }
    await shot(page, "01-dag");
    await page.click('button[data-testid="launch-mission"]');
    await page.waitForURL(/#\/$/, { timeout: 20000 }); await page.waitForTimeout(2500);
    const mid = (await api("/api/missions")).filter((m) => m.title === TITLE)
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0]?.id;
    if (!mid) throw new Error("mission missing"); out.missionId = mid;
    await page.selectOption('select[data-testid="mission-select"]', mid);

    let maxConc = 0, concAt = null, multiShot = false;
    const timeline = [];
    for (let i = 0; i < 150; i++) {
      await page.waitForTimeout(15000);
      let d; try { d = await api(`/api/missions/${mid}`); }
      catch (e) { console.log(`poll ${i} api err`); continue; }
      const running = (d.tasks || []).filter((t) => t.status === "RUNNING").length;
      if (running > maxConc) { maxConc = running; concAt = new Date().toISOString(); }
      if (running >= 2 && !multiShot) {
        multiShot = true;
        await page.reload(); await page.waitForTimeout(1500);
        await page.selectOption('select[data-testid="mission-select"]', mid);
        await page.waitForTimeout(1000);
        await shot(page, "02-concurrent");
      }
      if (i % 8 === 0 || TERMINAL.includes(d.status)) {
        try { await page.reload(); await page.waitForTimeout(1500);
          await page.selectOption('select[data-testid="mission-select"]', mid);
          await shot(page, `03-poll-${String(i).padStart(3, "0")}-${d.status}`); } catch {}
      }
      const counts = {};
      for (const t of d.tasks || []) counts[t.status] = (counts[t.status] || 0) + 1;
      timeline.push({ i, status: d.status, running, counts });
      console.log(`poll ${i}: ${d.status} running=${running} ${JSON.stringify(counts)}`);
      if (TERMINAL.includes(d.status)) break;
    }
    out.maxConcurrent = maxConc; out.maxConcurrentAt = concAt; out.timeline = timeline;
    const fin = await api(`/api/missions/${mid}`);
    out.finalStatus = fin.status; out.blocking = fin.blocking_issue || null;
    out.tasks = (fin.tasks || []).map((t) => ({ id: t.id, status: t.status, provider: t.assigned_provider, attempts: t.attempts }));
    out.runs = (fin.runs || []).map((r) => ({ provider: r.provider, role: r.role, fc: r.failure_class, exit: r.exit_code }));
    console.log("FINAL:", out.finalStatus, out.blocking);
  } catch (e) { out.scriptError = String((e && e.message) || e); console.error("SCRIPT ERROR:", e); }
  finally {
    try { out.screenshots = fs.readdirSync(SHOTS).filter((f) => f.endsWith(".png")); } catch {}
    fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(out, null, 2));
    await browser.close().catch(() => {});
  }
  if (out.scriptError) process.exit(1);
}
main();
