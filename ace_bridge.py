"""ACE Bridge — connects the ACE control plane to the Overlay365
marketing team (Social Media Dashboard "The Observer" leading Voice
Keeper / Scheduler / Format Auditor / Tracker) and every content /
promo / marketing agent in the fleet.

Direction of control:

    fleet marketing agents  --propose-->  ACE (facts -> rules -> gate
    -> escalation queue)  --approved/low-risk only-->  ExecutionLayer
    --deliver-->  SMD publish queue (:8030 /api/ai/schedule)

Nothing is published that did not pass ACE's ConstraintGate and risk
classification. The Observer reads via marketing_pulse().

Run standalone (smoke):  python ace_bridge.py selftest
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --- locate the ACE package (sibling checkout at Downloads/ACE) --------------

_ACE_HOME = Path(os.environ.get("ACE_HOME", r"C:\Users\User\Downloads\ACE"))
if _ACE_HOME.exists():
    _parent = str(_ACE_HOME.parent)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    # Windows import case-sensitivity: register 'ace' -> 'ACE'
    try:
        import ace  # noqa: F401
    except ImportError:
        import importlib

        sys.modules["ace"] = importlib.import_module(_ACE_HOME.name)

from ace.constraints import BoundsConstraintGate, GateResult, GateVerdict  # noqa: E402
from ace.execution import Channel, ExecutionLayer, FileOutboxChannel  # noqa: E402
from ace.facts import Fact, FactSource  # noqa: E402
from ace.observability import ObservabilityLayer  # noqa: E402
from ace.orchestrator import ACEOrchestrator  # noqa: E402
from ace.rules import InMemoryRuleEngine, ProposedAction  # noqa: E402

from skill_arsenal import (  # noqa: E402
    SkillAwareMarketingGate,
    assert_skill_facts,
    load_marketing_skills,
    route_skill,
)

SMD_BASE_URL = os.environ.get("SMD_BASE_URL", "http://127.0.0.1:8030")
_BRIDGE_DIR = Path(__file__).resolve().parent
FALLBACK_OUTBOX = _BRIDGE_DIR / "outbox" / "bridge_outbox.jsonl"


# --------------------------------------------------------------------------- channels


class SMDScheduleChannel(Channel):
    """Delivers approved content actions to the SMD publish queue.

    Falls back to a local file outbox when SMD is unreachable or when
    ACE_BRIDGE_DRY_RUN=1, so the bridge degrades gracefully offline.
    """

    name = "smd_schedule"

    def __init__(
        self,
        base_url: str = SMD_BASE_URL,
        fallback_path: Path = FALLBACK_OUTBOX,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_path = fallback_path
        self.timeout = timeout
        self._fallback_channel = FileOutboxChannel(fallback_path)

    def deliver(self, action_type: str, subject: str, params: dict) -> str:
        payload = {
            "text": params.get("copy") or params.get("text", ""),
            "images": params.get("images"),
            "platforms": params.get("platforms", ["x"]),
            "scheduled_at": params.get("scheduled_at"),
            "source_chain": f"ace-bridge:{action_type}:{subject}",
        }
        if os.environ.get("ACE_BRIDGE_DRY_RUN") == "1":
            return self._fallback(payload, reason="dry_run")
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/ai/schedule",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return str(body.get("queued", {}).get("id", "smd-ack"))
        except (urllib.error.URLError, OSError, ValueError):
            return self._fallback(payload, reason="smd_unreachable")

    def _fallback(self, payload: dict, reason: str) -> str:
        ref = self._fallback_channel.deliver(
            "smd_schedule_post", payload["source_chain"], payload
        )
        return f"{reason}:{ref}"


# --------------------------------------------------------------------------- gate

CONTENT_ACTION_TYPES = frozenset({
    "publish_blog_post",
    "publish_ad",
    "send_nurture_email",
})


class MarketingGate(BoundsConstraintGate):
    """BoundsConstraintGate + marketing-team sign-off semantics.

    The reference gate NEEDS_REVIEWs content types it has no rule for
    (fail-closed). For the marketing team that review IS deterministic:
    a content action may proceed only when the proposal carries explicit
    Voice Keeper (brand voice) and Format Auditor (platform format)
    approvals. Anything else still goes to the human queue.
    """

    def check(self, action: ProposedAction) -> GateResult:
        result = super().check(action)
        if (
            result.verdict == GateVerdict.NEEDS_REVIEW
            and action.action_type in CONTENT_ACTION_TYPES
        ):
            if (
                action.params.get("voice_approved") is True
                and action.params.get("format_approved") is True
            ):
                return GateResult(
                    action,
                    GateVerdict.ALLOW,
                    "marketing team sign-off: voice_approved + format_approved",
                    ("marketing_team_sign_off",),
                )
            missing = [
                label
                for label, flag in (
                    ("voice_approved", action.params.get("voice_approved")),
                    ("format_approved", action.params.get("format_approved")),
                )
                if flag is not True
            ]
            return GateResult(
                action,
                GateVerdict.NEEDS_REVIEW,
                f"awaiting marketing team sign-off: {', '.join(missing)}",
                ("marketing_team_sign_off",),
            )
        return result


def build_bridge(
    gate: MarketingGate | None = None,
    skill_aware: bool = True,
) -> tuple[ACEOrchestrator, ExecutionLayer, ObservabilityLayer]:
    """Wire one shared ACE control plane for the marketing team.

    With skill_aware=True (default) the gate is wrapped in
    SkillAwareMarketingGate, which fail-closes any proposal claiming a
    marketing skill that is not registered in .draymond/registry.json.

    Routing:
      publish_blog_post          -> SMD schedule queue (social)
      send_nurture_email         -> file outbox until SMTP channel lands
      everything else            -> default file outbox (auditable)
    """
    gate = gate or MarketingGate()
    if skill_aware:
        gate = SkillAwareMarketingGate(gate)
    orchestrator = ACEOrchestrator(rule_engine=InMemoryRuleEngine(), constraint_gate=gate)

    smd_channel = SMDScheduleChannel()
    email_outbox = FileOutboxChannel(_BRIDGE_DIR / "outbox" / "emails.jsonl")

    execution = ExecutionLayer(
        channels={
            smd_channel.name: smd_channel,
            email_outbox.name: email_outbox,
        },
        default_channel=email_outbox.name,
        action_routes={
            # social/content promos land in the SMD publish queue
            "publish_blog_post": smd_channel.name,
            "publish_ad": smd_channel.name,
            # email waits for a real SMTP channel; auditable outbox for now
            "send_nurture_email": email_outbox.name,
        },
    )

    observability = ObservabilityLayer(orchestrator.escalation_queue, orchestrator.tms)
    return orchestrator, execution, observability


def propose_content(
    orch: ACEOrchestrator,
    *,
    action_type: str,
    subject: str,
    agent: str,
    params: dict[str, Any],
    supporting_facts: list[Fact] | None = None,
    skills_applied: list[str] | None = None,
) -> Any:
    """One-call proposal path for fleet marketing agents.

    Asserts the agent's supporting facts into the TMS. With
    skills_applied, asserts a `marketing_skill.applied` provenance fact
    per curated skill (validated against the .draymond registry —
    unknown slugs raise). The claims are also mirrored into params so
    SkillAwareMarketingGate can fail-closed on them at check time.
    Returns the EscalationItem.
    """
    fact_ids: list[str] = []
    for fact in supporting_facts or []:
        orch.tms.assert_fact(fact)
        fact_ids.append(fact.fact_id)
    if skills_applied:
        facts = assert_skill_facts(orch.tms, subject, skills_applied)
        fact_ids.extend(f.fact_id for f in facts)
        params = {**params, "skills_applied": list(skills_applied)}
    action = ProposedAction(
        action_type=action_type,
        subject=subject,
        agent=agent,
        params=params,
        derived_from=tuple(fact_ids),
    )
    return orch.escalation_queue.submit(action, orch.constraint_gate.check(action))


def marketing_pulse(obs: ObservabilityLayer) -> dict[str, Any]:
    """The Observer's view: what fired, what was blocked, and why."""
    return {
        "summary": obs.action_summary(),
        "blocked": obs.blocked_actions(),
    }


def selftest() -> int:
    os.environ["ACE_BRIDGE_DRY_RUN"] = "1"
    orch, ex, obs = build_bridge()

    voice_fact = Fact(
        predicate="brand_architect.voice_approved",
        subject="post_001",
        value=True,
        source=FactSource.BRAND_ARCHITECT,
    )

    # 1. No sign-off -> held for human review (fail-closed).
    unsigned = propose_content(
        orch,
        action_type="publish_blog_post",
        subject="post_unsigned",
        agent="scheduler",
        params={"copy": "Draft post", "platforms": ["x"]},
    )
    # 2. Voice Keeper + Format Auditor sign-off -> auto-executes to SMD.
    signed = propose_content(
        orch,
        action_type="publish_blog_post",
        subject="post_001",
        agent="voice_keeper",
        params={
            "copy": "Overlay365 weekly digest",
            "platforms": ["x"],
            "voice_approved": True,
            "format_approved": True,
        },
        supporting_facts=[voice_fact],
    )
    # 3. Skill arsenal: route a problem, claim the routed skill.
    matches = route_skill("plan an A/B test for our landing page conversion rate")
    print("skill route (ab test):", [(m["slug"], m["score"]) for m in matches])
    skill_slug = matches[0]["slug"] if matches else None
    if skill_slug:
        skilled = propose_content(
            orch,
            action_type="publish_ad",
            subject="campaign_001",
            agent="growth_optimizer",
            params={
                "copy": "Test two landing page variants",
                "voice_approved": True,
                "format_approved": True,
            },
            skills_applied=[skill_slug],
        )
        why = obs.why_did_this_fire(skilled.item_id)
        print("skilled:", skilled.status.value,
              "| provenance fact ids:", len(why["licensed_by_facts"]),
              "(incl. marketing_skill.applied)")
    # 4a. Unknown skill claim via kwarg -> rejected at the fact layer.
    try:
        propose_content(
            orch,
            action_type="publish_blog_post",
            subject="post_bad_skill",
            agent="voice_keeper",
            params={"copy": "x", "voice_approved": True, "format_approved": True},
            skills_applied=["not-a-real-skill"],
        )
        bad_skill_status = "submitted"
    except ValueError as exc:
        bad_skill_status = f"rejected at fact layer"
    # 4b. Unknown skill claim smuggled in params -> held at the gate layer.
    bad_gate_item = propose_content(
        orch,
        action_type="publish_blog_post",
        subject="post_bad_skill_gate",
        agent="voice_keeper",
        params={"copy": "x", "skills_applied": ["not-a-real-skill"],
                "voice_approved": True, "format_approved": True},
    )
    bad_gate_status = (
        "held at gate" if bad_gate_item.status.value == "pending" else
        bad_gate_item.status.value
    )
    print("unknown skill:", bad_skill_status, "/", bad_gate_status)

    receipts = ex.drain(orch.escalation_queue)
    print("unsigned:", unsigned.status.value, "-", unsigned.notes)
    print("signed:  ", signed.status.value)
    print("receipts:", [(r.status, r.detail[:60]) for r in receipts])
    print("pulse:", json.dumps(marketing_pulse(obs), indent=1))
    ok = (
        unsigned.status.value == "pending"
        and signed.status.value == "auto_executed"
        and len(receipts) == 1
        and receipts[0].status == "executed"
        and bad_skill_status == "rejected at fact layer"
        and bad_gate_status == "held at gate"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
