"""Solana tx feed for Layer B. RPC miss / truncated history = incomplete."""

from __future__ import annotations

from filter_floor.adapters.rpc import RpcError
from filter_floor.graph.types import (
    EarlyTxFetch,
    FundingHop,
    ParsedTokenTx,
    TokenMove,
    addr_key,
)

SYSTEM_PROGRAM = "11111111111111111111111111111111"


class SolanaGraphSource:
    def __init__(self, rpc) -> None:
        self.rpc = rpc

    def fetch_early_txs(self, token: str, limit: int) -> EarlyTxFetch:
        try:
            signatures = self.rpc.get_signatures_for_address(token, limit=limit)
        except RpcError as exc:
            return EarlyTxFetch(
                txs=[],
                complete=False,
                rpc_partial_failure=True,
                notes=[f"getSignaturesForAddress failed: {exc}"],
            )
        if signatures is None:
            return EarlyTxFetch(
                txs=[],
                complete=False,
                rpc_partial_failure=True,
                notes=["getSignaturesForAddress returned nothing"],
            )

        txs: list[ParsedTokenTx] = []
        notes: list[str] = []
        rpc_partial = False
        for signature in signatures:
            try:
                raw = self.rpc.get_transaction(signature)
            except RpcError as exc:
                rpc_partial = True
                notes.append(f"getTransaction {signature} failed: {exc}")
                continue
            if raw is None:
                rpc_partial = True
                notes.append(f"getTransaction {signature} missing")
                continue
            parsed = parse_solana_token_tx(raw, token, signature=signature)
            if parsed is not None:
                txs.append(parsed)

        # Newest-first RPC list: fewer than `limit` means we reached the start.
        reached_start = len(signatures) < limit
        complete = reached_start and not rpc_partial
        if not signatures:
            # Empty history without an RPC error is still not a proven launch picture.
            complete = False
            notes.append("no signatures for token; graph incomplete")
        return EarlyTxFetch(
            txs=txs,
            complete=complete,
            rpc_partial_failure=rpc_partial,
            notes=notes,
        )

    def first_inbound_native(self, wallet: str) -> FundingHop:
        limit = 50
        try:
            signatures = self.rpc.get_signatures_for_address(wallet, limit=limit)
        except RpcError as exc:
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=False,
                notes=[f"funder signatures failed: {exc}"],
            )
        if not signatures:
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=False,
                notes=["no signatures for wallet; first inbound unknown"],
            )
        reached_start = len(signatures) < limit
        # RPC is newest-first; walk oldest → newest for the first inbound.
        for signature in reversed(signatures):
            try:
                raw = self.rpc.get_transaction(signature)
            except RpcError as exc:
                return FundingHop(
                    wallet=wallet,
                    funder=None,
                    complete=False,
                    notes=[f"funder tx failed: {exc}"],
                )
            if raw is None:
                continue
            funder = first_native_sender(raw, wallet)
            if funder:
                return FundingHop(wallet=wallet, funder=funder, complete=True)
        if reached_start:
            return FundingHop(
                wallet=wallet,
                funder=None,
                complete=True,
                notes=["no inbound native transfer in wallet history"],
            )
        return FundingHop(
            wallet=wallet,
            funder=None,
            complete=False,
            notes=["wallet history truncated before first inbound"],
        )


def parse_solana_token_tx(
    raw: dict,
    token: str,
    *,
    signature: str | None = None,
) -> ParsedTokenTx | None:
    tx_id = signature or _signature_of(raw) or "unknown"
    slot = raw.get("slot")
    if isinstance(slot, str) and slot.isdigit():
        slot = int(slot)
    if slot is not None and not isinstance(slot, int):
        slot = None

    shorthand = raw.get("graph")
    if isinstance(shorthand, dict):
        buyers = [str(b) for b in (shorthand.get("buyers") or [])]
        moves = [
            TokenMove(
                tx_id=tx_id,
                slot=slot,
                sender=str(m.get("from") or ""),
                receiver=str(m.get("to") or ""),
            )
            for m in (shorthand.get("moves") or [])
            if isinstance(m, dict)
        ]
        return ParsedTokenTx(
            tx_id=tx_id,
            slot=slot,
            buyers=buyers,
            moves=moves,
            package_id=str(shorthand.get("package_id") or slot or ""),
        )

    token_key = addr_key(token)
    pre_map, post_map = _token_owner_amounts(raw, token_key)
    owners = set(pre_map) | set(post_map)
    buyers: list[str] = []
    moves: list[TokenMove] = []
    decreased: list[str] = []
    increased: list[str] = []
    for owner in owners:
        before = pre_map.get(owner, 0)
        after = post_map.get(owner, 0)
        if after > before:
            buyers.append(owner)
            increased.append(owner)
        elif after < before:
            decreased.append(owner)
    if len(increased) == 1:
        receiver = increased[0]
        for sender in decreased:
            moves.append(
                TokenMove(tx_id=tx_id, slot=slot, sender=sender, receiver=receiver)
            )
    else:
        for sender in decreased:
            for receiver in increased:
                if sender != receiver:
                    moves.append(
                        TokenMove(tx_id=tx_id, slot=slot, sender=sender, receiver=receiver)
                    )
    return ParsedTokenTx(tx_id=tx_id, slot=slot, buyers=buyers, moves=moves)


def first_native_sender(raw: dict, wallet: str) -> str | None:
    keys = _account_keys(raw)
    pre, post = _native_balances(raw)
    if not keys or pre is None or post is None:
        shorthand = raw.get("graph") if isinstance(raw.get("graph"), dict) else {}
        deltas = (shorthand or {}).get("native_deltas") or {}
        if isinstance(deltas, dict) and deltas:
            wallet_key = addr_key(wallet)
            gained = int(deltas.get(wallet) or deltas.get(wallet_key) or 0)
            if gained <= 0:
                return None
            sender = None
            worst = 0
            for account, delta in deltas.items():
                try:
                    value = int(delta)
                except (TypeError, ValueError):
                    continue
                if addr_key(str(account)) == wallet_key:
                    continue
                if value < worst:
                    worst = value
                    sender = str(account)
            return sender
        return None

    wallet_key = addr_key(wallet)
    idx = next((i for i, key in enumerate(keys) if addr_key(key) == wallet_key), None)
    if idx is None or idx >= len(pre) or idx >= len(post):
        return None
    if post[idx] <= pre[idx]:
        return None
    sender = None
    worst = 0
    for i, key in enumerate(keys):
        if i >= len(pre) or i >= len(post):
            break
        if addr_key(key) in {wallet_key, SYSTEM_PROGRAM}:
            continue
        delta = post[i] - pre[i]
        if delta < worst:
            worst = delta
            sender = key
    return sender


def _signature_of(raw: dict) -> str | None:
    tx = raw.get("transaction")
    if isinstance(tx, dict):
        sigs = tx.get("signatures") or []
        if sigs:
            return str(sigs[0])
    if raw.get("signature"):
        return str(raw["signature"])
    return None


def _account_keys(raw: dict) -> list[str]:
    inner = raw.get("transaction") if isinstance(raw.get("transaction"), dict) else raw
    message = inner.get("message") if isinstance(inner, dict) else None
    keys_raw = []
    if isinstance(message, dict):
        keys_raw = message.get("accountKeys") or message.get("account_keys") or []
    keys: list[str] = []
    for item in keys_raw:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict) and item.get("pubkey"):
            keys.append(str(item["pubkey"]))
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    loaded = meta.get("loadedAddresses") or {}
    if isinstance(loaded, dict):
        for group in ("writable", "readonly"):
            for item in loaded.get(group) or []:
                if isinstance(item, str):
                    keys.append(item)
    return keys


def _native_balances(raw: dict) -> tuple[list[int] | None, list[int] | None]:
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list):
        return None, None
    try:
        return [int(x) for x in pre], [int(x) for x in post]
    except (TypeError, ValueError):
        return None, None


def _token_owner_amounts(raw: dict, token_key: str) -> tuple[dict[str, int], dict[str, int]]:
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    return (
        _balance_map(meta.get("preTokenBalances"), token_key),
        _balance_map(meta.get("postTokenBalances"), token_key),
    )


def _balance_map(rows: object, token_key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = row.get("mint")
        if not isinstance(mint, str) or addr_key(mint) != token_key:
            continue
        owner = row.get("owner")
        if not isinstance(owner, str) or not owner:
            continue
        amount = _token_amount(row)
        out[owner] = out.get(owner, 0) + amount
    return out


def _token_amount(row: dict) -> int:
    ui = row.get("uiTokenAmount") or {}
    if isinstance(ui, dict) and ui.get("amount") is not None:
        try:
            return int(ui["amount"])
        except (TypeError, ValueError):
            return 0
    try:
        return int(row.get("amount") or 0)
    except (TypeError, ValueError):
        return 0
