"""Dump mocked mint account bytes as JSON fixtures (no mainnet)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from filter_floor.adapters.spl_mint import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from tests.fixtures.solana_mints import (
    mint_authorities_revoked,
    mint_with_freeze_authority,
    mint_with_mint_authority,
    token2022_malformed_tlv,
    token2022_permanent_delegate,
    token2022_transfer_hook,
    token2022_unclassified_extension,
)

OUT = Path(__file__).resolve().parent / "solana"


def _write(name: str, owner: str, data: bytes, expect: dict, note: str) -> None:
    payload = {
        "note": note,
        "owner": owner,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "expect": expect,
    }
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _write(
        "mint_authority_active.json",
        TOKEN_PROGRAM_ID,
        mint_with_mint_authority(),
        {
            "mint_authority_revoked": "FAIL",
            "freeze_authority_revoked": "PASS",
            "token2022_or_hook_risk": "PASS",
        },
        "SPL Token mint with mint authority still set → mint FAIL, AVOID.",
    )
    _write(
        "freeze_authority_active.json",
        TOKEN_PROGRAM_ID,
        mint_with_freeze_authority(),
        {
            "mint_authority_revoked": "PASS",
            "freeze_authority_revoked": "FAIL",
            "token2022_or_hook_risk": "PASS",
        },
        "SPL Token mint with freeze authority still set → freeze FAIL, AVOID.",
    )
    _write(
        "authorities_revoked.json",
        TOKEN_PROGRAM_ID,
        mint_authorities_revoked(),
        {
            "mint_authority_revoked": "PASS",
            "freeze_authority_revoked": "PASS",
            "token2022_or_hook_risk": "PASS",
            "lp_locked_or_burned": "UNKNOWN",
            "honeypot_or_unsellable": "UNKNOWN",
        },
        "Classic SPL mint, authorities revoked. LP/honeypot stay UNKNOWN so not PASS_FILTER.",
    )
    _write(
        "token2022_permanent_delegate.json",
        TOKEN_2022_PROGRAM_ID,
        token2022_permanent_delegate(),
        {"token2022_or_hook_risk": "FAIL"},
        "Token-2022 permanent delegate → token2022 FAIL, AVOID. Never guess PASS.",
    )
    _write(
        "token2022_transfer_hook.json",
        TOKEN_2022_PROGRAM_ID,
        token2022_transfer_hook(),
        {"token2022_or_hook_risk": "FAIL"},
        "Token-2022 transfer hook → token2022 FAIL, AVOID.",
    )
    _write(
        "token2022_unclassified_extension.json",
        TOKEN_2022_PROGRAM_ID,
        token2022_unclassified_extension(),
        {"token2022_or_hook_risk": "UNKNOWN"},
        "Unclassified Token-2022 extension → UNKNOWN, never PASS.",
    )
    _write(
        "token2022_malformed_tlv.json",
        TOKEN_2022_PROGRAM_ID,
        token2022_malformed_tlv(),
        {"token2022_or_hook_risk": "UNKNOWN", "owner_or_upgrade_risk": "UNKNOWN"},
        "Malformed Token-2022 TLV → UNKNOWN, never PASS.",
    )


if __name__ == "__main__":
    main()
