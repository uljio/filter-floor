"""Scan pipeline: Layer A → (hard veto stop) → Layer B graph → memory → score → write."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from filter_floor.adapters.rpc import SolanaRpc
from filter_floor.config import load_chains, load_vetoes
from filter_floor.graph import analyze_layer_b
from filter_floor.graph.evm_feed import EvmGraphSource
from filter_floor.graph.solana_feed import SolanaGraphSource
from filter_floor.graph.types import GraphOutcome, GraphSource
from filter_floor.models import Chain, CheckStatus, LayerA, ScanResult, Verdict
from filter_floor.scanners import get_scanner
from filter_floor.scanners.base import Scanner, unknown_layer_b
from filter_floor.scoring import compute_score
from filter_floor.storage.cases import make_case_id, write_case
from filter_floor.storage.memory import MemoryStore
from filter_floor.storage.outcomes import schedule_for_scan
from filter_floor.vetoes import evaluate


def data_dir_from_env() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()


def run_scan(
    chain: Chain,
    token: str,
    data_dir: Path | None = None,
    *,
    solana_rpc: SolanaRpc | None = None,
    scanner: Scanner | None = None,
    graph_source: GraphSource | None = None,
) -> ScanResult:
    token = token.strip()
    if not token:
        raise ValueError("token is required")

    scanned_at = datetime.now(timezone.utc)
    case_id = make_case_id(scanned_at, chain, token)
    root = data_dir or data_dir_from_env()
    store = MemoryStore(root)

    chosen = scanner or get_scanner(chain, solana_rpc=solana_rpc)
    layer_a = chosen.scan_layer_a(token)
    deployer = _deployer_from_layer_a(layer_a.details) or "unknown"

    outcome = GraphOutcome()
    skipped_graph = False
    if _layer_a_hard_veto(layer_a):
        skipped_graph = True
        layer_b = unknown_layer_b(
            {
                "token": token,
                "graph_skipped": True,
                "reason": "layer_a_hard_veto",
                "TODO(verify)": "Layer B skipped after Layer A hard veto",
            }
        )
    else:
        source = graph_source or _default_graph_source(chain, chosen, solana_rpc)
        layer_b, outcome = analyze_layer_b(
            token=token,
            deployer=None if deployer == "unknown" else deployer,
            source=source,
            memory=store,
        )

    cluster_size = len(outcome.wallets) if outcome.wallets else 0
    memory = store.to_deployer_memory(
        deployer,
        current_token=token,
        cluster_id=outcome.cluster_id,
        cluster_size=cluster_size,
    )
    score = compute_score(layer_a, layer_b, memory)
    decision = evaluate(layer_a, layer_b, memory, score)
    verdict = decision.verdict
    caution_reasons = list(decision.caution_reasons)

    sources = _sources_for(chosen.name, layer_a.details, skipped_graph=skipped_graph)
    if chain is Chain.robinhood:
        rh_enabled = os.environ.get("ROBINHOOD_ENABLED", "0") == "1"
        pons_factory = (
            (load_chains().get("robinhood") or {})
            .get("factory_addresses", {})
            .get("pons_v2")
            or ""
        ).strip()
        if not rh_enabled:
            caution_reasons.append(
                "robinhood gated (ROBINHOOD_ENABLED=0); factory unverified"
            )
        if not pons_factory:
            caution_reasons.append(
                "pons_v2 factory empty; VERIFY ON-CHAIN BEFORE ENABLE; not PASS"
            )
        if (not rh_enabled or not pons_factory) and verdict is Verdict.PASS_FILTER:
            verdict = Verdict.CAUTION

    result = ScanResult(
        case_id=case_id,
        chain=chain,
        token=token,
        deployer=None if deployer == "unknown" else deployer,
        launched_at=None,
        scanned_at=scanned_at,
        layer_a=layer_a,
        layer_b=layer_b,
        memory=memory,
        score_0_100=score,
        verdict=verdict,
        veto_reasons=list(decision.veto_reasons),
        caution_reasons=caution_reasons,
        sources=sources,
    )
    store.upsert_after_scan(
        deployer=deployer,
        token=token,
        chain=chain,
        case_id=case_id,
        scanned_at=scanned_at,
        verdict=verdict,
        cluster_id=outcome.cluster_id,
        cluster_size=cluster_size,
        funder=outcome.funder,
        cluster_wallets=outcome.wallets,
    )
    write_case(result, root)
    schedule_for_scan(result, root)
    return result


def _layer_a_hard_veto(layer_a: LayerA) -> bool:
    fields = load_vetoes()["hard_vetoes"]["layer_a_fail"]
    return any(getattr(layer_a, field) == CheckStatus.FAIL for field in fields)


def _default_graph_source(
    chain: Chain,
    scanner: Scanner,
    solana_rpc: SolanaRpc | None,
) -> GraphSource:
    if chain is Chain.solana:
        rpc = solana_rpc or getattr(scanner, "rpc", None) or SolanaRpc.from_env()
        return SolanaGraphSource(rpc)
    return EvmGraphSource(getattr(scanner, "rpc", None))


def _deployer_from_layer_a(details: dict) -> str | None:
    for key in ("creator", "owner"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _sources_for(
    scanner_name: str,
    details: dict,
    *,
    skipped_graph: bool,
) -> list[str]:
    sources: list[str] = []
    extra = details.get("sources")
    if isinstance(extra, list):
        sources.extend(str(item) for item in extra if item)
    if scanner_name not in sources:
        sources.insert(0, scanner_name)
    if scanner_name.endswith("-stub") and "m0-stub" not in sources:
        sources.append("m0-stub")
    if details.get("rugcheck") is not None and "rugcheck" not in sources:
        sources.append("rugcheck")
    if details.get("is_pumpfun") and "pumpfun" not in sources:
        sources.append("pumpfun")
    if not skipped_graph and "graph-layer-b" not in sources:
        sources.append("graph-layer-b")
    return sources
