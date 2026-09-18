"""Pump.fun create listener. Prefer logsSubscribe; reconnect on drop."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from filter_floor.adapters.rpc import (
    RpcError,
    SolanaRpc,
    classify_tx_rpc_error,
    sanitize_rpc_text,
)
from filter_floor.models import Chain, ScanResult
from filter_floor.scanners.pumpfun import (
    PUMP_PROGRAM_ID,
    PumpCreate,
    extract_pump_create_stats,
    logs_contain_create,
    pump_ix_summaries,
)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_SIG_LIMIT = 20
MAX_WS_RECONNECTS = 10
WS_PING_INTERVAL_S = 20
WS_PING_TIMEOUT_S = 20

ScanFn = Callable[[Chain, str], ScanResult]
ErrFn = Callable[[str], None]


@dataclass(frozen=True)
class PollPass:
    creates: list[PumpCreate]
    signatures: int
    skipped_null: int = 0
    skipped_error: int = 0
    skipped_version: int = 0
    skipped_ratelimit: int = 0
    log_create: int = 0
    inner_create: int = 0
    decoded: int = 0
    undecoded_ixs: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    @property
    def skipped_rpc(self) -> int:
        return (
            self.skipped_null
            + self.skipped_error
            + self.skipped_version
            + self.skipped_ratelimit
        )

    def summary_line(self) -> str:
        return (
            f"signatures={self.signatures} "
            f"creates={len(self.creates)} "
            f"skipped_rpc={self.skipped_rpc} "
            f"null={self.skipped_null} "
            f"error={self.skipped_error} "
            f"version={self.skipped_version} "
            f"ratelimit={self.skipped_ratelimit} "
            f"log_create={self.log_create} "
            f"inner_create={self.inner_create} "
            f"decoded={self.decoded}"
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


DEFAULT_TX_DELAY_MS = 120.0


def tx_delay_s() -> float:
    """Pause between getTransaction calls. SOLANA_TX_DELAY_MS, default 120."""
    raw = os.environ.get("SOLANA_TX_DELAY_MS", "").strip()
    if not raw:
        raw = os.environ.get("SOLANA_WATCH_TX_DELAY_MS", "").strip()
    if not raw:
        return DEFAULT_TX_DELAY_MS / 1000.0
    try:
        ms = float(raw)
    except ValueError:
        return DEFAULT_TX_DELAY_MS / 1000.0
    return max(0.0, ms / 1000.0)


def watch_tx_delay_s() -> float:
    return tx_delay_s()


def poll_new_creates(
    rpc: SolanaRpc,
    seen_signatures: set[str],
    *,
    limit: int = DEFAULT_SIG_LIMIT,
    delay_s: float = 0.0,
    sleep_fn=time.sleep,
) -> PollPass:
    """Count Pump.fun program signatures. Does not getTransaction on each sig.

    Creates are decoded from logsSubscribe (or fetch_creates_for_logs) only.
    """
    _ = delay_s, sleep_fn
    try:
        signatures = rpc.get_signatures_for_address(PUMP_PROGRAM_ID, limit=limit)
    except RpcError as exc:
        kind = classify_tx_rpc_error(exc)
        return PollPass(
            creates=[],
            signatures=0,
            skipped_error=1 if kind == "error" else 0,
            skipped_version=1 if kind == "version" else 0,
            skipped_ratelimit=1 if kind == "ratelimit" else 0,
        )

    for signature in signatures:
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
    return PollPass(
        creates=[],
        signatures=len(signatures),
        log_create=0,
        inner_create=0,
        decoded=0,
    )


def fetch_creates_for_logs(
    rpc: SolanaRpc,
    signature: str,
    logs: object,
    *,
    delay_s: float = 0.0,
    sleep_fn=time.sleep,
) -> PollPass:
    """getTransaction only when logs contain Create / CreateV2."""
    log_create = 1 if logs_contain_create(logs) else 0
    if not signature or not log_create:
        return PollPass(
            creates=[],
            signatures=1 if signature else 0,
            log_create=0,
            inner_create=0,
            decoded=0,
        )
    if delay_s > 0:
        sleep_fn(delay_s)
    try:
        tx = rpc.get_transaction(signature)
    except RpcError as exc:
        kind = classify_tx_rpc_error(exc)
        return PollPass(
            creates=[],
            signatures=1,
            skipped_error=1 if kind == "error" else 0,
            skipped_version=1 if kind == "version" else 0,
            skipped_ratelimit=1 if kind == "ratelimit" else 0,
            log_create=log_create,
            inner_create=0,
            decoded=0,
        )
    if not tx:
        return PollPass(
            creates=[],
            signatures=1,
            skipped_null=1,
            log_create=log_create,
            inner_create=0,
            decoded=0,
        )
    found, _tx_log_create, inner_create = extract_pump_create_stats(
        tx, signature=signature
    )
    undecoded: tuple[tuple[str, int], ...] = ()
    if not found:
        undecoded = tuple(pump_ix_summaries(tx))
    return PollPass(
        creates=found,
        signatures=1,
        log_create=max(log_create, _tx_log_create),
        inner_create=inner_create,
        decoded=len(found),
        undecoded_ixs=undecoded,
    )


def handle_create_poll(
    poll: PollPass,
    *,
    do_scan: ScanFn,
    seen_mints: set[str],
    print_fn,
    err_fn: ErrFn,
    min_score_alert: int = 50,
) -> list[ScanResult]:
    """Print stats; scan every decoded=1 create; dump disc_hex on decoded=0."""
    err_fn(poll.summary_line())
    if poll.log_create and poll.decoded == 0:
        rows = poll.undecoded_ixs or (("-", 0),)
        for disc_hex, account_count in rows:
            err_fn(f"disc_hex={disc_hex or '-'} account_count={account_count}")
    results: list[ScanResult] = []
    if poll.decoded < 1:
        return results
    for create in poll.creates:
        result = _scan_decoded_create(
            create,
            do_scan=do_scan,
            seen_mints=seen_mints,
            print_fn=print_fn,
            err_fn=err_fn,
            min_score_alert=min_score_alert,
        )
        if result is not None:
            results.append(result)
    return results


def _scan_decoded_create(
    create: PumpCreate,
    *,
    do_scan: ScanFn,
    seen_mints: set[str],
    print_fn,
    err_fn: ErrFn,
    min_score_alert: int,
) -> ScanResult | None:
    _ = min_score_alert
    mint = (create.mint or "").strip()
    if mint and mint in seen_mints:
        return None
    try:
        result = do_scan(Chain.solana, mint)
    except Exception as exc:
        _ = exc
        err_fn(f"scan_error={type(exc).__name__}")
        return None
    if mint:
        seen_mints.add(mint)
    print_fn(
        f"{result.case_id} {result.chain.value} {result.token} "
        f"{result.score_0_100} {result.verdict.value}"
    )
    return result


def _print_err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _print_out(line: str) -> None:
    print(line, flush=True)


def run_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: SolanaRpc | None = None,
    scan_fn: ScanFn | None = None,
    sleep_fn=time.sleep,
    print_fn=None,
    err_fn: ErrFn | None = None,
    interval_s: float | None = None,
    delay_s: float | None = None,
    limit: int = DEFAULT_SIG_LIMIT,
    seen_mints: set[str] | None = None,
    seen_signatures: set[str] | None = None,
) -> list[ScanResult]:
    """Poll Pump.fun signatures for volume stats. Does not fetch every tx.

    Prints one line per token: case_id chain token score verdict.
    After each poll, prints
    signatures=N creates=M skipped_rpc=K null=X error=Y version=V ratelimit=R
    log_create=A inner_create=B decoded=C
    on stderr. Does not open a browser. Dedupes by mint and signature.
    """
    from filter_floor.pipeline import run_scan

    client = rpc or SolanaRpc.from_env()
    do_scan = scan_fn or (
        lambda chain, token: run_scan(chain, token, solana_rpc=client)
    )
    write_out = print_fn or _print_out
    write_err = err_fn or _print_err
    wait = poll_interval_s() if interval_s is None else interval_s
    sig_limit = max(1, int(limit))
    tx_delay = tx_delay_s() if delay_s is None else max(0.0, float(delay_s))
    signatures = seen_signatures if seen_signatures is not None else set()
    mints = seen_mints if seen_mints is not None else set()
    results: list[ScanResult] = []

    while True:
        poll = poll_new_creates(
            client,
            signatures,
            limit=sig_limit,
            delay_s=tx_delay,
            sleep_fn=sleep_fn,
        )
        results.extend(
            handle_create_poll(
                poll,
                do_scan=do_scan,
                seen_mints=mints,
                print_fn=write_out,
                err_fn=write_err,
                min_score_alert=min_score_alert,
            )
        )
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
    print_fn=None,
    err_fn: ErrFn | None = None,
    delay_s: float | None = None,
    sleep_fn=time.sleep,
    seen_mints: set[str] | None = None,
) -> None:
    """logsSubscribe for Pump program. Fetch txs only on Create / CreateV2 logs."""
    import asyncio
    import json

    import websockets

    from filter_floor.pipeline import run_scan

    client = rpc or SolanaRpc.from_env()
    do_scan = scan_fn or (
        lambda chain, token: run_scan(chain, token, solana_rpc=client)
    )
    write_out = print_fn or _print_out
    write_err = err_fn or _print_err
    tx_delay = tx_delay_s() if delay_s is None else max(0.0, float(delay_s))
    mints = seen_mints if seen_mints is not None else set()
    pending_delay = False

    async def _run() -> None:
        nonlocal pending_delay
        subscribe = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMP_PROGRAM_ID]},
                {"commitment": "confirmed"},
            ],
        }
        async with websockets.connect(
            wss_url,
            ping_interval=WS_PING_INTERVAL_S,
            ping_timeout=WS_PING_TIMEOUT_S,
        ) as ws:
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
                if not signature or not logs_contain_create(logs):
                    continue
                pause = tx_delay if pending_delay else 0.0
                pending_delay = True
                poll = await asyncio.to_thread(
                    fetch_creates_for_logs,
                    client,
                    str(signature),
                    logs,
                    delay_s=pause,
                    sleep_fn=sleep_fn,
                )
                await asyncio.to_thread(
                    handle_create_poll,
                    poll,
                    do_scan=do_scan,
                    seen_mints=mints,
                    print_fn=write_out,
                    err_fn=write_err,
                    min_score_alert=min_score_alert,
                )
                if once:
                    return

    asyncio.run(_run())


def start_watch(
    *,
    min_score_alert: int = 50,
    once: bool = False,
    rpc: SolanaRpc | None = None,
    scan_fn: ScanFn | None = None,
    print_fn=None,
    err_fn: ErrFn | None = None,
    limit: int = DEFAULT_SIG_LIMIT,
) -> list[ScanResult] | None:
    """Prefer logsSubscribe when SOLANA_WSS_URL is set.

    On WS drop: print one poll line, then reconnect (ping 20s / timeout 20s).
    Max 10 reconnects, then stay on polling. Messages never include URL/key.
    """
    write_out = print_fn or _print_out
    write_err = err_fn or _print_err
    wss = os.environ.get("SOLANA_WSS_URL", "").strip()
    seen_mints: set[str] = set()
    seen_signatures: set[str] = set()
    watch_kwargs = dict(
        min_score_alert=min_score_alert,
        rpc=rpc,
        scan_fn=scan_fn,
        print_fn=write_out,
        err_fn=write_err,
        seen_mints=seen_mints,
        seen_signatures=seen_signatures,
        limit=limit,
    )
    if not wss:
        write_err("solana WS watch skipped: SOLANA_WSS_URL unset; falling back to polling")
        return run_watch(once=once, **watch_kwargs)

    reconnects = 0
    while True:
        try:
            run_ws_watch(
                wss_url=wss,
                min_score_alert=min_score_alert,
                once=once,
                rpc=rpc,
                scan_fn=scan_fn,
                print_fn=write_out,
                err_fn=write_err,
                seen_mints=seen_mints,
            )
            return None
        except Exception as exc:
            reason = sanitize_rpc_text(f"{type(exc).__name__}: {exc}")
            write_err(f"solana WS watch failed; falling back to polling: {reason}")
            run_watch(once=True, **watch_kwargs)
            if once:
                return None
            reconnects += 1
            if reconnects > MAX_WS_RECONNECTS:
                write_err("solana WS reconnects exhausted; staying on polling")
                return run_watch(once=False, **watch_kwargs)
            write_err(f"solana WS reconnect {reconnects}/{MAX_WS_RECONNECTS}")
