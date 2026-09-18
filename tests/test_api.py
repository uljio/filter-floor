"""M5 localhost API: TestClient only. Existing pipeline verdicts. No BUY."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from filter_floor.api import HOST, PORT, app as api_app
from filter_floor.cli import app as cli_app
from filter_floor.models import OutcomeLabel, Verdict
from filter_floor.storage.cases import write_case
from filter_floor.storage.funnel import funnel_path
from filter_floor.storage.outcomes import schedule_for_scan, write_outcome_record
from tests.test_outcomes import _result

runner = CliRunner()
client = TestClient(api_app)


def test_api_bind_is_localhost_only():
    assert HOST == "127.0.0.1"
    assert PORT == 3001
    assert HOST != "0.0.0.0"
    assert "0.0.0.0" not in HOST


def test_ff_api_refuses_non_localhost():
    invoked = runner.invoke(cli_app, ["api", "--host", "0.0.0.0"])
    assert invoked.exit_code == 1
    assert "127.0.0.1" in invoked.output
    assert "BUY" not in invoked.output


def test_ff_api_starts_uvicorn_on_localhost(monkeypatch):
    import uvicorn

    called: dict = {}

    def fake_run(fastapi_app, host: str, port: int) -> None:
        called["host"] = host
        called["port"] = port
        called["app"] = fastapi_app

    monkeypatch.setattr(uvicorn, "run", fake_run)
    invoked = runner.invoke(cli_app, ["api"])
    assert invoked.exit_code == 0, invoked.output
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 3001
    assert called["app"] is api_app
    assert "BUY" not in invoked.output


def test_post_scan_returns_pipeline_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    token = "ApiScan111111111111111111111111111111111"
    resp = client.post("/scan", json={"chain": "solana", "token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "CAUTION"
    assert body["token"] == token
    assert body["chain"] == "solana"
    assert "BUY" not in resp.text
    assert body["verdict"] != "PASS_FILTER"
    json_path = tmp_path / "scans" / f"{body['case_id']}.json"
    assert json_path.is_file()


def test_get_case_after_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    token = "ApiCase111111111111111111111111111111111"
    created = client.post("/scan", json={"chain": "base", "token": token})
    assert created.status_code == 200, created.text
    case_id = created.json()["case_id"]
    got = client.get(f"/cases/{case_id}")
    assert got.status_code == 200, got.text
    assert got.json()["verdict"] == created.json()["verdict"]
    assert got.json()["score_0_100"] == created.json()["score_0_100"]
    assert "BUY" not in got.text


def test_get_case_missing_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    resp = client.get("/cases/20260916-solana-missing")
    assert resp.status_code == 404
    assert "BUY" not in resp.text


def test_get_funnel(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    scanned_at = now - timedelta(hours=2)
    avoid = _result(token="ApiFunAvd", scanned_at=scanned_at, verdict=Verdict.AVOID)
    caution = _result(token="ApiFunCtn", scanned_at=scanned_at, verdict=Verdict.CAUTION)
    passed = _result(
        token="ApiFunPas", scanned_at=scanned_at, verdict=Verdict.PASS_FILTER
    )
    old = _result(
        token="ApiFunOld",
        scanned_at=now - timedelta(days=10),
        verdict=Verdict.AVOID,
    )
    for result in (avoid, caution, passed, old):
        write_case(result, tmp_path)
        record = schedule_for_scan(result, tmp_path)
        if result is passed:
            record.at_24h = OutcomeLabel.rugged
            write_outcome_record(record, tmp_path)

    resp = client.get("/funnel", params={"days": 7})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scanned"] == 3
    assert body["vetoed"] == 1
    assert body["caution"] == 1
    assert body["pass"] == 1
    assert body["later_rugged"] == 1
    assert body["days"] == 7
    assert "BUY" not in resp.text
    assert not funnel_path(tmp_path).exists()

    again = client.get("/funnel")
    assert again.status_code == 200
    assert again.json()["scanned"] == 3
    assert not funnel_path(tmp_path).exists()


def test_get_index_is_local_html():
    resp = client.get("/")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text
    assert "Filter Floor" in body
    assert "http://127.0.0.1:3001" in body
    assert "BUY" not in body
    assert "cdn." not in body.lower()
    assert "jsdelivr" not in body.lower()
    assert "unpkg" not in body.lower()
    assert "0.0.0.0" not in body


def test_get_cases_lists_recent_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    recent = _result(
        token="ApiListNew", scanned_at=now - timedelta(hours=2), verdict=Verdict.CAUTION
    )
    old = _result(
        token="ApiListOld",
        scanned_at=now - timedelta(days=10),
        verdict=Verdict.AVOID,
    )
    write_case(recent, tmp_path)
    write_case(old, tmp_path)

    resp = client.get("/cases", params={"days": 1})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list)
    ids = {row["case_id"] for row in rows}
    assert recent.case_id in ids
    assert old.case_id not in ids
    row = next(item for item in rows if item["case_id"] == recent.case_id)
    assert set(row) == {
        "case_id",
        "chain",
        "token",
        "score",
        "verdict",
        "scanned_at",
    }
    assert row["token"] == recent.token
    assert row["score"] == recent.score_0_100
    assert row["verdict"] == "CAUTION"
    assert "BUY" not in resp.text


def test_api_scan_empty_token_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    resp = client.post("/scan", json={"chain": "solana", "token": "   "})
    assert resp.status_code in {400, 422}
    assert "BUY" not in resp.text
