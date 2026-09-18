"""Solana LP burn/lock. PASS/FAIL only when the LP mint account exists."""

from __future__ import annotations

from solders.pubkey import Pubkey

from filter_floor.adapters.rpc import AccountInfo
from filter_floor.adapters.spl_mint import TOKEN_PROGRAM_ID, pack_mint
from filter_floor.models import CheckStatus, Verdict
from filter_floor.scanners.solana import SolanaScanner
from filter_floor.scanners.solana_lp import (
    DEFAULT_RAYDIUM_AMM_V4,
    INCINERATOR,
    pack_raydium_v4_pool,
    read_solana_lp_lock,
)
from filter_floor.scoring import compute_score
from filter_floor.vetoes import evaluate
from tests.fakes import FakeSolanaRpc
from tests.fixtures.solana_mints import (
    mint_authorities_revoked,
    pubkey_from_byte,
    spl_account,
)
from tests.helpers import layer_b_unknown, memory_clean

SOL = "So11111111111111111111111111111111111111112"


def _pk(seed: int) -> str:
    return pubkey_from_byte(seed)


def _pack_token_account(mint: str, owner: str, amount: int) -> bytes:
    return (
        bytes(Pubkey.from_string(mint))
        + bytes(Pubkey.from_string(owner))
        + int(amount).to_bytes(8, "little")
    )


def _rpc_with_lp(*, mint: str, lp_mint: str, lp_holder: str, amount: int = 1000) -> FakeSolanaRpc:
    lp_acct = _pk(90)
    pool = _pk(91)
    pool_data = pack_raydium_v4_pool(
        base_mint=bytes(Pubkey.from_string(mint)),
        quote_mint=bytes(Pubkey.from_string(SOL)),
        lp_mint=bytes(Pubkey.from_string(lp_mint)),
    )
    rpc = FakeSolanaRpc(
        {
            mint: spl_account(mint_authorities_revoked()),
            lp_mint: spl_account(
                pack_mint(
                    mint_authority=None,
                    supply=amount,
                    decimals=6,
                    freeze_authority=None,
                )
            ),
            lp_acct: AccountInfo(
                owner=TOKEN_PROGRAM_ID,
                data=_pack_token_account(lp_mint, lp_holder, amount),
            ),
        }
    )
    rpc.program_accounts[DEFAULT_RAYDIUM_AMM_V4] = [
        (pool, AccountInfo(owner=DEFAULT_RAYDIUM_AMM_V4, data=pool_data))
    ]
    rpc.token_largest[lp_mint] = [{"address": lp_acct, "amount": str(amount)}]
    return rpc


def test_lp_burned_to_incinerator_is_pass():
    mint = _pk(60)
    lp_mint = _pk(61)
    rpc = _rpc_with_lp(mint=mint, lp_mint=lp_mint, lp_holder=INCINERATOR)
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.lp_locked_or_burned is CheckStatus.PASS
    assert layer_a.mint_authority_revoked is CheckStatus.PASS
    decision = evaluate(
        layer_a,
        layer_b_unknown(),
        memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert not any("lp_locked_or_burned" in r for r in decision.veto_reasons)


def test_lp_in_wallet_is_fail_caution_not_avoid():
    mint = _pk(62)
    lp_mint = _pk(63)
    holder = _pk(64)
    rpc = _rpc_with_lp(mint=mint, lp_mint=lp_mint, lp_holder=holder)
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.lp_locked_or_burned is CheckStatus.FAIL
    decision = evaluate(
        layer_a,
        layer_b_unknown(),
        memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is Verdict.CAUTION
    assert decision.verdict is not Verdict.AVOID
    assert not any("lp_locked_or_burned" in r for r in decision.veto_reasons)
    assert any("lp_locked_or_burned" in r for r in decision.caution_reasons)


def test_curve_token_no_pool_lp_unknown_not_avoid_from_lp():
    mint = _pk(65)
    rpc = FakeSolanaRpc({mint: spl_account(mint_authorities_revoked())})
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.lp_locked_or_burned is not CheckStatus.PASS
    decision = evaluate(
        layer_a,
        layer_b_unknown(),
        memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is not Verdict.AVOID
    assert not any("lp_locked_or_burned" in r for r in decision.veto_reasons)
    assert any("lp_locked_or_burned" in r and "UNKNOWN" in r for r in decision.caution_reasons)


def test_lp_mint_missing_is_unknown_not_pass():
    mint = _pk(66)
    lp_mint = _pk(67)
    pool = _pk(68)
    pool_data = pack_raydium_v4_pool(
        base_mint=bytes(Pubkey.from_string(mint)),
        quote_mint=bytes(Pubkey.from_string(SOL)),
        lp_mint=bytes(Pubkey.from_string(lp_mint)),
    )
    rpc = FakeSolanaRpc({mint: spl_account(mint_authorities_revoked())})
    rpc.program_accounts[DEFAULT_RAYDIUM_AMM_V4] = [
        (pool, AccountInfo(owner=DEFAULT_RAYDIUM_AMM_V4, data=pool_data))
    ]
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.lp_locked_or_burned is not CheckStatus.PASS


def test_program_accounts_rpc_miss_is_unknown_not_pass():
    mint = _pk(69)
    rpc = FakeSolanaRpc(
        {mint: spl_account(mint_authorities_revoked())},
        fail_on={"get_program_accounts"},
    )
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.details.get("rpc_partial_failure") is True
    assert layer_a.lp_locked_or_burned is not CheckStatus.PASS


def test_quote_mint_skips_raydium_gpa_unknown_not_pass():
    from filter_floor.scanners.solana_lp import SOL_MINT, USDC_MINT

    for mint in (SOL_MINT, USDC_MINT):
        rpc = FakeSolanaRpc({mint: spl_account(mint_authorities_revoked())})
        layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
        assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
        assert layer_a.lp_locked_or_burned is not CheckStatus.PASS
        assert layer_a.details.get("lp", {}).get("lp_skip") == "quote_mint"
        assert not any(call[0] == "get_program_accounts" for call in rpc.calls)


def test_lp_held_by_raydium_pool_or_amm_not_fail_as_rug():
    mint = _pk(70)
    lp_mint = _pk(71)
    pool = _pk(91)
    lp_vault = _pk(92)
    holders = (DEFAULT_RAYDIUM_AMM_V4, pool, lp_vault)
    for holder in holders:
        rpc = _rpc_with_lp(mint=mint, lp_mint=lp_mint, lp_holder=holder)
        if holder == lp_vault:
            pool_data = pack_raydium_v4_pool(
                base_mint=bytes(Pubkey.from_string(mint)),
                quote_mint=bytes(Pubkey.from_string(SOL)),
                lp_mint=bytes(Pubkey.from_string(lp_mint)),
                lp_vault=bytes(Pubkey.from_string(lp_vault)),
            )
            rpc.program_accounts[DEFAULT_RAYDIUM_AMM_V4] = [
                (pool, AccountInfo(owner=DEFAULT_RAYDIUM_AMM_V4, data=pool_data))
            ]
        layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
        assert layer_a.lp_locked_or_burned is not CheckStatus.FAIL
        assert layer_a.lp_locked_or_burned in (CheckStatus.PASS, CheckStatus.UNKNOWN)
        note = (layer_a.details.get("lp") or {}).get("pools") or []
        assert note, holder
        assert "not FAIL-as-rug" in str(note[0].get("lp_lock") or "")


def test_lp_in_known_locker_is_pass():
    mint = _pk(73)
    lp_mint = _pk(74)
    locker = _pk(75)
    rpc = _rpc_with_lp(mint=mint, lp_mint=lp_mint, lp_holder=locker)
    status, details = read_solana_lp_lock(rpc, mint, known_lockers={locker})
    assert status is CheckStatus.PASS
    assert details["pools"][0]["lp_lock"] == "LP in known locker"


def test_usdc_style_mint_freeze_still_avoid():
    from tests.fixtures.solana_mints import mint_with_mint_and_freeze_authority

    mint = _pk(76)
    rpc = FakeSolanaRpc({mint: spl_account(mint_with_mint_and_freeze_authority())})
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.mint_authority_revoked is CheckStatus.FAIL
    assert layer_a.freeze_authority_revoked is CheckStatus.FAIL
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    decision = evaluate(
        layer_a,
        layer_b_unknown(),
        memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is Verdict.AVOID
    assert any("mint_authority_revoked" in r for r in decision.veto_reasons)
    assert any("freeze_authority_revoked" in r for r in decision.veto_reasons)
