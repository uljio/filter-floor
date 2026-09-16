"""Funnel counts: scanned → vetoed → caution → pass → later rugged."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filter_floor.models import OutcomeLabel, Verdict
from filter_floor.storage.cases import iter_scans
from filter_floor.storage.outcomes import read_outcome_record

FUNNEL_FILENAME = "funnel.jsonl"


@dataclass(frozen=True)
class FunnelCounts:
    scanned: int
    vetoed: int
    caution: int
    pass_filter: int
    later_rugged: int
    days: int

    def as_json(self, ts: datetime) -> dict:
        return {
            "ts": _aware(ts).isoformat(),
            "days": self.days,
            "scanned": self.scanned,
            "vetoed": self.vetoed,
            "caution": self.caution,
            "pass": self.pass_filter,
            "later_rugged": self.later_rugged,
        }

    def one_line(self) -> str:
        return (
            f"scanned={self.scanned} vetoed={self.vetoed} caution={self.caution} "
            f"pass={self.pass_filter} later_rugged={self.later_rugged}"
        )


def funnel_path(data_dir: Path) -> Path:
    return data_dir / FUNNEL_FILENAME


def compute_funnel(
    data_dir: Path,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> FunnelCounts:
    if days < 1:
        raise ValueError("days must be >= 1")
    moment = _aware(now or datetime.now(timezone.utc))
    start = moment - timedelta(days=days)
    scanned = vetoed = caution = pass_filter = later_rugged = 0
    for result in iter_scans(data_dir):
        scanned_at = _aware(result.scanned_at)
        if scanned_at < start or scanned_at > moment:
            continue
        scanned += 1
        if result.verdict is Verdict.AVOID:
            vetoed += 1
        elif result.verdict is Verdict.CAUTION:
            caution += 1
        elif result.verdict is Verdict.PASS_FILTER:
            pass_filter += 1
        if _is_later_rugged(result.case_id, data_dir):
            later_rugged += 1
    return FunnelCounts(
        scanned=scanned,
        vetoed=vetoed,
        caution=caution,
        pass_filter=pass_filter,
        later_rugged=later_rugged,
        days=days,
    )


def write_funnel_jsonl(
    counts: FunnelCounts,
    data_dir: Path,
    *,
    now: datetime | None = None,
) -> Path:
    path = funnel_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(counts.as_json(now or datetime.now(timezone.utc)))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return path


def compute_and_write(
    data_dir: Path,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> FunnelCounts:
    moment = now or datetime.now(timezone.utc)
    counts = compute_funnel(data_dir, days=days, now=moment)
    write_funnel_jsonl(counts, data_dir, now=moment)
    return counts


def _is_later_rugged(case_id: str, data_dir: Path) -> bool:
    record = read_outcome_record(case_id, data_dir)
    if record is None:
        return False
    return any(
        label is OutcomeLabel.rugged
        for label in (record.at_1h, record.at_6h, record.at_24h)
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
