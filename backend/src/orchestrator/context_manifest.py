"""Baseline prompt/context measurement (Increment 1).

Measures the GG-authored prompt string without rewriting it. No raw prompt
is persisted by default — only sizes, a hash of the redacted rendering, a
versioned local estimate, and minimal block metadata.

Estimator ``char4-v1`` is ``ceil(chars/4)``: an explicitly approximate
local metric, never tokenizer precision. Display as ``~N estimated``.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .security import redact

ESTIMATOR_ID = "char4-v1"
MANIFEST_SCHEMA_VERSION = "v1"
TEMPLATE_VERSION_LEGACY = "legacy-v1"
CONTEXT_POLICY_LEGACY = "legacy-v1"


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    estimator_id: str = ESTIMATOR_ID


def estimate_tokens(text: str) -> TokenEstimate:
    """Conservative local estimate: ceil(characters/4)."""
    return TokenEstimate(tokens=math.ceil(len(text) / 4) if text else 0)


def count_words(text: str) -> int:
    return len(text.split())


def redacted_hash(text: str) -> tuple[str, str]:
    """Hash the redacted UTF-8 rendering. Basis is explicit so hashes of
    low-entropy secrets cannot be mistaken for safe content."""
    safe = redact(text)
    digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()
    return digest, "redacted_rendered_utf8"


@dataclass(frozen=True)
class PromptManifest:
    prompt_hash: str
    hash_basis: str
    prompt_chars: int
    prompt_bytes: int
    prompt_words: int
    estimated_prompt_tokens: int
    estimator_id: str = ESTIMATOR_ID
    prompt_template_version: str = TEMPLATE_VERSION_LEGACY
    context_policy_version: str = CONTEXT_POLICY_LEGACY


def measure_prompt(prompt: str) -> PromptManifest:
    digest, basis = redacted_hash(prompt)
    est = estimate_tokens(prompt)
    return PromptManifest(
        prompt_hash=digest,
        hash_basis=basis,
        prompt_chars=len(prompt),
        prompt_bytes=len(prompt.encode("utf-8")),
        prompt_words=count_words(prompt),
        estimated_prompt_tokens=est.tokens,
    )
