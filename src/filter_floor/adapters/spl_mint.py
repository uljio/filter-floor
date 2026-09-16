"""Parse SPL Token / Token-2022 mint accounts. Uncertain facts are UNKNOWN, never PASS."""

from __future__ import annotations

from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from filter_floor.models import CheckStatus

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

MINT_SIZE = 82
ACCOUNT_TYPE_OFFSET = 165
ACCOUNT_TYPE_MINT = 1

# spl-token-2022 ExtensionType. Presence of unclassified types → UNKNOWN.
EXT_UNINITIALIZED = 0
EXT_TRANSFER_FEE_CONFIG = 1
EXT_MINT_CLOSE_AUTHORITY = 3
EXT_CONFIDENTIAL_TRANSFER_MINT = 4
EXT_DEFAULT_ACCOUNT_STATE = 6
EXT_IMMUTABLE_OWNER = 7
EXT_MEMO_TRANSFER = 8
EXT_NON_TRANSFERABLE = 9
EXT_INTEREST_BEARING_CONFIG = 10
EXT_CPI_GUARD = 11
EXT_PERMANENT_DELEGATE = 12
EXT_TRANSFER_HOOK = 14
EXT_METADATA_POINTER = 16
EXT_TOKEN_METADATA = 17
EXT_GROUP_POINTER = 18
EXT_TOKEN_GROUP = 19
EXT_GROUP_MEMBER_POINTER = 20
EXT_TOKEN_GROUP_MEMBER = 21
EXT_CONFIDENTIAL_MINT_BURN = 22
EXT_SCALED_UI_AMOUNT = 23
EXT_PAUSABLE = 24

# Can trap transfers or seize funds. FAIL until a later milestone classifies otherwise.
DANGEROUS_EXTENSIONS = {
    EXT_TRANSFER_FEE_CONFIG: "transfer_fee_config",
    EXT_DEFAULT_ACCOUNT_STATE: "default_account_state",
    EXT_NON_TRANSFERABLE: "non_transferable",
    EXT_PERMANENT_DELEGATE: "permanent_delegate",
    EXT_TRANSFER_HOOK: "transfer_hook",
    EXT_PAUSABLE: "pausable",
}

# Close-authority is an upgrade/owner surface, not a transfer hook.
OWNER_RISK_EXTENSIONS = {
    EXT_MINT_CLOSE_AUTHORITY: "mint_close_authority",
}

# Documented as non-trapping at mint level. Only these may contribute to PASS.
SAFE_EXTENSIONS = {
    EXT_UNINITIALIZED,
    EXT_IMMUTABLE_OWNER,
    EXT_MEMO_TRANSFER,
    EXT_INTEREST_BEARING_CONFIG,
    EXT_CPI_GUARD,
    EXT_METADATA_POINTER,
    EXT_TOKEN_METADATA,
    EXT_GROUP_POINTER,
    EXT_TOKEN_GROUP,
    EXT_GROUP_MEMBER_POINTER,
    EXT_TOKEN_GROUP_MEMBER,
}

EXTENSION_NAMES = {
    **DANGEROUS_EXTENSIONS,
    **OWNER_RISK_EXTENSIONS,
    EXT_UNINITIALIZED: "uninitialized",
    EXT_CONFIDENTIAL_TRANSFER_MINT: "confidential_transfer_mint",
    EXT_IMMUTABLE_OWNER: "immutable_owner",
    EXT_MEMO_TRANSFER: "memo_transfer",
    EXT_INTEREST_BEARING_CONFIG: "interest_bearing_config",
    EXT_CPI_GUARD: "cpi_guard",
    EXT_METADATA_POINTER: "metadata_pointer",
    EXT_TOKEN_METADATA: "token_metadata",
    EXT_GROUP_POINTER: "group_pointer",
    EXT_TOKEN_GROUP: "token_group",
    EXT_GROUP_MEMBER_POINTER: "group_member_pointer",
    EXT_TOKEN_GROUP_MEMBER: "token_group_member",
    EXT_CONFIDENTIAL_MINT_BURN: "confidential_mint_burn",
    EXT_SCALED_UI_AMOUNT: "scaled_ui_amount",
}


@dataclass
class ParsedMint:
    """Facts extracted from mint account bytes. None/unknown never coerce to PASS."""

    program_kind: str
    owner: str
    supply: int | None = None
    decimals: int | None = None
    mint_authority: str | None = None
    mint_authority_status: CheckStatus = CheckStatus.UNKNOWN
    freeze_authority: str | None = None
    freeze_authority_status: CheckStatus = CheckStatus.UNKNOWN
    token2022_status: CheckStatus = CheckStatus.UNKNOWN
    owner_or_upgrade_status: CheckStatus = CheckStatus.UNKNOWN
    extensions: list[str] = field(default_factory=list)
    dangerous_extensions: list[str] = field(default_factory=list)
    parse_notes: list[str] = field(default_factory=list)


def pack_coption_pubkey(pubkey: bytes | None) -> bytes:
    if pubkey is None:
        return (0).to_bytes(4, "little") + bytes(32)
    if len(pubkey) != 32:
        raise ValueError("pubkey must be 32 bytes")
    return (1).to_bytes(4, "little") + pubkey


def pack_mint(
    *,
    mint_authority: bytes | None,
    supply: int,
    decimals: int,
    freeze_authority: bytes | None,
    initialized: bool = True,
) -> bytes:
    data = bytearray()
    data += pack_coption_pubkey(mint_authority)
    data += int(supply).to_bytes(8, "little")
    data.append(decimals & 0xFF)
    data.append(1 if initialized else 0)
    data += pack_coption_pubkey(freeze_authority)
    if len(data) != MINT_SIZE:
        raise ValueError(f"packed mint is {len(data)} bytes, want {MINT_SIZE}")
    return bytes(data)


def pack_token2022_mint(
    base: bytes,
    extensions: list[tuple[int, bytes]] | None = None,
) -> bytes:
    """Official Token-2022 mint: 82-byte mint + pad to 165 + AccountType + TLV."""
    if len(base) != MINT_SIZE:
        raise ValueError("Token-2022 base mint must be 82 bytes")
    buf = bytearray(base)
    buf.extend(bytes(ACCOUNT_TYPE_OFFSET - len(buf)))
    buf.append(ACCOUNT_TYPE_MINT)
    for ext_type, ext_data in extensions or []:
        buf.extend(int(ext_type).to_bytes(2, "little"))
        buf.extend(len(ext_data).to_bytes(2, "little"))
        buf.extend(ext_data)
    return bytes(buf)


def parse_mint_account(owner: str, data: bytes) -> ParsedMint:
    """Parse mint bytes. Incomplete or exotic layouts stay UNKNOWN."""
    notes: list[str] = []
    if owner == TOKEN_PROGRAM_ID:
        program_kind = "spl-token"
    elif owner == TOKEN_2022_PROGRAM_ID:
        program_kind = "token-2022"
    else:
        notes.append(
            "TODO(verify): account owner is not SPL Token or Token-2022; "
            "not treated as a mint"
        )
        return ParsedMint(
            program_kind="unknown",
            owner=owner,
            parse_notes=notes,
        )

    if len(data) < MINT_SIZE:
        notes.append(
            f"TODO(verify): mint data length {len(data)} < {MINT_SIZE}; RPC miss or not a mint"
        )
        return ParsedMint(program_kind=program_kind, owner=owner, parse_notes=notes)

    initialized = data[45]
    if initialized not in (0, 1):
        notes.append("TODO(verify): is_initialized is not 0/1")
        return ParsedMint(program_kind=program_kind, owner=owner, parse_notes=notes)
    if initialized == 0:
        notes.append("TODO(verify): mint account is not initialized")
        return ParsedMint(program_kind=program_kind, owner=owner, parse_notes=notes)

    supply = int.from_bytes(data[36:44], "little")
    decimals = data[44]
    mint_auth, mint_status, mint_note = _parse_coption_pubkey(data, 0)
    freeze_auth, freeze_status, freeze_note = _parse_coption_pubkey(data, 46)
    if mint_note:
        notes.append(mint_note)
    if freeze_note:
        notes.append(freeze_note)

    parsed = ParsedMint(
        program_kind=program_kind,
        owner=owner,
        supply=supply,
        decimals=decimals,
        mint_authority=mint_auth,
        mint_authority_status=mint_status,
        freeze_authority=freeze_auth,
        freeze_authority_status=freeze_status,
        parse_notes=notes,
    )

    if program_kind == "spl-token":
        if len(data) != MINT_SIZE:
            notes.append(
                f"TODO(verify): SPL Token mint length {len(data)} != {MINT_SIZE}; "
                "not classified as a classic mint"
            )
            parsed.token2022_status = CheckStatus.UNKNOWN
            parsed.owner_or_upgrade_status = CheckStatus.UNKNOWN
            return parsed
        parsed.token2022_status = CheckStatus.PASS
        parsed.owner_or_upgrade_status = CheckStatus.PASS
        return parsed

    return _parse_token2022(parsed, data)


def _parse_coption_pubkey(
    data: bytes, offset: int
) -> tuple[str | None, CheckStatus, str | None]:
    option = int.from_bytes(data[offset : offset + 4], "little")
    raw = data[offset + 4 : offset + 36]
    if option == 0:
        return None, CheckStatus.PASS, None
    if option == 1:
        try:
            return str(Pubkey.from_bytes(raw)), CheckStatus.FAIL, None
        except Exception:
            return None, CheckStatus.UNKNOWN, "TODO(verify): COption pubkey bytes invalid"
    return (
        None,
        CheckStatus.UNKNOWN,
        f"TODO(verify): COption tag {option} is not 0/1",
    )


def _parse_token2022(parsed: ParsedMint, data: bytes) -> ParsedMint:
    if len(data) == MINT_SIZE:
        parsed.token2022_status = CheckStatus.PASS
        parsed.owner_or_upgrade_status = CheckStatus.PASS
        parsed.parse_notes.append("token-2022 mint has no extension trailer")
        return parsed

    if len(data) < ACCOUNT_TYPE_OFFSET + 1:
        parsed.parse_notes.append(
            f"TODO(verify): token-2022 data length {len(data)} is between classic "
            "mint and AccountType offset; extensions not classified"
        )
        parsed.token2022_status = CheckStatus.UNKNOWN
        parsed.owner_or_upgrade_status = CheckStatus.UNKNOWN
        return parsed

    account_type = data[ACCOUNT_TYPE_OFFSET]
    if account_type != ACCOUNT_TYPE_MINT:
        parsed.parse_notes.append(
            f"TODO(verify): token-2022 AccountType {account_type} != mint ({ACCOUNT_TYPE_MINT})"
        )
        parsed.token2022_status = CheckStatus.UNKNOWN
        parsed.owner_or_upgrade_status = CheckStatus.UNKNOWN
        return parsed

    tlv, complete = _parse_tlv(data[ACCOUNT_TYPE_OFFSET + 1 :])
    if not complete:
        parsed.parse_notes.append(
            "TODO(verify): token-2022 TLV parse incomplete; not guessing PASS"
        )
        parsed.token2022_status = CheckStatus.UNKNOWN
        parsed.owner_or_upgrade_status = CheckStatus.UNKNOWN
        parsed.extensions = [EXTENSION_NAMES.get(t, f"type_{t}") for t, _ in tlv]
        return parsed

    names: list[str] = []
    dangerous: list[str] = []
    owner_risk: list[str] = []
    unclassified: list[str] = []
    for ext_type, _payload in tlv:
        name = EXTENSION_NAMES.get(ext_type, f"type_{ext_type}")
        names.append(name)
        if ext_type in DANGEROUS_EXTENSIONS:
            dangerous.append(name)
        elif ext_type in OWNER_RISK_EXTENSIONS:
            owner_risk.append(name)
        elif ext_type not in SAFE_EXTENSIONS:
            unclassified.append(name)
            parsed.parse_notes.append(
                f"TODO(verify): token-2022 extension {name} is unclassified; "
                "UNKNOWN not PASS"
            )

    parsed.extensions = names
    parsed.dangerous_extensions = dangerous

    if dangerous:
        parsed.token2022_status = CheckStatus.FAIL
    elif unclassified:
        parsed.token2022_status = CheckStatus.UNKNOWN
    else:
        parsed.token2022_status = CheckStatus.PASS

    if owner_risk:
        parsed.owner_or_upgrade_status = CheckStatus.FAIL
        parsed.parse_notes.append(
            "mint_close_authority present: owner_or_upgrade_risk FAIL"
        )
    elif not complete:
        parsed.owner_or_upgrade_status = CheckStatus.UNKNOWN
    else:
        parsed.owner_or_upgrade_status = CheckStatus.PASS

    return parsed


def _parse_tlv(blob: bytes) -> tuple[list[tuple[int, bytes]], bool]:
    """Return (extensions, complete). complete=False means do not PASS."""
    found: list[tuple[int, bytes]] = []
    offset = 0
    length = len(blob)
    while offset < length:
        remaining = blob[offset:]
        if all(b == 0 for b in remaining):
            return found, True
        if offset + 4 > length:
            return found, False
        ext_type = int.from_bytes(blob[offset : offset + 2], "little")
        ext_len = int.from_bytes(blob[offset + 2 : offset + 4], "little")
        offset += 4
        if offset + ext_len > length:
            return found, False
        found.append((ext_type, bytes(blob[offset : offset + ext_len])))
        offset += ext_len
    return found, True
