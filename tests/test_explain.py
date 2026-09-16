"""M5 explainer: skip when disabled; guard never rewrites the scan verdict."""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from filter_floor.cli import app
from filter_floor.explain import (
    SYSTEM_RULE,
    ExplainOutcome,
    claimed_verdicts,
    explain_case,
    explain_scan,
    guard_explanation,
    skip_reason,
    system_prompt,
    user_prompt,
)
from filter_floor.models import CheckStatus, Verdict
from filter_floor.storage.cases import read_scan, write_case
from tests.test_outcomes import NOW, _result

runner = CliRunner()


def _caution_unknown(**kwargs):
    result = _result(verdict=Verdict.CAUTION, scanned_at=NOW, **kwargs)
    assert result.layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    return result


def _llm_client(text: str, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": text}}]},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_skip_when_explain_disabled(monkeypatch):
    monkeypatch.setenv("EXPLAIN_ENABLED", "0")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")
    assert skip_reason() == "skipped (EXPLAIN_ENABLED=0)"
    outcome = explain_scan(_caution_unknown(token="SkipOff1"))
    assert outcome.skipped is True
    assert outcome.prose is None
    assert outcome.verdict is Verdict.CAUTION
    assert "EXPLAIN_ENABLED" in (outcome.reason or "")


def test_skip_when_api_key_missing(monkeypatch):
    monkeypatch.setenv("EXPLAIN_ENABLED", "1")
    monkeypatch.setenv("XAI_API_KEY", "")
    assert skip_reason() == "skipped (XAI_API_KEY missing)"
    outcome = explain_scan(_caution_unknown(token="SkipKey1"))
    assert outcome.skipped is True
    assert outcome.prose is None
    assert "XAI_API_KEY" in (outcome.reason or "")


def test_prompt_is_scan_json_only_and_forbids_verdict_change():
    result = _caution_unknown(token="PromptTok")
    sys_p = system_prompt()
    assert SYSTEM_RULE in sys_p
    assert "Do not change the verdict" in sys_p
    assert "Do not add facts not in JSON" in sys_p
    user = user_prompt(result)
    payload = json.loads(user)
    assert payload["verdict"] == "CAUTION"
    assert payload["case_id"] == result.case_id
    assert payload["layer_a"]["mint_authority_revoked"] == "UNKNOWN"


def test_guard_does_not_rewrite_caution_unknown_to_pass():
    guarded = guard_explanation(
        "Authorities look fine. Verdict is PASS_FILTER. This is a BUY.",
        Verdict.CAUTION,
    )
    assert "PASS_FILTER" not in guarded
    assert "CAUTION" in guarded
    assert "BUY" not in guarded
    assert len(guarded.split()) <= 120
    assert claimed_verdicts(guarded) == {"CAUTION"}


def test_guard_does_not_rewrite_avoid_to_pass():
    guarded = guard_explanation(
        "Mint is active but I would still PASS_FILTER.",
        Verdict.AVOID,
    )
    assert "PASS_FILTER" not in guarded
    assert "AVOID" in guarded
    assert len(guarded.split()) <= 120


def test_guard_restates_verdict_and_caps_words():
    long_prose = ("word " * 200) + "CAUTION"
    guarded = guard_explanation(long_prose, Verdict.CAUTION)
    assert len(guarded.split()) <= 120
    assert "CAUTION" in guarded
    assert "BUY" not in guarded


def test_explain_scan_applies_guard_and_does_not_mutate_result(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPLAIN_ENABLED", "1")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")
    result = _caution_unknown(token="GuardLive")
    write_case(result, tmp_path)
    before = read_scan(result.case_id, tmp_path)

    outcome = explain_scan(
        result,
        http=_llm_client("Clean token. PASS_FILTER. Safe to BUY."),
    )
    assert outcome.skipped is False
    assert outcome.verdict is Verdict.CAUTION
    assert outcome.verdict is before.verdict
    assert outcome.prose is not None
    assert "CAUTION" in outcome.prose
    assert "PASS_FILTER" not in outcome.prose
    assert "BUY" not in outcome.prose

    after = read_scan(result.case_id, tmp_path)
    assert after.verdict is Verdict.CAUTION
    assert after.model_dump() == before.model_dump()


def test_explain_case_reads_scan_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPLAIN_ENABLED", "1")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")
    result = _caution_unknown(token="CaseJson")
    write_case(result, tmp_path)
    outcome = explain_case(
        result.case_id,
        tmp_path,
        http=_llm_client("Layer A mint is UNKNOWN so verdict is CAUTION."),
    )
    assert isinstance(outcome, ExplainOutcome)
    assert outcome.skipped is False
    assert outcome.verdict is Verdict.CAUTION
    assert "CAUTION" in (outcome.prose or "")


def test_explain_request_failure_skips(monkeypatch):
    monkeypatch.setenv("EXPLAIN_ENABLED", "1")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")
    outcome = explain_scan(
        _caution_unknown(token="FailHttp"),
        http=_llm_client("", status=503),
    )
    assert outcome.skipped is True
    assert outcome.prose is None
    assert "failed" in (outcome.reason or "")
    assert outcome.verdict is Verdict.CAUTION


def test_ff_explain_skipped_prints_skipped_not_prose(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EXPLAIN_ENABLED", "0")
    result = _caution_unknown(token="CliSkip1")
    write_case(result, tmp_path)
    invoked = runner.invoke(app, ["explain", result.case_id])
    assert invoked.exit_code == 0, invoked.output
    assert "skipped" in invoked.output.lower()
    assert "EXPLAIN_ENABLED" in invoked.output
    assert "BUY" not in invoked.output
    assert "PASS_FILTER" not in invoked.output
    assert "looks clean" not in invoked.output.lower()


def test_ff_explain_missing_case_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    invoked = runner.invoke(app, ["explain", "20260916-solana-missing"])
    assert invoked.exit_code == 1
    assert "not found" in invoked.output.lower()
    assert "BUY" not in invoked.output


def test_ff_explain_enabled_guarded_output(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EXPLAIN_ENABLED", "1")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")
    result = _caution_unknown(token="CliGuard")
    write_case(result, tmp_path)

    def fake_explain_scan(scan, *, http=None):
        return explain_scan(
            scan,
            http=_llm_client("UNKNOWN mint is actually fine. PASS_FILTER. BUY."),
        )

    monkeypatch.setattr("filter_floor.cli.explain_scan", fake_explain_scan)
    invoked = runner.invoke(app, ["explain", result.case_id])
    assert invoked.exit_code == 0, invoked.output
    assert "CAUTION" in invoked.output
    assert "PASS_FILTER" not in invoked.output
    assert "BUY" not in invoked.output
    stored = read_scan(result.case_id, tmp_path)
    assert stored.verdict is Verdict.CAUTION
