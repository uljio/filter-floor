"""Pump.fun Layer A: bonding-curve caution, not auto-AVOID. Mocked RPC only."""

from __future__ import annotations

from filter_floor.adapters.rpc import AccountInfo
from filter_floor.models import CheckStatus, Verdict
from filter_floor.scanners.pumpfun import (
    BIRTH_CONCENTRATION_NOTE,
    CREATE_DISCRIMINATOR,
    PUMP_PROGRAM_ID,
    bonding_curve_pda,
    extract_pump_creates,
)
from filter_floor.scanners.pumpfun import PumpfunScanner
from filter_floor.scanners.solana import SolanaScanner
from filter_floor.scoring import compute_score
from filter_floor.vetoes import evaluate
from tests.fakes import FakeSolanaRpc
from tests.fixtures.solana_mints import (
    mint_authorities_revoked,
    mint_with_mint_authority,
    pubkey_from_byte,
    spl_account,
)
from tests.helpers import layer_b_unknown, memory_clean


def _curve_account(*, complete: bool, creator: bytes | None = None) -> AccountInfo:
    data = bytearray(8)  # discriminator
    data.extend((0).to_bytes(8, "little") * 5)
    data.append(1 if complete else 0)
    data.extend(creator if creator is not None else bytes([9] * 32))
    return AccountInfo(owner=PUMP_PROGRAM_ID, data=bytes(data), lamports=1)


def test_bonding_curve_is_caution_detail_not_avoid():
    mint = pubkey_from_byte(41)
    pda = str(bonding_curve_pda(mint))
    rpc = FakeSolanaRpc(
        {
            mint: spl_account(mint_authorities_revoked()),
            pda: _curve_account(complete=False),
        }
    )
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.details.get("is_pumpfun") is True
    assert layer_a.details.get("bonding_curve_inventory") == BIRTH_CONCENTRATION_NOTE
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.mint_authority_revoked is CheckStatus.PASS
    assert layer_a.freeze_authority_revoked is CheckStatus.PASS
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is not Verdict.AVOID
    assert decision.verdict is Verdict.CAUTION
    assert not any("bonding" in r.lower() and "FAIL" in r for r in decision.veto_reasons)
    assert not any("lp_locked_or_burned" in r for r in decision.veto_reasons)


def test_pumpfun_scanner_mint_fail_still_avoid():
    mint = pubkey_from_byte(42)
    pda = str(bonding_curve_pda(mint))
    rpc = FakeSolanaRpc(
        {
            mint: spl_account(mint_with_mint_authority()),
            pda: _curve_account(complete=False),
        }
    )
    layer_a = PumpfunScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.details.get("is_pumpfun") is True
    assert layer_a.mint_authority_revoked is CheckStatus.FAIL
    decision = evaluate(
        layer_a, layer_b_unknown(), memory_clean(),
        compute_score(layer_a, layer_b_unknown(), memory_clean()),
    )
    assert decision.verdict is Verdict.AVOID


def test_graduated_curve_still_does_not_pass_lp():
    mint = pubkey_from_byte(43)
    pda = str(bonding_curve_pda(mint))
    rpc = FakeSolanaRpc(
        {
            mint: spl_account(mint_authorities_revoked()),
            pda: _curve_account(complete=True),
        }
    )
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.details.get("pumpfun_complete") is True
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.lp_locked_or_burned is not CheckStatus.PASS


def test_extract_pump_create_from_mocked_tx():
    mint = pubkey_from_byte(44)
    creator = pubkey_from_byte(45)
    tx = {
        "logs": ["Program log: Instruction: Create"],
        "account_keys": [mint, creator, PUMP_PROGRAM_ID],
        "instructions": [
            {
                "program_id": PUMP_PROGRAM_ID,
                "accounts": [mint, "a", "b", "c", "d", "e", "f", creator],
                "data_hex": CREATE_DISCRIMINATOR.hex() + "00",
            }
        ],
    }
    found = extract_pump_creates(tx, signature="sig1")
    assert len(found) == 1
    assert found[0].mint == mint
    assert found[0].creator == creator
    assert found[0].signature == "sig1"


def test_extract_pump_create_from_jsonparsed_rpc_shape():
    from filter_floor.adapters.b58 import b58encode

    mint = pubkey_from_byte(47)
    creator = pubkey_from_byte(48)
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": mint, "signer": False, "writable": True},
                    {"pubkey": creator, "signer": True, "writable": True},
                    {"pubkey": PUMP_PROGRAM_ID, "signer": False, "writable": False},
                ],
                "instructions": [
                    {
                        "programId": PUMP_PROGRAM_ID,
                        "accounts": [mint, "a", "b", "c", "d", "e", "f", creator],
                        "data": b58encode(CREATE_DISCRIMINATOR + b"\x00"),
                    }
                ],
            }
        },
        "meta": {"logMessages": ["Program log: Instruction: Create"]},
    }
    found = extract_pump_creates(tx, signature="sig-parsed")
    assert len(found) == 1
    assert found[0].mint == mint
    assert found[0].creator == creator


def test_unknown_pump_discriminator_is_skipped_not_guessed():
    mint = pubkey_from_byte(46)
    tx = {
        "logs": ["Program log: Instruction: Create"],
        "instructions": [
            {
                "program_id": PUMP_PROGRAM_ID,
                "accounts": [mint],
                "data_hex": "deadbeefdeadbeef",
            }
        ],
    }
    assert extract_pump_creates(tx) == []


def test_extract_pump_create_from_inner_instructions():
    from filter_floor.adapters.b58 import b58encode
    from filter_floor.scanners.pumpfun import extract_pump_create_stats

    mint = pubkey_from_byte(49)
    creator = pubkey_from_byte(50)
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": mint, "signer": False, "writable": True},
                    {"pubkey": creator, "signer": True, "writable": True},
                    {"pubkey": PUMP_PROGRAM_ID, "signer": False, "writable": False},
                ],
                "instructions": [
                    {
                        "programId": "ComputeBudget111111111111111111111111111111",
                        "accounts": [],
                        "data": "",
                    }
                ],
            }
        },
        "meta": {
            "logMessages": ["Program log: Instruction: CreateV2"],
            "innerInstructions": [
                {
                    "index": 0,
                    "instructions": [
                        {
                            "programId": PUMP_PROGRAM_ID,
                            "accounts": [
                                mint, "a", "b", "c", "d", "e", "f", creator,
                            ],
                            "data": b58encode(CREATE_DISCRIMINATOR + b"\x00"),
                        }
                    ],
                }
            ],
        },
    }
    found, log_create, inner_create = extract_pump_create_stats(tx, signature="sig-inner")
    assert log_create == 1
    assert inner_create == 1
    assert len(found) == 1
    assert found[0].mint == mint
    assert found[0].creator == creator
    assert extract_pump_creates(tx, signature="sig-inner")[0].mint == mint


def test_pump_ix_summaries_disc_hex_and_account_count():
    from filter_floor.scanners.pumpfun import pump_ix_summaries

    mint = pubkey_from_byte(61)
    tx = {
        "logs": ["Program log: Instruction: CreateV2"],
        "instructions": [
            {
                "program_id": PUMP_PROGRAM_ID,
                "accounts": [mint, "a"],
                "data_hex": "aabbccdd11223344ff",
            }
        ],
    }
    assert pump_ix_summaries(tx) == [("aabbccdd11223344", 2)]
