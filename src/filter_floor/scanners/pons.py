"""Robinhood Chain / Pons V2 Layer A.

Factory addresses stay empty until verified on-chain. ROBINHOOD_ENABLED
defaults to 0: the scanner does not hit RPC and returns UNKNOWN, never PASS.
Unverified Pons factories stay UNKNOWN even if the gate is later flipped on.
"""

from __future__ import annotations

import os

from filter_floor.config import load_chains
from filter_floor.models import LayerA, LayerB
from filter_floor.scanners.base import unknown_layer_a
from filter_floor.scanners.evm import EvmRpc, EvmScanner, rpc_from_env

VERIFY_NOTE = "VERIFY ON-CHAIN BEFORE ENABLE"


def robinhood_enabled_from_env() -> bool:
    return os.environ.get("ROBINHOOD_ENABLED", "0") == "1"


class PonsScanner(EvmScanner):
    name = "pons-robinhood-layer-a"

    def __init__(
        self,
        rpc: EvmRpc | None = None,
        *,
        chain_name: str = "robinhood",
        chain_id: int | None = None,
        factory_addresses: dict[str, str] | None = None,
        explorer_url: str = "",
        quote_tokens: dict[str, str] | None = None,
        gated: bool | None = None,
        gate_reason: str = "",
        robinhood_enabled: bool | None = None,
    ) -> None:
        enabled = (
            robinhood_enabled
            if robinhood_enabled is not None
            else robinhood_enabled_from_env()
        )
        if gated is None:
            gated = not enabled
        super().__init__(
            rpc if not gated else None,
            chain_name=chain_name,
            chain_id=chain_id,
            factory_addresses=factory_addresses,
            explorer_url=explorer_url,
            quote_tokens=quote_tokens or {},
            gated=gated,
            gate_reason=gate_reason
            or (
                "ROBINHOOD_ENABLED=0; Pons V2 factory unverified; "
                f"{VERIFY_NOTE}; not PASS"
            ),
        )
        self.robinhood_enabled = enabled

    @classmethod
    def from_chain_config(cls) -> PonsScanner:
        cfg = load_chains()["robinhood"]
        enabled = robinhood_enabled_from_env()
        chain_id_raw = cfg.get("chain_id")
        chain_id = int(chain_id_raw) if chain_id_raw is not None else None
        rpc = rpc_from_env(str(cfg["rpc_env_key"])) if enabled else None
        return cls(
            rpc=rpc,
            chain_name="robinhood",
            chain_id=chain_id,
            factory_addresses=dict(cfg.get("factory_addresses") or {}),
            explorer_url=str(cfg.get("explorer_url") or ""),
            robinhood_enabled=enabled,
        )

    def scan_layer_a(self, token: str) -> LayerA:
        pons = (self.factory_addresses.get("pons_v2") or "").strip()
        if self.gated or not self.robinhood_enabled:
            return unknown_layer_a(
                {
                    "token": token,
                    "chain": self.chain_name,
                    "robinhood_enabled": False,
                    "pons_v2_factory": pons,
                    "pons_v2_factory_configured": bool(pons),
                    "TODO(verify)": self.gate_reason,
                }
            )

        if not pons:
            # Gate is on but factory is still unverified: ERC20 facts may be
            # read, factory/pool facts stay UNKNOWN and are never PASS.
            layer_a = super().scan_layer_a(token)
            layer_a.details["robinhood_enabled"] = True
            layer_a.details["pons_v2_factory"] = ""
            layer_a.details["pons_v2_factory_configured"] = False
            layer_a.details["TODO(verify):pons_v2"] = (
                f"Pons V2 factory address empty; {VERIFY_NOTE}; not PASS"
            )
            return layer_a

        layer_a = super().scan_layer_a(token)
        layer_a.details["robinhood_enabled"] = True
        layer_a.details["pons_v2_factory"] = pons
        layer_a.details["pons_v2_factory_configured"] = True
        layer_a.details["TODO(verify):pons_v2"] = (
            "Pons V2 factory filled in config but ABI/on-chain identity "
            f"is still operator-verified; {VERIFY_NOTE}"
        )
        return layer_a

    def scan_layer_b(self, token: str) -> LayerB:
        layer_b = super().scan_layer_b(token)
        layer_b.details["TODO(verify)"] = (
            f"Pons V2 graph not fetched; factory unverified; {VERIFY_NOTE}"
        )
        layer_b.details["robinhood_enabled"] = self.robinhood_enabled
        return layer_b
