"""Same-slot / same-package bundle heuristic."""

from __future__ import annotations

from collections import defaultdict

from filter_floor.graph.types import EarlyTxFetch, addr_key
from filter_floor.models import CheckStatus


def fail_closed(detected_fail: bool, complete: bool) -> CheckStatus:
    if detected_fail:
        return CheckStatus.FAIL
    if not complete:
        return CheckStatus.UNKNOWN
    return CheckStatus.PASS


def detect_bundle(
    fetch: EarlyTxFetch,
    *,
    min_buyers: int,
) -> tuple[CheckStatus, dict]:
    """FAIL if ≥ min_buyers distinct buyers share the launch slot or a tx package."""
    details: dict = {
        "launch_slot": None,
        "buyers_in_launch_slot": [],
        "max_same_slot_buyers": 0,
        "max_same_package_buyers": 0,
        "bundle_complete": False,
    }
    if fetch.rpc_partial_failure:
        details["rpc_partial_failure"] = True
        details["bundle_complete"] = False
        details["TODO(verify)"] = "bundle RPC miss; UNKNOWN, never PASS"
        # Evidence of a bundle still FAILs even if the rest of the graph is incomplete.
        detected, extra = _count_groups(fetch, min_buyers)
        details.update(extra)
        return fail_closed(detected, complete=False), details

    if not fetch.txs:
        details["TODO(verify)"] = "no token txs fetched; bundle UNKNOWN, never PASS"
        details["bundle_complete"] = False
        return fail_closed(False, complete=False), details

    detected, extra = _count_groups(fetch, min_buyers)
    details.update(extra)
    complete = bool(fetch.complete and extra.get("launch_slot") is not None)
    details["bundle_complete"] = complete
    if not complete and not detected:
        details["TODO(verify)"] = "launch slot / tx history incomplete; bundle UNKNOWN"
    return fail_closed(detected, complete), details


def _count_groups(fetch: EarlyTxFetch, min_buyers: int) -> tuple[bool, dict]:
    by_slot: dict[int, set[str]] = defaultdict(set)
    by_package: dict[str, set[str]] = defaultdict(set)
    for tx in fetch.txs:
        buyers = {addr_key(b) for b in tx.buyers if b}
        if tx.slot is not None:
            by_slot[tx.slot].update(buyers)
        if tx.package_id:
            by_package[str(tx.package_id)].update(buyers)

    slots_present = [tx.slot for tx in fetch.txs if tx.slot is not None]
    launch_slot = min(slots_present) if slots_present else None
    launch_buyers = sorted(by_slot.get(launch_slot, set())) if launch_slot is not None else []
    max_slot = max((len(v) for v in by_slot.values()), default=0)
    max_pkg = max((len(v) for v in by_package.values()), default=0)
    detected = max_slot >= min_buyers or max_pkg >= min_buyers
    if launch_slot is not None and len(launch_buyers) >= min_buyers:
        detected = True
    return detected, {
        "launch_slot": launch_slot,
        "buyers_in_launch_slot": launch_buyers,
        "max_same_slot_buyers": max_slot,
        "max_same_package_buyers": max_pkg,
    }
