"""Exact-SHA evidence + immutable attempt lineage + writer provenance (Increment 3).

One canonical provenance layer over Git truth. No extra provider calls:
every function here is deterministic SQLite/Git plumbing.

Core invariant enforced at delivery:

    review.reviewed_sha == verification.sha == criteria.checked_sha
        == fresh_checkout.sha == delivery.sha == candidate SHA
    reviewer ∉ provider_writer_set(candidate range)

Any post-evidence code change makes prior exact-SHA evidence stale.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import utcnow

logger = logging.getLogger(__name__)

ACTOR_PROVIDER = "PROVIDER"
ACTOR_HUMAN = "HUMAN_OPERATOR"
ACTOR_SYSTEM = "SYSTEM"
ACTOR_UNKNOWN = "UNKNOWN_EXTERNAL"
ACTOR_LEGACY = "LEGACY_UNKNOWN"

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def valid_sha(value: str | None) -> bool:
    """Full commit SHA syntax only. Never trust abbreviated or LLM-supplied strings blindly."""
    return bool(value) and bool(_FULL_SHA.fullmatch(value.strip().lower()))


def normalize_sha(value: str | None) -> str | None:
    if not valid_sha(value):
        return None
    return str(value).strip().lower()


def repo_key(project_id: str | None, common_dir: str) -> str:
    """Repository identity scoping SHA evidence (project + git common dir)."""
    return f"{project_id or ''}@{common_dir or ''}"


def _now() -> str:
    return utcnow().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# -- write provenance ----------------------------------------------------------

#: Roles whose provider runs are expected to change code and therefore get
#: checkpoint linkage. Planning/review/testing runs are not code-writing
#: attempts; stray commits they leave behind surface as UNKNOWN_EXTERNAL in
#: range analysis instead of being misattributed.
CODE_WRITING_ROLES = frozenset({"implementation", "repair"})


async def capture_write_start(workdir: Path | None) -> tuple[str | None, bool, list[str]]:
    """Capture (base_sha, dirty, dirty_paths≤10) before a code-writing run. Never raises."""
    from . import git_ops

    if workdir is None:
        return None, False, []
    try:
        base = await git_ops.head_sha(workdir)
    except Exception:
        logger.debug("write-start head capture failed", exc_info=True)
        return None, False, []
    try:
        st = await git_ops.status(workdir)
    except Exception:
        logger.debug("write-start status capture failed", exc_info=True)
        return base, False, []
    paths = list((st.modified or []) + (st.added or []) + (st.deleted or []) + (st.untracked or []))[:10]
    return base, not st.is_clean, paths


async def record_provider_write(
    db: Any,
    *,
    workdir: Path | None,
    run_id: str,
    mission_id: str | None = None,
    task_id: str | None = None,
    product_project_id: str | None = None,
    phase_id: str | None = None,
    provider: str | None = None,
    role: str | None = None,
    base_sha: str | None = None,
    dirty_before: bool = False,
    repo_key_value: str = "",
) -> str | None:
    """Link a provider run to its resulting checkpoint SHA (current HEAD). Never raises."""
    from . import git_ops

    if workdir is None:
        return None
    try:
        result = await git_ops.head_sha(workdir)
        tree = await git_ops.tree_sha(workdir, result) if result else None
    except Exception:
        logger.debug("write-result capture failed for run %s", run_id, exc_info=True)
        return None
    try:
        return record_write(
            db,
            run_id=run_id,
            mission_id=mission_id,
            task_id=task_id,
            product_project_id=product_project_id,
            phase_id=phase_id,
            actor_type=ACTOR_PROVIDER,
            actor_detail="",
            provider=provider,
            role=role,
            base_sha=base_sha,
            result_sha=result,
            tree_sha=tree,
            dirty_before=dirty_before,
            repo_key_value=repo_key_value,
        )
    except Exception:
        logger.debug("write provenance insert failed for run %s", run_id, exc_info=True)
        return None


def record_write(
    db: Any,
    *,
    run_id: str | None,
    mission_id: str | None = None,
    task_id: str | None = None,
    product_project_id: str | None = None,
    phase_id: str | None = None,
    actor_type: str = ACTOR_PROVIDER,
    actor_detail: str = "",
    provider: str | None = None,
    role: str | None = None,
    base_sha: str | None = None,
    result_sha: str | None = None,
    tree_sha: str | None = None,
    dirty_before: bool = False,
    repo_key_value: str = "",
) -> str | None:
    """Link one producing commit to its producer. Returns row id (None when no result SHA)."""
    result = normalize_sha(result_sha)
    if result is None:
        return None
    base = normalize_sha(base_sha)
    row_id = _new_id("wprov")
    try:
        db.insert(
            "write_provenance",
            {
                "id": row_id,
                "run_id": run_id,
                "mission_id": mission_id,
                "task_id": task_id,
                "product_project_id": product_project_id,
                "phase_id": phase_id,
                "actor_type": actor_type,
                "actor_detail": (actor_detail or "")[:500],
                "provider": provider,
                "role": role,
                "base_sha": base,
                "result_sha": result,
                "tree_sha": normalize_sha(tree_sha),
                "dirty_before": int(bool(dirty_before)),
                "repo_key": repo_key_value,
                "created_at": _now(),
            },
        )
    except sqlite3.IntegrityError:
        # Same run recorded twice (retry-safe insert); return the existing row.
        logger.debug("write provenance already recorded for run %s", run_id)
        existing = db.query("SELECT id FROM write_provenance WHERE run_id=?", (run_id,))
        return existing[0]["id"] if existing else None
    return row_id


async def range_writers(
    db: Any,
    repo: Path,
    base_sha: str | None,
    candidate_sha: str | None,
) -> dict[str, Any]:
    """Compute the writer set for commits in (base..candidate].

    Returns {writers: [{actor_type, provider, run_id, result_sha, dirty_before}],
    unattributed: [sha], complete: bool}. Merge commits with no provenance row
    are derived SYSTEM (content comes from matched parents); any other
    unmatched commit is UNKNOWN_EXTERNAL. No guessing.
    """
    from . import git_ops

    result: dict[str, Any] = {"writers": [], "unattributed": [], "complete": True, "checked": False}
    candidate = normalize_sha(candidate_sha)
    if candidate is None:
        result["complete"] = False
        return result
    if not await git_ops.commit_exists(repo, candidate):
        result["complete"] = False
        return result
    base = normalize_sha(base_sha)
    if base is not None and not await git_ops.commit_exists(repo, base):
        base = None
    commits = await git_ops.rev_list(repo, base or "", candidate) if base else []
    if base is None:
        # No trustworthy lower bound: only the candidate itself is evaluated.
        commits = []
    seen: dict[str, dict[str, Any]] = {}
    unattributed: list[str] = []
    for sha in commits:
        rows = db.query("SELECT * FROM write_provenance WHERE result_sha=?", (sha,))
        if rows:
            for r in rows:
                key = f"{r.get('actor_type')}:{r.get('provider')}:{r.get('run_id')}"
                seen[key] = {
                    "actor_type": r.get("actor_type"),
                    "provider": r.get("provider"),
                    "run_id": r.get("run_id"),
                    "result_sha": sha,
                    "dirty_before": bool(r.get("dirty_before")),
                }
                if r.get("actor_type") == ACTOR_UNKNOWN or r.get("dirty_before"):
                    unattributed.append(sha) if sha not in unattributed else None
            continue
        # Unmatched: merge commits are SYSTEM-derived; anything else is unknown.
        parents = await git_ops.rev_list_parents(repo, sha)
        if len(parents) > 1:
            seen[f"SYSTEM:merge:{sha}"] = {
                "actor_type": ACTOR_SYSTEM,
                "provider": None,
                "run_id": None,
                "result_sha": sha,
                "dirty_before": False,
            }
        else:
            unattributed.append(sha)
    # The candidate tip itself: matched above when in range; when candidate ==
    # base (no commits) there is nothing to attribute.
    result["writers"] = sorted(seen.values(), key=lambda w: str(w["result_sha"]))
    result["unattributed"] = sorted(set(unattributed))
    result["complete"] = not result["unattributed"]
    result["checked"] = True
    return result


def provider_writers(writers: list[dict[str, Any]]) -> set[str]:
    """Provider names with PROVIDER-actor writes (independence boundary is provider-level)."""
    return {str(w["provider"]) for w in writers if w.get("actor_type") == ACTOR_PROVIDER and w.get("provider")}


# -- phase attempts -------------------------------------------------------------


def begin_phase_attempt(db: Any, phase_id: str, mission_id: str | None, trigger: str = "INITIAL") -> dict[str, Any]:
    """Append an immutable phase attempt; numbering is UNIQUE(phase_id, attempt_number)."""
    previous = db.query(
        "SELECT id FROM project_phase_attempts WHERE phase_id=? ORDER BY attempt_number DESC LIMIT 1", (phase_id,)
    )
    attempt_number = 1
    prev_id: str | None = None
    if previous:
        prev_row = db.get("project_phase_attempts", previous[0]["id"])
        if prev_row:
            attempt_number = int(prev_row.get("attempt_number") or 0) + 1
            prev_id = str(prev_row.get("id"))
    for _ in range(3):
        row_id = _new_id("phatt")
        try:
            db.insert(
                "project_phase_attempts",
                {
                    "id": row_id,
                    "phase_id": phase_id,
                    "attempt_number": attempt_number,
                    "mission_id": mission_id,
                    "trigger": trigger,
                    "status": "RUNNING",
                    "base_sha": None,
                    "result_sha": None,
                    "started_at": _now(),
                    "finished_at": None,
                    "previous_attempt_id": prev_id,
                },
            )
            row = db.get("project_phase_attempts", row_id)
            return dict(row) if row else {"id": row_id, "attempt_number": attempt_number}
        except sqlite3.IntegrityError:
            # Concurrent attempt claimed this number; recompute and retry.
            logger.debug("phase attempt number race for %s, retrying", phase_id)
            previous = db.query(
                "SELECT id FROM project_phase_attempts WHERE phase_id=? ORDER BY attempt_number DESC LIMIT 1",
                (phase_id,),
            )
            if previous:
                prev_row = db.get("project_phase_attempts", previous[0]["id"])
                if prev_row:
                    attempt_number = int(prev_row.get("attempt_number") or 0) + 1
                    prev_id = str(prev_row.get("id"))
    raise RuntimeError(f"could not allocate phase attempt number for {phase_id}")


def finish_phase_attempt(
    db: Any, attempt_id: str, status: str, result_sha: str | None = None, base_sha: str | None = None
) -> None:
    db.execute(
        "UPDATE project_phase_attempts SET status=?, result_sha=COALESCE(?, result_sha),"
        " base_sha=COALESCE(?, base_sha), finished_at=? WHERE id=?",
        (status, normalize_sha(result_sha), normalize_sha(base_sha), _now(), attempt_id),
    )


def latest_review_for_mission(db: Any, mission_id: str) -> dict[str, Any] | None:
    rows = db.query("SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at DESC LIMIT 1", (mission_id,))
    return dict(rows[0]) if rows else None


# -- evidence evaluation ----------------------------------------------------------

STATE_VALID = "VALID"
STATE_STALE = "STALE"
STATE_FAILED = "FAILED"
STATE_MISSING = "MISSING"
STATE_WAIVED = "WAIVED"
STATE_UNKNOWN = "UNKNOWN"


@dataclass
class PhaseCandidate:
    phase_id: str
    phase_key: str = ""
    candidate_sha: str | None = None
    mission_id: str | None = None


@dataclass
class EvidenceInputs:
    project_id: str
    candidate_sha: str
    plan_revision: int
    repo: Path | None = None
    repo_key_value: str = ""
    phases: list[PhaseCandidate] = field(default_factory=list)
    oldest_base_sha: str | None = None


async def evaluate_artifact_evidence(db: Any, inputs: EvidenceInputs) -> dict[str, Any]:
    """Canonical evidence status for one candidate SHA. Pure queries + git reads.

    Never executes providers, checkouts, or tests. Blocking reasons are
    operator-actionable strings without secrets.
    """
    from . import git_ops

    candidate = normalize_sha(inputs.candidate_sha)
    blocking: list[str] = []
    out: dict[str, Any] = {
        "candidate_sha": candidate,
        "plan_revision": inputs.plan_revision,
        "repo_key": inputs.repo_key_value,
        "writers": [],
        "writers_complete": False,
        "review": {"state": STATE_MISSING},
        "verification": {"state": STATE_MISSING},
        "criteria": {"total": 0, "passed": 0, "stale": 0, "failed": 0, "missing": 0, "waived": 0, "details": []},
        "fresh_checkout": {"state": STATE_MISSING},
        "delivery_ready": False,
        "blocking_reasons": blocking,
    }
    if candidate is None:
        blocking.append("no candidate SHA")
        out["blocking_reasons"] = blocking
        return out

    repo_ok = inputs.repo is not None
    if repo_ok and not await git_ops.commit_exists(assert_path(inputs.repo), candidate):
        blocking.append(f"candidate {candidate[:8]} does not exist in this repository")
        out["review"]["state"] = STATE_UNKNOWN
        out["blocking_reasons"] = blocking
        return out

    # Writers over the full range.
    full = (
        await range_writers(db, assert_path(inputs.repo), inputs.oldest_base_sha, candidate)
        if repo_ok
        else {"writers": [], "unattributed": [], "complete": False, "checked": False}
    )
    out["writers"] = full["writers"]
    out["writers_complete"] = bool(full["checked"] and full["complete"])
    if not full["checked"]:
        blocking.append("writer provenance could not be checked (repository unavailable)")
    elif not full["complete"]:
        blocking.append(
            f"writer provenance INCOMPLETE: unattributed commits {', '.join(s[:8] for s in full['unattributed'][:5])}"
        )
    provider_set = provider_writers(full["writers"])

    # Review: every phase candidate needs an independent exact review, and no
    # non-SYSTEM commit may sit uncovered after the last reviewed tip.
    review_state = STATE_VALID
    review_detail: dict[str, Any] = {"state": STATE_VALID, "phases": []}
    if not inputs.phases:
        review_state = STATE_MISSING
        review_detail = {"state": STATE_MISSING, "detail": "no phase candidates to review"}
        blocking.append("no reviewed phase candidates")
    else:
        for phase in inputs.phases:
            csha = normalize_sha(phase.candidate_sha)
            entry: dict[str, Any] = {"phase_id": phase.phase_id, "candidate_sha": csha}
            if csha is None:
                entry.update(state=STATE_MISSING, detail="phase has no candidate SHA")
                review_state = STATE_MISSING
                blocking.append(f"phase {phase.phase_key or phase.phase_id} has no candidate SHA")
                review_detail["phases"].append(entry)
                continue
            if repo_ok and not await git_ops.is_ancestor(assert_path(inputs.repo), csha, candidate):
                entry.update(
                    state=STATE_STALE,
                    detail=f"phase candidate {csha[:8]} is not an ancestor of delivery {candidate[:8]}",
                )
                if review_state == STATE_VALID:
                    review_state = STATE_STALE
                blocking.append(f"phase {phase.phase_key or phase.phase_id} candidate rewritten since review")
                review_detail["phases"].append(entry)
                continue
            rev = None
            if phase.mission_id:
                rev = latest_review_for_mission(db, phase.mission_id)
            if rev is None:
                entry.update(state=STATE_MISSING, detail="no review attempt recorded")
                review_state = STATE_MISSING
                blocking.append(f"phase {phase.phase_key or phase.phase_id} has no review attempt")
            elif not rev.get("independent"):
                entry.update(
                    state=STATE_FAILED,
                    detail=f"review by {rev.get('review_provider')} was not independent",
                    reviewer=rev.get("review_provider"),
                )
                review_state = STATE_FAILED
                blocking.append(f"phase {phase.phase_key or phase.phase_id} review was not independent")
            elif normalize_sha(rev.get("reviewed_head_sha")) != csha:
                entry.update(
                    state=STATE_STALE,
                    detail=f"reviewed {str(rev.get('reviewed_head_sha') or '?')[:8]}, current {csha[:8]}",
                    reviewer=rev.get("review_provider"),
                    reviewed_sha=rev.get("reviewed_head_sha"),
                )
                if review_state == STATE_VALID:
                    review_state = STATE_STALE
                blocking.append(
                    f"phase {phase.phase_key or phase.phase_id} changed after review "
                    f"(reviewed {str(rev.get('reviewed_head_sha') or '?')[:8]}, now {csha[:8]})"
                )
            elif str(rev.get("review_provider") or "") in provider_set:
                entry.update(
                    state=STATE_FAILED,
                    detail=f"reviewer {rev.get('review_provider')} is in the candidate writer set",
                    reviewer=rev.get("review_provider"),
                )
                review_state = STATE_FAILED
                blocking.append(f"phase {phase.phase_key or phase.phase_id} reviewer is a candidate writer")
            else:
                entry.update(state=STATE_VALID, reviewer=rev.get("review_provider"), reviewed_sha=csha)
            review_detail["phases"].append(entry)
        # Commits after the last phase tip must be SYSTEM-only (merges,
        # acceptance bookkeeping with their own provenance rows).
        if repo_ok and review_state == STATE_VALID:
            tips = [normalize_sha(p.candidate_sha) for p in inputs.phases]
            tips = [t for t in tips if t]
            if tips and candidate not in tips:
                extra = await git_ops.rev_list(assert_path(inputs.repo), tips[-1], candidate)
                uncovered = [s for s in extra if not _sha_has_provenance(db, s)]
                if uncovered:
                    review_state = STATE_STALE
                    blocking.append(
                        f"unreviewed changes after last phase review: {', '.join(s[:8] for s in uncovered[:5])}"
                    )
        review_detail["state"] = review_state
    out["review"] = review_detail

    # Generic verification at exact candidate.
    vrows = db.query(
        "SELECT * FROM verification_attempts WHERE sha=? AND status='passed' ORDER BY finished_at DESC LIMIT 1",
        (candidate,),
    )
    vrows = [r for r in vrows if _row_in_scope(r, inputs)]
    if vrows:
        out["verification"] = {"state": STATE_VALID, "sha": candidate, "attempt_id": vrows[0]["id"]}
    else:
        any_rows = db.query("SELECT id FROM verification_attempts WHERE sha=? LIMIT 1", (candidate,))
        any_rows = [r for r in any_rows if _verification_row_in_scope(db, r["id"], inputs)]
        out["verification"] = {
            "state": STATE_MISSING if not any_rows else STATE_FAILED,
            "sha": candidate,
        }
        blocking.append(f"no passing verification attempt for {candidate[:8]}")

    # Criteria at exact candidate + current plan revision.
    criteria_spec = _required_criteria(db, inputs.project_id, inputs.plan_revision)
    out["criteria"]["total"] = len(criteria_spec)
    for spec in criteria_spec:
        cid = spec["criterion_id"]
        if spec.get("waived"):
            out["criteria"]["waived"] += 1
            out["criteria"]["details"].append({"criterion_id": cid, "state": STATE_WAIVED})
            continue
        att = db.query(
            "SELECT * FROM criterion_attempts WHERE project_id=? AND criterion_id=?"
            " AND checked_sha=? AND plan_revision=? ORDER BY created_at DESC LIMIT 1",
            (inputs.project_id, cid, candidate, inputs.plan_revision),
        )
        if not att:
            # Legacy fallback: a current criterion_results row at this SHA/rev
            # counts (migration-era evidence), but it cannot show history.
            legacy = db.query(
                "SELECT * FROM criterion_results WHERE project_id=? AND criterion_id=?", (inputs.project_id, cid)
            )
            legacy_ok = [
                r
                for r in legacy
                if r.get("sha") == candidate
                and (r.get("plan_revision") in (None, inputs.plan_revision))
                and r.get("status") == "SATISFIED"
            ]
            if legacy_ok:
                out["criteria"]["passed"] += 1
                out["criteria"]["details"].append({"criterion_id": cid, "state": STATE_VALID, "legacy": True})
            else:
                out["criteria"]["missing"] += 1
                out["criteria"]["details"].append({"criterion_id": cid, "state": STATE_MISSING})
                blocking.append(f"criterion {cid} has no passing attempt at {candidate[:8]}")
            continue
        row = att[0]
        if row.get("result") == "SATISFIED":
            if row.get("capability") == "REPLAYABLE" and row.get("context") != "fresh":
                fresh = db.query(
                    "SELECT id FROM criterion_attempts WHERE project_id=? AND criterion_id=?"
                    " AND checked_sha=? AND plan_revision=? AND result='SATISFIED' AND context='fresh' LIMIT 1",
                    (inputs.project_id, cid, candidate, inputs.plan_revision),
                )
                if not fresh:
                    out["criteria"]["stale"] += 1
                    out["criteria"]["details"].append(
                        {"criterion_id": cid, "state": STATE_STALE, "detail": "no fresh-checkout replay"}
                    )
                    blocking.append(f"criterion {cid} not replayed in fresh checkout at {candidate[:8]}")
                    continue
            out["criteria"]["passed"] += 1
            out["criteria"]["details"].append({"criterion_id": cid, "state": STATE_VALID})
        else:
            out["criteria"]["failed"] += 1
            out["criteria"]["details"].append({"criterion_id": cid, "state": STATE_FAILED})
            blocking.append(f"criterion {cid} latest attempt at {candidate[:8]} is {row.get('result')}")

    # Fresh checkout at exact candidate.
    frows = db.query(
        "SELECT * FROM fresh_checkout_attempts WHERE project_id=? AND sha=? AND status='passed'"
        " ORDER BY created_at DESC LIMIT 1",
        (inputs.project_id, candidate),
    )
    if frows:
        out["fresh_checkout"] = {"state": STATE_VALID, "sha": candidate, "attempt_id": frows[0]["id"]}
    else:
        any_fresh = db.query(
            "SELECT id FROM fresh_checkout_attempts WHERE project_id=? AND sha=? LIMIT 1",
            (inputs.project_id, candidate),
        )
        out["fresh_checkout"] = {"state": STATE_MISSING if not any_fresh else STATE_FAILED, "sha": candidate}
        blocking.append(f"no passing fresh-checkout attempt for {candidate[:8]}")

    out["delivery_ready"] = not blocking
    out["blocking_reasons"] = blocking
    return out


def assert_path(repo: Path | None) -> Path:
    if repo is None:
        raise ValueError("repository path required for git-backed evidence check")
    return repo


def _sha_has_provenance(db: Any, sha: str) -> bool:
    rows = db.query("SELECT id FROM write_provenance WHERE result_sha=? LIMIT 1", (sha,))
    return bool(rows)


def _row_in_scope(row: dict[str, Any], inputs: EvidenceInputs) -> bool:
    if row.get("product_project_id") and row.get("product_project_id") != inputs.project_id:
        return False
    return True


def _verification_row_in_scope(db: Any, attempt_id: str, inputs: EvidenceInputs) -> bool:
    row = db.get("verification_attempts", attempt_id)
    if not row:
        return False
    return _row_in_scope(row, inputs)


def _required_criteria(db: Any, project_id: str, plan_revision: int) -> list[dict[str, Any]]:
    """Required criteria for the current plan revision (pure read of plan_revisions)."""
    import json as _json

    rows = db.query(
        "SELECT plan_json FROM plan_revisions WHERE project_id=? AND revision=? LIMIT 1",
        (project_id, plan_revision),
    )
    if not rows:
        return []
    try:
        raw = rows[0]["plan_json"]
        plan = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    waived = _waived_criteria(db, project_id)
    out: list[dict[str, Any]] = []
    for req in plan.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        for criterion in req.get("acceptance") or []:
            if not isinstance(criterion, dict) or not criterion.get("id"):
                continue
            cid = str(criterion["id"])
            out.append(
                {
                    "criterion_id": cid,
                    "requirement_id": str(req.get("id", "")),
                    "verify": str(criterion.get("verify", "")),
                    "waived": cid in waived,
                }
            )
    return out


def _waived_criteria(db: Any, project_id: str) -> set[str]:
    try:
        rows = db.query(
            "SELECT target_id FROM acceptance_waivers WHERE project_id=? AND target_kind='criterion'",
            (project_id,),
        )
    except Exception:
        return set()
    return {str(r.get("target_id")) for r in rows if r.get("target_id")}
