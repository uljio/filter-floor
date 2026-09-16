"""Base watch: Uniswap V3 PoolCreated polling. Mocked RPC only."""

from __future__ import annotations

import json
from pathlib import Path

from filter_floor.listeners.evm_ws import (
    launch_token_from_pool,
    poll_new_pools,
    run_watch,
)
from filter_floor.models import Chain, Verdict
from tests.test_evm import FakeEvmRpc, POOL, TOKEN, BASE_QUOTE_TOKENS

FIXTURES = Path(__file__).parent / "fixtures"


def test_launch_token_picks_non_quote():
    decoded = {
        "token0": TOKEN,
        "token1": BASE_QUOTE_TOKENS["weth"].lower(),
    }
    assert launch_token_from_pool(decoded) == TOKEN


def test_poll_new_pools_decodes_fixture():
    log = json.loads((FIXTURES / "evm_pool_created_log.json").read_text(encoding="utf-8"))
    rpc = FakeEvmRpc(logs=[log])
    found = poll_new_pools(rpc, log["address"], from_block=1, to_block=16)
    assert len(found) == 1
    assert found[0]["token0"] == TOKEN
    assert found[0]["pool"] == POOL


def test_run_watch_once_scans_new_token():
    log = json.loads((FIXTURES / "evm_pool_created_log.json").read_text(encoding="utf-8"))
    scanned: list[str] = []

    def scan_fn(chain, token):
        assert chain is Chain.base
        scanned.append(token)
        from datetime import datetime, timezone

        from filter_floor.models import ScanResult
        from tests.helpers import layer_a_unknown, layer_b_unknown, memory_clean

        now = datetime.now(timezone.utc)
        return ScanResult(
            case_id="20260916-base-0x111111",
            chain=Chain.base,
            token=token,
            scanned_at=now,
            layer_a=layer_a_unknown(),
            layer_b=layer_b_unknown(),
            memory=memory_clean(),
            score_0_100=0,
            verdict=Verdict.CAUTION,
        )

    rpc = FakeEvmRpc(logs=[log], block_number=16)
    lines: list[str] = []
    results = run_watch(
        once=True,
        rpc=rpc,
        factory=log["address"],
        scan_fn=scan_fn,
        print_fn=lines.append,
        lookback=32,
    )
    assert scanned == [TOKEN]
    assert results[0].verdict is Verdict.CAUTION
    assert "CAUTION" in lines[0]
    assert "BUY" not in lines[0]


def test_run_watch_rpc_miss_does_not_invent_pass():
    rpc = FakeEvmRpc(block_number=None)
    lines: list[str] = []
    results = run_watch(
        once=True,
        rpc=rpc,
        factory="0x33128a8fc17869897dce68ed026d694621f6fdfd",
        print_fn=lines.append,
    )
    assert results == []
    assert any("RPC miss" in line for line in lines)
    assert all("PASS_FILTER" not in line for line in lines)
