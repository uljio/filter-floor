"""Solana LP burn/lock. PASS/FAIL only when the LP mint account exists and is read.

Missing pool / missing LP mint / RPC miss = UNKNOWN, never PASS.
Burned or known locker = PASS. Unlocked/pullable LP in a normal wallet = FAIL
(verdict CAUTION, not AVOID). Unknown locker is FAIL, not PASS.
If the largest LP holder is the Raydium pool/vault/AMM program, that is
PASS or UNKNOWN with a note — not FAIL-as-rug.
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
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Quote mints sit in thousands of Raydium pools; gPA is unbounded. Skip.
QUOTE_MINTS = frozenset({SOL_MINT, USDC_MINT, USDT_MINT})
MAX_RAYDIUM_POOLS = 8
RAYDIUM_V4_DATA_SIZE = 752

# SPL / Solana burn destinations.
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
BURN_OWNERS = {INCINERATOR, SYSTEM_PROGRAM}

# LiquidityStateLayoutV4 (Raydium AMM).
RAYDIUM_BASE_VAULT_OFFSET = 336
RAYDIUM_QUOTE_VAULT_OFFSET = 368
RAYDIUM_BASE_MINT_OFFSET = 400
RAYDIUM_QUOTE_MINT_OFFSET = 432
RAYDIUM_LP_MINT_OFFSET = 464
RAYDIUM_LP_VAULT_OFFSET = 656
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


def pack_raydium_v4_pool(
    *,
    base_mint: bytes,
    quote_mint: bytes,
    lp_mint: bytes,
    lp_vault: bytes | None = None,
    base_vault: bytes | None = None,
    quote_vault: bytes | None = None,
) -> bytes:
    size = RAYDIUM_MIN_POOL_LEN
    if lp_vault is not None:
        size = max(size, RAYDIUM_LP_VAULT_OFFSET + 32)
    if base_vault is not None:
        size = max(size, RAYDIUM_BASE_VAULT_OFFSET + 32)
    if quote_vault is not None:
        size = max(size, RAYDIUM_QUOTE_VAULT_OFFSET + 32)
    data = bytearray(size)
    data[RAYDIUM_BASE_MINT_OFFSET : RAYDIUM_BASE_MINT_OFFSET + 32] = base_mint
    data[RAYDIUM_QUOTE_MINT_OFFSET : RAYDIUM_QUOTE_MINT_OFFSET + 32] = quote_mint
    data[RAYDIUM_LP_MINT_OFFSET : RAYDIUM_LP_MINT_OFFSET + 32] = lp_mint
    if base_vault is not None:
        data[RAYDIUM_BASE_VAULT_OFFSET : RAYDIUM_BASE_VAULT_OFFSET + 32] = base_vault
    if quote_vault is not None:
        data[RAYDIUM_QUOTE_VAULT_OFFSET : RAYDIUM_QUOTE_VAULT_OFFSET + 32] = quote_vault
    if lp_vault is not None:
        data[RAYDIUM_LP_VAULT_OFFSET : RAYDIUM_LP_VAULT_OFFSET + 32] = lp_vault
    return bytes(data)


def _pubkey_at(data: bytes, offset: int) -> str | None:
    if len(data) < offset + 32:
        return None
    raw = bytes(data[offset : offset + 32])
    if raw == bytes(32):
        return None
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(raw))


def parse_raydium_v4_lp_mint(data: bytes) -> str | None:
    return _pubkey_at(data, RAYDIUM_LP_MINT_OFFSET)


def parse_raydium_v4_protocol_owners(pool: str, data: bytes) -> set[str]:
    """Pool account, AMM program, and vaults are not unlocked-wallet rugs."""
    owners = {pool, raydium_amm_v4_program()}
    for offset in (
        RAYDIUM_BASE_VAULT_OFFSET,
        RAYDIUM_QUOTE_VAULT_OFFSET,
        RAYDIUM_LP_VAULT_OFFSET,
    ):
        pubkey = _pubkey_at(data, offset)
        if pubkey:
            owners.add(pubkey)
    return owners


def known_lp_lockers() -> set[str]:
    """Verified locker programs from config. Empty means none classified as PASS."""
    try:
        rows = (load_chains().get("solana") or {}).get("known_lp_lockers") or []
    except (OSError, ValueError):
        return set()
    return {str(item).strip() for item in rows if str(item).strip()}


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
                program,
                filters=[
                    {"dataSize": RAYDIUM_V4_DATA_SIZE},
                    _memcmp_filter(offset, token),
                ],
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


def _owner_kind(owner: str, protocol_owners: set[str], lockers: set[str]) -> str:
    if owner in BURN_OWNERS:
        return "burn"
    if owner in lockers:
        return "locker"
    if owner in protocol_owners:
        return "protocol"
    return "wallet"


def _classify_lp_mint(
    rpc: SolanaRpc,
    lp_mint: str,
    *,
    protocol_owners: set[str] | None = None,
    known_lockers: set[str] | None = None,
) -> tuple[CheckStatus, dict]:
    details: dict = {"lp_mint": lp_mint}
    protocol_owners = set(protocol_owners or ())
    lockers = set(known_lockers or ())
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
    locked = 0
    protocol = 0
    wallet_holders: list[str] = []
    largest_owner: str | None = None
    largest_kind: str | None = None
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
        kind = _owner_kind(owner, protocol_owners, lockers)
        if largest_owner is None:
            largest_owner = owner
            largest_kind = kind
        if kind == "burn":
            burned += amount
        elif kind == "locker":
            locked += amount
            details.setdefault("lp_locker_holders", []).append(
                {"owner": owner, "amount": str(amount)}
            )
        elif kind == "protocol":
            protocol += amount
            details.setdefault("lp_protocol_holders", []).append(
                {"owner": owner, "amount": str(amount)}
            )
        else:
            wallet_holders.append(owner)
            details.setdefault("lp_unlocked_holders", []).append(
                {"owner": owner, "amount": str(amount)}
            )

    if largest_owner is not None:
        details["lp_largest_owner"] = largest_owner
        details["lp_largest_kind"] = largest_kind

    if wallet_holders:
        if largest_kind == "protocol":
            details["lp_lock"] = (
                "largest LP holder is Raydium pool/vault/AMM program; not FAIL-as-rug"
            )
            details["TODO(verify):lp"] = (
                "largest LP holder is protocol; remaining LP in wallets not classified as rug"
            )
            return CheckStatus.UNKNOWN, details
        details["lp_lock"] = "LP tokens in non-burn wallet (unlocked/pullable)"
        return CheckStatus.FAIL, details
    if largest_kind == "protocol" or protocol:
        details["lp_lock"] = (
            "largest LP holder is Raydium pool/vault/AMM program; not FAIL-as-rug"
        )
        return CheckStatus.PASS, details
    if largest_kind == "locker" or locked:
        details["lp_lock"] = "LP in known locker"
        return CheckStatus.PASS, details
    if parsed.supply and burned >= parsed.supply:
        details["lp_lock"] = "all LP in burn/incinerator"
        return CheckStatus.PASS, details
    if burned:
        details["lp_lock"] = "all LP in burn/incinerator"
        return CheckStatus.PASS, details
    if unread:
        details["TODO(verify):lp"] = "LP mint exists but holder owners unread"
        return CheckStatus.UNKNOWN, details
    details["TODO(verify):lp"] = "LP mint exists but lock/burn not classified"
    return CheckStatus.UNKNOWN, details


def read_solana_lp_lock(
    rpc: SolanaRpc,
    token: str,
    *,
    known_lockers: set[str] | None = None,
) -> tuple[CheckStatus, dict]:
    """Classify lp_locked_or_burned for a mint.

    PASS/FAIL only after the LP mint account is fetched. No pool = UNKNOWN.
    Quote mints skip Raydium gPA (unbounded). That is UNKNOWN, never PASS.
    """
    token = token.strip()
    if token in QUOTE_MINTS:
        return CheckStatus.UNKNOWN, {
            "lp_skip": "quote_mint",
            "TODO(verify):lp": (
                "quote mint (SOL/USDC/USDT); Raydium getProgramAccounts skipped "
                "to avoid unbounded pool scan; LP not classified, not PASS"
            ),
        }
    pools, details = _find_raydium_pools(rpc, token)
    if details.get("rpc_partial_failure") and not pools:
        return CheckStatus.UNKNOWN, details
    if not pools:
        details["TODO(verify):lp"] = (
            "no Raydium AMM v4 pool account for this mint; LP not read, not PASS"
        )
        return CheckStatus.UNKNOWN, details
    if len(pools) > MAX_RAYDIUM_POOLS:
        details["raydium_pool_count"] = len(pools)
        details["TODO(verify):lp"] = (
            f"Raydium returned {len(pools)} pools (cap {MAX_RAYDIUM_POOLS}); "
            "LP not fully classified, not PASS"
        )
        return CheckStatus.UNKNOWN, details

    lockers = set(known_lockers) if known_lockers is not None else known_lp_lockers()
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
        protocol_owners = parse_raydium_v4_protocol_owners(pubkey, info.data)
        status, lp_details = _classify_lp_mint(
            rpc,
            lp_mint,
            protocol_owners=protocol_owners,
            known_lockers=lockers,
        )
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
