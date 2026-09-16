"""Pipeline + CLI: stub scan writes files; empty scan is CAUTION."""

from __future__ import annotations

import json

import typer
from typer.testing import CliRunner

from filter_floor.cli import app
from filter_floor.models import Chain, CheckStatus, ScanResult, Verdict
from filter_floor.pipeline import run_scan
from filter_floor.scanners.clanker import ClankerScanner
from filter_floor.scanners.evm import EvmScanner
from filter_floor.scanners.pons import PonsScanner
from filter_floor.scanners.pumpfun import PumpfunScanner
from filter_floor.scanners.solana import SolanaScanner
from filter_floor.storage.cases import make_case_id

runner = CliRunner()

LAYER_A_FIELDS = (
    "mint_authority_revoked",
    "freeze_authority_revoked",
    "lp_locked_or_burned",
    "honeypot_or_unsellable",
    "owner_or_upgrade_risk",
    "token2022_or_hook_risk",
)


def test_stub_scanners_return_unknown_not_pass():
    token = "stubtoken"
    scanners = [
        SolanaScanner(),
        EvmScanner(),
        PumpfunScanner(),
        PonsScanner(),
        ClankerScanner(),
    ]
    for scanner in scanners:
        layer_a, layer_b = scanner.scan(token)
        for field in LAYER_A_FIELDS:
            assert getattr(layer_a, field) is CheckStatus.UNKNOWN
        assert layer_b.bundle_detected is CheckStatus.UNKNOWN
        assert layer_b.same_funder_as_known_bad is CheckStatus.UNKNOWN
        assert layer_b.early_consolidation is CheckStatus.UNKNOWN
        assert layer_b.bundled_supply_pct is None
        assert "TODO(verify)" in layer_a.details
        assert "TODO(verify)" in layer_b.details


def test_empty_scan_is_caution_never_pass_filter(tmp_path):
    result = run_scan(
        chain=Chain.solana,
        token="Mint111111111111111111111111111111111111111",
        data_dir=tmp_path,
    )
    assert result.verdict is Verdict.CAUTION
    assert result.verdict is not Verdict.PASS_FILTER
    assert result.score_0_100 == 0
    json_path = tmp_path / "scans" / f"{result.case_id}.json"
    md_path = tmp_path / "cases" / f"{result.case_id}.md"
    assert json_path.is_file()
    assert md_path.is_file()
    stored = ScanResult.model_validate_json(json_path.read_text(encoding="utf-8"))
    assert stored.verdict is Verdict.CAUTION
    assert "BUY" not in md_path.read_text(encoding="utf-8")


def test_ff_scan_writes_json_and_case_md(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    token = "SoDummyX111111111111111111111111111111111"
    invoked = runner.invoke(
        app,
        ["scan", "--chain", "solana", "--token", token],
    )
    assert invoked.exit_code == 0, invoked.output
    assert "BUY" not in invoked.output
    assert "PASS_FILTER" not in invoked.output
    assert "CAUTION" in invoked.output

    scans = list((tmp_path / "scans").glob("*.json"))
    cases = list((tmp_path / "cases").glob("*.md"))
    assert len(scans) == 1
    assert len(cases) == 1
    payload = json.loads(scans[0].read_text(encoding="utf-8"))
    assert payload["verdict"] == "CAUTION"
    assert payload["token"] == token
    assert payload["chain"] == "solana"
    assert "case_id" in payload
    md = cases[0].read_text(encoding="utf-8")
    assert payload["case_id"] in md
    assert "**CAUTION**" in md


def test_ff_show_reads_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    token = "ShowTok11111111111111111111111111111111111"
    scan_result = runner.invoke(
        app, ["scan", "--chain", "base", "--token", token]
    )
    assert scan_result.exit_code == 0, scan_result.output
    case_id = scan_result.output.split()[0]
    shown = runner.invoke(app, ["show", case_id])
    assert shown.exit_code == 0, shown.output
    assert case_id in shown.output
    assert "CAUTION" in shown.output


def test_case_id_format():
    from datetime import datetime, timezone
    from filter_floor.models import Chain

    scanned_at = datetime(2026, 9, 16, tzinfo=timezone.utc)
    case_id = make_case_id(scanned_at, Chain.solana, "ABCDEFGHrestofmint")
    assert case_id == "20260916-solana-ABCDEFGH"


def test_stub_commands_exit_not_in_milestone():
    invoked = runner.invoke(app, ["watch", "--chain", "robinhood"])
    assert invoked.exit_code == 1
    assert "Robinhood watch disabled" in invoked.output
    assert "PASS_FILTER" not in invoked.output
    assert "BUY" not in invoked.output


def test_label_without_due_exits():
    invoked = runner.invoke(app, ["label"])
    assert invoked.exit_code == 1
    assert "label requires --due" in invoked.output
    assert "BUY" not in invoked.output


def test_watch_solana_once_mocked(monkeypatch):
    from datetime import datetime, timezone

    from filter_floor.models import ScanResult, Verdict
    from tests.helpers import layer_a_unknown, layer_b_unknown, memory_clean

    def fake_start_watch(*, min_score_alert: int, once: bool):
        assert once is True
        assert min_score_alert == 50
        now = datetime.now(timezone.utc)
        result = ScanResult(
            case_id="20260916-solana-WatchTok",
            chain=Chain.solana,
            token="WatchTok111111111111111111111111111111111",
            scanned_at=now,
            layer_a=layer_a_unknown(),
            layer_b=layer_b_unknown(),
            memory=memory_clean(),
            score_0_100=0,
            verdict=Verdict.CAUTION,
        )
        typer.echo(
            f"{result.case_id} {result.chain.value} {result.token} "
            f"{result.score_0_100} {result.verdict.value}"
        )

    monkeypatch.setattr(
        "filter_floor.listeners.solana_ws.start_watch", fake_start_watch
    )
    invoked = runner.invoke(app, ["watch", "--chain", "solana", "--once"])
    assert invoked.exit_code == 0, invoked.output
    assert "CAUTION" in invoked.output
    assert "BUY" not in invoked.output


def test_watch_base_once_mocked(monkeypatch):
    from datetime import datetime, timezone

    from filter_floor.models import ScanResult, Verdict
    from tests.helpers import layer_a_unknown, layer_b_unknown, memory_clean

    def fake_start_watch(*, min_score_alert: int, once: bool):
        assert once is True
        now = datetime.now(timezone.utc)
        result = ScanResult(
            case_id="20260916-base-0x111111",
            chain=Chain.base,
            token="0x1111111111111111111111111111111111111111",
            scanned_at=now,
            layer_a=layer_a_unknown(),
            layer_b=layer_b_unknown(),
            memory=memory_clean(),
            score_0_100=0,
            verdict=Verdict.CAUTION,
        )
        typer.echo(
            f"{result.case_id} {result.chain.value} {result.token} "
            f"{result.score_0_100} {result.verdict.value}"
        )

    monkeypatch.setattr(
        "filter_floor.listeners.evm_ws.start_watch", fake_start_watch
    )
    invoked = runner.invoke(app, ["watch", "--chain", "base", "--once"])
    assert invoked.exit_code == 0, invoked.output
    assert "CAUTION" in invoked.output
    assert "BUY" not in invoked.output
