"""Pump.fun create listener. Polling is the default; WS is optional fallback."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from filter_floor.adapters.rpc import RpcError, SolanaRpc
from filter_floor.models import Chain, ScanResult
from filter_floor.scanners.pumpfun import PUMP_PROGRAM_ID, PumpCreate, extract_pump_creates

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_SIG_LIMIT = 20

ScanFn = Callable[[Chain, str], ScanResult]
ErrFn = Callable[[str], None]


@dataclass(frozen=True)
class PollPass:
    creates: list[PumpCreate]
    signatures: int
    skipped_null: int = 0
    skipped_error: int = 0

    @property
    def skipped_rpc(self) -> int:
        return self.skipped_null + self.skipped_error

    def summary_line(self) -> str:
        return (
            f"signatures={self.signatures} "
            f"creates={len(self.creates)} "
            f"skipped_rpc={self.skipped_rpc} "
            f"null={self.skipped_null} "
            f"error={self.skipped_error}"
        )


def poll_interval_s() -> float:
    raw = os.environ.get("SOLANA_WATCH_POLL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_POLL_SECONDS
    return max(1.0, value)


DEFAULT_TX_DELAY_MS = 50.0


def watch_tx_delay_s() -> float:
    """Pause between getTransaction calls. SOLANA_WATCH_TX_DELAY_MS, default 50."""
    raw = os.environ.get("SOLANA_WATCH_TX_DELAY_MS", "").strip()
    if not raw:
        return DEFAULT_TX_DELAY_MS / 1000.0
    try:
        ms = float(raw)
    except ValueError:
        return DEFAULT_TX_DELAY_MS / 1000.0
    return max(0.0, ms / 1000.0)


def poll_new_creates(
    rpc: SolanaRpc,
    seen_signatures: set[str],
    *,
    limit: int = DEFAULT_SIG_LIMIT,
    delay_s: float = 0.0,
    sleep_fn=time.sleep,
) -> PollPass:
    """One polling pass over Pump.fun program signatures. Unknown txs are skipped."""
    try:
        signatures = rpc.get_signatures_for_address(PUMP_PROGRAM_ID, limit=limit)
    except RpcError:
        return PollPass(creates=[], signatures=0, skipped_null=0, skipped_error=1)

    found: list[PumpCreate] = []
    skipped_null = 0
    skipped_error = 0
    fetched = 0
    pause = max(0.0, float(delay_s))
    for signature in signatures:
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        if fetched and pause > 0:
            sleep_fn(pause)
        fetched += 1
        try:
            tx = rpc.get_transaction(signature)
        except RpcError:
            skipped_error += 1
            continue
        if not tx:
            skipped_null += 1
            continue
        found.extend(extract_pump_creates(tx, signature=signature))
    return PollPass(
        creates=found,
        signatures=len(signatures),
        skipped_null=skipped_null,
        skipped_error=skipped_error,
    )


def _print_err(line: str) -> None:
    print(line, file=sys.stderr)


def run_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: SolanaRpc | None = None,
    scan_fn: ScanFn | None = None,
    sleep_fn=time.sleep,
    print_fn=print,
    err_fn: ErrFn | None = None,
    interval_s: float | None = None,
    delay_s: float | None = None,
    limit: int = DEFAULT_SIG_LIMIT,
) -> list[ScanResult]:
    """Poll Pump.fun creates, scan each new mint, write cases via scan_fn.

    Prints one line per token: case_id chain token score verdict.
    After each poll, prints signatures=N creates=M skipped_rpc=K null=X error=Y
    on stderr. Does not open a browser. Dedupes by mint and signature.
    """
    from filter_floor.pipeline import run_scan

    client = rpc or SolanaRpc.from_env()
    do_scan = scan_fn or (
        lambda chain, token: run_scan(chain, token, solana_rpc=client)
    )
    write_err = err_fn or _print_err
    wait = poll_interval_s() if interval_s is None else interval_s
    sig_limit = max(1, int(limit))
    tx_delay = watch_tx_delay_s() if delay_s is None else max(0.0, float(delay_s))
    seen_signatures: set[str] = set()
    seen_mints: set[str] = set()
    results: list[ScanResult] = []

    while True:
        poll = poll_new_creates(
            client,
            seen_signatures,
            limit=sig_limit,
            delay_s=tx_delay,
            sleep_fn=sleep_fn,
        )
        write_err(poll.summary_line())
        for create in _dedupe_creates(poll.creates, seen_mints):
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
    err_fn: ErrFn | None = None,
    limit: int = DEFAULT_SIG_LIMIT,
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
        err_fn=err_fn,
        limit=limit,
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
