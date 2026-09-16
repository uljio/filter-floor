# Token Filter Desk — Cursor Build Plan

**Product name (working):** Filter Floor  
**Goal:** A local, deterministic, multi-chain *pre-buy filter* for new launchpad tokens. It blocks mechanical rugs and serial-operator patterns. It does **not** pick winners and does **not** execute trades.

**Give this file to Cursor as the project spec.** Implement in this order. Do not invent a trading bot.

---

## 0. Non-negotiable product rules

1. Verdicts are only: `AVOID` | `CAUTION` | `PASS_FILTER`. Never `BUY`, never a size, never a target price.
2. Layer A failures are **hard vetoes**. A model/LLM cannot override them.
3. Unknown / RPC failure / incomplete simulation = `CAUTION` or `AVOID`, never `PASS_FILTER`.
4. No private keys, no seed phrases, no exchange write APIs, no auto-snipe in this repo.
5. LLMs (Grok Bot included) may *explain* a verdict. They may not *compute* Layer A/B facts.
6. Accuracy is measured by labeled outcomes (`rugged` / `bled` / `survived` at 1h/6h/24h), not by a pretty score.
7. Default posture is fail-closed and local-first. Paid RPCs are optional config.

If a future sniper is wanted, it is a **separate repo** with human approval. Do not add it here.

---

## 1. What we are building (and what we are not)

### Building
- CLI + small local API + optional dashboard
- Live listeners for new launches on:
  - Solana / Pump.fun
  - Robinhood Chain / Pons V2 (Arbitrum Orbit L2 — confirm factory address in config)
  - Base / Clanker + Uniswap V3 `PoolCreated`
- Six-check contract snapshot (AEGIS-class) **plus**:
  - Token-2022 / dangerous selectors
  - Simulated sell from a non-privileged wallet (EVM)
  - Funding-graph / bundle / cluster heuristics
  - Deployer memory (prior tokens + death rate + shared funder)
- Append-only case files and an outcome labeler
- Funnel metrics: scanned → vetoed → caution → pass → later rugged

### Not building
- Sentiment / Twitter alpha
- Copy-trading
- Position sizing
- Grok-as-oracle scoring
- Six chat agents that re-derive on-chain facts in prose

Grok Bot’s job later: `git pull`, run CLI, append a short note. Cursor builds the factory.

---

## 2. Suggested repo layout

```
filter-floor/
  README.md
  CURSOR_PLAN_token_filter_desk.md   # this file
  pyproject.toml                     # or package.json if TS; prefer Python 3.12 + Node only if needed for WS
  .env.example
  config/
    chains.yaml
    scoring.yaml
    vetoes.yaml
  src/filter_floor/
    cli.py
    api.py
    models.py                        # pydantic
    scoring.py
    vetoes.py
    storage/
      cases.py
      memory.py
      outcomes.py
      funnel.py
    scanners/
      base.py                        # interface
      solana.py
      evm.py
      pumpfun.py
      pons.py
      clanker.py
    graph/
      bundles.py
      funding.py
      clusters.py
    listeners/
      solana_ws.py
      evm_ws.py
    adapters/
      rugcheck.py                    # optional
      goplus.py                      # optional
      rpc.py
    explain.py                       # optional LLM wrapper; facts in, prose out
  tests/
    fixtures/
    test_vetoes.py
    test_scoring.py
    test_graph.py
    test_pipeline.py
  data/                              # gitignored live data
    cases/
    scans/
    memory/deployers/
    memory/clusters/
    outcomes/
    verdicts/
    funnel.jsonl
  scripts/
    scan_one.py
    watch.py
    label_due.py
    replay_fixtures.py
```

Language recommendation: **Python 3.12** for scanners, graph, storage, tests. Use `httpx`, `websockets`, `pydantic`, `typer`. Use `solders` / `solana` and `web3.py`. Keep the dashboard optional (FastAPI + a single HTML page). Do not start with a React SPA.

---

## 3. Data contracts (implement these types first)

```python
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

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
    score_0_100: int                    # risk score; high = more dangerous
    verdict: Verdict
    veto_reasons: list[str] = Field(default_factory=list)
    caution_reasons: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)

class OutcomeLabel(str, Enum):
    rugged = "rugged"       # LP gone, freeze used, -90% fast, unsellable
    bled = "bled"           # large drawdown, no single rug tx
    survived = "survived"   # still tradeable, not dead
    unknown = "unknown"

class Outcome(BaseModel):
    case_id: str
    at_1h: Optional[OutcomeLabel] = None
    at_6h: Optional[OutcomeLabel] = None
    at_24h: Optional[OutcomeLabel] = None
    notes: str = ""
```

Case file path: `data/cases/{case_id}.md` (human) + `data/scans/{case_id}.json` (machine).  
`case_id` format: `{YYYYMMDD}-{chain}-{token[:8]}`.

---

## 4. Scoring and vetoes

Put numbers in `config/vetoes.yaml` and `config/scoring.yaml` so they can be tuned without code changes.

### Hard vetoes → always AVOID
- Mint authority active (Solana) or owner can mint (EVM) and not clearly renounced
- Freeze authority active
- Simulated sell fails for a fresh non-privileged wallet
- LP tokens in deployer wallet / unlocked and pullable
- Token-2022 permanent delegate / transfer hook / fee extension that can trap transfers (flag + AVOID until classified)
- Deployer memory death_rate ≥ 0.70 with prior_token_count ≥ 5
- same_funder_as_known_bad = FAIL
- bundled_supply_pct ≥ 50

### Caution (cannot PASS_FILTER)
- Any Layer A field UNKNOWN
- top10_excluding_lp_pct ≥ 30
- bundled_supply_pct 20–50
- deployer prior_token_count ≥ 10 with death_rate ≥ 0.40
- funding_cluster_size ≥ 5
- RPC partial failure

### Score (risk, 0–100, higher = worse)
Start with a transparent weighted sum, then cap:

| Signal | Weight | Notes |
|---|---|---|
| mint/freeze/honeypot/LP fails | n/a | veto, do not average away |
| bundle + bundled_supply_pct | 25 | |
| holder concentration ex-LP | 20 | |
| funding cluster / known-bad funder | 25 | |
| deployer death_rate × log(1+count) | 20 | |
| metadata / exotic program | 10 | |

Caps (example, put in yaml):
- bundled_supply_pct ≥ 50 → score cannot be below 55 (i.e. cannot look “safe”)
- death_rate ≥ 0.7 → score cannot be below 70

**PASS_FILTER** only if: no veto, no caution rule, score < 35.

Do not call Grok to pick weights. After you have ≥200 labeled 24h outcomes, add a *separate* calibration script. Do not mix that into v1.

---

## 5. Chain-specific scan behavior

### Solana / Pump.fun
- Subscribe to Pump.fun program logs (`logsSubscribe`) when watching.
- Checks: mint authority, freeze authority, Token-2022 extensions, metadata mutability if cheap to read.
- First N buy txs in launch slot(s): same-slot count, shared funding source (one hop + two hop if RPC allows).
- LP / bonding-curve: treat “not graduated + creator can still dump curve inventory” as a caution detail, not automatically AVOID (almost all Pump tokens look like this at second 0). Document this in README so we do not false-veto the entire pad.
- Optional adapter: RugCheck API if `RUGCHECK_ENABLED=1`. Never require it for core scan.

### Base / Clanker / Uniswap V3
- Listen `PoolCreated` on Uniswap V3 factory (and Clanker factory if configured).
- ERC20: `owner()`, bytecode selector scan for mint/pause/blacklist/setFee.
- Simulate buy then sell on the pool with a throwaway fork if possible (`anvil` optional; otherwise `eth_call` patterns). If simulation infra is missing, mark honeypot UNKNOWN → CAUTION.
- LP lock: check if LP NFT / V2 LP token is burned or in a known locker. Unknown locker = CAUTION.

### Robinhood Chain / Pons V2
- Do not hardcode unverified factory addresses. Put them in `config/chains.yaml` with a comment “VERIFY ON-CHAIN BEFORE ENABLE”.
- Same EVM scanner as Base with chain_id + RPC from env.
- If the chain RPC is flaky, watch mode must back off and mark UNKNOWN rather than invent PASS.

`config/chains.yaml` must include: chain_id, rpc_env_key, wss_env_key, factory_addresses, explorer_url, notes.

---

## 6. Graph layer (the accuracy jump)

v1 heuristics, no ML:

1. **Same-slot bundle:** ≥5 distinct buyers in the launch slot / same tx package → bundle_detected FAIL.
2. **Shared funder:** of first 20 buyers + deployer, trace first inbound native token transfer. If ≥4 wallets share one funder, record cluster.
3. **Known-bad funder:** if that funder is in `data/memory/clusters/` with death_rate ≥ 0.6, FAIL.
4. **Consolidation:** if within first 50 txs, ≥3 early buyer balances move to one wallet, early_consolidation FAIL.

Persist:
- `data/memory/deployers/{address}.json`
- `data/memory/clusters/{cluster_id}.json`

Update memory on every scan. Never delete history from CLI without `--i-understand`.

Death label for a prior token (used by memory):
- liquidity ~0 or untradeable, or
- price −90% from first print within 24h and creator extracted, or
- freeze used / mint used after launch

If you cannot compute death yet, leave death_rate null and do not pretend it is 0.

---

## 7. Pipeline

```
detect or paste address
  → open case
  → Layer A scan
  → if hard veto: write AVOID, stop
  → Layer B graph
  → load/update deployer memory
  → score + verdict
  → write scans JSON + case md + verdict md
  → schedule outcome jobs at +1h +6h +24h
```

CLI:

```
ff scan --chain solana --token <mint>
ff scan --chain base --token <addr>
ff watch --chain solana [--min-score-alert 50]
ff label --due
ff funnel --days 7
ff show <case_id>
```

`ff watch` must:
- dedupe tokens
- rate-limit RPC
- write every scan even if AVOID
- print one line per token: `case_id chain token score verdict`
- never open a browser for each mint

---

## 8. Optional LLM explainer (off by default)

`src/filter_floor/explain.py`:
- Input: the ScanResult JSON only
- Output: ≤120 words + restatement of verdict
- System rule: “Do not change the verdict. Do not add facts not in JSON.”
- Env: `XAI_API_KEY` optional. Model default `grok-4-1-fast` or whatever is configured.
- If key missing, skip silently.

Grok Bot should call `ff scan` then optionally `ff explain <case_id>`, not re-scan in prose.

---

## 9. Local API (optional, v1.1)

FastAPI on `127.0.0.1:3001` only (localhost bind).

```
POST /scan {chain, token}
GET  /cases/{id}
GET  /funnel
```

No auth is acceptable **only** because it binds localhost. Document that. Do not expose 0.0.0.0.

This is the hook Grok Bot or a future agent hits: `http://127.0.0.1:3001/scan`.

---

## 10. Environment

`.env.example`:

```
SOLANA_RPC_URL=
SOLANA_WSS_URL=
BASE_RPC_URL=
BASE_WSS_URL=
ROBINHOOD_RPC_URL=
ROBINHOOD_WSS_URL=
RUGCHECK_ENABLED=0
GOPLUS_API_KEY=
XAI_API_KEY=
EXPLAIN_ENABLED=0
DATA_DIR=./data
```

Public RPCs allowed for bring-up. README must say live watch needs a paid/stable RPC (Helius / QuickNode / etc.).

---

## 11. Tests Cursor must write before claiming done

Use fixtures in `tests/fixtures/`:
1. Known mechanical rug: mint active → AVOID
2. Freeze active → AVOID
3. Clean authorities, high bundle pct → AVOID or CAUTION per yaml
4. Serial deployer fixture (10 dead tokens) → AVOID
5. Partial RPC / UNKNOWN mint → not PASS_FILTER
6. Scoring caps cannot be bypassed by “good” metadata
7. CLI `ff scan` writes both JSON and case md
8. `ff funnel` counts veto vs pass

Do not require live mainnet for CI. Mock RPC.

---

## 12. Implementation order (do this sequence)

### Milestone 0 — skeleton (half day)
- repo, pyproject, models, veto yaml, empty scanners returning UNKNOWN
- CLI `ff scan` writes files
- tests for veto merge logic with hand-built ScanResult

### Milestone 1 — Solana Layer A
- real mint/freeze/supply/decimals
- Pump.fun create listener (can start as polling if WS is hard)
- fixture tests

### Milestone 2 — EVM Layer A (Base first)
- owner / bytecode selectors / pair discovery
- sell simulation stub that returns UNKNOWN until fork works
- Robinhood config gated behind `ROBINHOOD_ENABLED=0`

### Milestone 3 — Layer B graph v1
- same-slot bundle
- one-hop shared funder
- memory JSON files

### Milestone 4 — outcomes + funnel
- `ff label --due` using DexScreener or native price/liquidity if available; else mark unknown
- funnel.jsonl

### Milestone 5 — explain + localhost API
- only after scans are deterministic

Do not build a sniper, dashboard charts, or multi-agent prompts in M0–M4.

---

## 13. How Grok Bot should use this repo later

One bot only (Floor Lead). Instructions to paste into that bot after M4:

```
You operate Filter Floor from this git repo.
You do not invent token facts.
Workflow:
1. git pull
2. ff scan --chain <chain> --token <addr>
3. Read data/scans/<case_id>.json
4. Report verdict + veto_reasons only
5. If EXPLAIN_ENABLED, run ff explain <case_id>
Never say BUY. Never hold keys. Never change scoring.yaml without the human.
If RPC returns UNKNOWN, say CAUTION and stop.
```

Do not create five more bots that duplicate scanners.

---

## 14. README sections Cursor must write

- What the verdicts mean
- Why Pump.fun tokens can look “concentrated” at birth
- How to run one scan
- How to run watch (and RPC warning)
- How memory grows and that it is local
- This is not financial advice
- How to label outcomes if auto-label fails

---

## 15. Definition of done (v1)

- `ff scan` works on Solana + Base with mocked and (optionally) live RPC
- Hard vetoes cannot be scored away
- Case JSON + markdown written for every scan
- `ff funnel` runs
- Tests listed in §11 pass
- No wallet key in repo
- No BUY language in CLI output
- Robinhood enabled only when factory addresses are filled and tested

---

## 16. Out of scope until human says so

- Auto-buy / Jupiter / Uniswap send
- Multi-agent Grok floor
- Training an ML model on outcomes
- Mobile app
- Hosting the API on a public URL
- Paying for third-party forensic APIs as a hard dependency

---

## 17. Cursor working style

- Implement the smallest passing slice per milestone.
- Prefer boring code over frameworks.
- When chain details are uncertain (Pons factory, Token-2022 edge cases), put `UNKNOWN` + a `TODO(verify)` comment rather than guessing a PASS.
- Do not scrape random GitHub scanners into the core path without isolating them as optional adapters.
- After each milestone, print how to run the new CLI command.

Start at Milestone 0 now.
