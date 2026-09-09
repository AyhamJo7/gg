"""Unit tests for product-plan extraction and validation."""

from __future__ import annotations

import copy
import json

from orchestrator.product_plan import (
    extract_product_plan,
    validate_product_plan,
)
from orchestrator.providers.fake import default_test_plan


def test_valid_plan_passes():
    assert validate_product_plan(default_test_plan()) == []


def test_extraction_from_fence_with_trailing_prose():
    text = "Done.\nPRODUCT_PLAN_JSON:\n```json\n" + json.dumps(default_test_plan()) + "\n```\nSome trailing notes."
    assert extract_product_plan(text) == default_test_plan()


def test_extraction_without_marker_is_none():
    assert extract_product_plan('{"product_name": "x"}') is None


def test_duplicate_requirement_ids_rejected():
    plan = default_test_plan()
    plan["requirements"].append(copy.deepcopy(plan["requirements"][0]))
    errors = validate_product_plan(plan)
    assert any("duplicate requirement" in e for e in errors)


def test_unknown_dependency_rejected():
    plan = default_test_plan()
    plan["phases"][0]["depends_on"] = ["nope"]
    errors = validate_product_plan(plan)
    assert any("unknown phase" in e for e in errors)


def test_cycle_rejected():
    plan = default_test_plan()
    plan["phases"][0]["depends_on"] = ["feature"]
    errors = validate_product_plan(plan)
    assert any("cycle" in e for e in errors)


def test_invalid_scope_rejected():
    plan = default_test_plan()
    plan["phases"][0]["workspace_scopes"] = ["submarine"]
    errors = validate_product_plan(plan)
    assert any("invalid scope" in e for e in errors)


def test_missing_acceptance_rejected():
    plan = default_test_plan()
    plan["requirements"][0]["acceptance"] = []
    plan["phases"][0]["acceptance"] = []
    errors = validate_product_plan(plan)
    assert any("R1" in e and "acceptance" in e for e in errors)
    assert any("foundation" in e and "acceptance" in e for e in errors)


def test_unknown_prerequisite_rejected():
    plan = default_test_plan()
    plan["phases"][0]["human_prerequisites"] = ["ghost"]
    errors = validate_product_plan(plan)
    assert any("unknown prerequisite" in e for e in errors)


def test_uncovered_requirement_rejected():
    plan = default_test_plan()
    plan["phases"][1]["requirement_ids"] = []
    errors = validate_product_plan(plan)
    assert any("R2" in e and "not covered" in e for e in errors)


def test_empty_phases_rejected():
    plan = default_test_plan()
    plan["phases"] = []
    assert any("phase" in e for e in validate_product_plan(plan))
