"""Solana Layer A: mint/freeze/Token-2022 via mocked RPC. No mainnet."""

from __future__ import annotations

import json
from pathlib import Path

from filter_floor.models import Chain, CheckStatus, Verdict
from filter_floor.pipeline import run_scan
from filter_floor.scanners.solana import SolanaScanner
from filter_floor.vetoes import evaluate
from filter_floor.scoring import compute_score
from tests.fakes import FakeSolanaRpc
from tests.fixtures.solana_mints import (
    mint_authorities_revoked,
    mint_with_freeze_authority,
    mint_with_mint_authority,
    pubkey_from_byte,
    spl_account,
    token2022_account,
    token2022_malformed_tlv,
    token2022_metadata_only,
    token2022_permanent_delegate,
    token2022_transfer_fee,
    token2022_transfer_hook,
    token2022_unclassified_extension,
)
from tests.helpers import layer_b_unknown, memory_clean

FIXTURES = Path(__file__).parent / "fixtures" / "solana"


def _scan(mint: str, rpc: FakeSolanaRpc):
    return SolanaScanner(rpc=rpc).scan_layer_a(mint)


def test_mint_authority_active_is_fail():
    mint = pubkey_from_byte(11)
    rpc = FakeSolanaRpc({mint: spl_account(mint_with_mint_authority())})
    layer_a = _scan(mint, rpc)
    assert layer_a.mint_authority_revoked is CheckStatus.FAIL
    assert layer_a.freeze_authority_revoked is CheckStatus.PASS
    assert layer_a.details["supply"] == "1000000000"
    assert layer_a.details["decimals"] == 6
    score = compute_score(layer_a, layer_b_unknown(), memory_clean())
    decision = evaluate(layer_a, layer_b_unknown(), memory_clean(), score)
    assert decision.verdict is Verdict.AVOID
    assert any("mint_authority_revoked" in r for r in decision.veto_reasons)


def test_freeze_authority_active_is_fail():
    mint = pubkey_from_byte(12)
    rpc = FakeSolanaRpc({mint: spl_account(mint_with_freeze_authority())})
    layer_a = _scan(mint, rpc)
    assert layer_a.freeze_authority_revoked is CheckStatus.FAIL
    assert layer_a.mint_authority_revoked is CheckStatus.PASS
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is Verdict.AVOID
    assert any("freeze_authority_revoked" in r for r in decision.veto_reasons)


def test_authorities_revoked_pass_mint_and_freeze_not_whole_layer():
    mint = pubkey_from_byte(13)
    rpc = FakeSolanaRpc({mint: spl_account(mint_authorities_revoked())})
    layer_a = _scan(mint, rpc)
    assert layer_a.mint_authority_revoked is CheckStatus.PASS
    assert layer_a.freeze_authority_revoked is CheckStatus.PASS
    assert layer_a.token2022_or_hook_risk is CheckStatus.PASS
    assert layer_a.owner_or_upgrade_risk is CheckStatus.PASS
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is not Verdict.PASS_FILTER
    assert decision.verdict is Verdict.CAUTION


def test_rpc_miss_is_unknown_never_pass():
    mint = pubkey_from_byte(14)
    rpc = FakeSolanaRpc(fail=True)
    layer_a = _scan(mint, rpc)
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.freeze_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.token2022_or_hook_risk is CheckStatus.UNKNOWN
    assert layer_a.details.get("rpc_partial_failure") is True
    assert layer_a.mint_authority_revoked is not CheckStatus.PASS
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is not Verdict.PASS_FILTER


def test_account_missing_is_unknown_never_pass():
    mint = pubkey_from_byte(15)
    rpc = FakeSolanaRpc({})
    layer_a = _scan(mint, rpc)
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.details.get("account_missing") is True
    assert decision_not_pass(layer_a)


def test_invalid_pubkey_is_unknown_never_pass():
    layer_a = SolanaScanner(rpc=FakeSolanaRpc()).scan_layer_a("not-a-mint")
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.token2022_or_hook_risk is CheckStatus.UNKNOWN
    assert "TODO(verify)" in layer_a.details


def test_token2022_permanent_delegate_is_fail():
    mint = pubkey_from_byte(16)
    rpc = FakeSolanaRpc({mint: token2022_account(token2022_permanent_delegate())})
    layer_a = _scan(mint, rpc)
    assert layer_a.token2022_or_hook_risk is CheckStatus.FAIL
    assert "permanent_delegate" in layer_a.details.get("token2022_dangerous_extensions", [])
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is Verdict.AVOID


def test_token2022_transfer_hook_is_fail():
    mint = pubkey_from_byte(17)
    rpc = FakeSolanaRpc({mint: token2022_account(token2022_transfer_hook())})
    layer_a = _scan(mint, rpc)
    assert layer_a.token2022_or_hook_risk is CheckStatus.FAIL
    assert "transfer_hook" in layer_a.details.get("token2022_dangerous_extensions", [])


def test_token2022_transfer_fee_is_fail():
    mint = pubkey_from_byte(18)
    rpc = FakeSolanaRpc({mint: token2022_account(token2022_transfer_fee())})
    layer_a = _scan(mint, rpc)
    assert layer_a.token2022_or_hook_risk is CheckStatus.FAIL
    assert "transfer_fee_config" in layer_a.details.get("token2022_dangerous_extensions", [])


def test_token2022_unclassified_extension_is_unknown_not_pass():
    mint = pubkey_from_byte(19)
    rpc = FakeSolanaRpc({mint: token2022_account(token2022_unclassified_extension())})
    layer_a = _scan(mint, rpc)
    assert layer_a.token2022_or_hook_risk is CheckStatus.UNKNOWN
    assert layer_a.token2022_or_hook_risk is not CheckStatus.PASS
    assert any("unclassified" in n for n in layer_a.details.get("mint_parse_notes", []))


def test_token2022_malformed_tlv_is_unknown_not_pass():
    mint = pubkey_from_byte(20)
    rpc = FakeSolanaRpc({mint: token2022_account(token2022_malformed_tlv())})
    layer_a = _scan(mint, rpc)
    assert layer_a.token2022_or_hook_risk is CheckStatus.UNKNOWN
    assert layer_a.owner_or_upgrade_risk is CheckStatus.UNKNOWN
    assert layer_a.token2022_or_hook_risk is not CheckStatus.PASS


def test_token2022_metadata_only_is_not_a_trap():
    mint = pubkey_from_byte(21)
    rpc = FakeSolanaRpc({mint: token2022_account(token2022_metadata_only())})
    layer_a = _scan(mint, rpc)
    assert layer_a.token2022_or_hook_risk is CheckStatus.PASS
    assert layer_a.mint_authority_revoked is CheckStatus.PASS
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN


def test_non_token_owner_is_unknown_not_pass():
    mint = pubkey_from_byte(22)
    from tests.fixtures.solana_mints import account, mint_authorities_revoked

    rpc = FakeSolanaRpc(
        {mint: account("11111111111111111111111111111111", mint_authorities_revoked())}
    )
    layer_a = _scan(mint, rpc)
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.token2022_or_hook_risk is CheckStatus.UNKNOWN


def test_json_mint_freeze_fixtures_roundtrip():
    mint_fail = json.loads((FIXTURES / "mint_authority_active.json").read_text(encoding="utf-8"))
    freeze_fail = json.loads((FIXTURES / "freeze_authority_active.json").read_text(encoding="utf-8"))
    assert mint_fail["expect"]["mint_authority_revoked"] == "FAIL"
    assert freeze_fail["expect"]["freeze_authority_revoked"] == "FAIL"
    mint = pubkey_from_byte(31)
    import base64

    rpc = FakeSolanaRpc(
        {
            mint: spl_account(base64.b64decode(mint_fail["data_base64"])),
        }
    )
    layer_a = _scan(mint, rpc)
    assert layer_a.mint_authority_revoked.value == mint_fail["expect"]["mint_authority_revoked"]
    rpc = FakeSolanaRpc(
        {mint: spl_account(base64.b64decode(freeze_fail["data_base64"]))}
    )
    layer_a = _scan(mint, rpc)
    assert layer_a.freeze_authority_revoked.value == freeze_fail["expect"]["freeze_authority_revoked"]


def test_pipeline_mocked_mint_fail_is_avoid(tmp_path):
    mint = pubkey_from_byte(23)
    rpc = FakeSolanaRpc({mint: spl_account(mint_with_mint_authority())})
    result = run_scan(Chain.solana, mint, tmp_path, solana_rpc=rpc)
    assert result.verdict is Verdict.AVOID
    assert "m0-stub" not in result.sources
    assert "solana" in result.sources
    assert (tmp_path / "scans" / f"{result.case_id}.json").is_file()


def test_pipeline_rpc_miss_not_pass_filter(tmp_path):
    mint = pubkey_from_byte(24)
    result = run_scan(Chain.solana, mint, tmp_path, solana_rpc=FakeSolanaRpc(fail=True))
    assert result.verdict is not Verdict.PASS_FILTER
    assert result.verdict is Verdict.CAUTION
    assert result.layer_a.mint_authority_revoked is CheckStatus.UNKNOWN


def decision_not_pass(layer_a) -> bool:
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is not Verdict.PASS_FILTER
    return True
