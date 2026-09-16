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


def test_lp_in_wallet_is_fail():
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
    assert decision.verdict is Verdict.AVOID


def test_no_pool_account_is_unknown_not_pass():
    mint = _pk(65)
    rpc = FakeSolanaRpc({mint: spl_account(mint_authorities_revoked())})
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.lp_locked_or_burned is not CheckStatus.PASS


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
