"""Solana LP burn/lock. PASS/FAIL only when the LP mint account exists and is read.

Missing pool / missing LP mint / RPC miss = UNKNOWN, never PASS.
Unknown locker is not PASS. Unlocked/pullable LP is FAIL.
Pump.fun bonding-curve inventory is not treated as an LP account.
"""

from __future__ import annotations

from filter_floor.adapters.b58 import b58encode
from filter_floor.adapters.rpc import AccountInfo, RpcError, SolanaRpc
from filter_floor.adapters.spl_mint import parse_mint_account
from filter_floor.config import load_chains
from filter_floor.models import CheckStatus

# Published Raydium AMM v4 program (not a guessed factory).
DEFAULT_RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
SOL_MINT = "So11111111111111111111111111111111111111112"

# SPL / Solana burn destinations.
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
BURN_OWNERS = {INCINERATOR, SYSTEM_PROGRAM}

# LiquidityStateLayoutV4 (Raydium AMM).
RAYDIUM_BASE_MINT_OFFSET = 400
RAYDIUM_QUOTE_MINT_OFFSET = 432
RAYDIUM_LP_MINT_OFFSET = 464
RAYDIUM_MIN_POOL_LEN = 496

# SPL token account: mint (32) + owner (32) + amount u64.
SPL_TOKEN_ACCOUNT_MIN = 72


def raydium_amm_v4_program() -> str:
    try:
        factories = (load_chains().get("solana") or {}).get("factory_addresses") or {}
    except (OSError, ValueError):
        return DEFAULT_RAYDIUM_AMM_V4
    return str(factories.get("raydium_amm_v4") or DEFAULT_RAYDIUM_AMM_V4).strip() or (
        DEFAULT_RAYDIUM_AMM_V4
    )


def pack_raydium_v4_pool(*, base_mint: bytes, quote_mint: bytes, lp_mint: bytes) -> bytes:
    data = bytearray(RAYDIUM_MIN_POOL_LEN)
    data[RAYDIUM_BASE_MINT_OFFSET : RAYDIUM_BASE_MINT_OFFSET + 32] = base_mint
    data[RAYDIUM_QUOTE_MINT_OFFSET : RAYDIUM_QUOTE_MINT_OFFSET + 32] = quote_mint
    data[RAYDIUM_LP_MINT_OFFSET : RAYDIUM_LP_MINT_OFFSET + 32] = lp_mint
    return bytes(data)


def parse_raydium_v4_lp_mint(data: bytes) -> str | None:
    if len(data) < RAYDIUM_MIN_POOL_LEN:
        return None
    raw = bytes(data[RAYDIUM_LP_MINT_OFFSET : RAYDIUM_LP_MINT_OFFSET + 32])
    if raw == bytes(32):
        return None
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(raw))


def parse_spl_token_account_owner(data: bytes) -> str | None:
    if len(data) < SPL_TOKEN_ACCOUNT_MIN:
        return None
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(bytes(data[32:64])))


def _memcmp_filter(offset: int, pubkey: str) -> dict:
    from solders.pubkey import Pubkey

    raw = bytes(Pubkey.from_string(pubkey))
    return {"memcmp": {"offset": offset, "bytes": b58encode(raw)}}


def _find_raydium_pools(rpc: SolanaRpc, token: str) -> tuple[list[tuple[str, AccountInfo]], dict]:
    details: dict = {}
    program = raydium_amm_v4_program()
    details["raydium_amm_v4"] = program
    found: list[tuple[str, AccountInfo]] = []
    seen: set[str] = set()
    for offset in (RAYDIUM_BASE_MINT_OFFSET, RAYDIUM_QUOTE_MINT_OFFSET):
        try:
            rows = rpc.get_program_accounts(
                program, filters=[_memcmp_filter(offset, token)]
            )
        except RpcError as exc:
            details["rpc_partial_failure"] = True
            details["lp_rpc_error"] = str(exc)
            details["TODO(verify):lp"] = f"Raydium pool RPC miss: {exc}"
            return [], details
        for pubkey, info in rows:
            if pubkey in seen:
                continue
            seen.add(pubkey)
            found.append((pubkey, info))
    details["raydium_pool_count"] = len(found)
    return found, details


def _classify_lp_mint(rpc: SolanaRpc, lp_mint: str) -> tuple[CheckStatus, dict]:
    details: dict = {"lp_mint": lp_mint}
    try:
        info = rpc.get_account_info(lp_mint)
    except RpcError as exc:
        details["rpc_partial_failure"] = True
        details["TODO(verify):lp"] = f"LP mint RPC miss: {exc}"
        return CheckStatus.UNKNOWN, details

    if info is None:
        details["lp_mint_missing"] = True
        details["TODO(verify):lp"] = "LP mint account missing; not PASS"
        return CheckStatus.UNKNOWN, details

    parsed = parse_mint_account(info.owner, info.data)
    if parsed.supply is not None:
        details["lp_supply"] = str(parsed.supply)
    if parsed.supply == 0:
        details["lp_lock"] = "lp mint supply is 0 (burned)"
        return CheckStatus.PASS, details

    try:
        largest = rpc.get_token_largest_accounts(lp_mint)
    except RpcError as exc:
        details["rpc_partial_failure"] = True
        details["TODO(verify):lp"] = f"getTokenLargestAccounts miss: {exc}"
        return CheckStatus.UNKNOWN, details

    details["lp_largest_count"] = len(largest)
    if not largest:
        details["TODO(verify):lp"] = "LP mint exists but largest accounts unread"
        return CheckStatus.UNKNOWN, details

    burned = 0
    unlocked_holders: list[str] = []
    unread = False
    for row in largest:
        amount = int(row.get("amount") or "0")
        token_account = str(row["address"])
        try:
            acct = rpc.get_account_info(token_account)
        except RpcError as exc:
            details["rpc_partial_failure"] = True
            details["TODO(verify):lp"] = f"LP token account RPC miss: {exc}"
            unread = True
            continue
        if acct is None:
            unread = True
            continue
        owner = parse_spl_token_account_owner(acct.data)
        if owner is None:
            unread = True
            continue
        if owner in BURN_OWNERS:
            burned += amount
        else:
            unlocked_holders.append(owner)
            details.setdefault("lp_unlocked_holders", []).append(
                {"owner": owner, "amount": str(amount)}
            )

    if parsed.supply and burned >= parsed.supply:
        details["lp_lock"] = "all LP in burn/incinerator"
        return CheckStatus.PASS, details
    if unlocked_holders:
        details["lp_lock"] = "LP tokens in non-burn wallet (unlocked/pullable)"
        return CheckStatus.FAIL, details
    if unread:
        details["TODO(verify):lp"] = "LP mint exists but holder owners unread"
        return CheckStatus.UNKNOWN, details
    details["TODO(verify):lp"] = "LP mint exists but lock/burn not classified"
    return CheckStatus.UNKNOWN, details


def read_solana_lp_lock(rpc: SolanaRpc, token: str) -> tuple[CheckStatus, dict]:
    """Classify lp_locked_or_burned for a mint.

    PASS/FAIL only after the LP mint account is fetched. No pool = UNKNOWN.
    """
    pools, details = _find_raydium_pools(rpc, token)
    if details.get("rpc_partial_failure") and not pools:
        return CheckStatus.UNKNOWN, details
    if not pools:
        details["TODO(verify):lp"] = (
            "no Raydium AMM v4 pool account for this mint; LP not read, not PASS"
        )
        return CheckStatus.UNKNOWN, details

    statuses: list[CheckStatus] = []
    pool_notes: list[dict] = []
    for pubkey, info in pools:
        lp_mint = parse_raydium_v4_lp_mint(info.data)
        note = {"pool": pubkey, "lp_mint": lp_mint}
        if not lp_mint:
            note["TODO(verify)"] = "pool account exists but lpMint unparsed"
            statuses.append(CheckStatus.UNKNOWN)
            pool_notes.append(note)
            continue
        status, lp_details = _classify_lp_mint(rpc, lp_mint)
        note.update(lp_details)
        statuses.append(status)
        pool_notes.append(note)
        if lp_details.get("rpc_partial_failure"):
            details["rpc_partial_failure"] = True

    details["pools"] = pool_notes
    if CheckStatus.FAIL in statuses:
        return CheckStatus.FAIL, details
    if CheckStatus.UNKNOWN in statuses:
        details.setdefault(
            "TODO(verify):lp",
            "one or more LP mints unread; not PASS",
        )
        return CheckStatus.UNKNOWN, details
    if statuses and all(s is CheckStatus.PASS for s in statuses):
        return CheckStatus.PASS, details
    return CheckStatus.UNKNOWN, details
