"""Scoring caps cannot be bypassed by empty/good metadata."""

from filter_floor.config import load_scoring
from filter_floor.models import CheckStatus
from filter_floor.scoring import compute_score
from tests.helpers import (
    layer_a_clean,
    layer_a_unknown,
    layer_b_clean,
    layer_b_unknown,
    memory_clean,
)


def test_bundle_cap_not_bypassed_by_good_metadata():
    caps = load_scoring()["caps"]["bundled_supply_pct"]
    layer_a = layer_a_clean()
    assert layer_a.token2022_or_hook_risk is CheckStatus.PASS
    layer_b = layer_b_clean(
        bundled_supply_pct=float(caps["threshold"]),
        top10_excluding_lp_pct=0.0,
        funding_cluster_size=0,
    )
    memory = memory_clean(death_rate=None, prior_token_count=0)
    score = compute_score(layer_a, layer_b, memory)
    assert score >= int(caps["min_score"])


def test_death_rate_cap_not_bypassed_by_good_metadata():
    caps = load_scoring()["caps"]["death_rate"]
    layer_a = layer_a_clean()
    layer_b = layer_b_clean(bundled_supply_pct=0.0)
    memory = memory_clean(death_rate=float(caps["threshold"]), prior_token_count=0)
    score = compute_score(layer_a, layer_b, memory)
    assert score >= int(caps["min_score"])


def test_empty_unknown_inputs_do_not_invent_high_score():
    score = compute_score(layer_a_unknown(), layer_b_unknown(), memory_clean())
    assert score == 0
