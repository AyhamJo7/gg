/** Browser regression: actual Vite UI + isolated lifecycle-fake-backend.py.
 * Rare states use explicit API fixtures, never fabricated production evidence.
 * GG_REVIEW_TOKEN_FILE must name the disposable backend token, NOT a live DB.
 * Screenshots stay outside Git. No real provider is configured by this script.
 */
import { createRequire } from 'node:module';
import { readFileSync, mkdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const tokenFile = process.env.GG_REVIEW_TOKEN_FILE;
if (!tokenFile || !tokenFile.startsWith('/tmp/gg-operator-review-')) throw new Error('Use an isolated review token under /tmp/gg-operator-review-*');
const token = readFileSync(tokenFile, 'utf8').trim();
const shots = process.env.GG_REVIEW_SHOTS || tokenFile.replace(/\/auth_token$/, '/shots');
mkdirSync(shots, { recursive: true });
const base = process.env.GG_REVIEW_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
// Test-only out-of-band token delivery through the existing desktop auth interface.
await context.addInitScript(token => { window.__TAURI_INTERNALS__ = { invoke: async () => token }; }, token);
const page = await context.newPage();
page.on('dialog', dialog => dialog.accept());
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const results = [];
const accessibility = [];
async function capture(name) {
  await page.screenshot({ path: `${shots}/${name}.png`, fullPage: true });
  if (process.env.GG_REVIEW_AXE) {
    await page.addScriptTag({ path: process.env.GG_REVIEW_AXE });
    const report = await page.evaluate(async () => window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } }));
    accessibility.push({ name, violations: report.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target) })) });
  }
}
async function open(hash) { await page.goto(`${base}/#${hash}`); await page.waitForTimeout(350); }
try {
  const health = await page.request.get('http://127.0.0.1:8787/api/providers', { headers: { Authorization: `Bearer ${token}` } });
  const providers = await health.json();
  assert(providers.length && providers.every(p => p.name.startsWith('fake-')), 'Refusing real-provider runtime');
  await open('/');
  await page.getByRole('heading', { name: 'Overview', exact: true }).waitFor();
  await capture('overview-live-1440');
  await page.getByRole('link', { name: 'Build a product', exact: true }).first().click();
  await page.getByLabel('Project name', { exact: true }).fill('Operator browser regression');
  await page.getByLabel('Product idea').fill('A small team issue tracker with explicit acceptance checks.');
  await page.getByTestId('lifecycle-create').click();
  await page.getByTestId('lifecycle-plan').waitFor();
  const liveId = page.url().split('/lifecycle/')[1].split('?')[0];
  await page.getByTestId('lifecycle-plan').click();
  await page.getByTestId('lifecycle-start').waitFor({ timeout: 30000 });
  await page.getByTestId('tab-plan').click();
  await capture('plan-live');
  await page.getByTestId('lifecycle-start').click();
  await page.getByTestId('tab-overview').click();
  await page.getByRole('button', { name: 'Review required actions' }).waitFor({ timeout: 30000 });
  await page.getByRole('button', { name: 'Pause project' }).click();
  await page.getByRole('button', { name: 'Resume project' }).waitFor({ timeout: 10000 });
  await page.getByRole('button', { name: 'Resume project' }).click();
  await page.getByRole('button', { name: 'Pause project' }).waitFor({ timeout: 10000 });
  await page.getByRole('button', { name: 'Review required actions' }).click();
  await capture('human-gates-live');
  await page.getByTestId('tab-execution').click();
  const missionLink = page.getByRole('link', { name: 'open in Mission Control' }).first();
  const target = await missionLink.getAttribute('href');
  await missionLink.click();
  assert(page.url().endsWith(target));
  await page.getByTestId('adopt-changes').click();
  await page.getByText(/then resolve the gate to continue/).waitFor();
  await page.getByRole('button', { name: /Adopted\/committed\/cleaned/ }).click();
  await page.waitForTimeout(3500);
  await capture('mission-live');
  await open(`/lifecycle/${liveId}?tab=activity`);
  await page.getByRole('button', { name: 'Inspect run' }).first().waitFor();
  await page.getByRole('button', { name: 'Inspect run' }).first().click();
  await page.getByRole('button', { name: 'View Context' }).click();
  await page.getByTestId('run-context').waitFor();
  await capture('run-context-live');
  results.push('LIVE: create → plan → start → Human Gate → pause/resume → exact mission link → explicit adoption → continue → run/context');

  await open(`/lifecycle/${liveId}`);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('heading', { name: 'Cancelled', exact: true }).waitFor({ timeout: 10000 });
  results.push('LIVE: confirmed product cancellation preserves history');

  const response = await page.request.get(`http://127.0.0.1:8787/api/product-projects/${liveId}`, { headers: { Authorization: `Bearer ${token}` } });
  const live = await response.json();
  const now = new Date().toISOString();
  const sha = 'a'.repeat(40), repairedSha = 'b'.repeat(40);
  let scenario = 'REPAIRING';
  const project = () => ({ ...live, id: 'ui-fixture', name: 'UI fixture — validation service', state: scenario === 'DELIVERED' ? 'DELIVERED' : scenario === 'HUMAN' ? 'WAITING_FOR_HUMAN' : 'BLOCKED',
    paused: 0, blocking_reason: scenario === 'EXHAUSTED' ? 'Repair attempt budget exhausted' : scenario === 'REPAIRING' ? 'Acceptance failed; autonomous repair is active' : null,
    phases: live.phases.map(p => ({ ...p, status: 'COMPLETED' })),
    gates: scenario === 'HUMAN' ? [{ ...live.gates[0], id: 'gate-fixture', title: 'Configure service credential', gate_type: 'secret', status: 'open', required_vars: ['SERVICE_TOKEN'], what_required: 'A service credential', why_required: 'GG cannot create an external account for you', human_action: 'Configure SERVICE_TOKEN in your editor', after_resolve: 'GG validates variable names and resumes', mission_gate_id: null }] : [],
    delivery_sha: scenario === 'DELIVERED' ? repairedSha : null,
    delivery_report: scenario === 'DELIVERED' ? { product_name: 'UI fixture', git_sha: repairedSha, repo_path: '/tmp/ui-fixture', requirements: [], run_instructions: 'Fixture only; no generated app is certified by this browser test.' } : {},
  });
  const repair = () => ({ id: 'cycle-fixture', project_id: 'ui-fixture', trigger_type: 'CRITERION_FAILED', trigger_evidence_id: 'criterion-fixture', trigger_sha: sha,
    target_requirement_id: 'R1', target_criterion_id: 'R1-A1', target_finding_id: null, classification: 'IMPLEMENTATION_DEFECT', status: scenario === 'DELIVERED' ? 'SUCCEEDED' : scenario,
    max_attempts: 2, attempts_used: scenario === 'EXHAUSTED' ? 2 : 1, stop_reason: scenario === 'EXHAUSTED' ? 'attempt budget exhausted' : null,
    gate_hint: null, created_at: now, completed_at: null,
    attempts: [{ id: 'attempt-fixture', attempt_number: 1, provider: 'opencode', base_sha: sha, result_sha: ['SUCCEEDED', 'DELIVERED', 'REVIEWING', 'RECHECKING'].includes(scenario) ? repairedSha : null,
      outcome: scenario, review_reviewer: ['SUCCEEDED', 'DELIVERED'].includes(scenario) ? 'codex' : null, review_outcome: 'passed', recheck_attempt_id: 'check-fixture', recheck_outcome: ['SUCCEEDED', 'DELIVERED'].includes(scenario) ? 'passed' : null }],
  });
  const evidence = () => ({ candidate_sha: repairedSha, plan_revision: 1, writers: [{ actor_type: 'PROVIDER', provider: 'opencode', result_sha: repairedSha }], writers_complete: true,
    review: { state: scenario === 'STALE' ? 'STALE' : scenario === 'DELIVERED' ? 'VALID' : 'MISSING', phases: [] },
    verification: { state: scenario === 'DELIVERED' ? 'VALID' : 'MISSING' }, fresh_checkout: { state: scenario === 'DELIVERED' ? 'VALID' : 'MISSING' },
    criteria: { total: 1, passed: scenario === 'DELIVERED' ? 1 : 0, missing: 0, failed: scenario === 'DELIVERED' ? 0 : 1, stale: scenario === 'STALE' ? 1 : 0, waived: 0 },
    delivery_ready: scenario === 'DELIVERED', blocking_reasons: scenario === 'DELIVERED' ? [] : ['Whitespace-only title must be rejected'], phase_attempts: [],
  });
  await page.route('**/api/product-projects/ui-fixture**', async route => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith('/repair-cycles') ? { cycles: scenario === 'HUMAN' ? [] : [repair()], stats: {} } : path.endsWith('/evidence') ? evidence() : project();
    await route.fulfill({ json: body });
  });
  for (const state of ['REPAIRING', 'EXHAUSTED', 'SUCCEEDED', 'WAITING_FOR_PROVIDER', 'HUMAN', 'STALE', 'DELIVERED']) {
    scenario = state;
    await open(`/lifecycle/ui-fixture?tab=${state === 'DELIVERED' ? 'delivery' : 'overview'}`);
    await page.getByRole('heading', { name: 'UI fixture — validation service' }).waitFor();
    if (state !== 'HUMAN' && state !== 'DELIVERED') await page.getByTestId('repair-panel').waitFor();
    if (state === 'HUMAN') { await page.getByRole('button', { name: 'Review required actions' }).click(); await page.getByText('GG cannot create an external account for you').waitFor(); }
    if (state === 'DELIVERED') await page.getByTestId('delivery-sha').waitFor();
    await capture(`fixture-${state.toLowerCase()}-1440`);
    results.push(`FIXTURE: ${state}`);
  }
  scenario = 'REPAIRING'; await open('/lifecycle/ui-fixture');
  await page.getByText('Repairing', { exact: true }).waitFor();
  scenario = 'SUCCEEDED';
  await page.getByText('Repair passed', { exact: true }).waitFor({ timeout: 9000 });
  results.push('FIXTURE: repair updates without navigation/reload');
  scenario = 'DELIVERED'; await page.setViewportSize({ width: 1920, height: 1080 });
  await open('/lifecycle/ui-fixture?tab=delivery'); await page.getByTestId('delivery-sha').waitFor();
  await capture('fixture-delivered-1920');
  await page.getByRole('button', { name: /Light mode/ }).click();
  await capture('fixture-delivered-light-1920');
  await page.getByRole('button', { name: /Dark mode/ }).click();

  const tasks = ['API', 'Frontend', 'Integration'].map((title, i) => ({ id: `task-${i}`, mission_id: 'mission-fixture', role: 'implementation', title,
    status: i === 0 ? 'COMPLETED' : i === 1 ? 'RUNNING' : 'BLOCKED', description: 'UI fixture task', assigned_provider: i === 1 ? 'opencode' : 'codex',
    workspace_scope: '[]', preferred_providers: '[]', summary: '', attempts: 1, max_attempts: 2, input_sha: sha, result_sha: i === 0 ? repairedSha : null,
    blocking_issue: i === 2 ? 'Dependency merge conflict: resolve branches and retry the task. No provider launched.' : null, created_at: now }));
  const mission = { id: 'mission-fixture', project_id: 'repo-fixture', title: 'UI fixture — parallel build', task: 'Build independent modules and integrate.', status: 'IMPLEMENTING', current_phase: 'implementation', current_provider: 'opencode', scheduling_mode: 'PARALLEL_SAFE', created_at: now, finished_at: null };
  await page.route('**/api/missions', route => route.fulfill({ json: [mission] }));
  await page.route('**/api/missions/mission-fixture', route => route.fulfill({ json: { ...mission, tasks, gates: [], findings: [{ id: 'finding', severity: 'HIGH', category: 'correctness', description: 'Reject whitespace titles', recommended_fix: 'Trim before validation', status: 'open' }], integrations: [], runs: [], latest_review: null, latest_handoff: null } }));
  await page.route('**/api/missions/mission-fixture/dag', route => route.fulfill({ json: { tasks, dependencies: [{ from_task_id: 'task-0', to_task_id: 'task-2' }, { from_task_id: 'task-1', to_task_id: 'task-2' }], branches: [], reservations: [], locks: [] } }));
  await page.route('**/api/projects/repo-fixture/git', route => route.fulfill({ json: { is_repo: false, modified: [], added: [], deleted: [], untracked: [], recent_commits: [] } }));
  await page.route('**/api/missions/mission-fixture/tasks/*/logs*', route => route.fulfill({ json: { task_id: 'task-1', runs: [], note: 'Fixture: no raw provider output' } }));
  await open('/missions?mission=mission-fixture');
  await page.getByRole('button', { name: 'Frontend: RUNNING' }).waitFor();
  await page.getByRole('button', { name: 'Frontend: RUNNING' }).focus();
  await page.keyboard.press('Enter');
  assert.equal(await page.getByRole('button', { name: 'Frontend: RUNNING' }).getAttribute('aria-pressed'), 'true');
  await capture('fixture-dag-1920');
  tasks[1].status = 'STALE';
  await page.getByRole('button', { name: 'Frontend: STALE' }).waitFor({ timeout: 8000 });
  await page.getByRole('button', { name: 'Frontend: STALE' }).click();
  results.push('FIXTURE: parallel DAG, keyboard selection, conflict, stale task, findings, task logs');
  await open('/new'); await capture('new-mission-1920');
  await open('/providers'); await capture('providers-1920');
  await page.route('**/api/providers', route => route.fulfill({ json: providers.map((p, i) => ({ ...p, state: i ? 'COOLDOWN' : 'UNAVAILABLE', last_error: i ? 'Observed rate limit; reset time not reported' : 'Executable unavailable', cooldown_until: i ? new Date(Date.now() + 60000).toISOString() : null })) }));
  await page.getByText('UNAVAILABLE', { exact: true }).waitFor({ timeout: 8000 });
  await capture('fixture-provider-unavailable-1920');
  results.push('FIXTURE: unavailable provider and locally observed cooldown; no quota percentage');
  await open('/'); await capture('overview-1920');
  await open('/projects'); await capture('repositories-1920');
  await open('/priority'); await capture('priority-1920');
  await open('/analytics'); await capture('analytics-1920');
  assert.deepEqual(errors, [], 'Browser exceptions');
  assert(accessibility.every(result => result.violations.length === 0), JSON.stringify(accessibility.filter(result => result.violations.length)));
  console.log(JSON.stringify({ results, browserErrors: errors, accessibility, screenshots: shots }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${shots}/failure.png`, fullPage: true });
  console.error(JSON.stringify({ url: page.url(), browserErrors: errors, body: await page.locator('body').innerText() }));
  throw error;
} finally { await browser.close(); }
