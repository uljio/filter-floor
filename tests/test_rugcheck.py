"""Optional RugCheck adapter stays off unless RUGCHECK_ENABLED=1."""

from __future__ import annotations

import httpx

from filter_floor.adapters.rugcheck import fetch_rugcheck_report, rugcheck_enabled
from filter_floor.models import CheckStatus
from filter_floor.scanners.solana import SolanaScanner
from tests.fakes import FakeSolanaRpc
from tests.fixtures.solana_mints import (
    mint_with_mint_authority,
    pubkey_from_byte,
    spl_account,
)


def _client(body: dict, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rugcheck_disabled_by_default():
    assert rugcheck_enabled({"RUGCHECK_ENABLED": "0"}) is False
    assert fetch_rugcheck_report("Mint11111111111111111111111111111111", enabled=False) is None


def test_rugcheck_enabled_merges_details_does_not_override_mint_fail(monkeypatch):
    mint = pubkey_from_byte(61)
    monkeypatch.setattr(
        "filter_floor.scanners.solana.fetch_rugcheck_report",
        lambda token, **kwargs: {
            "enabled": True,
            "score": 1,
            "rugged": False,
            "source": "rugcheck",
            "risks": ["mint"],
        },
    )
    rpc = FakeSolanaRpc({mint: spl_account(mint_with_mint_authority())})
    layer_a = SolanaScanner(rpc=rpc).scan_layer_a(mint)
    assert layer_a.mint_authority_revoked is CheckStatus.FAIL
    assert layer_a.details["rugcheck"]["source"] == "rugcheck"
    assert layer_a.details["rugcheck"]["score"] == 1


def test_rugcheck_http_error_does_not_invent_pass():
    mint = pubkey_from_byte(62)
    report = fetch_rugcheck_report(
        mint,
        enabled=True,
        http=_client({"error": "nope"}, status=500),
    )
    assert report is not None
    assert "error" in report
    assert "TODO(verify)" in report
