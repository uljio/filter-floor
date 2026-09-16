"""Hard veto and caution merge. Hand-built ScanResult pieces; no mainnet."""

from pathlib import Path

from filter_floor.models import CheckStatus, LayerA, Verdict
from filter_floor.scoring import compute_score
from filter_floor.vetoes import evaluate
from tests.helpers import layer_a_clean, layer_b_clean, memory_clean

FIXTURES = Path(__file__).parent / "fixtures"


def _layer_a_fixture(name: str, **overrides) -> LayerA:
    data = LayerA.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))
    if not overrides:
        return data
    return data.model_copy(update=overrides)


def _decide(layer_a, layer_b, memory):
    score = compute_score(layer_a, layer_b, memory)
    return score, evaluate(layer_a, layer_b, memory, score)


def test_mint_fail_is_avoid():
    layer_a = _layer_a_fixture("layer_a_mint_fail.json")
    _, decision = _decide(layer_a, layer_b_clean(), memory_clean())
    assert decision.verdict is Verdict.AVOID
    assert any("mint_authority_revoked" in r for r in decision.veto_reasons)


def test_freeze_fail_is_avoid():
    layer_a = _layer_a_fixture("layer_a_freeze_fail.json")
    _, decision = _decide(layer_a, layer_b_clean(), memory_clean())
    assert decision.verdict is Verdict.AVOID
    assert any("freeze_authority_revoked" in r for r in decision.veto_reasons)


def test_all_layer_a_unknown_is_not_pass_filter():
    layer_a = _layer_a_fixture("layer_a_all_unknown.json")
    _, decision = _decide(layer_a, layer_b_clean(), memory_clean())
    assert decision.verdict is not Verdict.PASS_FILTER
    assert decision.verdict is Verdict.CAUTION
    assert any("UNKNOWN" in r for r in decision.caution_reasons)


def test_high_bundle_pct_avoid_per_yaml():
    layer_b = layer_b_clean(bundled_supply_pct=50.0, bundle_detected=CheckStatus.FAIL)
    _, decision = _decide(layer_a_clean(), layer_b, memory_clean())
    assert decision.verdict is Verdict.AVOID
    assert any("bundled_supply_pct" in r for r in decision.veto_reasons)


def test_mid_bundle_pct_caution_per_yaml():
    layer_b = layer_b_clean(bundled_supply_pct=25.0)
    _, decision = _decide(layer_a_clean(), layer_b, memory_clean())
    assert decision.verdict is Verdict.CAUTION
    assert decision.verdict is not Verdict.AVOID
    assert any("bundled_supply_pct" in r for r in decision.caution_reasons)


def test_serial_deployer_death_rate_avoid():
    memory = memory_clean(prior_token_count=5, death_rate=0.70)
    _, decision = _decide(layer_a_clean(), layer_b_clean(), memory)
    assert decision.verdict is Verdict.AVOID
    assert any("death_rate" in r for r in decision.veto_reasons)


def test_same_funder_known_bad_avoid():
    layer_b = layer_b_clean(same_funder_as_known_bad=CheckStatus.FAIL)
    _, decision = _decide(layer_a_clean(), layer_b, memory_clean())
    assert decision.verdict is Verdict.AVOID
    assert any("same_funder_as_known_bad" in r for r in decision.veto_reasons)


def test_null_death_rate_with_prior_tokens_is_not_pass_filter():
    memory = memory_clean(prior_token_count=5, death_rate=None)
    _, decision = _decide(layer_a_clean(), layer_b_clean(), memory)
    assert decision.verdict is not Verdict.PASS_FILTER
    assert decision.verdict is Verdict.CAUTION
    assert any("null" in r for r in decision.caution_reasons)
    assert memory.death_rate is None


def test_clean_low_score_is_pass_filter():
    from filter_floor.config import load_scoring

    max_score = int(load_scoring()["pass_filter"]["max_score"])
    score, decision = _decide(layer_a_clean(), layer_b_clean(), memory_clean())
    assert score < max_score
    assert decision.verdict is Verdict.PASS_FILTER
    assert decision.veto_reasons == []
    assert decision.caution_reasons == []
