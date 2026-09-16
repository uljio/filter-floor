"""Scanner interface. Missing chain facts are UNKNOWN, never PASS."""

from __future__ import annotations

from abc import ABC, abstractmethod

from filter_floor.models import CheckStatus, LayerA, LayerB


def unknown_layer_a(details: dict | None = None) -> LayerA:
    payload = {"TODO(verify)": "Layer A chain facts not fetched"}
    if details:
        payload.update(details)
    return LayerA(
        mint_authority_revoked=CheckStatus.UNKNOWN,
        freeze_authority_revoked=CheckStatus.UNKNOWN,
        lp_locked_or_burned=CheckStatus.UNKNOWN,
        honeypot_or_unsellable=CheckStatus.UNKNOWN,
        owner_or_upgrade_risk=CheckStatus.UNKNOWN,
        token2022_or_hook_risk=CheckStatus.UNKNOWN,
        details=payload,
    )


def unknown_layer_b(details: dict | None = None) -> LayerB:
    payload = {"TODO(verify)": "Layer B graph facts not fetched"}
    if details:
        payload.update(details)
    return LayerB(
        bundle_detected=CheckStatus.UNKNOWN,
        bundled_supply_pct=None,
        top10_holder_pct=None,
        top10_excluding_lp_pct=None,
        funding_cluster_size=None,
        same_funder_as_known_bad=CheckStatus.UNKNOWN,
        early_consolidation=CheckStatus.UNKNOWN,
        details=payload,
    )


class Scanner(ABC):
    name: str

    @abstractmethod
    def scan_layer_a(self, token: str) -> LayerA:
        raise NotImplementedError

    @abstractmethod
    def scan_layer_b(self, token: str) -> LayerB:
        raise NotImplementedError

    def scan(self, token: str) -> tuple[LayerA, LayerB]:
        return self.scan_layer_a(token), self.scan_layer_b(token)
