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
CREATE_LOG_MARKERS = ("Instruction: Create", "Instruction: CreateV2")

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


def logs_contain_create(logs: object) -> bool:
    """True when Pump create / create_v2 appears in program logs."""
    if isinstance(logs, str):
        rows = [logs]
    elif isinstance(logs, list):
        rows = logs
    else:
        return False
    for line in rows:
        if not isinstance(line, str):
            continue
        if any(marker in line for marker in CREATE_LOG_MARKERS):
            return True
    return False


def extract_pump_creates(tx: dict, *, signature: str | None = None) -> list[PumpCreate]:
    found, _log_create, _inner_create = extract_pump_create_stats(tx, signature=signature)
    return found


def extract_pump_create_stats(
    tx: dict,
    *,
    signature: str | None = None,
) -> tuple[list[PumpCreate], int, int]:
    """Return (decoded creates, log_create 0/1, inner_create count)."""
    normalized = _normalize_tx(tx)
    if normalized is None:
        return [], 0, 0
    logs = normalized.get("logs") or []
    log_create = 1 if logs_contain_create(logs) else 0
    inner_ixs = normalized.get("inner_instructions") or []
    inner_create = sum(1 for ix in inner_ixs if _is_pump_create_ix(ix))
    found = _creates_from_instructions(
        list(normalized.get("instructions") or []),
        logs=logs,
        signature=signature,
    )
    return found, log_create, inner_create


def pump_ix_summaries(tx: dict) -> list[tuple[str, int]]:
    """(disc_hex, account_count) for each Pump program ix. No full tx dump."""
    normalized = _normalize_tx(tx)
    if normalized is None:
        return []
    out: list[tuple[str, int]] = []
    for ix in normalized.get("instructions") or []:
        if ix.get("program_id") != PUMP_PROGRAM_ID:
            continue
        data = ix.get("data") or b""
        disc = ""
        if isinstance(data, (bytes, bytearray)) and data:
            disc = bytes(data[:8]).hex()
        out.append((disc, len(ix.get("accounts") or [])))
    return out


def _is_pump_create_ix(ix: dict) -> bool:
    if ix.get("program_id") != PUMP_PROGRAM_ID:
        return False
    data = ix.get("data") or b""
    return isinstance(data, (bytes, bytearray)) and bytes(data[:8]) in KNOWN_CREATE_DISCS


def _creates_from_instructions(
    instructions: list[dict],
    *,
    logs: list,
    signature: str | None,
) -> list[PumpCreate]:
    found: list[PumpCreate] = []
    log_says_create = logs_contain_create(logs)
    for ix in instructions:
        if not _is_pump_create_ix(ix):
            data = ix.get("data") or b""
            if (
                log_says_create
                and ix.get("program_id") == PUMP_PROGRAM_ID
                and isinstance(data, (bytes, bytearray))
                and bytes(data[:8]) not in KNOWN_CREATE_DISCS
            ):
                continue
            continue
        accounts = ix.get("accounts") or []
        if not accounts:
            continue
        mint = accounts[0]
        creator = accounts[7] if len(accounts) > 7 else None
        data = ix.get("data") or b""
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
    inner_mock = tx.get("inner_instructions")
    looks_like_mock = (
        "message" not in tx
        and "transaction" not in tx
        and (
            isinstance(ixs, list)
            or isinstance(inner_mock, list)
        )
    )
    if looks_like_mock:
        outer = _normalize_mock_instructions(tx)
        inner = _normalize_mock_instructions(
            {"instructions": tx.get("inner_instructions") or []}
        )
        if not inner:
            meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
            inner = _inner_instructions(meta, _account_keys(tx.get("account_keys") or []))
        return {
            "logs": list(tx.get("logs") or []),
            "instructions": outer + inner,
            "inner_instructions": inner,
        }

    inner = tx.get("transaction") if isinstance(tx.get("transaction"), dict) else tx
    if not isinstance(inner, dict):
        return None
    message = inner.get("message")
    if not isinstance(message, dict):
        return None
    account_keys = _account_keys(message.get("accountKeys") or message.get("account_keys") or [])
    meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
    loaded = meta.get("loadedAddresses") or {}
    if isinstance(loaded, dict):
        for group in ("writable", "readonly"):
            for item in loaded.get(group) or []:
                if isinstance(item, str):
                    account_keys.append(item)
                elif isinstance(item, dict) and item.get("pubkey"):
                    account_keys.append(str(item["pubkey"]))
    outer: list[dict] = []
    for ix in message.get("instructions") or []:
        parsed = _normalize_one_ix(ix, account_keys)
        if parsed is not None:
            outer.append(parsed)
    inner_ixs = _inner_instructions(meta, account_keys)
    logs = meta.get("logMessages") or tx.get("logs") or []
    return {
        "logs": list(logs),
        "instructions": outer + inner_ixs,
        "inner_instructions": inner_ixs,
    }


def _inner_instructions(meta: dict, account_keys: list[str]) -> list[dict]:
    out: list[dict] = []
    rows = meta.get("innerInstructions") or meta.get("inner_instructions") or []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        for ix in row.get("instructions") or []:
            parsed = _normalize_one_ix(ix, account_keys)
            if parsed is not None:
                out.append(parsed)
    return out


def _normalize_one_ix(ix: object, account_keys: list[str]) -> dict | None:
    if not isinstance(ix, dict):
        return None
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
    parsed = ix.get("parsed")
    if program_id == PUMP_PROGRAM_ID and isinstance(parsed, dict):
        typ = str(parsed.get("type") or "").lower()
        info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
        mint = info.get("mint") or info.get("tokenMint")
        user = info.get("user") or info.get("creator")
        if isinstance(mint, str) and mint and mint not in resolved:
            resolved = [mint, *resolved]
        if isinstance(user, str) and user:
            while len(resolved) < 8:
                resolved.append("")
            resolved[7] = user
        if typ in ("create", "create_v2") and (
            len(data) < 8 or bytes(data[:8]) not in KNOWN_CREATE_DISCS
        ):
            data = CREATE_V2_DISCRIMINATOR if "v2" in typ else CREATE_DISCRIMINATOR
    return {"program_id": program_id, "accounts": resolved, "data": data}


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
    if isinstance(data, list) and data:
        return _ix_data_bytes(data[0])
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
