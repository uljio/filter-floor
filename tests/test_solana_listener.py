"""Pump.fun create polling. Mocked signatures/txs; no live websocket."""

from __future__ import annotations

from datetime import datetime, timezone

from filter_floor.adapters.rpc import RpcError
from filter_floor.listeners.solana_ws import (
    fetch_creates_for_logs,
    handle_create_poll,
    poll_new_creates,
    run_watch,
    start_watch,
)
from filter_floor.models import Chain, ScanResult, Verdict
from filter_floor.scanners.pumpfun import CREATE_DISCRIMINATOR, PUMP_PROGRAM_ID
from tests.fakes import FakeSolanaRpc
from tests.fixtures.solana_mints import pubkey_from_byte
from tests.helpers import layer_a_unknown, layer_b_unknown, memory_clean


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


def _create_inner_tx(mint: str) -> dict:
    return {
        "logs": ["Program log: Instruction: CreateV2"],
        "instructions": [],
        "inner_instructions": [
            {
                "program_id": PUMP_PROGRAM_ID,
                "accounts": [mint, "x", "y", "z", "g", "m", "md", "creator"],
                "data_hex": CREATE_DISCRIMINATOR.hex(),
            }
        ],
    }


_SUMMARY_TAIL = "log_create=0 inner_create=0 decoded=0"


def test_poll_new_creates_does_not_get_transaction():
    mint = pubkey_from_byte(51)
    rpc = FakeSolanaRpc(
        signatures=["sig-a", "sig-a"],
        transactions={"sig-a": _create_tx(mint)},
    )
    seen: set[str] = set()
    first = poll_new_creates(rpc, seen)
    assert first.creates == []
    assert first.signatures == 2
    assert first.skipped_rpc == 0
    assert first.log_create == 0
    assert first.inner_create == 0
    assert first.decoded == 0
    assert first.summary_line() == (
        "signatures=2 creates=0 skipped_rpc=0 null=0 error=0 version=0 ratelimit=0 "
        + _SUMMARY_TAIL
    )
    assert not any(c[0] == "get_transaction" for c in rpc.calls)
    second = poll_new_creates(rpc, seen)
    assert second.creates == []
    assert second.signatures == 2
    assert seen == {"sig-a"}


def test_poll_rpc_failure_returns_empty_not_invented_mints():
    rpc = FakeSolanaRpc(fail=True)
    poll = poll_new_creates(rpc, set())
    assert poll.creates == []
    assert poll.signatures == 0
    assert poll.skipped_rpc == 1
    assert poll.skipped_null == 0
    assert poll.skipped_error == 1
    assert poll.summary_line() == (
        "signatures=0 creates=0 skipped_rpc=1 null=0 error=1 version=0 ratelimit=0 "
        + _SUMMARY_TAIL
    )


def test_poll_respects_limit_without_fetching_txs():
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
    assert poll.creates == []
    assert poll.skipped_rpc == 0
    assert not any(c[0] == "get_transaction" for c in rpc.calls)
    assert poll.summary_line() == (
        "signatures=5 creates=0 skipped_rpc=0 null=0 error=0 version=0 ratelimit=0 "
        + _SUMMARY_TAIL
    )


def test_poll_does_not_delay_for_get_transaction():
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
    assert sleeps == []
    assert not any(c[0] == "get_transaction" for c in rpc.calls)


def test_fetch_creates_for_logs_only_on_create_markers():
    mint = pubkey_from_byte(55)
    rpc = FakeSolanaRpc(transactions={"sig-swap": _create_tx(mint)})
    skip = fetch_creates_for_logs(
        rpc, "sig-swap", ["Program log: Instruction: Buy"], delay_s=0.06, sleep_fn=lambda _s: None
    )
    assert skip.creates == []
    assert skip.log_create == 0
    assert skip.decoded == 0
    skip_idem = fetch_creates_for_logs(
        rpc, "sig-swap", ["Program log: Instruction: CreateIdempotent"]
    )
    assert skip_idem.log_create == 0
    assert skip_idem.decoded == 0
    assert not any(c[0] == "get_transaction" for c in rpc.calls)

    sleeps: list[float] = []
    hit = fetch_creates_for_logs(
        rpc,
        "sig-swap",
        ["Program log: Instruction: CreateV2"],
        delay_s=0.06,
        sleep_fn=sleeps.append,
    )
    assert sleeps == [0.06]
    assert [c.mint for c in hit.creates] == [mint]
    assert hit.log_create == 1
    assert hit.decoded == 1
    assert hit.summary_line() == (
        "signatures=1 creates=1 skipped_rpc=0 null=0 error=0 version=0 ratelimit=0 "
        "log_create=1 inner_create=0 decoded=1"
    )


def test_fetch_creates_counts_inner_create_before_decode():
    mint = pubkey_from_byte(56)
    rpc = FakeSolanaRpc(transactions={"sig-inner": _create_inner_tx(mint)})
    poll = fetch_creates_for_logs(
        rpc, "sig-inner", ["Program log: Instruction: Create"]
    )
    assert poll.log_create == 1
    assert poll.inner_create == 1
    assert poll.decoded == 1
    assert [c.mint for c in poll.creates] == [mint]


def test_fetch_creates_counts_skipped_rpc():
    rpc = FakeSolanaRpc(
        transactions={"sig-null": None},
        fail_transactions={"sig-err"},
        transaction_errors={
            "sig-ver": RpcError(
                "Transaction version (1) is not supported",
                rpc_code=-32015,
            ),
            "sig-429": RpcError("getTransaction failed: HTTP 429", status_code=429),
        },
    )
    logs = ["Program log: Instruction: Create"]
    null = fetch_creates_for_logs(rpc, "sig-null", logs)
    assert null.skipped_null == 1
    assert null.log_create == 1
    assert null.decoded == 0
    err = fetch_creates_for_logs(rpc, "sig-err", logs)
    assert err.skipped_error == 1
    ver = fetch_creates_for_logs(rpc, "sig-ver", logs)
    assert ver.skipped_version == 1
    rate = fetch_creates_for_logs(rpc, "sig-429", logs)
    assert rate.skipped_ratelimit == 1


def test_run_watch_once_does_not_scan_from_program_sigs():
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
    assert scanned == []
    assert results == []
    assert lines == []
    assert not any(c[0] == "get_transaction" for c in rpc.calls)
    assert err == [
        "signatures=1 creates=0 skipped_rpc=0 null=0 error=0 version=0 ratelimit=0 "
        + _SUMMARY_TAIL
    ]


def test_start_watch_prints_sanitized_ws_failure(monkeypatch):
    mint = pubkey_from_byte(57)
    rpc = FakeSolanaRpc(
        signatures=["sig-a"],
        transactions={"sig-a": _create_tx(mint)},
    )

    def boom(**_kwargs):
        raise RuntimeError("timed out connecting to wss://secret.example/ws?api-key=abc")

    monkeypatch.setenv("SOLANA_WSS_URL", "wss://secret.example/ws")
    monkeypatch.setattr("filter_floor.listeners.solana_ws.run_ws_watch", boom)
    err: list[str] = []
    start_watch(
        once=True,
        rpc=rpc,
        scan_fn=lambda chain, token: (_ for _ in ()).throw(AssertionError("no scan")),
        print_fn=lambda _line: None,
        err_fn=err.append,
        limit=1,
    )
    assert any(line.startswith("ws_down:") for line in err)
    blob = " ".join(err)
    assert "wss://" not in blob
    assert "secret.example" not in blob
    assert "abc" not in blob
    assert "RuntimeError" in blob
    assert "falling back to polling" not in blob
    assert not any(c[0] == "get_transaction" for c in rpc.calls)


def _scan_result(token: str) -> ScanResult:
    now = datetime.now(timezone.utc)
    return ScanResult(
        case_id="20260919-solana-watchscan",
        chain=Chain.solana,
        token=token,
        scanned_at=now,
        layer_a=layer_a_unknown(),
        layer_b=layer_b_unknown(),
        memory=memory_clean(),
        score_0_100=0,
        verdict=Verdict.CAUTION,
    )


def test_decoded_one_always_scans_and_prints_verdict():
    mint = pubkey_from_byte(58)
    rpc = FakeSolanaRpc(transactions={"sig-d": _create_tx(mint)})
    poll = fetch_creates_for_logs(
        rpc, "sig-d", ["Program log: Instruction: Create"]
    )
    assert poll.decoded == 1
    scanned: list[str] = []
    lines: list[str] = []
    err: list[str] = []

    def scan_fn(chain: Chain, token: str) -> ScanResult:
        scanned.append(token)
        assert chain is Chain.solana
        return _scan_result(token)

    results = handle_create_poll(
        poll,
        do_scan=scan_fn,
        seen_mints=set(),
        print_fn=lines.append,
        err_fn=err.append,
    )
    assert scanned == [mint]
    assert results[0].verdict is Verdict.CAUTION
    assert mint in lines[0]
    assert "CAUTION" in lines[0]
    assert "BUY" not in lines[0]


def test_scan_error_prints_type_not_url():
    mint = pubkey_from_byte(59)
    rpc = FakeSolanaRpc(transactions={"sig-e": _create_tx(mint)})
    poll = fetch_creates_for_logs(
        rpc, "sig-e", ["Program log: Instruction: Create"]
    )

    def boom(chain: Chain, token: str) -> ScanResult:
        raise RuntimeError("https://secret.example/rpc?api-key=abc")

    lines: list[str] = []
    err: list[str] = []
    results = handle_create_poll(
        poll,
        do_scan=boom,
        seen_mints=set(),
        print_fn=lines.append,
        err_fn=err.append,
    )
    assert results == []
    assert lines == []
    assert any(line == "scan_error=RuntimeError" for line in err)
    blob = " ".join(err)
    assert "secret.example" not in blob
    assert "abc" not in blob
    assert "https://" not in blob


def test_decoded_zero_prints_only_known_create_discs():
    mint = pubkey_from_byte(60)
    rpc = FakeSolanaRpc(
        transactions={
            "sig-u": {
                "logs": ["Program log: Instruction: CreateV2"],
                "instructions": [
                    {
                        "program_id": PUMP_PROGRAM_ID,
                        "accounts": [],
                        "data_hex": CREATE_DISCRIMINATOR.hex(),
                    },
                    {
                        "program_id": PUMP_PROGRAM_ID,
                        "accounts": [mint],
                        "data_hex": "e445a52e51cb9a1d",
                    },
                    {
                        "program_id": PUMP_PROGRAM_ID,
                        "accounts": [mint] * 8,
                        "data_hex": "a572670079cef751",
                    },
                ],
            }
        }
    )
    poll = fetch_creates_for_logs(
        rpc, "sig-u", ["Program log: Instruction: CreateV2"]
    )
    assert poll.decoded == 0
    assert poll.undecoded_ixs == ((CREATE_DISCRIMINATOR.hex(), 0),)
    err: list[str] = []
    handle_create_poll(
        poll,
        do_scan=lambda chain, token: (_ for _ in ()).throw(AssertionError("no scan")),
        seen_mints=set(),
        print_fn=lambda _line: None,
        err_fn=err.append,
    )
    assert f"disc_hex={CREATE_DISCRIMINATOR.hex()} account_count=0" in err
    assert not any("e445a52e51cb9a1d" in line for line in err)
    assert not any("a572670079cef751" in line for line in err)


def test_event_and_sell_discs_do_not_print_decoded_zero():
    mint = pubkey_from_byte(62)
    rpc = FakeSolanaRpc(
        transactions={
            "sig-ev": {
                "logs": ["Program log: Instruction: Create"],
                "instructions": [
                    {
                        "program_id": PUMP_PROGRAM_ID,
                        "accounts": [mint],
                        "data_hex": "e445a52e51cb9a1d",
                    }
                ],
            }
        }
    )
    poll = fetch_creates_for_logs(
        rpc, "sig-ev", ["Program log: Instruction: Create"]
    )
    assert poll.decoded == 0
    assert poll.undecoded_ixs == ()
    err: list[str] = []
    handle_create_poll(
        poll,
        do_scan=lambda chain, token: (_ for _ in ()).throw(AssertionError("no scan")),
        seen_mints=set(),
        print_fn=lambda _line: None,
        err_fn=err.append,
    )
    assert not any(line.startswith("disc_hex=") for line in err)


def test_ws_drop_retries_ws_not_polling(monkeypatch):
    ws_n = {"n": 0}
    sleeps: list[float] = []

    def fake_ws(**_kwargs):
        ws_n["n"] += 1
        if ws_n["n"] < 2:
            raise ConnectionError("keepalive ping timeout")

    def fake_poll(**_kwargs):
        raise AssertionError("polling must not be the detector")

    monkeypatch.setenv("SOLANA_WSS_URL", "wss://example.invalid")
    monkeypatch.setattr("filter_floor.listeners.solana_ws.run_ws_watch", fake_ws)
    monkeypatch.setattr("filter_floor.listeners.solana_ws.run_watch", fake_poll)
    err: list[str] = []
    start_watch(
        once=False,
        rpc=FakeSolanaRpc(signatures=["sig-a"]),
        scan_fn=lambda chain, token: (_ for _ in ()).throw(AssertionError("no scan")),
        print_fn=lambda _line: None,
        err_fn=err.append,
        limit=1,
        sleep_fn=sleeps.append,
    )
    assert ws_n["n"] == 2
    assert sleeps == [2.0]
    assert any(line.startswith("ws_down:") for line in err)
    blob = " ".join(err)
    assert "falling back to polling" not in blob
    assert "exhausted" not in blob
    assert "staying on polling" not in blob
    assert "wss://" not in blob
    assert "example.invalid" not in blob


def test_ws_backoff_caps_at_30s():
    from filter_floor.listeners.solana_ws import ws_backoff_s

    assert ws_backoff_s(0) == 2.0
    assert ws_backoff_s(1) == 5.0
    assert ws_backoff_s(2) == 15.0
    assert ws_backoff_s(3) == 30.0
    assert ws_backoff_s(99) == 30.0
