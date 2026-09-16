"""Pump.fun create polling. Mocked signatures/txs; no live websocket."""

from __future__ import annotations

from filter_floor.listeners.solana_ws import poll_new_creates, run_watch
from filter_floor.models import Chain, ScanResult, Verdict
from filter_floor.scanners.pumpfun import CREATE_DISCRIMINATOR, PUMP_PROGRAM_ID
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
    assert [c.mint for c in first] == [mint]
    second = poll_new_creates(rpc, seen)
    assert second == []


def test_poll_rpc_failure_returns_empty_not_invented_mints():
    rpc = FakeSolanaRpc(fail=True)
    assert poll_new_creates(rpc, set()) == []


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
    results = run_watch(
        once=True,
        rpc=rpc,
        scan_fn=scan_fn,
        print_fn=lines.append,
        interval_s=0,
    )
    assert scanned == [mint]
    assert len(results) == 1
    assert results[0].verdict is Verdict.CAUTION
    assert "CAUTION" in lines[0]
    assert "BUY" not in lines[0]
    assert mint in lines[0]
