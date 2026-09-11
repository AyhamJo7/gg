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
    return bool(value) and bool(_FULL_SHA.fullmatch(str(value).strip().lower()))


def normalize_sha(value: str | None) -> str | None:
    if not value or not valid_sha(value):
        return None
    return value.strip().lower()


async def repo_identity(repo: Path | None) -> str:
    """Stable physical repository identity (git common dir). '' when unknown.

    Physical, not project-scoped: a fresh clone has a DIFFERENT identity
    from its origin, so origin evidence never certifies the clone by hash
    equality alone — fresh runs record under the origin key explicitly.
    """
    if repo is None:
        return ""
    try:
        from . import git_ops

        return await git_ops.common_dir(repo)
    except Exception:
        logger.debug("repo identity failed", exc_info=True)
        return ""


def _now() -> str:
    return utcnow().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# -- write provenance ----------------------------------------------------------

#: Roles whose provider runs are checkpoint-linked. Every repo-affecting
#: role is tracked — including review: a reviewer that unexpectedly dirties
#: the tree must show up as a contributing writer (P-32), never vanish into
#: an unattributed checkpoint. No-change runs record base==result and do
#: not contribute (S-01).
TRACKED_WRITE_ROLES = frozenset({"planning", "implementation", "testing", "review", "repair"})

#: Backwards-compatible alias.
CODE_WRITING_ROLES = TRACKED_WRITE_ROLES


async def capture_write_start(
    workdir: Path | None, max_untracked_bytes: int = 5 * 1024 * 1024
) -> tuple[str | None, bool, list[str]]:
    """Capture (base_sha, blocking_dirt, blocking_paths≤10) before a code-writing run.

    Never raises. Ignores GG bookkeeping (.orchestrator/, gitignored by
    checkpoint policy) and untracked files the checkpoint would refuse
    (oversized — they can never be silently absorbed into a commit).
    Everything else unattributed blocks the run: it would otherwise be
    swept into the provider's checkpoint commit.
    """
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
    if st.is_clean:
        return base, False, []
    blocking: list[str] = []
    blocking.extend(p for p in list(st.modified or []) + list(st.added or []) + list(st.deleted or []))
    for path in list(st.untracked or [])[:50]:
        if path.startswith(".orchestrator/") or path == ".orchestrator":
            continue
        try:
            if (workdir / path).is_file() and (workdir / path).stat().st_size > max_untracked_bytes:
                continue
        except OSError:
            pass
        blocking.append(path)
    return base, bool(blocking), blocking[:10]


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
        identity = await repo_identity(workdir)
    except Exception:
        logger.debug("write-result capture failed for run %s", run_id, exc_info=True)
        return None
    if not repo_key_value:
        repo_key_value = identity
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

    Writer membership follows ACTUAL COMMITTED CONTRIBUTION, never invocation
    role: a provider belongs to the set iff a PROVIDER-actor write row links
    it to a commit in range whose result differs from its base (a real
    change). Runs that executed without changing anything participate in
    history but do not taint reviewer independence.

    Returns {writers: [{actor_type, provider, run_id, provider_run_id, role,
    base_sha, result_sha, repo_key, dirty_before, contributing, changed_paths}],
    unattributed: [sha], complete: bool}. Rows are repo-filtered: a row from
    another repository never enters this repo's writer set even when the
    commit hash text matches. Merge commits with no provenance row are
    derived SYSTEM (content comes from matched parents); any other unmatched
    commit is UNKNOWN_EXTERNAL. No guessing.
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
    expected_key = await repo_identity(repo)
    if not expected_key:
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
        rows = db.query("SELECT * FROM write_provenance WHERE result_sha=? AND repo_key=?", (sha, expected_key))
        if rows:
            for r in rows:
                role = r.get("role")
                r_base = normalize_sha(r.get("base_sha"))
                contributing = bool(r.get("actor_type") == ACTOR_PROVIDER and (r_base is None or r_base != sha))
                key = f"{r.get('actor_type')}:{r.get('provider')}:{r.get('run_id')}:{sha}"
                paths: list[str] = []
                if contributing and r_base:
                    try:
                        paths = await git_ops.diff_names(repo, r_base, sha)
                    except Exception:
                        logger.debug("changed-path probe failed for %s", sha, exc_info=True)
                seen[key] = {
                    "actor_type": r.get("actor_type"),
                    "provider": r.get("provider"),
                    "run_id": r.get("run_id"),
                    "provider_run_id": r.get("run_id"),
                    "role": role,
                    "base_sha": r_base,
                    "result_sha": sha,
                    "repo_key": r.get("repo_key"),
                    "dirty_before": bool(r.get("dirty_before")),
                    "contributing": contributing,
                    "changed_paths": paths,
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
                "provider_run_id": None,
                "role": "integration",
                "base_sha": None,
                "result_sha": sha,
                "repo_key": expected_key,
                "dirty_before": False,
                "contributing": True,
                "changed_paths": [],
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
    """Provider names that actually contributed committed changes.

    Contribution-based, never role-based: a PROVIDER actor counts iff its
    record is marked contributing (result commit differs from base). A run
    that executed without changing anything does not taint independence.
    Provider-level boundary: model variants of one provider are never
    treated as independent from each other.
    """
    return {
        str(w["provider"])
        for w in writers
        if w.get("actor_type") == ACTOR_PROVIDER and w.get("provider") and w.get("contributing", True)
    }


#: Kept for import compatibility; independence no longer consults roles.
INDEPENDENCE_ROLES = frozenset({"implementation", "repair"})


#: Stages whose provider runs are expected to have write rows.
WRITE_STAGES = frozenset(
    {"product_plan", "mission_plan", "dag_plan", "implementation", "repair", "task", "testing", "review"}
)


def mission_provider_writers(db: Any, mission_id: str) -> tuple[set[str], bool]:
    """(contributing provider writers, complete) for one mission.

    Contribution-approximated without git: a PROVIDER row counts iff its
    result differs from its base (unknown base counts as contributing —
    the fail-closed direction). No-change runs participate in history but
    do not exclude a reviewer. Mission-scoped (selection is per-mission);
    exact ranges are resolved with git where available.

    Complete is False when a successful tracked run has no write row
    (legacy/unlinked) or when dirt/unknown actors were captured — in that
    case independence cannot be certified.
    """
    rows = db.query(
        "SELECT provider, base_sha, result_sha FROM write_provenance WHERE mission_id=? AND actor_type=?"
        " AND provider IS NOT NULL",
        (mission_id, ACTOR_PROVIDER),
    )
    writers: set[str] = set()
    for r in rows:
        base = normalize_sha(r.get("base_sha"))
        res = normalize_sha(r.get("result_sha"))
        if base is None or (res is not None and base != res):
            writers.add(str(r["provider"]))
    runs = db.query(
        "SELECT id FROM provider_runs WHERE mission_id=? AND failure_class='NONE' AND stage IN"
        " ('product_plan','mission_plan','dag_plan','implementation','repair','task','testing','review')",
        (mission_id,),
    )
    if not runs:
        return writers, True
    linked = db.query(
        "SELECT COUNT(*) as n FROM write_provenance WHERE mission_id=? AND run_id IS NOT NULL", (mission_id,)
    )
    tainted = db.query(
        "SELECT id FROM write_provenance WHERE mission_id=? AND (dirty_before=1 OR actor_type=?) LIMIT 1",
        (mission_id, ACTOR_UNKNOWN),
    )
    complete = int((linked[0].get("n") if linked else 0) or 0) >= len(runs) and not tainted
    return writers, complete


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


async def adopt_head_as_human(
    db: Any,
    repo: Path,
    *,
    mission_id: str | None = None,
    product_project_id: str | None = None,
    phase_id: str | None = None,
    message: str = "human: adopt workspace changes",
) -> dict[str, Any]:
    """Explicitly checkpoint unattributed workspace changes as HUMAN_OPERATOR.

    The operator's adopt action — never automatic. Returns {adopted: bool,
    base_sha, result_sha, row_id}. A clean tree is a no-op returning the HEAD.
    """
    from . import git_ops

    try:
        base = await git_ops.head_sha(repo)
        st = await git_ops.status(repo)
    except Exception as exc:
        return {"adopted": False, "error": f"repository unreadable: {exc}"}
    if st.is_clean:
        return {"adopted": False, "base_sha": base, "result_sha": base, "row_id": None}
    common = ""
    try:
        common = await git_ops.common_dir(repo)
    except Exception:
        logger.debug("adopt repo key failed", exc_info=True)
    try:
        result = await git_ops.checkpoint(repo, message[:200] or "human: adopt workspace changes")
    except Exception as exc:
        return {"adopted": False, "error": f"checkpoint failed: {exc}"}
    if not result:
        return {"adopted": False, "error": "nothing committable (sensitive-only changes stay out)"}
    try:
        tree = await git_ops.tree_sha(repo, result)
    except Exception:
        tree = None
    row_id = record_write(
        db,
        run_id=None,
        mission_id=mission_id,
        product_project_id=product_project_id,
        phase_id=phase_id,
        actor_type=ACTOR_HUMAN,
        actor_detail="operator-adopted workspace changes",
        provider=None,
        role="human",
        base_sha=base,
        result_sha=result,
        tree_sha=tree,
        repo_key_value=common,
    )
    return {"adopted": True, "base_sha": base, "result_sha": result, "row_id": row_id}


async def _reviewer_outside_range_writers(db: Any, repo: Path | None, rev: dict[str, Any], candidate_sha: str) -> bool:
    """True iff the reviewer did not contribute to the exact reviewed artifact.

    Membership is resolved against writers of (reviewed_base..reviewed_head]
    — NOT the full delivery history: a reviewer who later writes in a
    subsequent phase does not retroactively taint their earlier review of
    an earlier artifact. Falls back to the stored writer set when the range
    cannot be resolved; fails closed when neither exists.
    """
    import json as _json

    reviewer = str(rev.get("review_provider") or "")
    if not reviewer:
        return False
    base = normalize_sha(rev.get("reviewed_base_sha"))
    head = normalize_sha(rev.get("reviewed_head_sha")) or normalize_sha(candidate_sha)
    if repo is not None and base and head:
        try:
            from . import git_ops

            if await git_ops.commit_exists(repo, head) and (base == head or await git_ops.commit_exists(repo, base)):
                exact = await range_writers(db, repo, base, head)
                if exact["checked"]:
                    return reviewer not in provider_writers(exact["writers"])
        except Exception:
            logger.debug("per-review range resolution failed", exc_info=True)
    try:
        stored = _json.loads(rev.get("writer_set_json") or "[]")
    except Exception:
        stored = []
    if isinstance(stored, list) and stored:
        return reviewer not in {str(p) for p in stored}
    # No stored set: mission-wide contribution approximation (fail closed
    # when provenance is incomplete; vacuously independent when nothing
    # was ever written).
    try:
        writers, complete = mission_provider_writers(db, str(rev.get("mission_id") or ""))
    except Exception:
        return False
    return (reviewer not in writers) and complete


def _writer_details(range_detail: list[dict[str, Any]], writer_set: list[str]) -> list[dict[str, Any]]:
    """Stable safe writer representation (S-32): provider, role, run, actor.

    Falls back to provider-name entries when exact range detail is
    unavailable (e.g. no repo at record time). Never includes raw logs.
    """
    by_provider: dict[str, dict[str, Any]] = {}
    for w in range_detail:
        if w.get("actor_type") != ACTOR_PROVIDER or not w.get("provider"):
            continue
        by_provider.setdefault(
            str(w["provider"]),
            {
                "provider": str(w["provider"]),
                "role": w.get("role"),
                "run_id": w.get("run_id"),
                "actor_type": w.get("actor_type"),
                "result_sha": w.get("result_sha"),
                "contributing": bool(w.get("contributing", True)),
            },
        )
    return [
        by_provider.get(
            p,
            {
                "provider": p,
                "role": None,
                "run_id": None,
                "actor_type": ACTOR_PROVIDER,
                "result_sha": None,
                "contributing": True,
            },
        )
        for p in writer_set
    ]


def latest_review_for_mission(db: Any, mission_id: str) -> dict[str, Any] | None:
    rows = db.query("SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at DESC LIMIT 1", (mission_id,))
    return dict(rows[0]) if rows else None


async def record_review_attempt(
    db: Any,
    events: Any,
    *,
    mission_id: str,
    product_project_id: str | None = None,
    phase_id: str | None = None,
    repo: Path | None = None,
    review_parsed: bool = True,
) -> dict[str, Any] | None:
    """Persist one immutable review attempt bound to its exact reviewed range.

    Independence rule: reviewer ∉ contributing candidate provider-writer set
    (actual committed writes in the reviewed range, any role), and writer
    provenance must be complete (no UNKNOWN/dirty). Self-review stays
    possible but is recorded independent=0, never certified.
    """
    import json as _json

    runs = db.query(
        "SELECT * FROM provider_runs WHERE mission_id=? AND role='review' AND failure_class='NONE' "
        "ORDER BY started_at DESC LIMIT 1",
        (mission_id,),
    )
    if not runs:
        return None
    run = runs[0]
    reviewer = str(run.get("provider") or "")
    reviewed_head = normalize_sha(run.get("git_commit_before")) or normalize_sha(run.get("git_commit_after"))

    impl_rows = db.query(
        "SELECT provider FROM provider_runs WHERE mission_id=? AND role='implementation'"
        " AND failure_class='NONE' ORDER BY started_at DESC LIMIT 1",
        (mission_id,),
    )
    implementer = str(impl_rows[0]["provider"]) if impl_rows else None

    writers, complete = mission_provider_writers(db, mission_id)
    earliest_base: str | None = None
    if writers or complete:
        bases = db.query(
            "SELECT base_sha FROM write_provenance WHERE mission_id=? AND base_sha IS NOT NULL"
            " ORDER BY created_at ASC LIMIT 1",
            (mission_id,),
        )
        if bases:
            earliest_base = normalize_sha(bases[0].get("base_sha"))
    reviewed_base = earliest_base or reviewed_head

    # Exact range writers when the repo is available (stronger than the
    # mission-wide approximation above).
    range_writers_set = set(writers)
    range_detail: list[dict[str, Any]] = []
    range_complete = complete
    if repo is not None and reviewed_base and reviewed_head:
        from . import git_ops

        if await git_ops.commit_exists(repo, reviewed_head) and (
            reviewed_base == reviewed_head or await git_ops.commit_exists(repo, reviewed_base)
        ):
            exact = await range_writers(db, repo, reviewed_base, reviewed_head)
            if exact["checked"]:
                range_writers_set = provider_writers(exact["writers"])
                range_detail = list(exact["writers"])
                range_complete = bool(exact["complete"])
    else:
        # No repo to resolve exact ranges: fall back to mission-wide
        # contribution approximation for display; certification still
        # requires completeness below.
        pass

    if not reviewer:
        independent, reason = False, "reviewer unknown"
    elif implementer is None and not writers:
        independent, reason = False, "implementation provider unknown (cannot prove independence)"
    elif not range_complete:
        independent, reason = False, "writer provenance incomplete (cannot certify independence)"
    elif reviewer in range_writers_set:
        independent, reason = False, f"reviewer {reviewer} is in the candidate writer set (self-review)"
    else:
        independent, reason = True, None

    writer_set = sorted(range_writers_set)
    review_id = _new_id("rev")
    review_repo_key = await repo_identity(repo) if repo is not None else ""
    db.insert(
        "reviews",
        {
            "id": review_id,
            "mission_id": mission_id,
            "implementation_provider": implementer,
            "review_provider": reviewer,
            "independent": int(independent),
            "degradation_reason": reason,
            "review_parsed": int(review_parsed),
            "reviewed_base_sha": reviewed_base,
            "reviewed_head_sha": reviewed_head,
            "writer_set_json": _json.dumps(writer_set),
            "writer_detail_json": _json.dumps(_writer_details(range_detail, writer_set)),
            "repo_key": review_repo_key,
            "created_at": _now(),
        },
    )
    try:
        from .models import EventType as _EventType

        events.publish(
            _EventType.REVIEW_RECORDED,
            mission_id,
            review_provider=reviewer,
            implementation_provider=implementer,
            independent=independent,
            degradation_reason=reason,
        )
    except Exception:
        logger.debug("review event publish failed", exc_info=True)
    row = db.get("reviews", review_id)
    return dict(row) if row else {"id": review_id}


def attach_finding_lineage(
    db: Any, mission_id: str, review_id: str, origin_sha: str | None, new_finding_ids: list[str]
) -> None:
    """Bind this cycle's findings to their origin review/SHA (P-21).

    Reopened rows keep their ORIGINAL origin; only NULL origins are set.
    Findings resolved in this cycle record the resolving review/SHA (P-22).
    """
    if new_finding_ids:
        placeholders = ",".join("?" for _ in new_finding_ids)
        try:
            db.execute(
                f"UPDATE review_findings SET origin_review_id=?, origin_sha=? WHERE mission_id=?"  # noqa: S608
                f" AND id IN ({placeholders}) AND origin_review_id IS NULL",
                (review_id, origin_sha, mission_id, *new_finding_ids),
            )
        except Exception:
            logger.debug("finding origin backfill failed", exc_info=True)
    try:
        db.execute(
            "UPDATE review_findings SET resolved_review_id=?, resolved_sha=? WHERE mission_id=?"
            " AND status='resolved' AND resolved_review_id IS NULL",
            (review_id, origin_sha, mission_id),
        )
    except Exception:
        logger.debug("finding resolution lineage failed", exc_info=True)


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

    # Repository identity for ALL evidence scoping below: only rows recorded
    # for THIS repository certify this candidate. Historical rows without a
    # key (LEGACY/UNKNOWN) can never certify a current artifact.
    expected_key = await repo_identity(assert_path(inputs.repo)) if repo_ok else ""
    out["repo_key"] = expected_key
    if repo_ok and not expected_key:
        blocking.append("repository identity unavailable — evidence cannot be scoped")
        out["blocking_reasons"] = blocking
        return out
    mission_ids = {p.mission_id for p in inputs.phases if p.mission_id}

    # Writers over the full range.
    full: dict[str, Any] = (
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
    # Full-range writers feed display + completeness; per-review membership
    # is resolved against each review's own range below.

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
            elif not await _reviewer_outside_range_writers(
                db,
                assert_path(inputs.repo) if repo_ok else None,
                rev,
                csha,
            ):
                entry.update(
                    state=STATE_FAILED,
                    detail=f"reviewer {rev.get('review_provider')} contributed to the reviewed artifact",
                    reviewer=rev.get("review_provider"),
                )
                review_state = STATE_FAILED
                blocking.append(f"phase {phase.phase_key or phase.phase_id} reviewer is a candidate writer")
            elif (rev.get("repo_key") or "") != expected_key or not expected_key:
                entry.update(
                    state=STATE_FAILED,
                    detail="review has no repository binding for this repository (cannot certify)",
                    reviewer=rev.get("review_provider"),
                )
                review_state = STATE_FAILED
                blocking.append(f"phase {phase.phase_key or phase.phase_id} review is not bound to this repository")
            elif not full["complete"]:
                entry.update(
                    state=STATE_FAILED,
                    detail="writer provenance incomplete for the reviewed range (cannot certify)",
                    reviewer=rev.get("review_provider"),
                )
                review_state = STATE_FAILED
                blocking.append(f"phase {phase.phase_key or phase.phase_id} writer provenance incomplete")
            else:
                entry.update(state=STATE_VALID, reviewer=rev.get("review_provider"), reviewed_sha=csha)
            review_detail["phases"].append(entry)
        # Commits after the last phase tip must be SYSTEM-only (merges,
        # acceptance bookkeeping with their own provenance rows).
        if repo_ok and review_state == STATE_VALID:
            tips = [normalize_sha(p.candidate_sha) for p in inputs.phases]
            tip_strs = [t for t in tips if t]
            if tip_strs and candidate not in tip_strs:
                extra = await git_ops.rev_list(assert_path(inputs.repo), tip_strs[-1], candidate)
                uncovered = [s for s in extra if not _sha_has_provenance(db, s, expected_key)]
                if uncovered:
                    review_state = STATE_STALE
                    blocking.append(
                        f"unreviewed changes after last phase review: {', '.join(s[:8] for s in uncovered[:5])}"
                    )
        review_detail["state"] = review_state
    out["review"] = review_detail

    # Generic verification at exact candidate, scoped to (project, repo).
    # A NULL-product row still needs a project mission AND the same repo key
    # (S-06); historical keyless rows never certify.
    def _verify_in_scope(r: dict[str, Any]) -> bool:
        if (r.get("repo_key") or "") != expected_key or not expected_key:
            return False
        if r.get("product_project_id"):
            return r.get("product_project_id") == inputs.project_id
        return bool(r.get("mission_id")) and r.get("mission_id") in mission_ids

    vrows = [
        r
        for r in db.query(
            "SELECT * FROM verification_attempts WHERE sha=? AND status='passed' ORDER BY finished_at DESC",
            (candidate,),
        )
        if _verify_in_scope(r)
    ]
    if vrows:
        out["verification"] = {"state": STATE_VALID, "sha": candidate, "attempt_id": vrows[0]["id"]}
    else:
        any_rows = [
            r for r in db.query("SELECT * FROM verification_attempts WHERE sha=?", (candidate,)) if _verify_in_scope(r)
        ]
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
            " AND checked_sha=? AND plan_revision=? AND repo_key=? ORDER BY created_at DESC LIMIT 1",
            (inputs.project_id, cid, candidate, inputs.plan_revision, expected_key),
        )
        if not att:
            # Legacy fallback: a current criterion_results row at this SHA/rev
            # counts (migration-era evidence), but it cannot show history —
            # and only with a matching repository binding.
            legacy = db.query(
                "SELECT * FROM criterion_results WHERE project_id=? AND criterion_id=?", (inputs.project_id, cid)
            )
            legacy_ok = [
                r
                for r in legacy
                if r.get("sha") == candidate
                and (r.get("plan_revision") in (None, inputs.plan_revision))
                and r.get("status") == "SATISFIED"
                and (r.get("repo_key") or "") == expected_key
                and expected_key
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
                    " AND checked_sha=? AND plan_revision=? AND result='SATISFIED' AND context='fresh'"
                    " AND repo_key=? LIMIT 1",
                    (inputs.project_id, cid, candidate, inputs.plan_revision, expected_key),
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

    # Fresh checkout at exact candidate, scoped to (project, repo).
    frows = [
        r
        for r in db.query(
            "SELECT * FROM fresh_checkout_attempts WHERE project_id=? AND sha=? AND status='passed'"
            " ORDER BY created_at DESC",
            (inputs.project_id, candidate),
        )
        if (r.get("repo_key") or "") == expected_key and expected_key
    ]
    if frows:
        out["fresh_checkout"] = {"state": STATE_VALID, "sha": candidate, "attempt_id": frows[0]["id"]}
    else:
        any_fresh = [
            r
            for r in db.query(
                "SELECT * FROM fresh_checkout_attempts WHERE project_id=? AND sha=?",
                (inputs.project_id, candidate),
            )
            if (r.get("repo_key") or "") == expected_key and expected_key
        ]
        out["fresh_checkout"] = {"state": STATE_MISSING if not any_fresh else STATE_FAILED, "sha": candidate}
        blocking.append(f"no passing fresh-checkout attempt for {candidate[:8]}")

    out["delivery_ready"] = not blocking
    out["blocking_reasons"] = blocking
    return out


def assert_path(repo: Path | None) -> Path:
    if repo is None:
        raise ValueError("repository path required for git-backed evidence check")
    return repo


def _sha_has_provenance(db: Any, sha: str, repo_key: str = "") -> bool:
    if repo_key:
        rows = db.query("SELECT id FROM write_provenance WHERE result_sha=? AND repo_key=? LIMIT 1", (sha, repo_key))
    else:
        rows = db.query("SELECT id FROM write_provenance WHERE result_sha=? LIMIT 1", (sha,))
    return bool(rows)


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
