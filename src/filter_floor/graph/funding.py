"""One-hop shared funder heuristic (first inbound native transfer)."""

from __future__ import annotations

from collections import defaultdict

from filter_floor.graph.bundles import fail_closed
from filter_floor.graph.types import (
    EarlyTxFetch,
    FundingHop,
    GraphSource,
    addr_key,
)
from filter_floor.models import CheckStatus


def first_buyers(
    fetch: EarlyTxFetch,
    *,
    limit: int,
    deployer: str | None,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    txs = sorted(
        fetch.txs,
        key=lambda tx: (tx.slot is None, tx.slot if tx.slot is not None else 0, tx.tx_id),
    )
    for tx in txs:
        for buyer in tx.buyers:
            key = addr_key(buyer)
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(buyer.strip())
            if len(ordered) >= limit:
                break
        if len(ordered) >= limit:
            break
    if deployer:
        dkey = addr_key(deployer)
        if dkey and dkey not in seen:
            ordered.append(deployer.strip())
    return ordered


def trace_funders(
    wallets: list[str],
    source: GraphSource,
) -> tuple[list[FundingHop], bool]:
    hops: list[FundingHop] = []
    complete = True
    for wallet in wallets:
        hop = source.first_inbound_native(wallet)
        hops.append(hop)
        if not hop.complete:
            complete = False
    return hops, complete


def shared_funder_cluster(
    hops: list[FundingHop],
    *,
    min_wallets: int,
) -> tuple[str | None, list[str]]:
    """Return (funder, wallets) for the largest group sharing one inbound funder."""
    by_funder: dict[str, list[str]] = defaultdict(list)
    for hop in hops:
        if not hop.funder:
            continue
        by_funder[addr_key(hop.funder)].append(hop.wallet)
    if not by_funder:
        return None, []
    funder_key, wallets = max(by_funder.items(), key=lambda item: len(item[1]))
    if len(wallets) < min_wallets:
        return None, []
    # Preserve a raw funder string from the hops.
    raw = next(h.funder for h in hops if h.funder and addr_key(h.funder) == funder_key)
    unique: list[str] = []
    seen: set[str] = set()
    for wallet in wallets:
        key = addr_key(wallet)
        if key in seen:
            continue
        seen.add(key)
        unique.append(wallet)
    return raw, unique


def consolidation_status(
    fetch: EarlyTxFetch,
    early_wallets: list[str],
    *,
    min_buyers: int,
    tx_window: int,
) -> tuple[CheckStatus, dict]:
    """FAIL if ≥ min_buyers early wallets send token balance to one receiver."""
    details: dict = {
        "consolidation_receiver": None,
        "consolidated_wallets": [],
        "consolidation_complete": False,
    }
    if fetch.rpc_partial_failure:
        details["rpc_partial_failure"] = True
        details["TODO(verify)"] = "consolidation RPC miss; UNKNOWN, never PASS"
        detected, extra = _consolidation_groups(fetch, early_wallets, min_buyers, tx_window)
        details.update(extra)
        return fail_closed(detected, complete=False), details

    detected, extra = _consolidation_groups(fetch, early_wallets, min_buyers, tx_window)
    details.update(extra)
    complete = bool(fetch.complete)
    details["consolidation_complete"] = complete

    if not complete and not detected:
        details["TODO(verify)"] = "first-N txs incomplete; consolidation UNKNOWN"
    return fail_closed(detected, complete), details


def _consolidation_groups(
    fetch: EarlyTxFetch,
    early_wallets: list[str],
    min_buyers: int,
    tx_window: int,
) -> tuple[bool, dict]:
    early = {addr_key(w) for w in early_wallets if w}
    txs = sorted(
        fetch.txs,
        key=lambda tx: (tx.slot is None, tx.slot if tx.slot is not None else 0, tx.tx_id),
    )[:tx_window]
    to_receiver: dict[str, set[str]] = defaultdict(set)
    for tx in txs:
        for move in tx.moves:
            sender = addr_key(move.sender)
            receiver = addr_key(move.receiver)
            if not sender or not receiver or sender == receiver:
                continue
            if sender in early:
                to_receiver[receiver].add(sender)
    if not to_receiver:
        return False, {"consolidation_receiver": None, "consolidated_wallets": []}
    receiver, senders = max(to_receiver.items(), key=lambda item: len(item[1]))
    detected = len(senders) >= min_buyers
    return detected, {
        "consolidation_receiver": receiver if detected else None,
        "consolidated_wallets": sorted(senders) if detected else [],
        "max_consolidation_size": len(senders),
    }
