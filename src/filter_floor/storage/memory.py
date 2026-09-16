"""Local deployer/cluster memory JSON. Never invent death_rate=0."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from filter_floor.graph.clusters import cluster_id_for_funder, cluster_matches_funder
from filter_floor.graph.types import addr_key
from filter_floor.models import Chain, DeployerMemory, Verdict

MEMORY_REL = Path("memory")


def deployers_dir(data_dir: Path) -> Path:
    return data_dir / MEMORY_REL / "deployers"


def clusters_dir(data_dir: Path) -> Path:
    return data_dir / MEMORY_REL / "clusters"


def deployer_path(data_dir: Path, deployer: str) -> Path:
    return deployers_dir(data_dir) / f"{_filename(deployer)}.json"


def cluster_path(data_dir: Path, cluster_id: str) -> Path:
    return clusters_dir(data_dir) / f"{_filename(cluster_id)}.json"


def _filename(value: str) -> str:
    text = value.strip() or "unknown"
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)[:120]


def death_rate_from_tokens(tokens: list[dict[str, Any]]) -> float | None:
    """Only labeled tokens (dead true/false) count. No labels → None, never 0."""
    labeled = [row for row in tokens if row.get("dead") is True or row.get("dead") is False]
    if not labeled:
        return None
    deaths = sum(1 for row in labeled if row.get("dead") is True)
    return deaths / len(labeled)


class MemoryStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def load_deployer_record(self, deployer: str) -> dict[str, Any] | None:
        path = deployer_path(self.data_dir, deployer)
        return _read_json(path)

    def load_cluster_record(self, cluster_id: str) -> dict[str, Any] | None:
        path = cluster_path(self.data_dir, cluster_id)
        return _read_json(path)

    def find_cluster_by_funder(self, funder: str) -> dict[str, Any] | None:
        direct = self.load_cluster_record(cluster_id_for_funder(funder))
        if direct is not None:
            return direct
        folder = clusters_dir(self.data_dir)
        if not folder.is_dir():
            return None
        for path in folder.glob("*.json"):
            record = _read_json(path)
            if record and cluster_matches_funder(record, funder):
                return record
        return None

    def to_deployer_memory(
        self,
        deployer: str,
        *,
        current_token: str | None = None,
        cluster_id: str | None = None,
        cluster_size: int = 0,
        extra_details: dict[str, Any] | None = None,
    ) -> DeployerMemory:
        record = self.load_deployer_record(deployer)
        if record is None:
            details = {"death_rate_computed": False, "memory_new": True}
            if extra_details:
                details.update(extra_details)
            return DeployerMemory(
                deployer=deployer,
                prior_token_count=0,
                death_rate=None,
                cluster_id=cluster_id,
                cluster_size=cluster_size,
                details=details,
            )
        tokens = list(record.get("tokens") or [])
        prior = [
            row
            for row in tokens
            if not (
                current_token
                and isinstance(row, dict)
                and str(row.get("token") or "") == current_token
            )
        ]
        stored_rate = record.get("death_rate")
        computed = death_rate_from_tokens(tokens)
        # Labels win when present. Otherwise keep stored rate (may be None).
        # Never coerce missing labels to 0.
        rate = computed if computed is not None else stored_rate
        if rate is not None:
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                rate = None
        details = dict(record.get("details") or {})
        details["death_rate_computed"] = computed is not None
        if rate is None:
            details["death_rate_null"] = True
            details["TODO(verify)"] = (
                "death_rate is null (not computed); null is not 0"
            )
        if extra_details:
            details.update(extra_details)
        return DeployerMemory(
            deployer=str(record.get("deployer") or deployer),
            prior_token_count=len(prior),
            death_rate=rate,
            avg_lifespan_hours=record.get("avg_lifespan_hours"),
            cluster_id=cluster_id or record.get("cluster_id"),
            cluster_size=cluster_size or int(record.get("cluster_size") or 0),
            verdict_hint=record.get("verdict_hint"),
            details=details,
        )

    def upsert_after_scan(
        self,
        *,
        deployer: str,
        token: str,
        chain: Chain,
        case_id: str,
        scanned_at: datetime,
        verdict: Verdict,
        cluster_id: str | None,
        cluster_size: int,
        funder: str | None,
        cluster_wallets: list[str],
    ) -> DeployerMemory:
        memory = self.to_deployer_memory(
            deployer,
            current_token=token,
            cluster_id=cluster_id,
            cluster_size=cluster_size,
        )
        self._write_deployer(
            deployer=deployer,
            token=token,
            chain=chain,
            case_id=case_id,
            scanned_at=scanned_at,
            verdict=verdict,
            cluster_id=cluster_id or memory.cluster_id,
            cluster_size=cluster_size or memory.cluster_size,
        )
        if cluster_id and funder:
            self._write_cluster(
                cluster_id=cluster_id,
                funder=funder,
                wallets=cluster_wallets,
                token=token,
                chain=chain,
                case_id=case_id,
                scanned_at=scanned_at,
                verdict=verdict,
            )
        return self.to_deployer_memory(
            deployer,
            current_token=token,
            cluster_id=cluster_id,
            cluster_size=cluster_size,
        )

    def apply_death_label(
        self,
        *,
        case_id: str,
        token: str,
        deployer: str | None,
        dead: bool | None,
        reason: str | None = None,
    ) -> None:
        """Set dead True/False from outcome evidence. unknown/None does not change death_rate."""
        if dead is None:
            return
        paths = self._records_for_token(case_id=case_id, token=token, deployer=deployer)
        if not paths:
            return
        for path in paths:
            record = _read_json(path)
            if not record:
                continue
            tokens = list(record.get("tokens") or [])
            if not _stamp_dead(tokens, case_id=case_id, token=token, dead=dead, reason=reason):
                continue
            computed = death_rate_from_tokens(tokens)
            stored = record.get("death_rate")
            death_rate = computed if computed is not None else stored
            if death_rate is not None:
                try:
                    death_rate = float(death_rate)
                except (TypeError, ValueError):
                    death_rate = None
            record["tokens"] = tokens
            record["death_rate"] = death_rate
            details = dict(record.get("details") or {})
            details["death_rate_computed"] = computed is not None
            details["death_rate_null"] = death_rate is None
            if reason:
                details["last_death_reason"] = reason
            record["details"] = details
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def _records_for_token(
        self,
        *,
        case_id: str,
        token: str,
        deployer: str | None,
    ) -> list[Path]:
        found: list[Path] = []
        seen: set[Path] = set()
        if deployer:
            path = deployer_path(self.data_dir, deployer)
            if path.is_file():
                found.append(path)
                seen.add(path.resolve())
        for folder in (deployers_dir(self.data_dir), clusters_dir(self.data_dir)):
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                record = _read_json(path)
                if not record:
                    continue
                if _row_matches(record.get("tokens") or [], case_id=case_id, token=token):
                    found.append(path)
                    seen.add(resolved)
        return found

    def reset(self) -> int:
        """Delete deployer and cluster JSON. Caller must require --i-understand."""
        removed = 0
        for folder in (deployers_dir(self.data_dir), clusters_dir(self.data_dir)):
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                path.unlink()
                removed += 1
        return removed

    def _write_deployer(
        self,
        *,
        deployer: str,
        token: str,
        chain: Chain,
        case_id: str,
        scanned_at: datetime,
        verdict: Verdict,
        cluster_id: str | None,
        cluster_size: int,
    ) -> None:
        path = deployer_path(self.data_dir, deployer)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = _read_json(path) or {
            "deployer": deployer,
            "tokens": [],
            "death_rate": None,
            "cluster_id": None,
            "cluster_size": 0,
        }
        tokens = list(record.get("tokens") or [])
        entry = {
            "token": token,
            "chain": chain.value,
            "case_id": case_id,
            "scanned_at": scanned_at.isoformat(),
            "verdict": verdict.value,
            "dead": None,
        }
        replaced = False
        for i, row in enumerate(tokens):
            if isinstance(row, dict) and str(row.get("token") or "") == token:
                merged = dict(row)
                merged.update(entry)
                if row.get("dead") is True or row.get("dead") is False:
                    merged["dead"] = row.get("dead")
                tokens[i] = merged
                replaced = True
                break
        if not replaced:
            tokens.append(entry)
        computed = death_rate_from_tokens(tokens)
        stored = record.get("death_rate")
        death_rate = computed if computed is not None else stored
        if death_rate is not None:
            try:
                death_rate = float(death_rate)
            except (TypeError, ValueError):
                death_rate = None
        prior = [row for row in tokens if str(row.get("token") or "") != token]
        payload = {
            "deployer": deployer,
            "prior_token_count": len(prior),
            "death_rate": death_rate,
            "avg_lifespan_hours": record.get("avg_lifespan_hours"),
            "cluster_id": cluster_id or record.get("cluster_id"),
            "cluster_size": cluster_size or int(record.get("cluster_size") or 0),
            "verdict_hint": record.get("verdict_hint"),
            "tokens": tokens,
            "details": {
                "death_rate_computed": computed is not None,
                "death_rate_null": death_rate is None,
            },
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _write_cluster(
        self,
        *,
        cluster_id: str,
        funder: str,
        wallets: list[str],
        token: str,
        chain: Chain,
        case_id: str,
        scanned_at: datetime,
        verdict: Verdict,
    ) -> None:
        path = cluster_path(self.data_dir, cluster_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = _read_json(path) or {
            "cluster_id": cluster_id,
            "funder": funder,
            "wallets": [],
            "tokens": [],
            "death_rate": None,
        }
        existing_wallets = [
            str(w) for w in (record.get("wallets") or []) if isinstance(w, str)
        ]
        merged_wallets: list[str] = []
        seen: set[str] = set()
        for wallet in existing_wallets + list(wallets) + [funder]:
            key = addr_key(wallet)
            if not key or key in seen:
                continue
            seen.add(key)
            merged_wallets.append(wallet)
        tokens = list(record.get("tokens") or [])
        entry = {
            "token": token,
            "chain": chain.value,
            "case_id": case_id,
            "scanned_at": scanned_at.isoformat(),
            "verdict": verdict.value,
            "dead": None,
        }
        if not any(isinstance(row, dict) and row.get("token") == token for row in tokens):
            tokens.append(entry)
        computed = death_rate_from_tokens(tokens)
        stored = record.get("death_rate")
        death_rate = computed if computed is not None else stored
        if death_rate is not None:
            try:
                death_rate = float(death_rate)
            except (TypeError, ValueError):
                death_rate = None
        payload = {
            "cluster_id": cluster_id,
            "funder": record.get("funder") or funder,
            "wallets": merged_wallets,
            "death_rate": death_rate,
            "tokens": tokens,
            "details": {
                "death_rate_computed": computed is not None,
                "death_rate_null": death_rate is None,
            },
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _row_matches(tokens: list[Any], *, case_id: str, token: str) -> bool:
    for row in tokens:
        if not isinstance(row, dict):
            continue
        if case_id and str(row.get("case_id") or "") == case_id:
            return True
        if token and str(row.get("token") or "") == token:
            return True
    return False


def _stamp_dead(
    tokens: list[Any],
    *,
    case_id: str,
    token: str,
    dead: bool,
    reason: str | None,
) -> bool:
    """Once dead, stay dead. unknown is never written as False here."""
    changed = False
    for row in tokens:
        if not isinstance(row, dict):
            continue
        matches = (case_id and str(row.get("case_id") or "") == case_id) or (
            token and str(row.get("token") or "") == token
        )
        if not matches:
            continue
        existing = row.get("dead")
        if existing is True:
            if reason and not row.get("death_reason"):
                row["death_reason"] = reason
            changed = True
            continue
        row["dead"] = dead
        if reason:
            row["death_reason"] = reason
        changed = True
    return changed


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data
