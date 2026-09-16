"""EVM tx feed for Layer B. Missing logs / RPC = incomplete, never PASS."""

from __future__ import annotations

from filter_floor.graph.types import EarlyTxFetch, FundingHop, ParsedTokenTx, TokenMove
from filter_floor.scanners.evm import decode_address, normalize_address

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ZERO = "0x" + ("0" * 40)


class EvmGraphSource:
    def __init__(self, rpc=None) -> None:
        self.rpc = rpc

    def fetch_early_txs(self, token: str, limit: int) -> EarlyTxFetch:
        if self.rpc is None:
            return EarlyTxFetch(
                txs=[],
                complete=False,
                rpc_partial_failure=True,
                notes=["EVM RPC missing; graph UNKNOWN"],
            )
        getter = getattr(self.rpc, "get_logs", None)
        if getter is None:
            return EarlyTxFetch(
                txs=[],
                complete=False,
                rpc_partial_failure=True,
                notes=["eth_getLogs not available; graph UNKNOWN"],
            )
        token_n = normalize_address(token)
        try:
            logs = getter(
                address=token_n,
                topics=[TRANSFER_TOPIC0],
                from_block="earliest",
                to_block="latest",
            )
        except Exception as exc:  # noqa: BLE001 — any RPC failure is UNKNOWN
            return EarlyTxFetch(
                txs=[],
                complete=False,
                rpc_partial_failure=True,
                notes=[f"eth_getLogs failed: {exc}"],
            )
        if logs is None:
            return EarlyTxFetch(
                txs=[],
                complete=False,
                rpc_partial_failure=True,
                notes=["eth_getLogs miss; graph UNKNOWN"],
            )
        grouped: dict[str, ParsedTokenTx] = {}
        for log in logs:
            parsed = _parse_transfer_log(log, token_n)
            if parsed is None:
                continue
            existing = grouped.get(parsed.tx_id)
            if existing is None:
                grouped[parsed.tx_id] = parsed
            else:
                existing.buyers.extend(parsed.buyers)
                existing.moves.extend(parsed.moves)
        txs = list(grouped.values())
        txs.sort(key=lambda tx: (tx.slot is None, tx.slot or 0, tx.tx_id))
        windowed = txs[:limit]
        complete = len(txs) <= limit and bool(txs)
        notes: list[str] = []
        if not txs:
            complete = False
            notes.append("no Transfer logs; graph incomplete")
        return EarlyTxFetch(txs=windowed, complete=complete, notes=notes)

    def first_inbound_native(self, wallet: str) -> FundingHop:
        if self.rpc is None:
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=False,
                notes=["EVM RPC missing; funder UNKNOWN"],
            )
        hop_fn = getattr(self.rpc, "first_inbound_native", None)
        if hop_fn is None:
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=False,
                notes=["no EVM first-inbound indexer; funder UNKNOWN"],
            )
        try:
            funder = hop_fn(wallet)
        except Exception as exc:  # noqa: BLE001
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=False,
                notes=[f"first inbound failed: {exc}"],
            )
        if funder is None:
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=False,
                notes=["first inbound native not found; UNKNOWN"],
            )
        return FundingHop(wallet=wallet, funder=str(funder), complete=True)


def _parse_transfer_log(log: dict, token: str) -> ParsedTokenTx | None:
    if not isinstance(log, dict):
        return None
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    topic0 = str(topics[0]).lower()
    if topic0 != TRANSFER_TOPIC0:
        return None
    sender = decode_address(str(topics[1]))
    receiver = decode_address(str(topics[2]))
    if receiver is None:
        return None
    tx_id = str(log.get("transactionHash") or log.get("tx_id") or "")
    if not tx_id:
        return None
    slot = _block_number(log.get("blockNumber") or log.get("slot"))
    buyers: list[str] = []
    if receiver.lower() != ZERO:
        buyers.append(receiver)
    moves: list[TokenMove] = []
    if sender and receiver and sender.lower() != ZERO:
        moves.append(TokenMove(tx_id=tx_id, slot=slot, sender=sender, receiver=receiver))
    _ = token
    return ParsedTokenTx(tx_id=tx_id, slot=slot, buyers=buyers, moves=moves)


def _block_number(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None
