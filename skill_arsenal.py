"""Skill Arsenal bridge — makes the fleet's curated marketing skills
(103 registered in .draymond/registry.json: coreyhaines31/marketingskills
+ msitarzewski/agency-agents personas) first-class inputs to the ACE
control plane.

What this buys the marketing team:
  1. PROVENANCE — a proposal that claims a skill asserts a
     `marketing_skill.applied` fact into ACE's TMS, so observability can
     answer "which skill licensed this action?"
  2. FAIL-CLOSED VALIDATION — SkillAwareMarketingGate rejects proposals
     claiming skills that are not in the registry.
  3. DETERMINISTIC ROUTING — route_skill() keyword-scores registry
     skill descriptions to pick which specialist skill should run a
     given campaign problem. No LLM, fully reproducible.

Registry is read from .draymond/registry.json (category == "marketing").
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ace.facts import Fact, FactSource

REGISTRY_PATH = Path(
    os.environ.get(
        "DRAYMOND_REGISTRY",
        r"C:\Users\User\Downloads\Uplift\Draymond-Orchestrator\.draymond\registry.json",
    )
)

_WORD = re.compile(r"[a-z0-9]+")


def load_marketing_skills(registry_path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    """All category=marketing skills from the fleet registry."""
    with open(registry_path, encoding="utf-8-sig") as fh:
        registry = json.load(fh)
    return [s for s in registry.get("skills", []) if s.get("category") == "marketing"]


def known_skill_slugs(skills: list[dict[str, Any]] | None = None) -> set[str]:
    return {s["slug"] for s in (skills or load_marketing_skills())}


def route_skill(problem: str, skills: list[dict[str, Any]] | None = None,
                top_n: int = 3) -> list[dict[str, Any]]:
    """Deterministic keyword scoring of the problem against each skill's
    name + description. Returns top-N ranked matches:
    [{slug, name, author, score}]. Zero matches -> empty list."""
    skills = skills or load_marketing_skills()
    problem_words = set(_WORD.findall(problem.lower()))
    if not problem_words:
        return []

    ranked = []
    for skill in skills:
        text_words = set(_WORD.findall(
            f"{skill.get('name', '')} {skill.get('slug', '')} "
            f"{skill.get('description', '')}".lower()
        ))
        # Jaccard-style overlap; slug/name words weighted double by
        # simply being counted once more.
        overlap = len(problem_words & text_words)
        score = round(overlap / max(1, len(problem_words)), 4)
        if overlap:
            ranked.append({
                "slug": skill["slug"],
                "name": skill.get("name", skill["slug"]),
                "author": skill.get("author", ""),
                "score": score,
            })
    ranked.sort(key=lambda x: (-x["score"], x["slug"]))
    return ranked[:top_n]


def assert_skill_facts(
    tms: Any,
    subject: str,
    skill_slugs: list[str],
    skills: list[dict[str, Any]] | None = None,
) -> list[Fact]:
    """Assert one provenance fact per applied skill into the TMS.

    Raises ValueError on any slug not present in the registry — callers
    never silently license an action with an unverified skill claim.
    """
    known = known_skill_slugs(skills)
    unknown = sorted(set(skill_slugs) - known)
    if unknown:
        raise ValueError(
            f"unknown marketing skill(s) not in .draymond registry: {unknown}"
        )
    facts = [
        Fact(
            predicate="marketing_skill.applied",
            subject=subject,
            value=slug,
            source=FactSource.MARKETING_SKILL,
            confidence=1.0,
        )
        for slug in skill_slugs
    ]
    for fact in facts:
        tms.assert_fact(fact)
    return facts


class SkillAwareMarketingGate:
    """Wraps MarketingGate: before delegating, verifies that any
    `skills_applied` claim on the proposal references registry-known
    skills. Unknown claims force NEEDS_REVIEW (fail-closed)."""

    def __init__(self, inner_gate: Any, skills: list[dict[str, Any]] | None = None):
        self.inner_gate = inner_gate
        self.known = known_skill_slugs(skills)

    def check(self, action):  # noqa: ANN001 — mirrors Gate protocol
        claimed = action.params.get("skills_applied") or []
        unknown = sorted(set(claimed) - self.known)
        if unknown:
            from ace.constraints import GateResult, GateVerdict

            return GateResult(
                action,
                GateVerdict.NEEDS_REVIEW,
                f"unknown marketing skill(s) claimed: {unknown}",
                ("marketing_skill_validation",),
            )
        return self.inner_gate.check(action)
