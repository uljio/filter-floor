"""Spec §11 test 8: ff funnel counts veto vs pass. Appends data/funnel.jsonl."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from filter_floor.cli import app
from filter_floor.models import OutcomeLabel, Verdict
from filter_floor.storage.cases import write_case
from filter_floor.storage.funnel import compute_and_write, funnel_path
from filter_floor.storage.outcomes import schedule_for_scan, write_outcome_record
from tests.test_outcomes import NOW, _result

runner = CliRunner()


def test_ff_funnel_counts_veto_vs_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    scanned_at = now - timedelta(hours=3)
    avoid = _result(token="FunnelAvd", scanned_at=scanned_at, verdict=Verdict.AVOID)
    caution = _result(token="FunnelCtn", scanned_at=scanned_at, verdict=Verdict.CAUTION)
    passed = _result(token="FunnelPas", scanned_at=scanned_at, verdict=Verdict.PASS_FILTER)
    old = _result(
        token="FunnelOld",
        scanned_at=now - timedelta(days=10),
        verdict=Verdict.AVOID,
    )
    for result in (avoid, caution, passed, old):
        write_case(result, tmp_path)
        record = schedule_for_scan(result, tmp_path)
        if result is passed:
            record.at_24h = OutcomeLabel.rugged
            write_outcome_record(record, tmp_path)

    invoked = runner.invoke(app, ["funnel", "--days", "7"])
    assert invoked.exit_code == 0, invoked.output
    assert "BUY" not in invoked.output
    assert "scanned=3" in invoked.output
    assert "vetoed=1" in invoked.output
    assert "caution=1" in invoked.output
    assert "pass=1" in invoked.output
    assert "later_rugged=1" in invoked.output

    path = funnel_path(tmp_path)
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    row = lines[0]
    assert row["scanned"] == 3
    assert row["vetoed"] == 1
    assert row["pass"] == 1
    assert row["caution"] == 1
    assert row["later_rugged"] == 1
    assert row["days"] == 7

    again = runner.invoke(app, ["funnel", "--days", "7"])
    assert again.exit_code == 0, again.output
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2


def test_compute_funnel_library_counts(tmp_path):
    scanned_at = NOW - timedelta(hours=1)
    write_case(_result(token="LibAvoid", scanned_at=scanned_at, verdict=Verdict.AVOID), tmp_path)
    write_case(
        _result(token="LibPass1", scanned_at=scanned_at, verdict=Verdict.PASS_FILTER),
        tmp_path,
    )
    counts = compute_and_write(tmp_path, days=7, now=NOW)
    assert counts.vetoed == 1
    assert counts.pass_filter == 1
    assert counts.scanned == 2
    assert "BUY" not in counts.one_line()
