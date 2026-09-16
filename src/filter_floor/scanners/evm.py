"""EVM Layer A (Base first): owner(), bytecode selectors, pool discovery.

Sell simulation is a stub: honeypot_or_unsellable is UNKNOWN until a real
fork / anvil / eth_call path exists. Missing RPC is UNKNOWN, never PASS.
Layer B is computed by the pipeline graph (Milestone 3).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from filter_floor.models import CheckStatus, LayerA, LayerB
from filter_floor.scanners.base import Scanner, unknown_layer_a, unknown_layer_b

# Canonical Base quote tokens (not factories). Used only for Uniswap V3 getPool.
BASE_QUOTE_TOKENS = {
    "weth": "0x4200000000000000000000000000000000000006",
    "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

UNISWAP_V3_FEE_TIERS = (100, 500, 3000, 10000)

OWNER_SELECTOR = "8da5cb5b"  # owner()
GET_OWNER_SELECTOR = "893d20e8"  # getOwner()
GET_POOL_SELECTOR = "1698ee82"  # getPool(address,address,uint24)

# EIP-1967 proxy slots.
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
EIP1967_ADMIN_SLOT = (
    "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
)

# PUSH4 selectors commonly used by mint / pause / blacklist / fee / upgrade.
SELECTOR_GROUPS: dict[str, frozenset[str]] = {
    "mint": frozenset(
        {
            "40c10f19",  # mint(address,uint256)
            "a0712d68",  # mint(uint256)
            "449a52f8",  # mintTo(address,uint256)
            "6a627842",  # mint(address)
        }
    ),
    "pause": frozenset(
        {
            "8456cb59",  # pause()
            "3f4ba83a",  # unpause()
            "5c975abb",  # paused()
        }
    ),
    "blacklist": frozenset(
        {
            "f9f92be4",  # blacklist(address)
            "0ecb93c0",  # addBlackList(address)
            "e4997dc5",  # removeBlackList(address)
            "e47d6060",  # isBlackListed(address)
            "fe575a87",  # isBlacklisted(address)
            "f3bdc228",  # destroyBlackFunds(address)
        }
    ),
    "setFee": frozenset(
        {
            "69fe0e2d",  # setFee(uint256)
            "c49b9a80",  # setSwapAndLiquifyEnabled(bool)
        }
    ),
    "upgrade": frozenset(
        {
            "3659cfe6",  # upgradeTo(address)
            "4f1ef286",  # upgradeToAndCall(address,bytes)
            "5c60da1b",  # implementation()
            "8f283970",  # changeAdmin(address)
            "52d1902d",  # proxiableUUID()
        }
    ),
    "owner": frozenset({OWNER_SELECTOR, GET_OWNER_SELECTOR, "715018a6", "f2fde38b"}),
}

ZERO_ADDRESS = "0x" + ("0" * 40)


class EvmRpc:
    """JSON-RPC surface used by Layer A. None means miss / revert / incomplete."""

    def get_code(self, address: str) -> str | None:
        raise NotImplementedError

    def eth_call(self, to: str, data: str) -> str | None:
        raise NotImplementedError

    def get_storage_at(self, address: str, slot: str) -> str | None:
        return None

    def get_logs(
        self,
        address: str,
        topics: list[str] | None = None,
        from_block: str = "earliest",
        to_block: str = "latest",
    ) -> list | None:
        return None


class HttpEvmRpc(EvmRpc):
    def __init__(self, url: str, timeout_s: float = 10.0) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def get_code(self, address: str) -> str | None:
        return self._rpc("eth_getCode", [normalize_address(address), "latest"])

    def eth_call(self, to: str, data: str) -> str | None:
        return self._rpc(
            "eth_call",
            [{"to": normalize_address(to), "data": data}, "latest"],
        )

    def get_storage_at(self, address: str, slot: str) -> str | None:
        return self._rpc(
            "eth_getStorageAt",
            [normalize_address(address), slot, "latest"],
        )

    def get_logs(
        self,
        address: str,
        topics: list[str] | None = None,
        from_block: str = "earliest",
        to_block: str = "latest",
    ) -> list | None:
        result = self._rpc_result(
            "eth_getLogs",
            [
                {
                    "address": normalize_address(address),
                    "topics": topics or [],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            ],
        )
        if not isinstance(result, list):
            return None
        return result

    def _rpc(self, method: str, params: list[Any]) -> str | None:
        result = self._rpc_result(method, params)
        if not isinstance(result, str):
            return None
        return result

    def _rpc_result(self, method: str, params: list[Any]) -> Any:
        try:
            response = httpx.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        if not isinstance(body, dict) or body.get("error"):
            return None
        return body.get("result")


def normalize_address(value: str) -> str:
    text = value.strip().lower()
    if not text.startswith("0x"):
        text = "0x" + text
    return text


def is_zero_address(value: str | None) -> bool:
    if value is None:
        return False
    hexpart = normalize_address(value)[2:]
    return hexpart != "" and set(hexpart) <= {"0"}


def pad_address(value: str) -> str:
    hexpart = normalize_address(value)[2:]
    return hexpart.rjust(64, "0")


def pad_uint(value: int) -> str:
    return format(value, "x").rjust(64, "0")


def decode_address(word: str | None) -> str | None:
    if word is None:
        return None
    hexpart = word.strip().lower().removeprefix("0x")
    if len(hexpart) < 40:
        return None
    return "0x" + hexpart[-40:]


def extract_push4_selectors(bytecode_hex: str) -> set[str]:
    hexpart = bytecode_hex.strip().lower().removeprefix("0x")
    if len(hexpart) % 2:
        hexpart = "0" + hexpart
    try:
        code = bytes.fromhex(hexpart)
    except ValueError:
        return set()
    found: set[str] = set()
    i = 0
    length = len(code)
    while i < length:
        if code[i] == 0x63 and i + 4 < length:
            found.add(code[i + 1 : i + 5].hex())
            i += 5
            continue
        i += 1
    return found


def encode_owner_call() -> str:
    return "0x" + OWNER_SELECTOR


def encode_get_owner_call() -> str:
    return "0x" + GET_OWNER_SELECTOR


def encode_get_pool(token_a: str, token_b: str, fee: int) -> str:
    return "0x" + GET_POOL_SELECTOR + pad_address(token_a) + pad_address(token_b) + pad_uint(fee)


def simulate_sell_stub(
    *,
    token: str,
    pool: str | None,
    rpc: EvmRpc | None,
) -> tuple[CheckStatus, dict[str, Any]]:
    """Sell simulation is not implemented in M2.

    Must not return PASS. Missing fork/anvil/eth_call = UNKNOWN → CAUTION.
    """
    _ = (token, pool, rpc)
    return CheckStatus.UNKNOWN, {
        "sell_simulation": "stub",
        "sell_simulation_status": "UNKNOWN",
        "TODO(verify)": (
            "sell simulation returns UNKNOWN until fork/anvil/eth_call actually works"
        ),
    }


def _matched_selectors(found: set[str], group: str) -> list[str]:
    hits = sorted(found & SELECTOR_GROUPS[group])
    return hits


def _status_for_privileged_selector(
    hits: list[str],
    owner: str | None,
    owner_known: bool,
) -> CheckStatus:
    """FAIL if a privileged selector exists and owner is not clearly renounced.

    Unproven mint/pause with a zero or missing owner stays UNKNOWN, never PASS.
    """
    if not hits:
        return CheckStatus.PASS
    if owner_known and owner is not None and not is_zero_address(owner):
        return CheckStatus.FAIL
    return CheckStatus.UNKNOWN


class EvmScanner(Scanner):
    name = "evm-layer-a"

    def __init__(
        self,
        rpc: EvmRpc | None = None,
        *,
        chain_name: str = "evm",
        chain_id: int | None = None,
        factory_addresses: dict[str, str] | None = None,
        explorer_url: str = "",
        quote_tokens: dict[str, str] | None = None,
        gated: bool = False,
        gate_reason: str = "",
    ) -> None:
        self.rpc = rpc
        self.chain_name = chain_name
        self.chain_id = chain_id
        self.factory_addresses = {
            key: (value or "").strip()
            for key, value in (factory_addresses or {}).items()
        }
        self.explorer_url = explorer_url
        self.quote_tokens = quote_tokens or {}
        self.gated = gated
        self.gate_reason = gate_reason or (
            "chain gated; factory unverified; VERIFY ON-CHAIN BEFORE ENABLE"
        )

    def scan_layer_a(self, token: str) -> LayerA:
        token_norm = normalize_address(token)
        details: dict[str, Any] = {
            "token": token,
            "token_normalized": token_norm,
            "chain": self.chain_name,
            "sources": [self.name],
        }
        if self.chain_id is not None:
            details["chain_id"] = self.chain_id

        if self.gated:
            details["TODO(verify)"] = self.gate_reason
            details["gated"] = True
            return unknown_layer_a(details)

        if self.rpc is None:
            details["TODO(verify)"] = "RPC not configured; Layer A chain facts not fetched"
            details["rpc_partial_failure"] = True
            return unknown_layer_a(details)

        code = self.rpc.get_code(token_norm)
        if code is None:
            details["TODO(verify)"] = "eth_getCode RPC miss"
            details["rpc_partial_failure"] = True
            return unknown_layer_a(details)

        code_hex = code.strip().lower()
        details["bytecode_len"] = max(0, (len(code_hex) - 2) // 2) if code_hex.startswith("0x") else 0
        if code_hex in {"0x", "0x0", ""}:
            details["TODO(verify)"] = "no contract bytecode at token address"
            details["bytecode_empty"] = True
            return unknown_layer_a(details)

        found = extract_push4_selectors(code_hex)
        mint_hits = _matched_selectors(found, "mint")
        pause_hits = _matched_selectors(found, "pause")
        blacklist_hits = _matched_selectors(found, "blacklist")
        fee_hits = _matched_selectors(found, "setFee")
        upgrade_hits = _matched_selectors(found, "upgrade")
        freeze_hits = pause_hits + blacklist_hits

        details["selectors"] = {
            "mint": mint_hits,
            "pause": pause_hits,
            "blacklist": blacklist_hits,
            "setFee": fee_hits,
            "upgrade": upgrade_hits,
        }

        owner, owner_known, owner_notes = self._read_owner(token_norm)
        details.update(owner_notes)
        if owner is not None:
            details["owner"] = owner

        proxy_notes = self._read_proxy_slots(token_norm)
        details.update(proxy_notes)
        proxy_impl = proxy_notes.get("eip1967_implementation")
        proxy_admin = proxy_notes.get("eip1967_admin")
        is_proxy = bool(
            (proxy_impl and not is_zero_address(str(proxy_impl)))
            or (proxy_admin and not is_zero_address(str(proxy_admin)))
        )

        mint_status = _status_for_privileged_selector(mint_hits, owner, owner_known)
        freeze_status = _status_for_privileged_selector(freeze_hits, owner, owner_known)
        fee_status = _status_for_privileged_selector(fee_hits, owner, owner_known)

        owner_status = self._owner_upgrade_status(
            owner=owner,
            owner_known=owner_known,
            upgrade_hits=upgrade_hits,
            is_proxy=is_proxy,
        )

        pools = self._discover_pools(token_norm)
        details["pools"] = pools
        unconfigured = [
            name
            for name, addr in self.factory_addresses.items()
            if not addr
        ]
        if unconfigured:
            details["unconfigured_factories"] = unconfigured
            details["TODO(verify):factories"] = (
                "empty factory addresses are not treated as PASS; "
                "VERIFY ON-CHAIN BEFORE ENABLE"
            )
        # Pair found does not prove LP is locked/burned.
        details["lp_locked_or_burned"] = (
            "UNKNOWN: pool discovery does not prove LP NFT/token is burned "
            "or in a known locker; unknown locker is not PASS"
        )

        honeypot_status, honeypot_details = simulate_sell_stub(
            token=token_norm,
            pool=pools[0]["pool"] if pools else None,
            rpc=self.rpc,
        )
        details.update(honeypot_details)

        if mint_status is CheckStatus.UNKNOWN and mint_hits:
            details["TODO(verify):mint"] = (
                "mint selector present but owner is zero or unread; "
                "not proven uncallable"
            )
        if freeze_status is CheckStatus.UNKNOWN and freeze_hits:
            details["TODO(verify):freeze"] = (
                "pause/blacklist selector present but owner is zero or unread"
            )
        if fee_status is CheckStatus.UNKNOWN and fee_hits:
            details["TODO(verify):setFee"] = (
                "setFee selector present but owner is zero or unread"
            )
        if owner_status is CheckStatus.UNKNOWN:
            details["TODO(verify):owner"] = (
                details.get("TODO(verify):owner")
                or "owner() unread or ambiguous; not PASS"
            )

        return LayerA(
            mint_authority_revoked=mint_status,
            freeze_authority_revoked=freeze_status,
            lp_locked_or_burned=CheckStatus.UNKNOWN,
            honeypot_or_unsellable=honeypot_status,
            owner_or_upgrade_risk=owner_status,
            token2022_or_hook_risk=fee_status,
            details=details,
        )

    def scan_layer_b(self, token: str) -> LayerB:
        return unknown_layer_b(
            {
                "token": token,
                "chain": self.chain_name,
                "TODO(verify)": "Layer B is computed in the pipeline graph, not the scanner",
            }
        )

    def _read_owner(self, token: str) -> tuple[str | None, bool, dict[str, Any]]:
        notes: dict[str, Any] = {}
        if self.rpc is None:
            notes["TODO(verify):owner"] = "RPC missing; owner() not called"
            return None, False, notes

        raw = self.rpc.eth_call(token, encode_owner_call())
        source = "owner()"
        if raw is None:
            raw = self.rpc.eth_call(token, encode_get_owner_call())
            source = "getOwner()"
        if raw is None:
            notes["owner_call"] = "reverted_or_rpc_miss"
            notes["TODO(verify):owner"] = "owner() / getOwner() unread"
            return None, False, notes

        owner = decode_address(raw)
        if owner is None:
            notes["owner_call"] = "undecodable"
            notes["TODO(verify):owner"] = "owner() result not an address"
            return None, False, notes
        notes["owner_call"] = source
        return owner, True, notes

    def _read_proxy_slots(self, token: str) -> dict[str, Any]:
        notes: dict[str, Any] = {}
        if self.rpc is None:
            return notes
        impl = decode_address(self.rpc.get_storage_at(token, EIP1967_IMPLEMENTATION_SLOT))
        admin = decode_address(self.rpc.get_storage_at(token, EIP1967_ADMIN_SLOT))
        if impl is not None and not is_zero_address(impl):
            notes["eip1967_implementation"] = impl
        if admin is not None and not is_zero_address(admin):
            notes["eip1967_admin"] = admin
        return notes

    def _owner_upgrade_status(
        self,
        *,
        owner: str | None,
        owner_known: bool,
        upgrade_hits: list[str],
        is_proxy: bool,
    ) -> CheckStatus:
        if upgrade_hits or is_proxy:
            return CheckStatus.FAIL
        if owner_known and owner is not None:
            if is_zero_address(owner):
                return CheckStatus.PASS
            return CheckStatus.FAIL
        return CheckStatus.UNKNOWN

    def _discover_pools(self, token: str) -> list[dict[str, Any]]:
        pools: list[dict[str, Any]] = []
        if self.rpc is None:
            return pools
        factory = (self.factory_addresses.get("uniswap_v3") or "").strip()
        if not factory:
            return pools
        quotes = self.quote_tokens or BASE_QUOTE_TOKENS
        for quote_name, quote_addr in quotes.items():
            if not quote_addr:
                continue
            if normalize_address(quote_addr) == token:
                continue
            for fee in UNISWAP_V3_FEE_TIERS:
                data = encode_get_pool(token, quote_addr, fee)
                raw = self.rpc.eth_call(factory, data)
                if raw is None:
                    continue
                pool = decode_address(raw)
                if pool is None or is_zero_address(pool):
                    continue
                pools.append(
                    {
                        "factory": "uniswap_v3",
                        "factory_address": normalize_address(factory),
                        "quote": quote_name,
                        "quote_address": normalize_address(quote_addr),
                        "fee": fee,
                        "pool": pool,
                    }
                )
        return pools


def rpc_from_env(rpc_env_key: str) -> HttpEvmRpc | None:
    url = (os.environ.get(rpc_env_key) or "").strip()
    if not url:
        return None
    return HttpEvmRpc(url)
