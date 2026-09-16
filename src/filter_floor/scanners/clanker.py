"""Base / Clanker Layer A. Uses the EVM scanner plus optional Clanker factory.

Clanker factory stays empty until verified on-chain. Empty factory is UNKNOWN,
never PASS. Uniswap V3 getPool is used when configured in chains.yaml.
"""

from __future__ import annotations

from filter_floor.config import load_chains
from filter_floor.models import LayerA, LayerB
from filter_floor.scanners.evm import (
    BASE_QUOTE_TOKENS,
    EvmRpc,
    EvmScanner,
    rpc_from_env,
)


class ClankerScanner(EvmScanner):
    name = "clanker-base-layer-a"

    def __init__(
        self,
        rpc: EvmRpc | None = None,
        *,
        chain_name: str = "base",
        chain_id: int | None = 8453,
        factory_addresses: dict[str, str] | None = None,
        explorer_url: str = "",
        quote_tokens: dict[str, str] | None = None,
        gated: bool = False,
        gate_reason: str = "",
    ) -> None:
        super().__init__(
            rpc,
            chain_name=chain_name,
            chain_id=chain_id,
            factory_addresses=factory_addresses,
            explorer_url=explorer_url,
            quote_tokens=quote_tokens if quote_tokens is not None else BASE_QUOTE_TOKENS,
            gated=gated,
            gate_reason=gate_reason,
        )

    @classmethod
    def from_chain_config(cls) -> ClankerScanner:
        cfg = load_chains()["base"]
        chain_id = cfg.get("chain_id")
        return cls(
            rpc=rpc_from_env(str(cfg["rpc_env_key"])),
            chain_name="base",
            chain_id=int(chain_id) if chain_id is not None else 8453,
            factory_addresses=dict(cfg.get("factory_addresses") or {}),
            explorer_url=str(cfg.get("explorer_url") or ""),
            quote_tokens=BASE_QUOTE_TOKENS,
        )

    def scan_layer_a(self, token: str) -> LayerA:
        layer_a = super().scan_layer_a(token)
        clanker = (self.factory_addresses.get("clanker") or "").strip()
        layer_a.details["clanker_factory"] = clanker
        layer_a.details["clanker_factory_configured"] = bool(clanker)
        if not clanker:
            layer_a.details["TODO(verify):clanker_factory"] = (
                "Clanker factory address empty; VERIFY ON-CHAIN BEFORE ENABLE; "
                "not PASS"
            )
        return layer_a

    def scan_layer_b(self, token: str) -> LayerB:
        layer_b = super().scan_layer_b(token)
        layer_b.details["TODO(verify)"] = (
            "Layer B is computed in the pipeline graph, not the scanner"
        )
        return layer_b
