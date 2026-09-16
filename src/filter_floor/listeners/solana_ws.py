"""Pump.fun create listener. Polling is the default; WS is optional fallback."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable

from filter_floor.adapters.rpc import RpcError, SolanaRpc
from filter_floor.models import Chain, ScanResult
from filter_floor.scanners.pumpfun import PUMP_PROGRAM_ID, PumpCreate, extract_pump_creates

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_SIG_LIMIT = 20

ScanFn = Callable[[Chain, str], ScanResult]


def poll_interval_s() -> float:
    raw = os.environ.get("SOLANA_WATCH_POLL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_POLL_SECONDS
    return max(1.0, value)


def poll_new_creates(
    rpc: SolanaRpc,
    seen_signatures: set[str],
    *,
    limit: int = DEFAULT_SIG_LIMIT,
) -> list[PumpCreate]:
    """One polling pass over Pump.fun program signatures. Unknown txs are skipped."""
    try:
        signatures = rpc.get_signatures_for_address(PUMP_PROGRAM_ID, limit=limit)
    except RpcError:
        return []

    found: list[PumpCreate] = []
    for signature in signatures:
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        try:
            tx = rpc.get_transaction(signature)
        except RpcError:
            continue
        if not tx:
            continue
        found.extend(extract_pump_creates(tx, signature=signature))
    return found


def run_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: SolanaRpc | None = None,
    scan_fn: ScanFn | None = None,
    sleep_fn=time.sleep,
    print_fn=print,
    interval_s: float | None = None,
) -> list[ScanResult]:
    """Poll Pump.fun creates, scan each new mint, write cases via scan_fn.

    Prints one line per token: case_id chain token score verdict.
    Does not open a browser. Dedupes by mint and signature.
    """
    from filter_floor.pipeline import run_scan

    client = rpc or SolanaRpc.from_env()
    do_scan = scan_fn or (
        lambda chain, token: run_scan(chain, token, solana_rpc=client)
    )
    wait = poll_interval_s() if interval_s is None else interval_s
    seen_signatures: set[str] = set()
    seen_mints: set[str] = set()
    results: list[ScanResult] = []

    while True:
        creates = poll_new_creates(client, seen_signatures)
        for create in _dedupe_creates(creates, seen_mints):
            result = do_scan(Chain.solana, create.mint)
            results.append(result)
            line = (
                f"{result.case_id} {result.chain.value} {result.token} "
                f"{result.score_0_100} {result.verdict.value}"
            )
            _ = min_score_alert
            print_fn(line)
        if once:
            break
        sleep_fn(wait)
    return results


def run_ws_watch(
    *,
    wss_url: str,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: SolanaRpc | None = None,
    scan_fn: ScanFn | None = None,
    print_fn=print,
) -> None:
    """Optional logsSubscribe. On any WS error, callers should fall back to polling."""
    import asyncio
    import json

    import websockets

    from filter_floor.pipeline import run_scan

    client = rpc or SolanaRpc.from_env()
    do_scan = scan_fn or (
        lambda chain, token: run_scan(chain, token, solana_rpc=client)
    )
    seen_mints: set[str] = set()

    async def _run() -> None:
        subscribe = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMP_PROGRAM_ID]},
                {"commitment": "confirmed"},
            ],
        }
        async with websockets.connect(wss_url) as ws:
            await ws.send(json.dumps(subscribe))
            while True:
                raw = await ws.recv()
                payload = json.loads(raw)
                value = (
                    payload.get("params", {}).get("result", {}).get("value", {})
                    if isinstance(payload, dict)
                    else {}
                )
                signature = value.get("signature")
                logs = value.get("logs") or []
                log_text = " ".join(logs) if isinstance(logs, list) else ""
                if not signature or "Instruction: Create" not in log_text:
                    if once:
                        return
                    continue
                try:
                    tx = client.get_transaction(str(signature))
                except RpcError:
                    if once:
                        return
                    continue
                if not tx:
                    if once:
                        return
                    continue
                for create in extract_pump_creates(tx, signature=str(signature)):
                    if create.mint in seen_mints:
                        continue
                    seen_mints.add(create.mint)
                    result = do_scan(Chain.solana, create.mint)
                    print_fn(
                        f"{result.case_id} {result.chain.value} {result.token} "
                        f"{result.score_0_100} {result.verdict.value}"
                    )
                    _ = min_score_alert
                if once:
                    return

    asyncio.run(_run())


def start_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: SolanaRpc | None = None,
    scan_fn: ScanFn | None = None,
    print_fn=print,
) -> list[ScanResult] | None:
    """Prefer polling. Attempt WS only when SOLANA_WSS_URL is set and once is false.

    WS is easy to flake; polling is the M1 path tests cover.
    """
    wss = os.environ.get("SOLANA_WSS_URL", "").strip()
    if wss and not once:
        try:
            run_ws_watch(
                wss_url=wss,
                min_score_alert=min_score_alert,
                once=once,
                rpc=rpc,
                scan_fn=scan_fn,
                print_fn=print_fn,
            )
            return None
        except Exception:
            print_fn("solana WS watch failed; falling back to polling")
    return run_watch(
        min_score_alert=min_score_alert,
        once=once,
        rpc=rpc,
        scan_fn=scan_fn,
        print_fn=print_fn,
    )


def _dedupe_creates(creates: Iterable[PumpCreate], seen_mints: set[str]) -> list[PumpCreate]:
    out: list[PumpCreate] = []
    for create in creates:
        mint = create.mint.strip()
        if not mint or mint in seen_mints:
            continue
        seen_mints.add(mint)
        out.append(create)
    return out
