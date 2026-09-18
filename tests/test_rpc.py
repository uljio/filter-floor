"""Solana RPC getTransaction retries and sanitization. No live RPC."""

from __future__ import annotations

import pytest

from filter_floor.adapters.rpc import (
    RpcError,
    SolanaRpc,
    classify_tx_rpc_error,
    sanitize_rpc_text,
)


def test_sanitize_rpc_text_strips_url_and_api_key():
    url = sanitize_rpc_text("POST https://api.helius.xyz/v0 429")
    assert "helius" not in url.lower()
    assert "[rpc]" in url
    keyed = sanitize_rpc_text("api-key=supersecret")
    assert "supersecret" not in keyed
    assert "api-key=[redacted]" in keyed
    wss = sanitize_rpc_text("timed out connecting to wss://secret.example/key")
    assert "secret.example" not in wss
    assert "[rpc]" in wss


def test_rpc_error_message_never_keeps_url():
    err = RpcError(
        "getTransaction failed: https://example.invalid/rpc?api-key=abc",
        status_code=429,
    )
    assert "example.invalid" not in str(err)
    assert "abc" not in str(err)


def test_get_transaction_uses_jsonparsed_and_version_1():
    rpc = SolanaRpc(url="http://localhost")
    seen: dict = {}

    def _call(method, params, timeout_s=None):
        seen["method"] = method
        seen["params"] = params
        return None

    rpc._call = _call  # type: ignore[method-assign]
    assert rpc.get_transaction("sig") is None
    assert seen["method"] == "getTransaction"
    opts = seen["params"][1]
    assert opts["encoding"] == "jsonParsed"
    assert opts["maxSupportedTransactionVersion"] == 1


def test_get_transaction_retries_429_with_1s_then_2s_backoff():
    rpc = SolanaRpc(url="http://localhost")
    n = {"i": 0}

    def _call(method, params, timeout_s=None):
        n["i"] += 1
        if n["i"] < 3:
            raise RpcError("getTransaction failed: HTTP 429", status_code=429)
        return {"slot": 1, "transaction": {}, "meta": {}}

    rpc._call = _call  # type: ignore[method-assign]
    sleeps: list[float] = []
    tx = rpc.get_transaction("sig", sleep_fn=sleeps.append)
    assert tx is not None
    assert n["i"] == 3
    assert sleeps == [1.0, 2.0]


def test_get_transaction_retries_then_raises_after_three_429s():
    rpc = SolanaRpc(url="http://localhost")
    n = {"i": 0}

    def _call(method, params, timeout_s=None):
        n["i"] += 1
        raise RpcError("getTransaction failed: HTTP 429", status_code=429)

    rpc._call = _call  # type: ignore[method-assign]
    sleeps: list[float] = []
    with pytest.raises(RpcError) as caught:
        rpc.get_transaction("sig", sleep_fn=sleeps.append)
    assert n["i"] == 3
    assert sleeps == [1.0, 2.0]
    assert caught.value.status_code == 429
    assert classify_tx_rpc_error(caught.value) == "ratelimit"


def test_version_error_is_not_retried():
    rpc = SolanaRpc(url="http://localhost")
    n = {"i": 0}

    def _call(method, params, timeout_s=None):
        n["i"] += 1
        raise RpcError(
            "Transaction version (1) is not supported",
            rpc_code=-32015,
        )

    rpc._call = _call  # type: ignore[method-assign]
    sleeps: list[float] = []
    with pytest.raises(RpcError) as caught:
        rpc.get_transaction("sig", sleep_fn=sleeps.append)
    assert n["i"] == 1
    assert sleeps == []
    assert classify_tx_rpc_error(caught.value) == "version"
    assert "BUY" not in str(caught.value)
