"""Data contracts (product spec §3). Verdict has no BUY."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Chain(str, Enum):
    solana = "solana"
    base = "base"
    robinhood = "robinhood"


class Verdict(str, Enum):
    AVOID = "AVOID"
    CAUTION = "CAUTION"
    PASS_FILTER = "PASS_FILTER"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NA = "NA"


class LayerA(BaseModel):
    mint_authority_revoked: CheckStatus
    freeze_authority_revoked: CheckStatus
    lp_locked_or_burned: CheckStatus
    honeypot_or_unsellable: CheckStatus
    owner_or_upgrade_risk: CheckStatus
    token2022_or_hook_risk: CheckStatus
    details: dict = Field(default_factory=dict)


class LayerB(BaseModel):
    bundle_detected: CheckStatus
    bundled_supply_pct: Optional[float] = None
    top10_holder_pct: Optional[float] = None
    top10_excluding_lp_pct: Optional[float] = None
    funding_cluster_size: Optional[int] = None
    same_funder_as_known_bad: CheckStatus = CheckStatus.UNKNOWN
    early_consolidation: CheckStatus = CheckStatus.UNKNOWN
    details: dict = Field(default_factory=dict)


class DeployerMemory(BaseModel):
    deployer: str
    prior_token_count: int = 0
    death_rate: Optional[float] = None
    avg_lifespan_hours: Optional[float] = None
    cluster_id: Optional[str] = None
    cluster_size: int = 0
    verdict_hint: Optional[str] = None
    details: dict = Field(default_factory=dict)


class ScanResult(BaseModel):
    case_id: str
    chain: Chain
    token: str
    deployer: Optional[str] = None
    launched_at: Optional[datetime] = None
    scanned_at: datetime
    layer_a: LayerA
    layer_b: LayerB
    memory: DeployerMemory
    score_0_100: int  # risk score; high = more dangerous
    verdict: Verdict
    veto_reasons: list[str] = Field(default_factory=list)
    caution_reasons: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class OutcomeLabel(str, Enum):
    rugged = "rugged"  # LP gone, freeze used, -90% fast, unsellable
    bled = "bled"  # large drawdown, no single rug tx
    survived = "survived"  # still tradeable, not dead
    unknown = "unknown"


class Outcome(BaseModel):
    case_id: str
    at_1h: Optional[OutcomeLabel] = None
    at_6h: Optional[OutcomeLabel] = None
    at_24h: Optional[OutcomeLabel] = None
    notes: str = ""
