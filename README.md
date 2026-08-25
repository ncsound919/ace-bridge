# ACE Bridge

Connects the **ACE** control plane (`C:\Users\User\Downloads\ACE`) to the
Overlay365 marketing stack: the SMD publish queue (Social Media Dashboard —
"The Observer" :8030) and every content / promo / marketing agent in the fleet
(Voice Keeper, Scheduler, Format Auditor, Tracker, Daily Marketing Run chain,
Content Creation Engine, Marketing Tool).

## Control direction

```
fleet marketing agents ──propose──▶ ACE  (facts → rules → gate → escalation)
                                          │ approved / LOW-risk only
                                          ▼
                                   ExecutionLayer
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
             SMD publish queue (:8030)              file outbox (auditable)
             POST /api/ai/schedule                  agents/ace-bridge/outbox/
```

Nothing reaches SMD that did not pass ACE's ConstraintGate and risk
classification. Fail-closed by design.

## Marketing-team semantics (`MarketingGate`)

The reference ACE gate NEEDS_REVIEWs any content type it has no rule for.
For this fleet, that review is deterministic sign-off carried on the proposal:

| Flag | Sign-off by |
|---|---|
| `voice_approved: true` | Voice Keeper — brand voice check passed |
| `format_approved: true` | Format Auditor — platform format audit passed |

Both present → `ALLOW` (LOW-risk content auto-executes). Either missing → held
in the pending queue with an explicit "awaiting sign-off" reason. Spend/pricing
actions always follow the normal ACE risk table regardless of sign-off.

## Skill Arsenal integration (`skill_arsenal.py`)

Connects the 103 curated marketing skills registered in
`.draymond/registry.json` (`coreyhaines31/marketingskills` +
`msitarzewski/agency-agents`) into the control plane:

- **Provenance** — `propose_content(..., skills_applied=["ab-testing"])`
  asserts a `marketing_skill.applied` fact per skill into ACE's TMS and
  chains it into the action's `derived_from`. The observability layer can
  then answer "which skill licensed this action?"
- **Fail-closed validation, two layers** — unknown slugs raise at the fact
  layer (`assert_skill_facts`) or hold the item at the gate layer
  (`SkillAwareMarketingGate`, default wrapper in `build_bridge`).
- **Deterministic routing** — `route_skill(problem_text)` keyword-scores
  registry skill names/descriptions to pick which specialist skill should
  run a campaign problem. No LLM, reproducible.

```python
from ace_bridge import build_bridge, propose_content
from skill_arsenal import route_skill

orch, ex, obs = build_bridge()
top = route_skill("plan an A/B test for our landing page conversion rate")
# -> [('ab-testing', 0.45), ('agency-multi-platform-publisher', 0.36), ...]

item = propose_content(orch, action_type="publish_ad", subject="camp_1",
    agent="growth_optimizer",
    params={"copy": "...", "voice_approved": True, "format_approved": True},
    skills_applied=[top[0]["slug"]])
```

Registry location override: `DRAYMOND_REGISTRY` env var.

## Usage from fleet agents

```python
from ace_bridge import build_bridge, propose_content, marketing_pulse
from ace.facts import Fact, FactSource

orch, ex, obs = build_bridge()

item = propose_content(
    orch,
    action_type="publish_blog_post",       # or publish_ad, send_nurture_email
    subject="post_001",
    agent="voice_keeper",                  # proposing agent
    params={"copy": "...", "platforms": ["x"],
            "voice_approved": True, "format_approved": True},
    supporting_facts=[Fact("brand_architect.voice_approved", "post_001",
                           True, FactSource.BRAND_ARCHITECT)],
)

receipts = ex.drain(orch.escalation_queue)   # deliver everything resolved
pulse = marketing_pulse(obs)                 # The Observer's weekly view
```

Human approvals for MEDIUM/HIGH items: `orch.escalation_queue.approve(item_id, human_id)`
then `ex.drain(...)` again.

## Environment

| Var | Default | Purpose |
|---|---|---|
| `ACE_HOME` | `C:\Users\User\Downloads\ACE` | ACE package location |
| `SMD_BASE_URL` | `http://127.0.0.1:8030` | Social Media Dashboard API |
| `ACE_BRIDGE_DRY_RUN` | unset | `1` = never touch SMD; outbox only |

If SMD is down, delivery degrades to the local JSONL outbox and is marked
`smd_unreachable:` — nothing is lost or silently dropped.

## Smoke test

```
python ace_bridge.py selftest
```
