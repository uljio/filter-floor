"""M4 outcomes: schedule +1h/+6h/+24h, due labeling, memory death. Mocked HTTP only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from typer.testing import CliRunner

from filter_floor.adapters.dexscreener import MarketSnapshot, fetch_dexscreener
from filter_floor.cli import app
from filter_floor.labeling import classify_snapshot, label_due, outcome_for_case
from filter_floor.models import Chain, Outcome, OutcomeLabel, ScanResult, Verdict
from filter_floor.pipeline import run_scan
from filter_floor.storage.cases import make_case_id, write_case
from filter_floor.storage.memory import deployer_path
from filter_floor.storage.outcomes import due_times, read_outcome_record
from tests.helpers import layer_a_clean, layer_a_unknown, layer_b_clean, layer_b_unknown, memory_clean

runner = CliRunner()
NOW = datetime(2026, 9, 16, 21, 0, tzinfo=timezone.utc)
DEPLOYER = "OutcomeDeployer11111111111111111111111"


def _result(
    *,
    token: str,
    scanned_at: datetime,
    verdict: Verdict = Verdict.CAUTION,
    deployer: str | None = DEPLOYER,
) -> ScanResult:
    case_id = make_case_id(scanned_at, Chain.solana, token)
    clean = verdict is Verdict.PASS_FILTER
    return ScanResult(
        case_id=case_id,
        chain=Chain.solana,
        token=token,
        deployer=deployer,
        scanned_at=scanned_at,
        layer_a=layer_a_clean() if clean else layer_a_unknown(),
        layer_b=layer_b_clean() if clean else layer_b_unknown(),
        memory=memory_clean(deployer=deployer or "unknown"),
        score_0_100=0,
        verdict=verdict,
    )


def _write_scan(tmp_path, result: ScanResult) -> ScanResult:
    write_case(result, tmp_path)
    from filter_floor.storage.outcomes import schedule_for_scan

    schedule_for_scan(result, tmp_path)
    return result


def _write_memory(tmp_path, *, deployer: str, token: str, case_id: str, dead=None) -> None:
    path = deployer_path(tmp_path, deployer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "deployer": deployer,
                "death_rate": None,
                "tokens": [
                    {
                        "token": token,
                        "chain": "solana",
                        "case_id": case_id,
                        "dead": dead,
                    }
                ],
                "details": {"death_rate_null": True},
            }
        ),
        encoding="utf-8",
    )


def test_scan_schedules_1h_6h_24h_due_times(tmp_path):
    result = run_scan(
        chain=Chain.solana,
        token="SchedTok11111111111111111111111111111111",
        data_dir=tmp_path,
    )
    record = read_outcome_record(result.case_id, tmp_path)
    assert record is not None
    dues = due_times(result.scanned_at)
    assert record.due_1h == dues["1h"]
    assert record.due_6h == dues["6h"]
    assert record.due_24h == dues["24h"]
    assert record.at_1h is None
    assert record.at_6h is None
    assert record.at_24h is None
    assert record.due_1h == result.scanned_at + timedelta(hours=1)
    assert record.due_6h == result.scanned_at + timedelta(hours=6)
    assert record.due_24h == result.scanned_at + timedelta(hours=24)
    md = (tmp_path / "cases" / f"{result.case_id}.md").read_text(encoding="utf-8")
    assert "due_1h" in md
    assert "due_6h" in md
    assert "due_24h" in md
    assert "BUY" not in md


def test_due_labeling_writes_outcome(tmp_path):
    scanned_at = NOW - timedelta(hours=2)
    result = _write_scan(tmp_path, _result(token="DueWrite1", scanned_at=scanned_at))
    snap = MarketSnapshot(
        price_usd=1.0,
        liquidity_usd=500.0,
        tradeable=True,
        source="mock",
    )
    labeled = label_due(
        tmp_path,
        now=NOW,
        market_fetch=lambda chain, token: snap,
    )
    assert labeled
    assert all(item.horizon == "1h" for item in labeled)
    outcome = outcome_for_case(result.case_id, tmp_path)
    assert isinstance(outcome, Outcome)
    assert outcome.case_id == result.case_id
    assert outcome.at_1h is OutcomeLabel.survived
    assert outcome.at_6h is None
    assert outcome.at_24h is None
    stored = read_outcome_record(result.case_id, tmp_path)
    assert stored is not None
    assert stored.to_outcome().at_1h is OutcomeLabel.survived


def test_missing_price_is_unknown_not_survived(tmp_path):
    scanned_at = NOW - timedelta(hours=2)
    result = _write_scan(tmp_path, _result(token="NoQuoteTok", scanned_at=scanned_at))
    labeled = label_due(
        tmp_path,
        now=NOW,
        market_fetch=lambda chain, token: MarketSnapshot(
            source="dexscreener",
            error="no_pairs",
        ),
    )
    assert labeled
    outcome = outcome_for_case(result.case_id, tmp_path)
    assert outcome is not None
    assert outcome.at_1h is OutcomeLabel.unknown
    assert outcome.at_1h is not OutcomeLabel.survived
    assert outcome.at_1h is not OutcomeLabel.rugged
    assert "unknown" in (outcome.notes or "")


def test_classify_missing_price_never_survived():
    decision = classify_snapshot(MarketSnapshot(source="dexscreener", error="offline"))
    assert decision.label is OutcomeLabel.unknown
    assert decision.dead is None
    assert decision.label is not OutcomeLabel.survived


def test_classify_liquidity_gone_is_rugged_with_death():
    decision = classify_snapshot(MarketSnapshot(price_usd=0.01, liquidity_usd=0.0, source="mock"))
    assert decision.label is OutcomeLabel.rugged
    assert decision.dead is True


def test_drawdown_without_creator_extracted_is_rugged_death_null():
    decision = classify_snapshot(
        MarketSnapshot(price_usd=0.05, liquidity_usd=200.0, tradeable=True, source="mock"),
        first_price_usd=1.0,
    )
    assert decision.label is OutcomeLabel.rugged
    assert decision.dead is None


def test_rugged_24h_updates_memory_unknown_is_not_zero_death_rate(tmp_path):
    dead_token = "RugToken111111111111111111111111111111"
    unknown_token = "UnkToken111111111111111111111111111111"
    scanned_at = NOW - timedelta(hours=25)
    dead_scan = _write_scan(
        tmp_path, _result(token=dead_token, scanned_at=scanned_at, deployer=DEPLOYER)
    )
    unk_scan = _write_scan(
        tmp_path, _result(token=unknown_token, scanned_at=scanned_at, deployer=DEPLOYER)
    )
    path = deployer_path(tmp_path, DEPLOYER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "deployer": DEPLOYER,
                "death_rate": None,
                "tokens": [
                    {
                        "token": dead_token,
                        "chain": "solana",
                        "case_id": dead_scan.case_id,
                        "dead": None,
                    },
                    {
                        "token": unknown_token,
                        "chain": "solana",
                        "case_id": unk_scan.case_id,
                        "dead": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_market(chain, token):
        _ = chain
        if token == dead_token:
            return MarketSnapshot(price_usd=0.01, liquidity_usd=0.0, tradeable=False, source="mock")
        return MarketSnapshot(source="dexscreener", error="no_pairs")

    labeled = label_due(tmp_path, now=NOW, market_fetch=fake_market)
    assert labeled
    dead_out = outcome_for_case(dead_scan.case_id, tmp_path)
    unk_out = outcome_for_case(unk_scan.case_id, tmp_path)
    assert dead_out is not None and dead_out.at_24h is OutcomeLabel.rugged
    assert unk_out is not None and unk_out.at_24h is OutcomeLabel.unknown
    stored = json.loads(path.read_text(encoding="utf-8"))
    by_token = {row["token"]: row.get("dead") for row in stored["tokens"]}
    assert by_token[dead_token] is True
    assert by_token[unknown_token] is None
    assert stored["death_rate"] == 1.0
    assert stored["death_rate"] != 0
    unk_only = [row for row in stored["tokens"] if row["token"] == unknown_token]
    assert unk_only[0]["dead"] is None


def test_unknown_only_leaves_death_rate_null(tmp_path):
    scanned_at = NOW - timedelta(hours=25)
    result = _write_scan(tmp_path, _result(token="OnlyUnknown", scanned_at=scanned_at))
    _write_memory(tmp_path, deployer=DEPLOYER, token=result.token, case_id=result.case_id)
    label_due(
        tmp_path,
        now=NOW,
        market_fetch=lambda chain, token: MarketSnapshot(error="no_pairs", source="mock"),
    )
    stored = json.loads(deployer_path(tmp_path, DEPLOYER).read_text(encoding="utf-8"))
    assert stored["death_rate"] is None
    assert stored["tokens"][0]["dead"] is None
    outcome = outcome_for_case(result.case_id, tmp_path)
    assert outcome is not None
    assert outcome.at_24h is OutcomeLabel.unknown


def test_ff_label_due_cli_writes_outcome_no_buy(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    scanned_at = datetime.now(timezone.utc) - timedelta(hours=2)
    result = _write_scan(tmp_path, _result(token="CliLabel1", scanned_at=scanned_at))

    def fake_fetch(chain, token, http=None, native=None):
        _ = chain, token, http, native
        return MarketSnapshot(source="dexscreener", error="offline")

    monkeypatch.setattr("filter_floor.labeling.fetch_market_snapshot", fake_fetch)
    invoked = runner.invoke(app, ["label", "--due"])
    assert invoked.exit_code == 0, invoked.output
    assert "BUY" not in invoked.output
    assert result.case_id in invoked.output
    assert "unknown" in invoked.output
    outcome = outcome_for_case(result.case_id, tmp_path)
    assert outcome is not None
    assert outcome.at_1h is OutcomeLabel.unknown


def test_dexscreener_parses_pairs_mocked_http():
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "priceUsd": "0.25",
                "liquidity": {"usd": 1200.0},
                "pairAddress": "pair1",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snap = fetch_dexscreener(Chain.solana, "mint111", http=client)
    assert snap.has_evidence
    assert snap.price_usd == 0.25
    assert snap.liquidity_usd == 1200.0
    assert snap.source == "dexscreener"


def test_dexscreener_http_error_is_unknown_not_survived():
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snap = fetch_dexscreener(Chain.solana, "mint111", http=client)
    assert not snap.has_evidence
    decision = classify_snapshot(snap)
    assert decision.label is OutcomeLabel.unknown
    assert decision.label is not OutcomeLabel.survived
