"""Merge Layer A/B/memory into veto_reasons, caution_reasons, and Verdict."""

from __future__ import annotations

from dataclasses import dataclass

from filter_floor.config import load_scoring, load_vetoes
from filter_floor.models import (
    CheckStatus,
    DeployerMemory,
    LayerA,
    LayerB,
    Verdict,
)

LAYER_A_FIELDS = (
    "mint_authority_revoked",
    "freeze_authority_revoked",
    "lp_locked_or_burned",
    "honeypot_or_unsellable",
    "owner_or_upgrade_risk",
    "token2022_or_hook_risk",
)

LAYER_B_STATUS_FIELDS = (
    "bundle_detected",
    "same_funder_as_known_bad",
    "early_consolidation",
)


@dataclass(frozen=True)
class VerdictDecision:
    verdict: Verdict
    veto_reasons: list[str]
    caution_reasons: list[str]


def evaluate(
    layer_a: LayerA,
    layer_b: LayerB,
    memory: DeployerMemory,
    score_0_100: int,
) -> VerdictDecision:
    """Decide AVOID / CAUTION / PASS_FILTER.

    PASS_FILTER only if there is no veto, no caution rule, and score is
    below config/scoring.yaml pass_filter.max_score.
    """
    vetoes_cfg = load_vetoes()
    scoring_cfg = load_scoring()
    veto_reasons = collect_veto_reasons(layer_a, layer_b, memory, vetoes_cfg)
    caution_reasons = collect_caution_reasons(layer_a, layer_b, memory, vetoes_cfg)

    if veto_reasons:
        return VerdictDecision(Verdict.AVOID, veto_reasons, caution_reasons)

    max_score = int(scoring_cfg["pass_filter"]["max_score"])
    if score_0_100 >= max_score:
        caution_reasons.append(
            f"score_0_100 {score_0_100} >= pass_filter.max_score {max_score}"
        )

    if caution_reasons:
        return VerdictDecision(Verdict.CAUTION, veto_reasons, caution_reasons)

    return VerdictDecision(Verdict.PASS_FILTER, veto_reasons, caution_reasons)


def collect_veto_reasons(
    layer_a: LayerA,
    layer_b: LayerB,
    memory: DeployerMemory,
    vetoes_cfg: dict | None = None,
) -> list[str]:
    cfg = vetoes_cfg or load_vetoes()
    hard = cfg["hard_vetoes"]
    reasons: list[str] = []

    for field in hard["layer_a_fail"]:
        if getattr(layer_a, field) == CheckStatus.FAIL:
            reasons.append(f"layer_a.{field} is FAIL")

    death = hard["death_rate"]
    min_rate = float(death["min_rate"])
    min_count = int(death["min_prior_token_count"])
    if (
        memory.death_rate is not None
        and memory.death_rate >= min_rate
        and memory.prior_token_count >= min_count
    ):
        reasons.append(
            "memory.death_rate "
            f"{memory.death_rate} >= {min_rate} with prior_token_count "
            f"{memory.prior_token_count} >= {min_count}"
        )

    if hard.get("same_funder_as_known_bad") and (
        layer_b.same_funder_as_known_bad == CheckStatus.FAIL
    ):
        reasons.append("layer_b.same_funder_as_known_bad is FAIL")

    bundle_threshold = float(hard["bundled_supply_pct"])
    if (
        layer_b.bundled_supply_pct is not None
        and layer_b.bundled_supply_pct >= bundle_threshold
    ):
        reasons.append(
            f"layer_b.bundled_supply_pct {layer_b.bundled_supply_pct} >= {bundle_threshold}"
        )

    return reasons


def collect_caution_reasons(
    layer_a: LayerA,
    layer_b: LayerB,
    memory: DeployerMemory,
    vetoes_cfg: dict | None = None,
) -> list[str]:
    cfg = vetoes_cfg or load_vetoes()
    caution = cfg["caution"]
    hard_bundle = float(cfg["hard_vetoes"]["bundled_supply_pct"])
    reasons: list[str] = []

    for field in caution.get("layer_a_fail") or []:
        if getattr(layer_a, field) == CheckStatus.FAIL:
            reasons.append(f"layer_a.{field} is FAIL")

    if caution.get("any_layer_a_unknown"):
        for field in LAYER_A_FIELDS:
            if getattr(layer_a, field) == CheckStatus.UNKNOWN:
                reasons.append(f"layer_a.{field} is UNKNOWN")

    if caution.get("any_layer_b_unknown"):
        for field in LAYER_B_STATUS_FIELDS:
            if getattr(layer_b, field) == CheckStatus.UNKNOWN:
                reasons.append(f"layer_b.{field} is UNKNOWN")

    if caution.get("any_layer_b_fail"):
        for field in ("bundle_detected", "early_consolidation"):
            if getattr(layer_b, field) == CheckStatus.FAIL:
                reasons.append(f"layer_b.{field} is FAIL")

    top10_limit = float(caution["top10_excluding_lp_pct"])
    if (
        layer_b.top10_excluding_lp_pct is not None
        and layer_b.top10_excluding_lp_pct >= top10_limit
    ):
        reasons.append(
            "layer_b.top10_excluding_lp_pct "
            f"{layer_b.top10_excluding_lp_pct} >= {top10_limit}"
        )

    bundle_min = float(caution["bundled_supply_pct"]["min"])
    if (
        layer_b.bundled_supply_pct is not None
        and bundle_min <= layer_b.bundled_supply_pct < hard_bundle
    ):
        reasons.append(
            "layer_b.bundled_supply_pct "
            f"{layer_b.bundled_supply_pct} in [{bundle_min}, {hard_bundle})"
        )

    deployer = caution["deployer"]
    min_count = int(deployer["min_prior_token_count"])
    min_rate = float(deployer["min_death_rate"])
    if (
        memory.death_rate is not None
        and memory.prior_token_count >= min_count
        and memory.death_rate >= min_rate
    ):
        reasons.append(
            "memory.prior_token_count "
            f"{memory.prior_token_count} >= {min_count} with death_rate "
            f"{memory.death_rate} >= {min_rate}"
        )

    cluster_limit = int(caution["funding_cluster_size"])
    if (
        layer_b.funding_cluster_size is not None
        and layer_b.funding_cluster_size >= cluster_limit
    ):
        reasons.append(
            f"layer_b.funding_cluster_size {layer_b.funding_cluster_size} >= {cluster_limit}"
        )

    if caution.get("rpc_partial_failure") and (
        layer_a.details.get("rpc_partial_failure")
        or layer_b.details.get("rpc_partial_failure")
        or memory.details.get("rpc_partial_failure")
    ):
        reasons.append("rpc_partial_failure")

    if caution.get("unknown_death_rate_with_prior_tokens") and (
        memory.death_rate is None and memory.prior_token_count > 0
    ):
        reasons.append(
            "memory.death_rate is null with prior_token_count "
            f"{memory.prior_token_count}; null is not 0"
        )

    return reasons
