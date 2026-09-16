"""Layer B graph v1: bundle, shared funder, known-bad cluster, consolidation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from filter_floor.config import load_vetoes
from filter_floor.graph.bundles import detect_bundle
from filter_floor.graph.clusters import cluster_id_for_funder, known_bad_status
from filter_floor.graph.funding import (
    consolidation_status,
    first_buyers,
    shared_funder_cluster,
    trace_funders,
)
from filter_floor.graph.types import GraphOutcome, GraphSource
from filter_floor.models import LayerB

if TYPE_CHECKING:
    from filter_floor.storage.memory import MemoryStore

__all__ = [
    "GraphOutcome",
    "GraphSource",
    "analyze_layer_b",
    "cluster_id_for_funder",
]


def analyze_layer_b(
    *,
    token: str,
    deployer: str | None,
    source: GraphSource,
    memory: MemoryStore,
    cfg: dict | None = None,
) -> tuple[LayerB, GraphOutcome]:
    graph_cfg = cfg or load_vetoes()["graph"]
    tx_window = int(graph_cfg["consolidation_tx_window"])
    fetch_limit = int(graph_cfg.get("signature_fetch_limit") or tx_window)
    min_bundle = int(graph_cfg["same_slot_min_buyers"])
    min_shared = int(graph_cfg["shared_funder_min_wallets"])
    buyer_limit = int(graph_cfg["first_buyers"])
    min_consol = int(graph_cfg["consolidation_min_buyers"])
    bad_rate = float(graph_cfg["known_bad_cluster_death_rate"])

    fetch = source.fetch_early_txs(token, limit=max(fetch_limit, tx_window))
    bundle_status, bundle_details = detect_bundle(fetch, min_buyers=min_bundle)

    wallets = first_buyers(fetch, limit=buyer_limit, deployer=deployer)
    hops, hops_complete = trace_funders(wallets, source)
    funder, cluster_wallets = shared_funder_cluster(hops, min_wallets=min_shared)
    cluster_id = cluster_id_for_funder(funder) if funder else None
    cluster_record = memory.find_cluster_by_funder(funder) if funder else None
    known_status, known_details = known_bad_status(
        funder,
        cluster_record,
        min_death_rate=bad_rate,
        hops_complete=hops_complete and fetch.complete,
    )

    consol_status, consol_details = consolidation_status(
        fetch,
        wallets,
        min_buyers=min_consol,
        tx_window=tx_window,
    )

    graph_complete = bool(
        fetch.complete
        and hops_complete
        and bundle_details.get("bundle_complete")
        and consol_details.get("consolidation_complete")
        and known_details.get("known_bad_complete")
    )
    details: dict = {
        "graph_complete": graph_complete,
        "first_buyers": wallets,
        "shared_funder": funder,
        "cluster_id": cluster_id,
        "cluster_wallets": cluster_wallets,
        "hops": [
            {"wallet": h.wallet, "funder": h.funder, "complete": h.complete}
            for h in hops
        ],
        **bundle_details,
        **known_details,
        **consol_details,
    }
    if fetch.notes:
        details["fetch_notes"] = fetch.notes
    if fetch.rpc_partial_failure:
        details["rpc_partial_failure"] = True
        details["TODO(verify)"] = "graph RPC miss; UNKNOWN, never PASS"
    elif not graph_complete:
        details.setdefault(
            "TODO(verify)",
            "incomplete graph; UNKNOWN, never PASS",
        )

    cluster_size = len(cluster_wallets) if cluster_wallets else None
    layer_b = LayerB(
        bundle_detected=bundle_status,
        bundled_supply_pct=None,
        top10_holder_pct=None,
        top10_excluding_lp_pct=None,
        funding_cluster_size=cluster_size,
        same_funder_as_known_bad=known_status,
        early_consolidation=consol_status,
        details=details,
    )
    outcome = GraphOutcome(
        cluster_id=cluster_id,
        funder=funder,
        wallets=cluster_wallets,
        graph_complete=graph_complete,
    )
    return layer_b, outcome
