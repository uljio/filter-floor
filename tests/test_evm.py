"""EVM Layer A (Base first). Mocked RPC only — no live mainnet."""

from __future__ import annotations

import json
from pathlib import Path

from web3 import Web3

from filter_floor.listeners.evm_ws import (
    POOL_CREATED_TOPIC0,
    decode_pool_created_log,
    pool_created_matches_token,
)
from filter_floor.models import Chain, CheckStatus, Verdict
from filter_floor.pipeline import run_scan
from filter_floor.scanners.clanker import ClankerScanner
from filter_floor.scanners.evm import (
    BASE_QUOTE_TOKENS,
    EIP1967_IMPLEMENTATION_SLOT,
    GET_POOL_SELECTOR,
    OWNER_SELECTOR,
    SELECTOR_GROUPS,
    EvmRpc,
    EvmScanner,
    encode_get_pool,
    extract_push4_selectors,
    pad_address,
    simulate_sell_stub,
)
from filter_floor.scanners.pons import PonsScanner
from filter_floor.scoring import compute_score
from filter_floor.vetoes import evaluate
from tests.helpers import memory_clean

FIXTURES = Path(__file__).parent / "fixtures"
ADDR = json.loads((FIXTURES / "evm_layer_a_addresses.json").read_text(encoding="utf-8"))
TOKEN = ADDR["token"]
OWNER = ADDR["owner"]
POOL = ADDR["pool"]
FACTORY = ADDR["uniswap_v3_factory"]
ZERO = "0x" + ("0" * 40)


def _sel(sig: str) -> str:
    return Web3.keccak(text=sig)[:4].hex()


def push4_bytecode(*selectors: str) -> str:
    parts = ["00"]
    for selector in selectors:
        hexpart = selector.lower().removeprefix("0x")
        assert len(hexpart) == 8, selector
        parts.append("63" + hexpart)
    parts.append("00")
    return "0x" + "".join(parts)


def addr_word(address: str) -> str:
    return "0x" + pad_address(address)


class FakeEvmRpc(EvmRpc):
    def __init__(
        self,
        *,
        code: str = "0x00",
        owner: str | None = None,
        pool: str | None = None,
        storage: dict[str, str] | None = None,
        code_miss: bool = False,
        call_miss: bool = False,
    ) -> None:
        self.code = code
        self.owner = owner
        self.pool = pool
        self.storage = {key.lower(): value for key, value in (storage or {}).items()}
        self.code_miss = code_miss
        self.call_miss = call_miss

    def get_code(self, address: str) -> str | None:
        if self.code_miss:
            return None
        return self.code

    def eth_call(self, to: str, data: str) -> str | None:
        if self.call_miss:
            return None
        data_l = data.lower()
        sel = data_l.removeprefix("0x")[:8]
        if sel == OWNER_SELECTOR:
            if self.owner is None:
                return None
            return addr_word(self.owner)
        if sel == GET_POOL_SELECTOR:
            if not self.pool:
                return addr_word(ZERO)
            return addr_word(self.pool)
        return None

    def get_storage_at(self, address: str, slot: str) -> str | None:
        return self.storage.get(slot.lower())


def _base_scanner(rpc: FakeEvmRpc, **kwargs) -> ClankerScanner:
    factories = kwargs.pop(
        "factory_addresses",
        {"uniswap_v3": FACTORY, "clanker": ""},
    )
    return ClankerScanner(
        rpc,
        factory_addresses=factories,
        quote_tokens=BASE_QUOTE_TOKENS,
        **kwargs,
    )


def test_selector_constants_match_keccak():
    assert _sel("owner()") == OWNER_SELECTOR
    assert _sel("getPool(address,address,uint24)") == GET_POOL_SELECTOR
    assert _sel("mint(address,uint256)") in SELECTOR_GROUPS["mint"]
    assert _sel("mint(uint256)") in SELECTOR_GROUPS["mint"]
    assert _sel("pause()") in SELECTOR_GROUPS["pause"]
    assert _sel("addBlackList(address)") in SELECTOR_GROUPS["blacklist"]
    assert _sel("setFee(uint256)") in SELECTOR_GROUPS["setFee"]
    assert _sel("setSwapAndLiquifyEnabled(bool)") in SELECTOR_GROUPS["setFee"]
    assert _sel("upgradeTo(address)") in SELECTOR_GROUPS["upgrade"]
    # Canonical Uniswap V3 Factory PoolCreated topic (published ABI).
    assert POOL_CREATED_TOPIC0.startswith("0x783cca1c0412dd0d695e784568c96da2e9c")


def test_extract_push4_selectors():
    code = push4_bytecode("40c10f19", "8456cb59")
    found = extract_push4_selectors(code)
    assert "40c10f19" in found
    assert "8456cb59" in found


def test_missing_rpc_is_unknown_not_pass():
    layer_a, layer_b = EvmScanner().scan(TOKEN)
    for field in (
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "lp_locked_or_burned",
        "honeypot_or_unsellable",
        "owner_or_upgrade_risk",
        "token2022_or_hook_risk",
    ):
        assert getattr(layer_a, field) is CheckStatus.UNKNOWN
    assert layer_a.details.get("rpc_partial_failure") is True
    assert "TODO(verify)" in layer_a.details
    assert layer_b.bundle_detected is CheckStatus.UNKNOWN
    score = compute_score(layer_a, layer_b, memory_clean())
    decision = evaluate(layer_a, layer_b, memory_clean(), score)
    assert decision.verdict is not Verdict.PASS_FILTER


def test_getcode_rpc_miss_is_unknown():
    scanner = _base_scanner(FakeEvmRpc(code_miss=True))
    layer_a = scanner.scan_layer_a(TOKEN)
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN
    assert layer_a.details.get("rpc_partial_failure") is True
    assert all(
        getattr(layer_a, field) is CheckStatus.UNKNOWN
        for field in (
            "mint_authority_revoked",
            "freeze_authority_revoked",
            "lp_locked_or_burned",
            "honeypot_or_unsellable",
            "owner_or_upgrade_risk",
            "token2022_or_hook_risk",
        )
    )


def test_empty_bytecode_is_unknown():
    scanner = _base_scanner(FakeEvmRpc(code="0x", owner=OWNER))
    layer_a = scanner.scan_layer_a(TOKEN)
    assert layer_a.details.get("bytecode_empty") is True
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN


def test_mint_selector_and_nonzero_owner_is_fail():
    rpc = FakeEvmRpc(code=push4_bytecode("40c10f19"), owner=OWNER)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.mint_authority_revoked is CheckStatus.FAIL
    assert layer_a.details["owner"] == OWNER
    assert "40c10f19" in layer_a.details["selectors"]["mint"]
    assert layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN


def test_pause_blacklist_selector_and_owner_is_freeze_fail():
    rpc = FakeEvmRpc(code=push4_bytecode("8456cb59", "0ecb93c0"), owner=OWNER)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.freeze_authority_revoked is CheckStatus.FAIL
    assert layer_a.details["selectors"]["pause"]
    assert layer_a.details["selectors"]["blacklist"]


def test_setfee_selector_is_token_hook_fail():
    rpc = FakeEvmRpc(code=push4_bytecode("69fe0e2d"), owner=OWNER)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.token2022_or_hook_risk is CheckStatus.FAIL


def test_nonzero_owner_is_upgrade_risk_fail():
    rpc = FakeEvmRpc(code=push4_bytecode("8da5cb5b"), owner=OWNER)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.owner_or_upgrade_risk is CheckStatus.FAIL


def test_upgrade_selector_is_owner_risk_fail_even_if_owner_zero():
    rpc = FakeEvmRpc(code=push4_bytecode("3659cfe6"), owner=ZERO)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.owner_or_upgrade_risk is CheckStatus.FAIL


def test_eip1967_implementation_is_owner_risk_fail():
    rpc = FakeEvmRpc(
        code=push4_bytecode("8da5cb5b"),
        owner=ZERO,
        storage={EIP1967_IMPLEMENTATION_SLOT: addr_word(OWNER)},
    )
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.owner_or_upgrade_risk is CheckStatus.FAIL
    assert layer_a.details["eip1967_implementation"] == OWNER


def test_mint_selector_with_zero_owner_is_unknown_not_pass():
    rpc = FakeEvmRpc(code=push4_bytecode("40c10f19"), owner=ZERO)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.mint_authority_revoked is CheckStatus.UNKNOWN
    assert "TODO(verify):mint" in layer_a.details


def test_owner_call_miss_is_unknown_not_pass():
    rpc = FakeEvmRpc(code=push4_bytecode("8da5cb5b"), owner=None)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.owner_or_upgrade_risk is CheckStatus.UNKNOWN
    assert layer_a.mint_authority_revoked is CheckStatus.PASS


def test_clean_bytecode_still_has_honeypot_and_lp_unknown():
    rpc = FakeEvmRpc(code=push4_bytecode("a9059cbb"), owner=ZERO, pool=POOL)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.mint_authority_revoked is CheckStatus.PASS
    assert layer_a.freeze_authority_revoked is CheckStatus.PASS
    assert layer_a.token2022_or_hook_risk is CheckStatus.PASS
    assert layer_a.owner_or_upgrade_risk is CheckStatus.PASS
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN
    assert layer_a.details["sell_simulation"] == "stub"
    assert layer_a.details["pools"]
    assert layer_a.details["pools"][0]["pool"] == POOL
    assert layer_a.details["clanker_factory_configured"] is False
    layer_b = _base_scanner(rpc).scan_layer_b(TOKEN)
    score = compute_score(layer_a, layer_b, memory_clean())
    decision = evaluate(layer_a, layer_b, memory_clean(), score)
    assert decision.verdict is Verdict.CAUTION
    assert decision.verdict is not Verdict.PASS_FILTER


def test_sell_simulation_stub_never_returns_pass():
    status, details = simulate_sell_stub(token=TOKEN, pool=POOL, rpc=FakeEvmRpc())
    assert status is CheckStatus.UNKNOWN
    assert status is not CheckStatus.PASS
    assert details["sell_simulation"] == "stub"
    assert "TODO(verify)" in details


def test_pool_discovery_encodes_getpool():
    data = encode_get_pool(TOKEN, BASE_QUOTE_TOKENS["weth"], 3000)
    assert data.startswith("0x" + GET_POOL_SELECTOR)
    assert TOKEN[2:].lower() in data
    rpc = FakeEvmRpc(code=push4_bytecode("a9059cbb"), owner=ZERO, pool=POOL)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    quotes = {row["quote"] for row in layer_a.details["pools"]}
    assert "weth" in quotes or "usdc" in quotes


def test_clanker_empty_factory_is_not_pass():
    rpc = FakeEvmRpc(code=push4_bytecode("a9059cbb"), owner=ZERO)
    layer_a = _base_scanner(rpc).scan_layer_a(TOKEN)
    assert layer_a.details["clanker_factory"] == ""
    assert "VERIFY ON-CHAIN BEFORE ENABLE" in layer_a.details["TODO(verify):clanker_factory"]
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN


def test_pons_gated_off_is_unknown_not_pass():
    scanner = PonsScanner(rpc=FakeEvmRpc(code=push4_bytecode("40c10f19"), owner=OWNER))
    layer_a, layer_b = scanner.scan(TOKEN)
    assert scanner.gated is True
    for field in (
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "lp_locked_or_burned",
        "honeypot_or_unsellable",
        "owner_or_upgrade_risk",
        "token2022_or_hook_risk",
    ):
        assert getattr(layer_a, field) is CheckStatus.UNKNOWN
    assert layer_a.details["robinhood_enabled"] is False
    assert "VERIFY ON-CHAIN BEFORE ENABLE" in layer_a.details["TODO(verify)"]
    assert layer_b.bundle_detected is CheckStatus.UNKNOWN


def test_pons_enabled_empty_factory_stays_unknown_for_factory_facts():
    rpc = FakeEvmRpc(code=push4_bytecode("a9059cbb"), owner=ZERO)
    scanner = PonsScanner(
        rpc,
        robinhood_enabled=True,
        factory_addresses={"pons_v2": ""},
    )
    layer_a = scanner.scan_layer_a(TOKEN)
    assert layer_a.details["pons_v2_factory_configured"] is False
    assert "VERIFY ON-CHAIN BEFORE ENABLE" in layer_a.details["TODO(verify):pons_v2"]
    assert layer_a.lp_locked_or_burned is CheckStatus.UNKNOWN
    assert layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN
    assert layer_a.honeypot_or_unsellable is not CheckStatus.PASS


def test_base_mint_fail_pipeline_is_avoid(tmp_path, monkeypatch):
    scanner = _base_scanner(FakeEvmRpc(code=push4_bytecode("40c10f19"), owner=OWNER))

    def _get_scanner(chain, *, solana_rpc=None):
        assert chain is Chain.base
        return scanner

    monkeypatch.setattr("filter_floor.pipeline.get_scanner", _get_scanner)
    result = run_scan(Chain.base, TOKEN, tmp_path)
    assert result.verdict is Verdict.AVOID
    assert any("mint_authority_revoked" in reason for reason in result.veto_reasons)
    assert "BUY" not in result.verdict.value
    assert result.deployer == OWNER
    assert result.layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN


def test_base_rpc_miss_pipeline_is_caution_not_pass(tmp_path):
    result = run_scan(Chain.base, TOKEN, tmp_path)
    assert result.verdict is Verdict.CAUTION
    assert result.verdict is not Verdict.PASS_FILTER
    assert result.layer_a.honeypot_or_unsellable is CheckStatus.UNKNOWN
    assert any("UNKNOWN" in reason for reason in result.caution_reasons)


def test_robinhood_gated_pipeline_is_caution_not_pass(tmp_path):
    result = run_scan(Chain.robinhood, TOKEN, tmp_path)
    assert result.verdict is Verdict.CAUTION
    assert result.verdict is not Verdict.PASS_FILTER
    assert any("robinhood gated" in reason for reason in result.caution_reasons)
    assert any("pons_v2 factory empty" in reason for reason in result.caution_reasons)
    assert all(status is CheckStatus.UNKNOWN for status in (
        result.layer_a.mint_authority_revoked,
        result.layer_a.honeypot_or_unsellable,
    ))
    assert "BUY" not in (result.veto_reasons + result.caution_reasons).__repr__()


def test_pool_created_log_fixture_decodes():
    log = json.loads((FIXTURES / "evm_pool_created_log.json").read_text(encoding="utf-8"))
    decoded = decode_pool_created_log(log)
    assert decoded is not None
    assert decoded["token0"] == TOKEN
    assert decoded["token1"] == BASE_QUOTE_TOKENS["weth"].lower()
    assert decoded["fee"] == 3000
    assert decoded["tick_spacing"] == 60
    assert decoded["pool"] == POOL
    assert pool_created_matches_token(decoded, TOKEN)
    assert decode_pool_created_log({"topics": ["0xdead"], "data": "0x"}) is None
