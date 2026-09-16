"""EVM websocket / log helpers for Uniswap V3 PoolCreated + Base watch."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from filter_floor.models import Chain, ScanResult
from filter_floor.scanners.evm import (
    BASE_QUOTE_TOKENS,
    EvmRpc,
    normalize_address,
    rpc_from_env,
)

# keccak256("PoolCreated(address,address,uint24,int24,address)")
POOL_CREATED_TOPIC0 = (
    "0x783cca1c0412dd0d695e784568c96da2e9c04bfcf553250dc2aa4e3986dd160c"
)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_LOOKBACK_BLOCKS = 20

ScanFn = Callable[[Chain, str], ScanResult]


def _topic_address(topic: str) -> str | None:
    hexpart = topic.strip().lower().removeprefix("0x")
    if len(hexpart) < 40:
        return None
    return "0x" + hexpart[-40:]


def _topic_uint(topic: str) -> int | None:
    hexpart = topic.strip().lower().removeprefix("0x")
    if not hexpart:
        return None
    try:
        return int(hexpart, 16)
    except ValueError:
        return None


def decode_pool_created_log(log: dict[str, Any]) -> dict[str, Any] | None:
    """Decode a Uniswap V3 PoolCreated log. Incomplete logs return None."""
    topics = log.get("topics") or []
    if len(topics) < 4:
        return None
    topic0 = str(topics[0]).lower()
    if topic0 != POOL_CREATED_TOPIC0:
        return None
    token0 = _topic_address(str(topics[1]))
    token1 = _topic_address(str(topics[2]))
    fee = _topic_uint(str(topics[3]))
    data = str(log.get("data") or "").strip().lower().removeprefix("0x")
    if token0 is None or token1 is None or fee is None:
        return None
    if len(data) < 128:
        return None
    tick_word = data[:64]
    pool_word = data[64:128]
    try:
        tick_spacing = int.from_bytes(bytes.fromhex(tick_word), "big", signed=True)
    except ValueError:
        return None
    pool = "0x" + pool_word[-40:]
    return {
        "token0": token0,
        "token1": token1,
        "fee": fee,
        "tick_spacing": tick_spacing,
        "pool": pool,
        "address": str(log.get("address") or "").lower(),
        "transaction_hash": log.get("transactionHash"),
        "block_number": log.get("blockNumber"),
    }


def pool_created_matches_token(decoded: dict[str, Any], token: str) -> bool:
    needle = token.strip().lower()
    if not needle.startswith("0x"):
        needle = "0x" + needle
    return decoded.get("token0") == needle or decoded.get("token1") == needle


def launch_token_from_pool(
    decoded: dict[str, Any],
    quote_tokens: dict[str, str] | None = None,
) -> str | None:
    quotes = {
        normalize_address(addr)
        for addr in (quote_tokens or BASE_QUOTE_TOKENS).values()
        if addr
    }
    token0 = decoded.get("token0")
    token1 = decoded.get("token1")
    if token0 in quotes and token1 not in quotes:
        return token1
    if token1 in quotes and token0 not in quotes:
        return token0
    if token0 not in quotes:
        return token0
    if token1 not in quotes:
        return token1
    return None


def poll_interval_s() -> float:
    raw = os.environ.get("BASE_WATCH_POLL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_POLL_SECONDS
    return max(1.0, value)


def poll_new_pools(
    rpc: EvmRpc,
    factory: str,
    *,
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    logs = rpc.get_logs(
        factory,
        [POOL_CREATED_TOPIC0],
        from_block=hex(from_block),
        to_block=hex(to_block),
    )
    if logs is None:
        return []
    found: list[dict[str, Any]] = []
    for log in logs:
        decoded = decode_pool_created_log(log)
        if decoded:
            found.append(decoded)
    return found


def run_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: EvmRpc | None = None,
    factory: str = "",
    scan_fn: ScanFn | None = None,
    sleep_fn=time.sleep,
    print_fn=print,
    interval_s: float | None = None,
    lookback: int = DEFAULT_LOOKBACK_BLOCKS,
    quote_tokens: dict[str, str] | None = None,
) -> list[ScanResult]:
    """Poll Uniswap V3 PoolCreated, scan the non-quote token, write cases.

    RPC miss skips the poll (UNKNOWN, never a fake PASS). Dedupes tokens.
    Prints one line: case_id chain token score verdict. No browser.
    """
    from filter_floor.config import load_chains
    from filter_floor.pipeline import run_scan

    cfg = load_chains().get("base") or {}
    factory_addr = (factory or (cfg.get("factory_addresses") or {}).get("uniswap_v3") or "").strip()
    if not factory_addr:
        print_fn("base watch: Uniswap V3 factory empty; not scanning")
        return []

    client = rpc or rpc_from_env(str(cfg.get("rpc_env_key") or "BASE_RPC_URL"))
    if client is None:
        print_fn("base watch: BASE_RPC_URL missing; not inventing PASS")
        return []

    do_scan = scan_fn or (lambda chain, token: run_scan(chain, token))
    wait = poll_interval_s() if interval_s is None else interval_s
    seen_tokens: set[str] = set()
    results: list[ScanResult] = []
    last_block: int | None = None

    while True:
        head = client.get_block_number()
        if head is None:
            print_fn("base watch RPC miss; poll skipped (UNKNOWN, not PASS)")
            if once:
                break
            sleep_fn(wait)
            continue
        start = 0 if last_block is None else last_block + 1
        if last_block is None:
            start = max(0, head - lookback)
        if start > head:
            if once:
                break
            sleep_fn(wait)
            continue
        pools = poll_new_pools(client, factory_addr, from_block=start, to_block=head)
        last_block = head
        for decoded in pools:
            token = launch_token_from_pool(decoded, quote_tokens)
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            result = do_scan(Chain.base, token)
            results.append(result)
            _ = min_score_alert
            print_fn(
                f"{result.case_id} {result.chain.value} {result.token} "
                f"{result.score_0_100} {result.verdict.value}"
            )
        if once:
            break
        sleep_fn(wait)
    return results


def start_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: EvmRpc | None = None,
    scan_fn: ScanFn | None = None,
    print_fn=print,
) -> list[ScanResult]:
    return run_watch(
        min_score_alert=min_score_alert,
        once=once,
        rpc=rpc,
        scan_fn=scan_fn,
        print_fn=print_fn,
    )
