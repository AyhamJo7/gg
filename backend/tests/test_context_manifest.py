"""Prompt manifest baseline: sizes, stable hash, estimator label, redaction."""

from __future__ import annotations

from orchestrator.context_manifest import ESTIMATOR_ID, measure_prompt


def test_sizes_and_estimate_labeled():
    prompt = "hello world " * 100  # 1200 chars
    manifest = measure_prompt(prompt)
    assert manifest.prompt_chars == len(prompt)
    assert manifest.prompt_bytes == len(prompt.encode("utf-8"))
    assert manifest.prompt_words == len(prompt.split())
    assert manifest.estimator_id == ESTIMATOR_ID == "char4-v1"
    # ceil(chars/4), explicitly approximate
    import math

    assert manifest.estimated_prompt_tokens == math.ceil(len(prompt) / 4)


def test_hash_stable_and_redacted_basis():
    a = measure_prompt("hello")
    b = measure_prompt("hello")
    assert a.prompt_hash == b.prompt_hash
    assert a.hash_basis == "redacted_rendered_utf8"
    assert measure_prompt("hello ").prompt_hash != a.prompt_hash


def test_no_raw_secret_in_hash_basis():
    # Redaction happens before hashing basis; two prompts differing only in
    # a secret value hash identically (no secret-shaped content hashed).
    p1 = "deploy with api_key=supersecretvalue12345678 end"
    p2 = "deploy with api_key=anothersecretvalue87654321 end"
    assert measure_prompt(p1).prompt_hash == measure_prompt(p2).prompt_hash


def test_non_ascii_sizes():
    prompt = "héllo 🌍 " * 10
    manifest = measure_prompt(prompt)
    assert manifest.prompt_bytes > manifest.prompt_chars
    assert manifest.estimated_prompt_tokens > 0
