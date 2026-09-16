"""Builders for mocked SPL / Token-2022 mint accounts."""

from __future__ import annotations

from solders.pubkey import Pubkey

from filter_floor.adapters.rpc import AccountInfo
from filter_floor.adapters.spl_mint import (
    EXT_METADATA_POINTER,
    EXT_PERMANENT_DELEGATE,
    EXT_TRANSFER_FEE_CONFIG,
    EXT_TRANSFER_HOOK,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    pack_mint,
    pack_token2022_mint,
)

AUTH_MINT = bytes([1] * 32)
AUTH_FREEZE = bytes([2] * 32)
DELEGATE = bytes([3] * 32)
HOOK_PROGRAM = bytes([4] * 32)
SUPPLY = 1_000_000_000
DECIMALS = 6


def pubkey_from_byte(seed: int) -> str:
    return str(Pubkey.from_bytes(bytes([seed & 0xFF] * 32)))


def account(owner: str, data: bytes) -> AccountInfo:
    return AccountInfo(owner=owner, data=data, lamports=1_000_000)


def mint_with_mint_authority() -> bytes:
    return pack_mint(
        mint_authority=AUTH_MINT,
        supply=SUPPLY,
        decimals=DECIMALS,
        freeze_authority=None,
    )


def mint_with_freeze_authority() -> bytes:
    return pack_mint(
        mint_authority=None,
        supply=SUPPLY,
        decimals=DECIMALS,
        freeze_authority=AUTH_FREEZE,
    )


def mint_authorities_revoked() -> bytes:
    return pack_mint(
        mint_authority=None,
        supply=SUPPLY,
        decimals=DECIMALS,
        freeze_authority=None,
    )


def token2022_permanent_delegate() -> bytes:
    return pack_token2022_mint(
        mint_authorities_revoked(),
        [(EXT_PERMANENT_DELEGATE, DELEGATE)],
    )


def token2022_transfer_hook() -> bytes:
    return pack_token2022_mint(
        mint_authorities_revoked(),
        [(EXT_TRANSFER_HOOK, HOOK_PROGRAM)],
    )


def token2022_transfer_fee() -> bytes:
    # Presence is enough to FAIL; payload need not match on-chain layout.
    return pack_token2022_mint(
        mint_authorities_revoked(),
        [(EXT_TRANSFER_FEE_CONFIG, bytes(108))],
    )


def token2022_metadata_only() -> bytes:
    return pack_token2022_mint(
        mint_authorities_revoked(),
        [(EXT_METADATA_POINTER, bytes(32))],
    )


def token2022_unclassified_extension() -> bytes:
    return pack_token2022_mint(
        mint_authorities_revoked(),
        [(99, b"\x01\x02")],
    )


def token2022_malformed_tlv() -> bytes:
    base = pack_token2022_mint(mint_authorities_revoked(), [])
    # Claim a 400-byte extension that is not present.
    return base + (1).to_bytes(2, "little") + (400).to_bytes(2, "little") + b"\x00"


def spl_account(data: bytes) -> AccountInfo:
    return account(TOKEN_PROGRAM_ID, data)


def token2022_account(data: bytes) -> AccountInfo:
    return account(TOKEN_2022_PROGRAM_ID, data)
