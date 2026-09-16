"""Shared builders for hand-built ScanResult pieces."""

from __future__ import annotations

from filter_floor.models import CheckStatus, DeployerMemory, LayerA, LayerB
from filter_floor.scanners.base import Scanner, unknown_layer_b


def layer_a_all(status: CheckStatus, **overrides) -> LayerA:
    fields = {
        "mint_authority_revoked": status,
        "freeze_authority_revoked": status,
        "lp_locked_or_burned": status,
        "honeypot_or_unsellable": status,
        "owner_or_upgrade_risk": status,
        "token2022_or_hook_risk": status,
    }
    fields.update(overrides)
    return LayerA(**fields)


def layer_a_clean(**overrides) -> LayerA:
    return layer_a_all(CheckStatus.PASS, **overrides)


def layer_a_unknown(**overrides) -> LayerA:
    return layer_a_all(CheckStatus.UNKNOWN, **overrides)


def layer_b_clean(**overrides) -> LayerB:
    fields = {
        "bundle_detected": CheckStatus.PASS,
        "bundled_supply_pct": 0.0,
        "top10_holder_pct": 0.0,
        "top10_excluding_lp_pct": 0.0,
        "funding_cluster_size": 0,
        "same_funder_as_known_bad": CheckStatus.PASS,
        "early_consolidation": CheckStatus.PASS,
    }
    fields.update(overrides)
    return LayerB(**fields)


def layer_b_unknown(**overrides) -> LayerB:
    fields = {
        "bundle_detected": CheckStatus.UNKNOWN,
        "bundled_supply_pct": None,
        "top10_holder_pct": None,
        "top10_excluding_lp_pct": None,
        "funding_cluster_size": None,
        "same_funder_as_known_bad": CheckStatus.UNKNOWN,
        "early_consolidation": CheckStatus.UNKNOWN,
    }
    fields.update(overrides)
    return LayerB(**fields)


def memory_clean(**overrides) -> DeployerMemory:
    fields = {
        "deployer": "TestDeployer11111111111111111111111111111",
        "prior_token_count": 0,
        "death_rate": None,
        "cluster_size": 0,
    }
    fields.update(overrides)
    return DeployerMemory(**fields)


class FixtureScanner(Scanner):
    """Layer A fixture for pipeline tests. Layer B is owned by the graph source."""

    name = "fixture"

    def __init__(self, layer_a: LayerA, rpc=None) -> None:
        self._layer_a = layer_a
        self.rpc = rpc

    def scan_layer_a(self, token: str) -> LayerA:
        _ = token
        return self._layer_a

    def scan_layer_b(self, token: str) -> LayerB:
        return unknown_layer_b({"token": token, "TODO(verify)": "pipeline graph owns Layer B"})
