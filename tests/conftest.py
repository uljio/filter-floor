"""Keep EVM tests offline. Do not clear SOLANA_RPC_URL (M1 owns that)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_evm_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_RPC_URL", "")
    monkeypatch.setenv("BASE_WSS_URL", "")
    monkeypatch.setenv("ROBINHOOD_RPC_URL", "")
    monkeypatch.setenv("ROBINHOOD_WSS_URL", "")
    monkeypatch.setenv("ROBINHOOD_ENABLED", "0")
    monkeypatch.setenv("EXPLAIN_ENABLED", "0")
    monkeypatch.setenv("XAI_API_KEY", "")
