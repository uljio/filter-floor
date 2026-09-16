"""Classify 1h/6h/24h outcomes from market evidence. Unknown when evidence is missing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from filter_floor.adapters.dexscreener import MarketSnapshot, fetch_market_snapshot
from filter_floor.config import load_outcomes
from filter_floor.models import Chain, Outcome, OutcomeLabel, ScanResult
from filter_floor.storage.cases import iter_scans
from filter_floor.storage.memory import MemoryStore
from filter_floor.storage.outcomes import (
    OutcomeRecord,
    due_horizons,
    read_outcome_record,
    schedule_for_scan,
    sync_case_outcome_section,
    write_outcome_record,
)

MarketFetch = Callable[[Chain, str], MarketSnapshot]


@dataclass
class LabelDecision:
    label: OutcomeLabel
    dead: bool | None
    notes: str
    evidence: dict = field(default_factory=dict)


@dataclass
class LabeledHorizon:
    case_id: str
    horizon: str
    label: OutcomeLabel
    dead: bool | None
    notes: str


def classify_snapshot(
    snapshot: MarketSnapshot,
    *,
    first_price_usd: float | None = None,
    cfg: dict | None = None,
) -> LabelDecision:
    """Map quotes to OutcomeLabel. Never invent survived/rugged without evidence."""
    rules = cfg or load_outcomes()
    dead_liq = float(rules["liquidity_dead_usd"])
    survived_min = float(rules["survived_min_liquidity_usd"])
    rugged_dd = float(rules["drawdown_rugged"])
    bled_dd = float(rules["drawdown_bled"])

    evidence: dict = {
        "source": snapshot.source,
        "price_usd": snapshot.price_usd,
        "liquidity_usd": snapshot.liquidity_usd,
        "tradeable": snapshot.tradeable,
        "creator_extracted": snapshot.creator_extracted,
        "freeze_used_after_launch": snapshot.freeze_used_after_launch,
        "mint_used_after_launch": snapshot.mint_used_after_launch,
    }
    if snapshot.error:
        evidence["error"] = snapshot.error

    if snapshot.freeze_used_after_launch is True or snapshot.mint_used_after_launch is True:
        reason = (
            "freeze_used_after_launch"
            if snapshot.freeze_used_after_launch
            else "mint_used_after_launch"
        )
        evidence["death_reason"] = reason
        return LabelDecision(
            OutcomeLabel.rugged,
            dead=True,
            notes=reason,
            evidence=evidence,
        )

    if snapshot.tradeable is False:
        evidence["death_reason"] = "untradeable"
        return LabelDecision(
            OutcomeLabel.rugged,
            dead=True,
            notes="untradeable",
            evidence=evidence,
        )

    if snapshot.liquidity_usd is not None and snapshot.liquidity_usd <= dead_liq:
        evidence["death_reason"] = "liquidity_gone"
        return LabelDecision(
            OutcomeLabel.rugged,
            dead=True,
            notes=f"liquidity_usd={snapshot.liquidity_usd} <= {dead_liq}",
            evidence=evidence,
        )

    if (
        snapshot.price_usd is not None
        and first_price_usd is not None
        and first_price_usd > 0
    ):
        drawdown = 1.0 - (snapshot.price_usd / first_price_usd)
        evidence["drawdown"] = drawdown
        evidence["first_price_usd"] = first_price_usd
        if drawdown >= rugged_dd:
            evidence["outcome_reason"] = "drawdown_rugged"
            dead = True if snapshot.creator_extracted is True else None
            if snapshot.creator_extracted is True:
                evidence["death_reason"] = "drawdown_and_creator_extracted"
            notes = f"drawdown={drawdown:.4f} (>= {rugged_dd})"
            if dead is None:
                notes += "; death_rate left null (creator_extracted unknown)"
            return LabelDecision(
                OutcomeLabel.rugged,
                dead=dead,
                notes=notes,
                evidence=evidence,
            )
        if drawdown >= bled_dd:
            evidence["outcome_reason"] = "drawdown_bled"
            return LabelDecision(
                OutcomeLabel.bled,
                dead=False,
                notes=f"drawdown={drawdown:.4f} in [{bled_dd}, {rugged_dd})",
                evidence=evidence,
            )

    if not snapshot.has_evidence:
        return LabelDecision(
            OutcomeLabel.unknown,
            dead=None,
            notes=snapshot.error or "no_price_or_liquidity",
            evidence=evidence,
        )

    tradeable = snapshot.tradeable
    if (
        tradeable is None
        and snapshot.liquidity_usd is not None
        and snapshot.price_usd is not None
    ):
        tradeable = snapshot.liquidity_usd > dead_liq

    if (
        tradeable is True
        and snapshot.liquidity_usd is not None
        and snapshot.liquidity_usd >= survived_min
        and snapshot.price_usd is not None
    ):
        evidence["outcome_reason"] = "still_tradeable"
        return LabelDecision(
            OutcomeLabel.survived,
            dead=False,
            notes=f"tradeable liquidity_usd={snapshot.liquidity_usd}",
            evidence=evidence,
        )

    return LabelDecision(
        OutcomeLabel.unknown,
        dead=None,
        notes="insufficient_evidence",
        evidence=evidence,
    )


def label_due(
    data_dir: Path,
    *,
    now: datetime | None = None,
    market_fetch: MarketFetch | None = None,
    native_for: Callable[[ScanResult], MarketSnapshot | None] | None = None,
) -> list[LabeledHorizon]:
    """Label due horizons. Missing quotes → unknown. Does not invent survived/rugged."""
    moment = now or datetime.now(timezone.utc)
    store = MemoryStore(data_dir)
    labeled: list[LabeledHorizon] = []

    for result in iter_scans(data_dir):
        record = read_outcome_record(result.case_id, data_dir) or schedule_for_scan(
            result, data_dir
        )
        pending = due_horizons(record, moment)
        if not pending:
            continue
        snapshot = _snapshot_for(result, market_fetch, native_for)
        for horizon in pending:
            decision = classify_snapshot(
                snapshot, first_price_usd=record.first_price_usd
            )
            _apply_decision(record, horizon, decision, snapshot)
            if _should_update_memory(decision, horizon):
                store.apply_death_label(
                    case_id=result.case_id,
                    token=result.token,
                    deployer=result.deployer,
                    dead=decision.dead,
                    reason=str(decision.evidence.get("death_reason") or decision.notes),
                )
            labeled.append(
                LabeledHorizon(
                    case_id=result.case_id,
                    horizon=horizon,
                    label=decision.label,
                    dead=decision.dead,
                    notes=decision.notes,
                )
            )
        write_outcome_record(record, data_dir)
        sync_case_outcome_section(record, data_dir)
    return labeled


def outcome_for_case(case_id: str, data_dir: Path) -> Outcome | None:
    record = read_outcome_record(case_id, data_dir)
    if record is None:
        return None
    return record.to_outcome()


def _snapshot_for(
    result: ScanResult,
    market_fetch: MarketFetch | None,
    native_for: Callable[[ScanResult], MarketSnapshot | None] | None,
) -> MarketSnapshot:
    if market_fetch is not None:
        return market_fetch(result.chain, result.token)
    native = native_for(result) if native_for else None
    return fetch_market_snapshot(result.chain, result.token, native=native)


def _apply_decision(
    record: OutcomeRecord,
    horizon: str,
    decision: LabelDecision,
    snapshot: MarketSnapshot,
) -> None:
    record.set_label(horizon, decision.label)
    record.evidence[horizon] = decision.evidence
    note = f"{horizon}={decision.label.value} ({decision.notes})"
    record.notes = f"{record.notes}; {note}".strip("; ") if record.notes else note
    if record.first_price_usd is None and snapshot.price_usd is not None:
        record.first_price_usd = snapshot.price_usd
    if record.first_liquidity_usd is None and snapshot.liquidity_usd is not None:
        record.first_liquidity_usd = snapshot.liquidity_usd


def _should_update_memory(decision: LabelDecision, horizon: str) -> bool:
    if decision.dead is True:
        return True
    return decision.dead is False and horizon == "24h"
