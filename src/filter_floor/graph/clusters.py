"""Cluster ids and known-bad funder lookup against local memory JSON."""

from __future__ import annotations

import re

from filter_floor.graph.types import addr_key
from filter_floor.models import CheckStatus


def cluster_id_for_funder(funder: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", funder.strip())
    if not cleaned:
        cleaned = "unknown"
    return f"funder-{cleaned[:48]}"


def known_bad_status(
    funder: str | None,
    cluster_record: dict | None,
    *,
    min_death_rate: float,
    hops_complete: bool,
) -> tuple[CheckStatus, dict]:
    """FAIL only when the funder's stored death_rate is known and ≥ threshold.

    Null death_rate is not 0 and is not treated as known-bad.
    Incomplete hops without a FAIL stay UNKNOWN, never PASS.
    """
    details: dict = {
        "known_bad_funder": funder,
        "cluster_death_rate": None,
        "known_bad_complete": hops_complete,
    }
    if cluster_record is not None:
        rate = cluster_record.get("death_rate")
        details["cluster_id"] = cluster_record.get("cluster_id")
        details["cluster_death_rate"] = rate
        if rate is not None:
            try:
                rate_f = float(rate)
            except (TypeError, ValueError):
                rate_f = None
            if rate_f is not None and rate_f >= min_death_rate:
                details["known_bad_complete"] = True
                return CheckStatus.FAIL, details

    if not hops_complete:
        details["TODO(verify)"] = "funder hops incomplete; known-bad UNKNOWN, never PASS"
        return CheckStatus.UNKNOWN, details
    if funder is None:
        # Graph finished and no shared funder: not a known-bad hit.
        return CheckStatus.PASS, details
    return CheckStatus.PASS, details


def cluster_matches_funder(record: dict, funder: str) -> bool:
    stored = record.get("funder")
    if isinstance(stored, str) and addr_key(stored) == addr_key(funder):
        return True
    wallets = record.get("wallets") or []
    funder_key = addr_key(funder)
    return any(isinstance(w, str) and addr_key(w) == funder_key for w in wallets)
