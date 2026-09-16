"""Outcome files under data/outcomes/{case_id}.json. Due times are recorded with the case."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel, Field

from filter_floor.models import Chain, Outcome, OutcomeLabel, ScanResult
from filter_floor.storage.cases import cases_dir

HORIZONS = ("1h", "6h", "24h")
HORIZON_HOURS = {"1h": 1, "6h": 6, "24h": 24}


class OutcomeRecord(BaseModel):
    """Stored schedule + labels. Spec Outcome is the at_* subset."""

    case_id: str
    chain: Chain
    token: str
    deployer: Optional[str] = None
    scanned_at: datetime
    due_1h: datetime
    due_6h: datetime
    due_24h: datetime
    at_1h: Optional[OutcomeLabel] = None
    at_6h: Optional[OutcomeLabel] = None
    at_24h: Optional[OutcomeLabel] = None
    notes: str = ""
    first_price_usd: Optional[float] = None
    first_liquidity_usd: Optional[float] = None
    evidence: dict = Field(default_factory=dict)

    def to_outcome(self) -> Outcome:
        return Outcome(
            case_id=self.case_id,
            at_1h=self.at_1h,
            at_6h=self.at_6h,
            at_24h=self.at_24h,
            notes=self.notes,
        )

    def due_at(self, horizon: str) -> datetime:
        return getattr(self, f"due_{horizon}")

    def label_at(self, horizon: str) -> OutcomeLabel | None:
        return getattr(self, f"at_{horizon}")

    def set_label(self, horizon: str, label: OutcomeLabel) -> None:
        setattr(self, f"at_{horizon}", label)


def outcomes_dir(data_dir: Path) -> Path:
    return data_dir / "outcomes"


def outcome_path(data_dir: Path, case_id: str) -> Path:
    return outcomes_dir(data_dir) / f"{case_id}.json"


def due_times(scanned_at: datetime) -> dict[str, datetime]:
    scanned = _aware(scanned_at)
    return {
        horizon: scanned + timedelta(hours=hours)
        for horizon, hours in HORIZON_HOURS.items()
    }


def schedule_for_scan(result: ScanResult, data_dir: Path) -> OutcomeRecord:
    """Create (or keep) +1h/+6h/+24h due times for this case. Does not invent labels."""
    existing = read_outcome_record(result.case_id, data_dir)
    if existing is not None:
        sync_case_outcome_section(existing, data_dir)
        return existing
    dues = due_times(result.scanned_at)
    record = OutcomeRecord(
        case_id=result.case_id,
        chain=result.chain,
        token=result.token,
        deployer=result.deployer,
        scanned_at=result.scanned_at,
        due_1h=dues["1h"],
        due_6h=dues["6h"],
        due_24h=dues["24h"],
    )
    write_outcome_record(record, data_dir)
    sync_case_outcome_section(record, data_dir)
    return record


def read_outcome_record(case_id: str, data_dir: Path) -> OutcomeRecord | None:
    path = outcome_path(data_dir, case_id)
    if not path.is_file():
        return None
    return OutcomeRecord.model_validate_json(path.read_text(encoding="utf-8"))


def write_outcome_record(record: OutcomeRecord, data_dir: Path) -> Path:
    path = outcome_path(data_dir, record.case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def iter_outcome_records(data_dir: Path) -> Iterator[OutcomeRecord]:
    folder = outcomes_dir(data_dir)
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.json")):
        try:
            yield OutcomeRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def due_horizons(record: OutcomeRecord, now: datetime) -> list[str]:
    moment = _aware(now)
    due: list[str] = []
    for horizon in HORIZONS:
        if record.label_at(horizon) is not None:
            continue
        if _aware(record.due_at(horizon)) <= moment:
            due.append(horizon)
    return due


def render_outcome_section(record: OutcomeRecord) -> str:
    lines = ["## Outcome schedule"]
    for horizon in HORIZONS:
        label = record.label_at(horizon)
        label_text = label.value if label is not None else "(pending)"
        lines.append(
            f"- due_{horizon}: {_aware(record.due_at(horizon)).isoformat()} "
            f"label: {label_text}"
        )
    if record.notes:
        lines.append(f"- notes: {record.notes}")
    return "\n".join(lines) + "\n"


def sync_case_outcome_section(record: OutcomeRecord, data_dir: Path) -> None:
    path = cases_dir(data_dir) / f"{record.case_id}.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    section = render_outcome_section(record)
    marker = "## Outcome schedule"
    if marker in text:
        text = text[: text.index(marker)] + section
    else:
        text = text.rstrip() + "\n\n" + section
    path.write_text(text, encoding="utf-8")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
