"""Transparent weighted risk score 0–100. Never averages away a veto."""

from __future__ import annotations

import math

from filter_floor.config import load_scoring
from filter_floor.models import CheckStatus, DeployerMemory, LayerA, LayerB


def compute_score(
    layer_a: LayerA,
    layer_b: LayerB,
    memory: DeployerMemory,
) -> int:
    """Return a risk score in 0–100 (higher = more dangerous).

    Mint/freeze/honeypot fails are hard vetoes handled in vetoes.py.
    They are not diluted into this average. Caps from scoring.yaml still
    apply so a high bundle or death rate cannot look "safe" via empty metadata.
    """
    cfg = load_scoring()
    weights = cfg["weights"]
    norms = cfg["normalization"]
    caps = cfg["caps"]

    raw = 0.0
    raw += weights["bundle_and_bundled_supply_pct"] * _bundle_frac(layer_b)
    raw += weights["holder_concentration_ex_lp"] * _holder_frac(layer_b)
    raw += weights["funding_cluster_known_bad"] * _funding_frac(layer_b, norms)
    raw += weights["deployer_death_rate"] * _death_frac(memory, norms)
    raw += weights["metadata_exotic_program"] * _metadata_frac(layer_a)

    score = int(round(raw))
    score = _apply_caps(score, layer_b, memory, caps)
    return max(0, min(100, score))


def _bundle_frac(layer_b: LayerB) -> float:
    pct = layer_b.bundled_supply_pct
    if pct is not None:
        return min(1.0, max(0.0, pct / 100.0))
    if layer_b.bundle_detected == CheckStatus.FAIL:
        return 1.0
    return 0.0


def _holder_frac(layer_b: LayerB) -> float:
    pct = layer_b.top10_excluding_lp_pct
    if pct is None:
        return 0.0
    return min(1.0, max(0.0, pct / 100.0))


def _funding_frac(layer_b: LayerB, norms: dict) -> float:
    if layer_b.same_funder_as_known_bad == CheckStatus.FAIL:
        return 1.0
    cluster = layer_b.funding_cluster_size
    if cluster is None:
        return 0.0
    denom = float(norms["funding_cluster_size"])
    if denom <= 0:
        return 0.0
    return min(1.0, max(0.0, cluster / denom))


def _death_frac(memory: DeployerMemory, norms: dict) -> float:
    if memory.death_rate is None:
        return 0.0
    denom_n = float(norms["death_rate_log_count"])
    denom = math.log(1.0 + denom_n) if denom_n > 0 else 1.0
    scaled = memory.death_rate * math.log(1.0 + memory.prior_token_count) / denom
    return min(1.0, max(0.0, scaled))


def _metadata_frac(layer_a: LayerA) -> float:
    if layer_a.token2022_or_hook_risk == CheckStatus.FAIL:
        return 1.0
    if layer_a.details.get("exotic_program") is True:
        return 1.0
    return 0.0


def _apply_caps(score: int, layer_b: LayerB, memory: DeployerMemory, caps: dict) -> int:
    bundle_cap = caps["bundled_supply_pct"]
    if (
        layer_b.bundled_supply_pct is not None
        and layer_b.bundled_supply_pct >= bundle_cap["threshold"]
    ):
        score = max(score, int(bundle_cap["min_score"]))

    death_cap = caps["death_rate"]
    if memory.death_rate is not None and memory.death_rate >= death_cap["threshold"]:
        score = max(score, int(death_cap["min_score"]))
    return score
