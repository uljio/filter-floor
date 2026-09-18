# Filter Floor

Local, fail-closed **pre-buy filter** for new launchpad tokens. It blocks mechanical rugs and serial-operator patterns. It does **not** pick winners and does **not** execute trades.

Verdicts are only `AVOID`, `CAUTION`, and `PASS_FILTER`. There is no BUY language, no size, and no target price.

This is **not financial advice**.

## What the verdicts mean

| Verdict | Meaning |
|---|---|
| `AVOID` | A hard veto fired (mint/freeze/honeypot/Token-2022 trap, known-bad funder, high bundled supply, serial deployer death rate). Do not treat this token as filter-clean. |
| `CAUTION` | Incomplete facts (UNKNOWN checks), a caution rule, or a risk score at/above the pass cap. `PASS_FILTER` is blocked. |
| `PASS_FILTER` | No veto, no caution rule, and risk score below `pass_filter.max_score` (35 in `config/scoring.yaml`). This is **not** a recommendation to trade. |

Unknown, missing RPC, or incomplete simulation is `CheckStatus.UNKNOWN`. UNKNOWN never becomes PASS. Empty / stub scanners return UNKNOWN on Layer A and Layer B, so a skeleton scan lands **`CAUTION`**, never `PASS_FILTER`.

Layer A FAIL on mint, freeze, honeypot, owner/upgrade, or Token-2022 trap is always `AVOID`. Unlocked / unburned LP is `FAIL` on that field and `CAUTION`, not a hard AVOID. Scoring cannot average a veto away.

## Why Pump.fun tokens can look concentrated at birth

At second 0 a Pump.fun token is usually still on the bonding curve. The creator can still hold curve inventory, so holder concentration and “bundle-looking” prints are common for almost every mint. Filter Floor treats **bonding-curve inventory as a caution detail, not an automatic AVOID**, so the whole pad is not false-vetoed. After graduation, LP lock / burn and holder graph are evaluated on their own facts.

## How to run one scan

Python 3.12+ (3.13 is fine). From the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
ff scan --chain solana --token <mint>
ff scan --chain base --token <addr>
ff show <case_id>
```

Each scan writes:

- `data/scans/{case_id}.json` (machine)
- `data/cases/{case_id}.md` (human)

`case_id` is `{YYYYMMDD}-{chain}-{token[:8]}`.

Solana Layer A reads mint authority, freeze authority, supply, decimals, Token-2022 extensions, and Raydium AMM v4 LP burn/lock when the LP mint account exists (`PASS` if burned, in a known locker, or held by the Raydium pool/vault/AMM; `FAIL` if still in a normal wallet — that field FAIL is `CAUTION`, not AVOID). Missing pool/LP account stays `UNKNOWN`. Base EVM Layer A reads `owner()`, bytecode selectors, Uniswap V3 `getPool`, position-NFT burn/lock when those logs exist, and sell simulation via `eth_call` with state override. If the RPC cannot override/fork, `honeypot_or_unsellable` stays `UNKNOWN` (never PASS). Layer B then runs same-slot bundle, one-hop shared funder, known-bad cluster, and early-consolidation heuristics. Incomplete graph facts stay `UNKNOWN` and cannot `PASS_FILTER`. Robinhood / Pons stays gated (`ROBINHOOD_ENABLED=0`) with empty factories.

```powershell
ff scan --chain solana --token <mint>
ff scan --chain base --token 0xYourToken
```

Needs `BASE_RPC_URL` for live Base reads. Empty/missing RPC is `UNKNOWN`, never `PASS`. Clanker factory is empty until verified on-chain.

Optional RugCheck is off unless `RUGCHECK_ENABLED=1`. It never overrides on-chain mint/freeze FAIL and never invents PASS.

Public RPC URLs in `.env.example` are for bring-up only.

## How to run watch (and RPC warning)

```powershell
ff watch --chain solana --min-score-alert 50
ff watch --chain solana --once
ff watch --chain solana --once --limit 100
ff watch --chain base --once
```

Solana watch polls Pump.fun program signatures for `create` (websocket subscribe is attempted only if `SOLANA_WSS_URL` is set, then falls back to polling). Only Pump `Create` txs are scanned; `--limit` (default 20) is how many program signatures each poll fetches. After each poll it prints one stderr line: `signatures=N creates=M skipped_rpc=K null=X error=Y version=V ratelimit=R` (`null` is a missing tx, `version` is an unsupported tx version, `ratelimit` is HTTP 429 / 503 / JSON-RPC -32005 after retries). `getTransaction` uses `jsonParsed` and `maxSupportedTransactionVersion: 1`, retries 429/-32005/503 up to 3 times (1s then 2s backoff), and pauses between calls via `SOLANA_TX_DELAY_MS` (default 120). CLI output never prints the RPC URL or API key. Base watch polls Uniswap V3 `PoolCreated` on the factory in `config/chains.yaml`. Both dedupe tokens, write every scan (including `AVOID`), and print one line per token: `case_id chain token score verdict`. They do not open a browser. `--once` runs a single poll pass. Robinhood watch stays off until the Pons factory is filled and `ROBINHOOD_ENABLED=1`.

**RPC warning:** live watch needs a paid/stable RPC (Helius, QuickNode, or equivalent). Public endpoints will rate-limit and produce UNKNOWN checks. Filter Floor will not invent PASS when the chain cannot be read.

## How memory grows (it is local)

Deployer and cluster memory live under `data/memory/` on this machine only:

- `data/memory/deployers/{address}.json`
- `data/memory/clusters/{cluster_id}.json`

Every `ff scan` loads and updates these files. History is local, append-style, and is not uploaded anywhere by this tool.

If death rate cannot be computed from labeled outcomes it stays **`null`** — it is **not** treated as 0, and it cannot look like a clean pass. After a scan, inspect memory with:

```powershell
Get-Content data\memory\deployers\<address>.json
Get-Content data\memory\clusters\<cluster_id>.json
```

Deleting memory from the CLI requires an explicit flag:

```powershell
ff memory reset --i-understand
```

Without `--i-understand` the command refuses and leaves files in place.

## How to label outcomes if auto-label fails

`ff label --due` labels any 1h / 6h / 24h horizon whose due time has passed. Each `ff scan` records those due times on the case (`data/outcomes/{case_id}.json` and the case markdown).

It uses DexScreener (or a native price/liquidity snapshot if one is available). If there is no quote, no pair, or the request fails, the horizon is **`unknown`**. Filter Floor does not invent `survived` or `rugged`.

```powershell
ff label --due
```

Prints one line per labeled horizon: `case_id horizon label`. Labels are `rugged`, `bled`, `survived`, or `unknown`. CLI output never says BUY.

Death labels used by deployer memory (only when evidence exists):

- liquidity ~0 / untradeable
- price −90% from first print within 24h **and** creator extracted
- freeze or mint used after launch

If death cannot be computed, `death_rate` stays **`null`**. An `unknown` outcome is not treated as 0 death rate.

If auto-label fails, edit files under `data/outcomes/` using those four labels at 1h / 6h / 24h. Funnel accuracy is measured from those labels, not from a pretty score.

## Funnel

```powershell
ff funnel --days 7
```

Counts `scanned → vetoed → caution → pass → later rugged` for scans in the last N days and appends a snapshot line to `data/funnel.jsonl`. `vetoed` is `AVOID`, `pass` is `PASS_FILTER`. `later_rugged` is a case that later received a `rugged` outcome label.

## Optional LLM explainer

Off by default. `ff explain <case_id>` reads `data/scans/{case_id}.json` only. It does **not** re-scan, does **not** change the stored verdict, and does **not** add facts that are not in that JSON. UNKNOWN checks stay unknown; the explainer cannot rewrite them to `PASS_FILTER`.

Set `EXPLAIN_ENABLED=1` and `XAI_API_KEY` to enable. Model default is `grok-4-1-fast` (`EXPLAIN_MODEL` or `config/explain.yaml`). If the key is missing or `EXPLAIN_ENABLED=0`, the command prints `skipped` and invents no prose.

```powershell
ff scan --chain solana --token <mint>
ff explain <case_id>
```

## Local API (localhost only)

FastAPI binds **`127.0.0.1:3001` only**. There is no auth, which is acceptable **only** because the process is localhost. Do not bind `0.0.0.0`. Do not expose this URL.

```powershell
ff api
```

```
POST http://127.0.0.1:3001/scan    {"chain": "solana"|"base"|"robinhood", "token": "<addr>"}
GET  http://127.0.0.1:3001/cases/{id}
GET  http://127.0.0.1:3001/funnel?days=7
```

`POST /scan` runs the same deterministic pipeline as `ff scan` and returns that verdict. A later single Floor Lead bot can hit `http://127.0.0.1:3001/scan`. This repo does not run that bot.

`ff api --host 0.0.0.0` is refused.

## Config

Numbers live in yaml, not hardcoded in Python:

- `config/vetoes.yaml` — hard veto thresholds and caution rules
- `config/scoring.yaml` — weights 25/20/25/20/10, caps, `pass_filter.max_score`
- `config/chains.yaml` — `chain_id`, RPC env keys, factories, explorer, notes
- `config/outcomes.yaml` — liquidity and drawdown thresholds for `ff label --due`
- `config/explain.yaml` — optional explainer model and word cap

Robinhood / Pons V2 factories are empty with `VERIFY ON-CHAIN BEFORE ENABLE`. `ROBINHOOD_ENABLED` defaults to `0`. `RUGCHECK_ENABLED` defaults to `0`. `EXPLAIN_ENABLED` defaults to `0`.

## Tests

```powershell
pytest
```

No live mainnet is required. Fixtures are hand-built `ScanResult` graphs, mocked Solana mint/freeze/Token-2022 account bytes under `tests/fixtures/solana/`, mocked EVM bytecode/RPC under `tests/fixtures/` + `tests/test_evm.py`, mocked Layer B feeds in `tests/test_graph.py`, mocked DexScreener/HTTP in `tests/test_outcomes.py`, and mocked xAI HTTP in `tests/test_explain.py`. The local API is exercised with FastAPI `TestClient` (no live bind).

## Out of scope

No wallets, keys, snipers, auto-buy, copy-trading, dashboards, or extra agents in this repo.
