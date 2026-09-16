"""DexScreener quotes for outcome labeling. Failures are unknown, never invented labels."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from filter_floor.config import load_outcomes
from filter_floor.models import Chain

DEFAULT_URL_TEMPLATE = "https://api.dexscreener.com/latest/dex/tokens/{token}"
DEFAULT_TIMEOUT_S = 8.0

# DexScreener chainId values that match Filter Floor chains.
_CHAIN_IDS = {
    Chain.solana: "solana",
    Chain.base: "base",
}


@dataclass
class MarketSnapshot:
    """Price/liquidity evidence for one token. Missing fields stay None."""

    price_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    tradeable: Optional[bool] = None
    creator_extracted: Optional[bool] = None
    freeze_used_after_launch: Optional[bool] = None
    mint_used_after_launch: Optional[bool] = None
    source: str = "none"
    error: Optional[str] = None
    details: dict = field(default_factory=dict)

    @property
    def has_evidence(self) -> bool:
        return any(
            value is not None
            for value in (
                self.price_usd,
                self.liquidity_usd,
                self.tradeable,
                self.creator_extracted,
                self.freeze_used_after_launch,
                self.mint_used_after_launch,
            )
        )


def fetch_dexscreener(
    chain: Chain,
    token: str,
    *,
    http: httpx.Client | None = None,
    url_template: str | None = None,
) -> MarketSnapshot:
    """Fetch DexScreener pairs. Transport/empty/parse misses return error snapshots."""
    cfg = load_outcomes().get("dexscreener") or {}
    template = url_template or cfg.get("url_template") or DEFAULT_URL_TEMPLATE
    timeout_s = float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
    url = str(template).replace("{token}", token.strip())
    owns = http is None
    client = http or httpx.Client(timeout=timeout_s)
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return MarketSnapshot(
            source="dexscreener",
            error=str(exc),
            details={"TODO(verify)": "DexScreener miss; label unknown"},
        )
    finally:
        if owns:
            client.close()

    if not isinstance(payload, dict):
        return MarketSnapshot(
            source="dexscreener",
            error="non-object JSON",
            details={"TODO(verify)": "DexScreener body not classified"},
        )
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        return MarketSnapshot(
            source="dexscreener",
            error="no_pairs",
            details={"TODO(verify)": "no DexScreener pairs; not evidence of rug or survival"},
        )
    return snapshot_from_pairs(pairs, chain)


def snapshot_from_pairs(pairs: list[Any], chain: Chain) -> MarketSnapshot:
    wanted = _CHAIN_IDS.get(chain)
    matching: list[dict] = []
    fallback: list[dict] = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        fallback.append(item)
        chain_id = str(item.get("chainId") or "").strip().lower()
        if wanted and chain_id == wanted:
            matching.append(item)
    pool = matching or fallback
    if not pool:
        return MarketSnapshot(
            source="dexscreener",
            error="no_pairs",
            details={"TODO(verify)": "DexScreener pairs empty after filter"},
        )

    def _liq(row: dict) -> float:
        value = _as_float((row.get("liquidity") or {}).get("usd") if isinstance(row.get("liquidity"), dict) else None)
        return value if value is not None else -1.0

    best = max(pool, key=_liq)
    price = _as_float(best.get("priceUsd"))
    liquidity = None
    raw_liq = best.get("liquidity")
    if isinstance(raw_liq, dict):
        liquidity = _as_float(raw_liq.get("usd"))
    tradeable = None
    if liquidity is not None and liquidity <= 0:
        tradeable = False
    elif liquidity is not None and price is not None:
        tradeable = True
    return MarketSnapshot(
        price_usd=price,
        liquidity_usd=liquidity,
        tradeable=tradeable,
        source="dexscreener",
        details={
            "pairAddress": best.get("pairAddress"),
            "dexId": best.get("dexId"),
            "chainId": best.get("chainId"),
        },
    )


def fetch_market_snapshot(
    chain: Chain,
    token: str,
    *,
    http: httpx.Client | None = None,
    native: MarketSnapshot | None = None,
) -> MarketSnapshot:
    """DexScreener first, then native snapshot if it has evidence. Else unknown."""
    dex = fetch_dexscreener(chain, token, http=http)
    if dex.has_evidence:
        return dex
    if native is not None and native.has_evidence:
        return native
    return dex


def _as_float(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
