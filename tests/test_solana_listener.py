"""Pump.fun create polling. Mocked signatures/txs; no live websocket."""

from __future__ import annotations

from filter_floor.listeners.solana_ws import poll_new_creates, run_watch
from filter_floor.models import Chain, ScanResult, Verdict
from filter_floor.scanners.pumpfun import CREATE_DISCRIMINATOR, PUMP_PROGRAM_ID
from filter_floor.adapters.rpc import RpcError
from tests.fakes import FakeSolanaRpc
from tests.fixtures.solana_mints import pubkey_from_byte
from tests.helpers import layer_a_unknown, layer_b_unknown, memory_clean

from datetime import datetime, timezone


def _create_tx(mint: str) -> dict:
    return {
        "logs": ["Program log: Instruction: Create"],
        "instructions": [
            {
                "program_id": PUMP_PROGRAM_ID,
                "accounts": [mint, "x", "y", "z", "g", "m", "md", "creator"],
                "data_hex": CREATE_DISCRIMINATOR.hex(),
            }
        ],
    }


def test_poll_new_creates_parses_mocked_program_sigs():
    mint = pubkey_from_byte(51)
    rpc = FakeSolanaRpc(
        signatures=["sig-a", "sig-a"],
        transactions={"sig-a": _create_tx(mint)},
    )
    seen: set[str] = set()
    first = poll_new_creates(rpc, seen)
    assert [c.mint for c in first.creates] == [mint]
    assert first.signatures == 2
    assert first.skipped_rpc == 0
    assert first.summary_line() == (
        "signatures=2 creates=1 skipped_rpc=0 null=0 error=0 version=0 ratelimit=0"
    )
    second = poll_new_creates(rpc, seen)
    assert second.creates == []
    assert second.signatures == 2
    assert second.skipped_rpc == 0
    assert second.skipped_null == 0
    assert second.skipped_error == 0


def test_poll_rpc_failure_returns_empty_not_invented_mints():
    rpc = FakeSolanaRpc(fail=True)
    poll = poll_new_creates(rpc, set())
    assert poll.creates == []
    assert poll.signatures == 0
    assert poll.skipped_rpc == 1
    assert poll.skipped_null == 0
    assert poll.skipped_error == 1
    assert poll.summary_line() == (
        "signatures=0 creates=0 skipped_rpc=1 null=0 error=1 version=0 ratelimit=0"
    )


def test_poll_respects_limit_and_counts_skipped_rpc():
    mint = pubkey_from_byte(53)
    rpc = FakeSolanaRpc(
        signatures=[f"sig-{i}" for i in range(10)],
        transactions={"sig-0": _create_tx(mint), "sig-1": None},
        fail_transactions={"sig-2"},
        transaction_errors={
            "sig-3": RpcError(
                "Transaction version (1) is not supported",
                rpc_code=-32015,
            ),
            "sig-4": RpcError("getTransaction failed: HTTP 429", status_code=429),
        },
    )
    poll = poll_new_creates(rpc, set(), limit=5)
    assert poll.signatures == 5
    assert [c.mint for c in poll.creates] == [mint]
    assert poll.skipped_rpc == 4
    assert poll.skipped_null == 1
    assert poll.skipped_error == 1
    assert poll.skipped_version == 1
    assert poll.skipped_ratelimit == 1
    assert poll.summary_line() == (
        "signatures=5 creates=1 skipped_rpc=4 null=1 error=1 version=1 ratelimit=1"
    )


def test_poll_delays_between_get_transaction_calls():
    mint = pubkey_from_byte(54)
    rpc = FakeSolanaRpc(
        signatures=["sig-a", "sig-b"],
        transactions={"sig-a": _create_tx(mint), "sig-b": _create_tx(mint)},
    )
    sleeps: list[float] = []
    poll_new_creates(
        rpc,
        set(),
        delay_s=0.06,
        sleep_fn=sleeps.append,
    )
    assert sleeps == [0.06]


def test_run_watch_once_scans_new_mint():
    mint = pubkey_from_byte(52)
    rpc = FakeSolanaRpc(
        signatures=["sig-b"],
        transactions={"sig-b": _create_tx(mint)},
    )
    scanned: list[str] = []

    def scan_fn(chain: Chain, token: str) -> ScanResult:
        scanned.append(token)
        assert chain is Chain.solana
        now = datetime.now(timezone.utc)
        return ScanResult(
            case_id="20260916-solana-testdupe",
            chain=chain,
            token=token,
            scanned_at=now,
            layer_a=layer_a_unknown(),
            layer_b=layer_b_unknown(),
            memory=memory_clean(),
            score_0_100=0,
            verdict=Verdict.CAUTION,
        )

    lines: list[str] = []
    err: list[str] = []
    results = run_watch(
        once=True,
        rpc=rpc,
        scan_fn=scan_fn,
        print_fn=lines.append,
        err_fn=err.append,
        interval_s=0,
        delay_s=0,
        limit=20,
    )
    assert scanned == [mint]
    assert len(results) == 1
    assert results[0].verdict is Verdict.CAUTION
    assert "CAUTION" in lines[0]
    assert "BUY" not in lines[0]
    assert mint in lines[0]
    assert err == [
        "signatures=1 creates=1 skipped_rpc=0 null=0 error=0 version=0 ratelimit=0"
    ]
    assert "BUY" not in err[0]
