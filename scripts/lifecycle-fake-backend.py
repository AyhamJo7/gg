#!/usr/bin/env python3
"""Test backend with deterministic fake providers for lifecycle browser E2E.

Usage: lifecycle-fake-backend.py PORT DB_PATH PRODUCTS_ROOT [gated]
Serves the full GG API with FakeAdapters + PlanProvider (no quota).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

import uvicorn

from orchestrator.api.app import create_app
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter, PlanProvider, default_test_plan, gated_test_plan


class E2eWorker(FakeAdapter):
    """Deterministic stand-in for implementation work in acceptance E2E.

    Behaves like the `work` script, but when the phase prompt contains the
    repair trigger phrase it additionally performs the scripted repair:
    writing the strict-mode marker plus a regression test. This models a
    repair engineer changing repo state; the criterion probe then
    objectively passes. Test scaffolding only.
    """

    executable = "true"
    TRIGGER = "fix-validation"

    def build_command(self, request):  # type: ignore[no-untyped-def]
        return ["true"]

    async def execute(self, request, on_output):  # type: ignore[no-untyped-def]
        from pathlib import Path as _Path

        if request.role == "implementation":
            marker = request.workdir / "agent_work.txt"
            marker.write_text(f"{self.name} {request.role} call={self.calls}\n")
            on_output(f"[{self.name}] wrote {marker.name}")
            if self.TRIGGER in request.prompt.lower():
                strict = request.workdir / ".strict"
                strict.write_text("strict validation enabled by repair phase\n")
                tests = request.workdir / "tests"
                tests.mkdir(parents=True, exist_ok=True)
                (tests / "regression.test.js").write_text(
                    "const { test } = require('node:test');\n"
                    "const assert = require('node:assert/strict');\n"
                    "const fs = require('node:fs');\n"
                    "const path = require('node:path');\n"
                    "test('repair enabled strict validation', () => {\n"
                    "  assert.equal(fs.existsSync(path.join(__dirname, '..', '.strict')), true);\n"
                    "});\n"
                )
                on_output(f"[{self.name}] applied scripted repair (strict mode + regression test)")
            self.calls += 1
            from orchestrator.models import FailureClass as _FC
            from orchestrator.models import ProviderState as _PS
            from orchestrator.providers.base import ExecutionResult as _ER

            return _ER(
                state=_PS.COMPLETED, failure_class=_FC.NONE, exit_code=0, duration_s=0.01,
                summary="e2e work", stdout_path=_Path(request.log_dir / f"{request.run_id}.stdout.log"),
                stderr_path=_Path(request.log_dir / f"{request.run_id}.stderr.log"),
                raw_tail="e2e work", assistant_text="e2e work",
            )
        return await super().execute(request, on_output)


SEED_SERVER_JS = """const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const PORT = Number(process.env.PORT || 4100);
const STRICT_FILE = path.join(__dirname, '.strict');
let nextId = 1;
const issues = [];
const server = http.createServer((req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(obj));
  };
  if (req.method === 'POST' && req.url === '/api/issues') {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      let title = '';
      try { title = JSON.parse(body).title; } catch { return send(400, { error: 'bad json' }); }
      if (fs.existsSync(STRICT_FILE)) {
        if (typeof title !== 'string' || title.trim() === '') return send(400, { error: 'title required' });
        title = title.trim();
      }
      const issue = { id: nextId++, title };
      issues.push(issue);
      return send(201, { data: issue });
    });
    return;
  }
  if (req.method === 'GET' && req.url === '/api/issues') return send(200, { data: issues });
  return send(404, { error: 'not found' });
});
server.listen(PORT, '127.0.0.1', () => {
  console.log('SEED-SERVER-PORT:' + server.address().port);
});
"""

SEED_PROBE_JS = """(async () => {
  const { spawn } = await import('node:child_process');
  const path = await import('node:path');
  const root = path.dirname(new URL(import.meta.url).pathname);
  const child = spawn('node', [path.join(root, '..', 'server.js')], {
    env: { ...process.env, PORT: '0' }, stdio: ['ignore', 'pipe', 'inherit'],
  });
  let port = 0;
  let out = '';
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not report port')), 10000);
    child.stdout.on('data', (c) => {
      out += String(c);
      const m = out.match(/SEED-SERVER-PORT:(\\d+)/);
      if (m) { clearTimeout(timer); port = Number(m[1]); resolve(); }
    });
    child.on('exit', (code) => reject(new Error('server exited ' + code)));
  }).catch((e) => { console.error(String(e)); child.kill(); process.exit(2); });
  const get = (b) => fetch(`http://127.0.0.1:${port}/api/issues`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(b), signal: AbortSignal.timeout(8000),
  });
  let code = 0;
  try {
    const r = await get({ title: '   ' });
    console.log(`POST spaces-only -> ${r.status}, want 400`);
    if (r.status !== 400) code = 1;
    const ok = await get({ title: 'Real' });
    console.log(`POST valid -> ${ok.status}, want 201`);
    if (ok.status !== 201) code = 1;
    console.log(code === 0 ? 'WHITESPACE-PROBE: PASS' : 'WHITESPACE-PROBE: FAIL');
    child.kill();
    await new Promise((r) => { child.on('exit', r); setTimeout(r, 3000); });
    process.exit(code);
  } finally {
    child.kill();
  }
})().catch((e) => { console.error(e); process.exit(2); });
"""

SEED_TEST_JS = """const { test } = require('node:test');
const assert = require('node:assert/strict');
test('seed suite passes', () => {
  assert.equal(1 + 1, 2);
});
"""

SEED_PACKAGE_JSON = """{"name": "e2e-target", "scripts": {"test": "node --test \\"tests/*.test.js\\""}}"""


def _seed_buggy_app(repo: Path) -> None:
    (repo / "server.js").write_text(SEED_SERVER_JS)
    checks = repo / "checks"
    checks.mkdir(exist_ok=True)
    (checks / "whitespace-probe.js").write_text(SEED_PROBE_JS)
    tests = repo / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "seed.test.js").write_text(SEED_TEST_JS)
    (repo / "package.json").write_text(SEED_PACKAGE_JSON)


def _criterion_plan() -> dict:
    def req(rid: str, title: str, verify: str) -> dict:
        return {
            "id": rid, "title": title, "description": title, "kind": "functional",
            "acceptance": [{"id": f"{rid}-A1", "description": f"{title} verified", "verify": verify}],
        }

    return {
        "product_name": "E2E Validation Service",
        "goal": "prove failing criteria block delivery until repaired",
        "users": "e2e",
        "journeys": ["probe the endpoint"],
        "requirements": [req("R1", "Blank titles rejected", "node checks/whitespace-probe.js")],
        "non_functional": [],
        "assumptions": [],
        "out_of_scope": [],
        "risks": [],
        "architecture": {
            "frontend": "none", "backend": "node:http", "database": "in-memory",
            "auth": "none", "api_design": "POST /api/issues", "integrations": [],
            "deployment": "local", "testing_strategy": "node --test + probe",
            "security_notes": "input validation", "repo_structure": "flat",
            "dependency_strategy": "none",
            "decisions": [{"area": "runtime", "choice": "node stdlib", "rationale": "deterministic offline"}],
        },
        "phases": [
            {
                "key": "foundation", "title": "Foundation", "goal": "scaffold the service",
                "deliverables": ["server"], "tasks": ["scaffold server"],
                "depends_on": [], "workspace_scopes": ["backend"], "suggested_providers": [],
                "acceptance": [{"id": "foundation-A1", "description": "server exists", "verify": "npm test"}],
                "requirement_ids": ["R1"], "verify_commands": ["npm test"],
                "human_prerequisites": [], "effort": "S",
            }
        ],
        "external_prerequisites": [],
    }


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8789
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp()) / "e2e.db"
    products_root = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="gg-e2e-products-")
    gated = len(sys.argv) > 4 and sys.argv[4] == "gated"
    custom = os.environ.get("GG_E2E_CUSTOM_PLAN")
    seed_app = os.environ.get("GG_E2E_SEED_APP") == "1"
    if custom == "criterion":
        plan = _criterion_plan()
    else:
        plan = gated_test_plan() if gated else default_test_plan()
    script = ["malformed", "ok"] if os.environ.get("GG_E2E_MALFORMED_FIRST") else ["ok"]
    if custom == "criterion":
        adapters = {
            "fake-planner": PlanProvider("fake-planner", script, plan),
            "fake-a": E2eWorker("fake-a", ["work"]),
            "fake-b": FakeAdapter("fake-b", ["ok"]),
        }
    else:
        adapters = {
            "fake-planner": PlanProvider("fake-planner", script, plan),
            "fake-a": FakeAdapter("fake-a", ["work"]),
            "fake-b": FakeAdapter("fake-b", ["ok"]),
        }
    names = list(adapters.keys())
    data = {
        "providers": {n: {"enabled": True, "timeout_minutes": 1} for n in names},
        "orchestration": {
            "review_required": True,
            "max_repair_cycles": 1,
            "max_phase_attempts": 2,
            "cooldown_base_seconds": 0.05,
            "cooldown_multiplier": 1.0,
            "cooldown_max_seconds": 0.2,
            "scheduler_tick_seconds": 0.5,
            "max_provider_wait_seconds": 30,
        },
        "priority": {
            "planning": ["fake-planner"],
            "implementation": ["fake-a", "fake-b"],
            "testing": ["fake-a", "fake-b"],
            "review": ["fake-a", "fake-b"],
            "repair": ["fake-a", "fake-b"],
        },
        "lifecycle": {"workspace_root": products_root},
        "git": {"auto_checkpoint": True},
    }
    config = Config(data)
    db = Database(db_path)
    orch = Orchestrator(db, config, adapters)
    # Test scaffolding only: fake implementers write agent_work.txt but cannot
    # author a package manifest, so seed a minimal passing toolchain into each
    # provisioned target repo (what a real foundation phase would create).
    import json as _json

    _real_ensure = orch.coordinator.ensure_target_repo

    def _ensure_and_seed(project_id: str) -> dict:
        target = _real_ensure(project_id)
        if seed_app:
            _seed_buggy_app(Path(target["path"]))
            return target
        pkg = Path(target["path"]) / "package.json"
        if not pkg.exists():
            pkg.write_text(
                _json.dumps(
                    {
                        "name": "e2e-target",
                        "scripts": {
                            "test": "node -e \"process.exit(0)\"",
                            "build": "node -e \"process.exit(0)\"",
                        },
                    }
                )
            )
        return target

    orch.coordinator.ensure_target_repo = _ensure_and_seed  # type: ignore[method-assign]
    app = create_app(db_path, config, orch)
    print(f"fake lifecycle backend on {port} db={db_path} products={products_root}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
