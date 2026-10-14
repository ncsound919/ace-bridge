"""AssetAwareMarketingGate — requires `asset_approved` for proposals that carry
rendered assets.

Mirrors SkillAwareMarketingGate (same wrapper shape). If an action references an
asset — `params["assets"]` (a non-empty list) or `params["asset_manifest"]` —
and does not carry `asset_approved is True`, it forces NEEDS_REVIEW
(fail-closed). Otherwise it delegates unchanged.

The sign-off is produced by CREATIVE/artcraft-suite after a deterministic audit:

    from audit.signoff import signoff_params
    params = {**params, **signoff_params(report, manifest_entry.to_dict())}

Wrap the existing gate with it in build_bridge to activate:

    gate = AssetAwareMarketingGate(SkillAwareMarketingGate(MarketingGate()))
"""

from __future__ import annotations

from typing import Any


class AssetAwareMarketingGate:
    def __init__(self, inner_gate: Any):
        self.inner_gate = inner_gate

    @staticmethod
    def _references_assets(params: dict) -> bool:
        return bool(params.get("assets")) or bool(params.get("asset_manifest"))

    def check(self, action):  # noqa: ANN001 — mirrors Gate protocol
        params = action.params or {}
        if self._references_assets(params) and params.get("asset_approved") is not True:
            from ace.constraints import GateResult, GateVerdict

            return GateResult(
                action,
                GateVerdict.NEEDS_REVIEW,
                "awaiting asset sign-off: asset_approved",
                ("asset_audit",),
            )
        return self.inner_gate.check(action)
