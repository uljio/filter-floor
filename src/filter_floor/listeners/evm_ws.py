"""EVM websocket / log helpers for Uniswap V3 PoolCreated.

Not wired to `ff watch` (that command stays a later-milestone stub).
Pair discovery in Layer A uses factory getPool; this module decodes
PoolCreated logs so a future watcher can reuse the same event layout.
"""

from __future__ import annotations

from typing import Any

# keccak256("PoolCreated(address,address,uint24,int24,address)")
POOL_CREATED_TOPIC0 = (
    "0x783cca1c0412dd0d695e784568c96da2e9c04bfcf553250dc2aa4e3986dd160c"
)


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
