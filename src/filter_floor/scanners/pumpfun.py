"""Pump.fun Layer A helpers. Bonding-curve inventory is a caution detail, not auto-AVOID."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from solders.pubkey import Pubkey

from filter_floor.adapters.b58 import b58decode
from filter_floor.adapters.rpc import AccountInfo, RpcError, SolanaRpc
from filter_floor.models import LayerA, LayerB
from filter_floor.scanners.base import Scanner, unknown_layer_b

# Same address as config/chains.yaml factory_addresses.pumpfun.
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BONDING_CURVE_SEED = b"bonding-curve"

CREATE_DISCRIMINATOR = hashlib.sha256(b"global:create").digest()[:8]
CREATE_V2_DISCRIMINATOR = hashlib.sha256(b"global:create_v2").digest()[:8]
KNOWN_CREATE_DISCS = {CREATE_DISCRIMINATOR, CREATE_V2_DISCRIMINATOR}

BIRTH_CONCENTRATION_NOTE = (
    "not graduated + creator can still dump curve inventory: caution detail, "
    "not auto-AVOID (almost all Pump tokens look like this at second 0)"
)


def pump_program_pubkey() -> Pubkey:
    return Pubkey.from_string(PUMP_PROGRAM_ID)


def bonding_curve_pda(mint: str | Pubkey) -> Pubkey:
    mint_pk = mint if isinstance(mint, Pubkey) else Pubkey.from_string(mint)
    pda, _bump = Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(mint_pk)],
        pump_program_pubkey(),
    )
    return pda


def read_bonding_curve(
    rpc: SolanaRpc,
    mint: str,
) -> dict:
    """Fetch the Pump.fun bonding-curve account. Missing curve is not a FAIL."""
    details: dict = {"token": mint}
    try:
        mint_pk = Pubkey.from_string(mint)
    except Exception:
        details["TODO(verify)"] = "invalid mint; bonding curve not read"
        return details

    pda = bonding_curve_pda(mint_pk)
    details["bonding_curve"] = str(pda)
    try:
        info = rpc.get_account_info(str(pda))
    except RpcError as exc:
        details["rpc_partial_failure"] = True
        details["bonding_curve_error"] = str(exc)
        details["TODO(verify)"] = "bonding curve RPC miss; not auto-AVOID"
        return details

    if info is None:
        details["is_pumpfun"] = False
        details["TODO(verify)"] = "no bonding-curve account; token may not be Pump.fun"
        return details

    details["is_pumpfun"] = True
    details["bonding_curve_owner"] = info.owner
    parsed = _parse_bonding_curve(info)
    details.update(parsed)
    if parsed.get("complete") is False:
        details["bonding_curve_inventory"] = BIRTH_CONCENTRATION_NOTE
    elif parsed.get("complete") is True:
        details["bonding_curve_inventory"] = (
            "bonding curve marked complete (graduated); LP lock still UNKNOWN"
        )
    else:
        details["bonding_curve_inventory"] = BIRTH_CONCENTRATION_NOTE
        details["TODO(verify)"] = (
            "bonding-curve layout not fully parsed; inventory treated as caution, "
            "not auto-AVOID"
        )
    return details


def _parse_bonding_curve(info: AccountInfo) -> dict:
    data = info.data
    out: dict = {}
    # Anchor disc (8) + 5 * u64 reserves/supply + complete bool + optional creator.
    if len(data) < 49:
        out["complete"] = None
        out["TODO(verify)"] = f"bonding-curve account length {len(data)} < 49"
        return out
    offset = 8
    names = (
        "virtual_token_reserves",
        "virtual_sol_reserves",
        "real_token_reserves",
        "real_sol_reserves",
        "token_total_supply",
    )
    for name in names:
        out[name] = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
    complete_byte = data[offset]
    offset += 1
    if complete_byte not in (0, 1):
        out["complete"] = None
        out["TODO(verify)"] = f"bonding-curve complete byte {complete_byte} not 0/1"
        return out
    out["complete"] = bool(complete_byte)
    if len(data) >= offset + 32:
        try:
            out["creator"] = str(Pubkey.from_bytes(data[offset : offset + 32]))
        except Exception:
            out["TODO(verify)"] = "bonding-curve creator bytes not a pubkey"
    return out


@dataclass(frozen=True)
class PumpCreate:
    mint: str
    signature: str | None = None
    creator: str | None = None
    discriminator: str | None = None


def extract_pump_creates(tx: dict, *, signature: str | None = None) -> list[PumpCreate]:
    """Pull Pump.fun create mints from a mocked or RPC getTransaction payload.

    Unknown layouts are skipped (not guessed).
    """
    normalized = _normalize_tx(tx)
    if normalized is None:
        return []
    found: list[PumpCreate] = []
    logs = normalized.get("logs") or []
    log_says_create = any(
        "Instruction: Create" in line or "Instruction: CreateV2" in line
        for line in logs
        if isinstance(line, str)
    )
    for ix in normalized.get("instructions") or []:
        program_id = ix.get("program_id")
        if program_id != PUMP_PROGRAM_ID:
            continue
        data = ix.get("data") or b""
        if not isinstance(data, (bytes, bytearray)):
            continue
        if len(data) < 8 or bytes(data[:8]) not in KNOWN_CREATE_DISCS:
            if log_says_create and bytes(data[:8]) not in KNOWN_CREATE_DISCS:
                # Log looks like create but discriminator is unknown: skip, do not guess.
                continue
            continue
        accounts = ix.get("accounts") or []
        if not accounts:
            continue
        mint = accounts[0]
        creator = accounts[7] if len(accounts) > 7 else None
        found.append(
            PumpCreate(
                mint=str(mint),
                signature=signature,
                creator=str(creator) if creator else None,
                discriminator=bytes(data[:8]).hex(),
            )
        )
    return found


def _normalize_tx(tx: dict) -> dict | None:
    ixs = tx.get("instructions")
    looks_like_mock = (
        isinstance(ixs, list)
        and "message" not in tx
        and (
            "account_keys" in tx
            or any(
                isinstance(ix, dict) and (ix.get("program_id") or ix.get("data_hex"))
                for ix in ixs
            )
        )
    )
    if looks_like_mock:
        return {
            "logs": list(tx.get("logs") or []),
            "instructions": _normalize_mock_instructions(tx),
        }

    inner = tx.get("transaction") if isinstance(tx.get("transaction"), dict) else tx
    if not isinstance(inner, dict):
        return None
    message = inner.get("message")
    if not isinstance(message, dict):
        return None
    account_keys = _account_keys(message.get("accountKeys") or message.get("account_keys") or [])
    raw_ixs = message.get("instructions") or []
    instructions = []
    for ix in raw_ixs:
        if not isinstance(ix, dict):
            continue
        program_id = ix.get("programId") or ix.get("program_id")
        if not program_id:
            idx = ix.get("programIdIndex")
            if isinstance(idx, int) and 0 <= idx < len(account_keys):
                program_id = account_keys[idx]
        accs = ix.get("accounts") or []
        resolved = []
        for acc in accs:
            if isinstance(acc, int) and 0 <= acc < len(account_keys):
                resolved.append(account_keys[acc])
            elif isinstance(acc, str):
                resolved.append(acc)
        data = _ix_data_bytes(ix.get("data"))
        instructions.append(
            {"program_id": program_id, "accounts": resolved, "data": data}
        )
    meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
    logs = meta.get("logMessages") or tx.get("logs") or []
    return {"logs": list(logs), "instructions": instructions}


def _normalize_mock_instructions(tx: dict) -> list[dict]:
    out = []
    for ix in tx.get("instructions") or []:
        if not isinstance(ix, dict):
            continue
        data = ix.get("data")
        if data is None and ix.get("data_hex"):
            data = bytes.fromhex(str(ix["data_hex"]))
        elif isinstance(data, str):
            data = _ix_data_bytes(data)
        out.append(
            {
                "program_id": ix.get("program_id"),
                "accounts": list(ix.get("accounts") or []),
                "data": data if isinstance(data, (bytes, bytearray)) else b"",
            }
        )
    return out


def _account_keys(raw: list) -> list[str]:
    keys: list[str] = []
    for item in raw:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict) and item.get("pubkey"):
            keys.append(str(item["pubkey"]))
    return keys


def _ix_data_bytes(data: object) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if not isinstance(data, str) or not data:
        return b""
    try:
        return b58decode(data)
    except ValueError:
        try:
            return bytes.fromhex(data)
        except ValueError:
            return b""


class PumpfunScanner(Scanner):
    """Layer A for a known/suspected Pump.fun mint. Layer B stays UNKNOWN (M3)."""

    name = "pumpfun"

    def __init__(self, rpc: SolanaRpc | None = None) -> None:
        self.rpc = rpc

    def scan_layer_a(self, token: str) -> LayerA:
        from filter_floor.scanners.solana import scan_solana_layer_a

        return scan_solana_layer_a(token, rpc=self.rpc, pump_hint=True)

    def scan_layer_b(self, token: str) -> LayerB:
        return unknown_layer_b(
            {
                "token": token,
                "TODO(verify)": (
                    "Layer B is computed in the pipeline graph, not the scanner"
                ),
            }
        )
