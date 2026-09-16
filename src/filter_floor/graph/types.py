"""Layer B graph value types. Incomplete fetches stay incomplete; never invent PASS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


def addr_key(address: str) -> str:
    text = address.strip()
    if text.lower().startswith("0x"):
        return text.lower()
    return text


@dataclass(frozen=True)
class TokenMove:
    tx_id: str
    slot: int | None
    sender: str
    receiver: str
    amount: int | None = None


@dataclass
class ParsedTokenTx:
    tx_id: str
    slot: int | None
    buyers: list[str]
    moves: list[TokenMove] = field(default_factory=list)
    package_id: str | None = None

    def __post_init__(self) -> None:
        if self.package_id is None and self.slot is not None:
            self.package_id = str(self.slot)


@dataclass
class EarlyTxFetch:
    txs: list[ParsedTokenTx]
    complete: bool
    rpc_partial_failure: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class FundingHop:
    wallet: str
    funder: str | None
    complete: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class GraphOutcome:
    cluster_id: str | None = None
    funder: str | None = None
    wallets: list[str] = field(default_factory=list)
    graph_complete: bool = False


class GraphSource(Protocol):
    def fetch_early_txs(self, token: str, limit: int) -> EarlyTxFetch:
        ...

    def first_inbound_native(self, wallet: str) -> FundingHop:
        ...
