#!/usr/bin/env python3
"""Post-mission resource leak audit for GG Orchestrator.

Reads a mission database (never modifies it) and reports:
- non-terminal tasks, unreleased provider reservations / task locks
- providers stuck outside AVAILABLE
- task branches removed or with missing worktree dirs
- provider-run PIDs that are still alive (possible orphans)
- integration / review / verification record presence

Prints only counts, IDs, and paths — never log contents (secret-safe).

Usage: leak-audit.py /path/to/orchestrator.db [--mission ID] [--repo PATH]
Exit code 0 = clean, 1 = leaks/findings.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, OverflowError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="path to orchestrator.db (read-only)")
    ap.add_argument("--mission", default=None)
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()

    uri = f"file:{args.db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    findings: list[str] = []

    mfilter = "WHERE mission_id=?" if args.mission else ""
    mparams: tuple[str, ...] = (args.mission,) if args.mission else ()

    tfilter = "WHERE mission_id=? AND" if args.mission else "WHERE"
    rows = conn.execute(
        f"SELECT id, status FROM tasks {tfilter} UPPER(status) NOT IN "  # noqa: S608
        "('COMPLETED','FAILED','CANCELLED','UNVERIFIED')",
        mparams,
    ).fetchall()
    print(f"non_terminal_tasks={len(rows)}")
    findings += [f"task {r['id']} stuck in {r['status']}" for r in rows]

    res = conn.execute(
        "SELECT id, provider, task_id FROM provider_reservations WHERE released_at IS NULL"
    ).fetchall()
    if args.mission:
        tids = {r["id"] for r in conn.execute("SELECT id FROM tasks WHERE mission_id=?", (args.mission,))}
        res = [r for r in res if r["task_id"] in tids]
    print(f"unreleased_reservations={len(res)}")
    findings += [f"reservation {r['id']} ({r['provider']}/{r['task_id']})" for r in res]

    locks = conn.execute("SELECT task_id FROM task_locks WHERE released_at IS NULL").fetchall()
    print(f"unreleased_locks={len(locks)}")
    findings += [f"lock held by {r['task_id']}" for r in locks]

    busy = conn.execute("SELECT name, state FROM providers WHERE state != 'AVAILABLE'").fetchall()
    print(f"non_available_providers={len(busy)}")
    findings += [f"provider {r['name']} in {r['state']}" for r in busy]

    branches = conn.execute(
        "SELECT task_id, branch_name, worktree_path FROM task_branches WHERE removed_at IS NULL"
        + (" AND task_id IN (SELECT id FROM tasks WHERE mission_id=?)" if args.mission else ""),
        mparams,
    ).fetchall()
    missing = [b for b in branches if not Path(b["worktree_path"]).is_dir()]
    print(f"branches={len(branches)} missing_worktrees={len(missing)}")
    findings += [f"missing worktree for {b['task_id']}: {b['worktree_path']}" for b in missing]

    alive = []
    for r in conn.execute(
        "SELECT id, pid, pgid FROM provider_runs WHERE pid IS NOT NULL" + (" AND mission_id=?" if args.mission else ""),
        mparams,
    ):
        if _alive(r["pid"]):
            alive.append(f"{r['id']} pid={r['pid']}")
    print(f"live_run_pids={len(alive)}")
    findings += [f"possibly orphaned run {a}" for a in alive]

    for table in ("task_integrations", "reviews", "review_findings"):
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table}" + (" WHERE mission_id=?" if args.mission else ""),  # noqa: S608
            mparams,
        ).fetchone()[0]
        print(f"{table}={n}")

    if args.repo:
        import subprocess

        def _git(*git_args: str) -> tuple[int, str]:
            try:
                r = subprocess.run(
                    ["git", "-C", args.repo, *git_args],
                    capture_output=True, text=True, timeout=30,
                )
                return r.returncode, r.stdout
            except (OSError, subprocess.SubprocessError):
                return 128, ""

        def _ignored(rel: str) -> bool:
            code, _ = _git("check-ignore", "-q", "--", rel)
            return code == 0

        rc, out = _git("status", "--porcelain=v1", "--ignored", "-z")
        if rc != 0:
            print("repo_status=unreadable")
            findings.append("git status unreadable for repo")
        else:
            ignored = modified = untracked = 0
            for entry in out.split("\0"):
                if len(entry) < 4 or entry[2] != " ":
                    continue  # blank, or rename-target half (its XY half is counted)
                code2, rel = entry[:2], entry[3:].strip().strip('"')
                if code2 == "!!" or _ignored(rel):
                    ignored += 1
                elif code2 == "??":
                    untracked += 1
                    findings.append(f"uncommitted new file: {rel}")
                else:
                    modified += 1
                    findings.append(f"uncommitted change: {code2.strip()} {rel}")
            print(f"repo_ignored={ignored} repo_modified_tracked={modified} repo_untracked={untracked}")

    if args.mission:
        mission = conn.execute("SELECT status FROM missions WHERE id=?", (args.mission,)).fetchone()
        if mission and mission["status"] == "COMPLETED":
            for label, sql in (
                ("integration", "SELECT 1 FROM task_integrations WHERE mission_id=? AND status='COMPLETED'"),
                ("review", "SELECT 1 FROM reviews WHERE mission_id=?"),
                ("final checkpoint", "SELECT 1 FROM checkpoints WHERE mission_id=?"),
            ):
                if not conn.execute(sql, (args.mission,)).fetchone():
                    findings.append(f"COMPLETED mission missing {label} evidence")
            print("evidence_check=done")
        elif mission:
            print(f"evidence_check=skipped status={mission['status']}")
        else:
            findings.append("mission not found")
            print("evidence_check=mission-missing")

    if findings:
        print("--- findings ---")
        for f in findings:
            print(f)
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
