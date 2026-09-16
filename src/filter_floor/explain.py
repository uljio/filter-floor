"""Optional LLM explainer: ScanResult JSON in, prose out. Never changes the verdict."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from filter_floor.config import load_explain
from filter_floor.models import ScanResult, Verdict
from filter_floor.storage.cases import read_scan

SYSTEM_RULE = "Do not change the verdict. Do not add facts not in JSON."
DEFAULT_MODEL = "grok-4-1-fast"
DEFAULT_MAX_WORDS = 120
DEFAULT_API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_TIMEOUT_S = 30.0

_VERDICT_TOKEN = re.compile(r"\b(AVOID|CAUTION|PASS_FILTER)\b")
_BUY = re.compile(r"\bBUY\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExplainOutcome:
    skipped: bool
    reason: str | None
    prose: str | None
    verdict: Verdict


def explain_enabled() -> bool:
    return os.environ.get("EXPLAIN_ENABLED", "0").strip() == "1"


def xai_api_key() -> str | None:
    key = os.environ.get("XAI_API_KEY", "").strip()
    return key or None


def skip_reason() -> str | None:
    if not explain_enabled():
        return "skipped (EXPLAIN_ENABLED=0)"
    if not xai_api_key():
        return "skipped (XAI_API_KEY missing)"
    return None


def configured_model() -> str:
    env = os.environ.get("EXPLAIN_MODEL", "").strip()
    if env:
        return env
    return str(load_explain().get("model") or DEFAULT_MODEL)


def configured_max_words() -> int:
    try:
        value = int(load_explain().get("max_words", DEFAULT_MAX_WORDS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORDS
    return value if value > 0 else DEFAULT_MAX_WORDS


def configured_api_url() -> str:
    return str(load_explain().get("api_url") or DEFAULT_API_URL)


def system_prompt() -> str:
    max_words = configured_max_words()
    return (
        "You explain a Filter Floor scan. "
        f"{SYSTEM_RULE} "
        f"Write at most {max_words} words. "
        "Restate the verdict exactly as given in the JSON. "
        "Never say BUY. Never invent on-chain facts. "
        "UNKNOWN checks stay unknown; they are not PASS."
    )


def user_prompt(result: ScanResult) -> str:
    return result.model_dump_json()


def claimed_verdicts(text: str) -> set[str]:
    return set(_VERDICT_TOKEN.findall(text))


def _word_count(text: str) -> int:
    return len(text.split())


def truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def guard_explanation(
    prose: str,
    verdict: Verdict,
    *,
    max_words: int | None = None,
) -> str:
    """Keep the original verdict. Strip BUY. Cap words. Restate the verdict.

    If the model names a different verdict (for example CAUTION → PASS_FILTER),
    discard that rewrite. UNKNOWN facts in the scan JSON are never upgraded.
    """
    limit = DEFAULT_MAX_WORDS if max_words is None else max_words
    text = _BUY.sub(" ", prose or "")
    text = re.sub(r"\s+", " ", text).strip()

    others = claimed_verdicts(text) - {verdict.value}
    if others:
        text = (
            f"The scan verdict is {verdict.value}. "
            "The explainer must not change it or add facts that are not in the scan JSON."
        )

    restatement = f"Verdict remains {verdict.value}."
    if verdict.value not in text:
        text = f"{text} {restatement}".strip() if text else restatement

    if _word_count(text) > limit:
        text = truncate_words(text, limit)
        if verdict.value not in text:
            restatement_n = _word_count(restatement)
            budget = max(0, limit - restatement_n)
            head = truncate_words(text, budget)
            text = f"{head} {restatement}".strip() if head else restatement
            text = truncate_words(text, limit)

    text = _BUY.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def explain_scan(
    result: ScanResult,
    *,
    http: httpx.Client | None = None,
) -> ExplainOutcome:
    """Explain a stored ScanResult. Never mutates the result or its files."""
    reason = skip_reason()
    if reason:
        return ExplainOutcome(
            skipped=True, reason=reason, prose=None, verdict=result.verdict
        )

    payload = {
        "model": configured_model(),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(result)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {xai_api_key()}",
        "Content-Type": "application/json",
    }

    owns = http is None
    client = http or httpx.Client(timeout=DEFAULT_TIMEOUT_S)
    try:
        response = client.post(configured_api_url(), json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return ExplainOutcome(
            skipped=True,
            reason="skipped (explainer request failed)",
            prose=None,
            verdict=result.verdict,
        )
    finally:
        if owns:
            client.close()

    prose = guard_explanation(
        str(raw or ""), result.verdict, max_words=configured_max_words()
    )
    return ExplainOutcome(
        skipped=False, reason=None, prose=prose, verdict=result.verdict
    )


def explain_case(
    case_id: str,
    data_dir: Path,
    *,
    http: httpx.Client | None = None,
) -> ExplainOutcome:
    result = read_scan(case_id, data_dir)
    return explain_scan(result, http=http)
