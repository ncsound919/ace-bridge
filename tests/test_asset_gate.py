import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asset_gate import AssetAwareMarketingGate  # noqa: E402


def install_fake_ace():
    ace = types.ModuleType("ace")
    constraints = types.ModuleType("ace.constraints")

    class GateVerdict:
        ALLOW = "allow"
        NEEDS_REVIEW = "needs_review"

    class GateResult:
        def __init__(self, action, verdict, reason, tags=()):
            self.action = action
            self.verdict = verdict
            self.reason = reason
            self.tags = tags

    constraints.GateResult = GateResult
    constraints.GateVerdict = GateVerdict
    ace.constraints = constraints
    sys.modules["ace"] = ace
    sys.modules["ace.constraints"] = constraints
    return GateVerdict, GateResult


class FakeAction:
    def __init__(self, params, action_type="publish_ad"):
        self.params = params
        self.action_type = action_type


class InnerGate:
    def __init__(self):
        self.called = 0

    def check(self, action):
        self.called += 1
        return ("inner", action)


def test_non_asset_proposal_delegates():
    install_fake_ace()
    inner = InnerGate()
    gate = AssetAwareMarketingGate(inner)
    result = gate.check(FakeAction({"copy": "hello"}))
    assert result[0] == "inner"
    assert inner.called == 1


def test_asset_without_signoff_is_held():
    GateVerdict, _ = install_fake_ace()
    inner = InnerGate()
    gate = AssetAwareMarketingGate(inner)
    result = gate.check(FakeAction({"assets": [{"path": "ad.svg"}], "copy": "x"}))
    assert result.verdict == GateVerdict.NEEDS_REVIEW
    assert "asset_approved" in result.reason
    assert inner.called == 0


def test_asset_manifest_without_signoff_is_held():
    GateVerdict, _ = install_fake_ace()
    gate = AssetAwareMarketingGate(InnerGate())
    result = gate.check(FakeAction({"asset_manifest": {"output_sha256": "AB"}}))
    assert result.verdict == GateVerdict.NEEDS_REVIEW


def test_asset_with_signoff_delegates():
    install_fake_ace()
    inner = InnerGate()
    gate = AssetAwareMarketingGate(inner)
    result = gate.check(FakeAction({"assets": [{"path": "ad.svg"}], "asset_approved": True}))
    assert result[0] == "inner"
    assert inner.called == 1


def test_asset_approved_false_is_held():
    GateVerdict, _ = install_fake_ace()
    gate = AssetAwareMarketingGate(InnerGate())
    result = gate.check(FakeAction({"assets": [{"path": "ad.svg"}], "asset_approved": False}))
    assert result.verdict == GateVerdict.NEEDS_REVIEW
