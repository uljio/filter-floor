"""Case file writes: data/scans/{case_id}.json and data/cases/{case_id}.md."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from filter_floor.models import Chain, ScanResult


def make_case_id(scanned_at: datetime, chain: Chain, token: str) -> str:
    day = scanned_at.strftime("%Y%m%d")
    token_key = token.strip()[:8]
    return f"{day}-{chain.value}-{token_key}"


def scans_dir(data_dir: Path) -> Path:
    return data_dir / "scans"


def cases_dir(data_dir: Path) -> Path:
    return data_dir / "cases"


def write_case(result: ScanResult, data_dir: Path) -> tuple[Path, Path]:
    json_path = scans_dir(data_dir) / f"{result.case_id}.json"
    md_path = cases_dir(data_dir) / f"{result.case_id}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_case_md(result), encoding="utf-8")
    return json_path, md_path


def read_scan(case_id: str, data_dir: Path) -> ScanResult:
    path = scans_dir(data_dir) / f"{case_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"scan not found: {path}")
    return ScanResult.model_validate_json(path.read_text(encoding="utf-8"))


def list_case_summaries(
    data_dir: Path,
    *,
    days: int = 1,
    now: datetime | None = None,
) -> list[dict]:
    """Recent scans as {case_id, chain, token, score, verdict, scanned_at}."""
    if days < 1:
        raise ValueError("days must be >= 1")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    start = moment - timedelta(days=days)
    rows: list[dict] = []
    for result in iter_scans(data_dir):
        scanned_at = result.scanned_at
        if scanned_at.tzinfo is None:
            scanned_at = scanned_at.replace(tzinfo=timezone.utc)
        else:
            scanned_at = scanned_at.astimezone(timezone.utc)
        if scanned_at < start or scanned_at > moment:
            continue
        rows.append(
            {
                "case_id": result.case_id,
                "chain": result.chain.value,
                "token": result.token,
                "score": result.score_0_100,
                "verdict": result.verdict.value,
                "scanned_at": scanned_at.isoformat(),
            }
        )
    rows.sort(key=lambda row: row["scanned_at"], reverse=True)
    return rows


def read_case_md(case_id: str, data_dir: Path) -> str:
    path = cases_dir(data_dir) / f"{case_id}.md"
    if not path.is_file():
        raise FileNotFoundError(f"case not found: {path}")
    return path.read_text(encoding="utf-8")


def iter_scans(data_dir: Path) -> Iterator[ScanResult]:
    folder = scans_dir(data_dir)
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.json")):
        try:
            yield ScanResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def render_case_md(result: ScanResult) -> str:
    vetoes = _bullet_list(result.veto_reasons)
    cautions = _bullet_list(result.caution_reasons)
    sources = ", ".join(result.sources) if result.sources else "(none)"
    return (
        f"# Case {result.case_id}\n"
        "\n"
        f"- chain: `{result.chain.value}`\n"
        f"- token: `{result.token}`\n"
        f"- deployer: `{result.deployer or 'unknown'}`\n"
        f"- scanned_at: {result.scanned_at.isoformat()}\n"
        f"- verdict: **{result.verdict.value}**\n"
        f"- score_0_100: {result.score_0_100}\n"
        f"- sources: {sources}\n"
        "\n"
        "## Veto reasons\n"
        f"{vetoes}\n"
        "\n"
        "## Caution reasons\n"
        f"{cautions}\n"
        "\n"
        "## Layer A\n"
        f"- mint_authority_revoked: {result.layer_a.mint_authority_revoked.value}\n"
        f"- freeze_authority_revoked: {result.layer_a.freeze_authority_revoked.value}\n"
        f"- lp_locked_or_burned: {result.layer_a.lp_locked_or_burned.value}\n"
        f"- honeypot_or_unsellable: {result.layer_a.honeypot_or_unsellable.value}\n"
        f"- owner_or_upgrade_risk: {result.layer_a.owner_or_upgrade_risk.value}\n"
        f"- token2022_or_hook_risk: {result.layer_a.token2022_or_hook_risk.value}\n"
        "\n"
        "## Layer B\n"
        f"- bundle_detected: {result.layer_b.bundle_detected.value}\n"
        f"- bundled_supply_pct: {result.layer_b.bundled_supply_pct}\n"
        f"- top10_excluding_lp_pct: {result.layer_b.top10_excluding_lp_pct}\n"
        f"- funding_cluster_size: {result.layer_b.funding_cluster_size}\n"
        f"- same_funder_as_known_bad: {result.layer_b.same_funder_as_known_bad.value}\n"
        f"- early_consolidation: {result.layer_b.early_consolidation.value}\n"
        "\n"
        "## Deployer memory\n"
        f"- deployer: `{result.memory.deployer}`\n"
        f"- prior_token_count: {result.memory.prior_token_count}\n"
        f"- death_rate: {result.memory.death_rate}\n"
        "\n"
        "This is a pre-buy filter verdict, not financial advice. "
        "Never treat PASS_FILTER as a recommendation to trade.\n"
    )


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)
