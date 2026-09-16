"""Layer B graph v1 + deployer/cluster memory. Mocked RPC only; no mainnet."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from filter_floor.cli import app
from filter_floor.graph.clusters import cluster_id_for_funder
from filter_floor.graph.types import ParsedTokenTx, TokenMove
from filter_floor.models import Chain, CheckStatus, Verdict
from filter_floor.pipeline import run_scan
from filter_floor.storage.memory import cluster_path, deployer_path
from tests.fakes import FakeGraphSource, FakeSolanaRpc
from tests.fixtures.solana_mints import mint_authorities_revoked, pubkey_from_byte, spl_account
from tests.helpers import FixtureScanner, layer_a_clean

runner = CliRunner()

DEPLOYER = "DeployerGraph111111111111111111111111111"
FUNDER = "SharedFunder111111111111111111111111111"
TOKEN = "TokenGraph11111111111111111111111111111"


def _tx(tx_id: str, slot: int, buyers: list[str], moves: list[TokenMove] | None = None) -> ParsedTokenTx:
    return ParsedTokenTx(tx_id=tx_id, slot=slot, buyers=buyers, moves=list(moves or []))


def _clean_layer_a(creator: str = DEPLOYER):
    return layer_a_clean(details={"creator": creator, "token": TOKEN})


def _scan(tmp_path, *, graph: FakeGraphSource, token: str = TOKEN, layer_a=None):
    return run_scan(
        Chain.solana,
        token,
        tmp_path,
        scanner=FixtureScanner(layer_a or _clean_layer_a()),
        graph_source=graph,
    )


def _unique_funders(wallets: list[str]) -> dict[str, str]:
    return {wallet: f"funder-of-{wallet}" for wallet in wallets}


def test_same_slot_bundle_five_buyers_is_fail(tmp_path):
    buyers = [f"buyer{i}" for i in range(5)]
    graph = FakeGraphSource(
        txs=[_tx("bundle", 10, buyers)],
        funders=_unique_funders(buyers + [DEPLOYER]),
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    assert result.layer_b.bundle_detected is CheckStatus.FAIL
    assert result.layer_b.bundle_detected is not CheckStatus.PASS
    assert result.verdict is not Verdict.PASS_FILTER
    assert any("bundle_detected" in reason for reason in result.caution_reasons + result.veto_reasons)


def test_shared_funder_cluster_recorded(tmp_path):
    buyers = [f"buyer{i}" for i in range(4)]
    graph = FakeGraphSource(
        txs=[_tx(f"t{i}", i + 1, [buyers[i]]) for i in range(4)],
        funders={**{b: FUNDER for b in buyers}, DEPLOYER: "other-funder"},
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    assert result.layer_b.funding_cluster_size == 4
    assert result.layer_b.details["shared_funder"] == FUNDER
    cid = cluster_id_for_funder(FUNDER)
    path = cluster_path(tmp_path, cid)
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["funder"] == FUNDER
    assert payload["death_rate"] is None
    assert len(payload["wallets"]) >= 4


def test_known_bad_funder_is_avoid(tmp_path):
    buyers = [f"buyer{i}" for i in range(4)]
    cid = cluster_id_for_funder(FUNDER)
    cluster_path(tmp_path, cid).parent.mkdir(parents=True, exist_ok=True)
    cluster_path(tmp_path, cid).write_text(
        json.dumps(
            {
                "cluster_id": cid,
                "funder": FUNDER,
                "wallets": [FUNDER],
                "death_rate": 0.6,
                "tokens": [{"token": "old-dead", "dead": True}],
            }
        ),
        encoding="utf-8",
    )
    graph = FakeGraphSource(
        txs=[_tx(f"t{i}", i + 1, [buyers[i]]) for i in range(4)],
        funders={**{b: FUNDER for b in buyers}, DEPLOYER: FUNDER},
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    assert result.layer_b.same_funder_as_known_bad is CheckStatus.FAIL
    assert result.verdict is Verdict.AVOID
    assert any("same_funder_as_known_bad" in reason for reason in result.veto_reasons)


def test_serial_deployer_memory_death_rate_avoid(tmp_path):
    path = deployer_path(tmp_path, DEPLOYER)
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = [
        {"token": f"prior-dead-{i}", "chain": "solana", "dead": True}
        for i in range(5)
    ]
    path.write_text(
        json.dumps(
            {
                "deployer": DEPLOYER,
                "prior_token_count": 5,
                "death_rate": 0.70,
                "tokens": tokens,
            }
        ),
        encoding="utf-8",
    )
    graph = FakeGraphSource(
        txs=[_tx("t0", 1, ["buyer0"]), _tx("t1", 2, ["buyer1"])],
        funders=_unique_funders(["buyer0", "buyer1", DEPLOYER]),
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    assert result.memory.prior_token_count >= 5
    assert result.memory.death_rate is not None
    assert result.memory.death_rate >= 0.70
    assert result.verdict is Verdict.AVOID
    assert any("death_rate" in reason for reason in result.veto_reasons)


def test_null_death_rate_is_not_treated_as_zero(tmp_path):
    path = deployer_path(tmp_path, DEPLOYER)
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = [{"token": f"prior-unlabeled-{i}", "chain": "solana"} for i in range(5)]
    path.write_text(
        json.dumps(
            {
                "deployer": DEPLOYER,
                "prior_token_count": 5,
                "death_rate": None,
                "tokens": tokens,
            }
        ),
        encoding="utf-8",
    )
    graph = FakeGraphSource(
        txs=[_tx("t0", 1, ["buyer0"]), _tx("t1", 2, ["buyer1"])],
        funders=_unique_funders(["buyer0", "buyer1", DEPLOYER]),
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    assert result.memory.death_rate is None
    assert result.memory.death_rate != 0
    assert result.memory.prior_token_count == 5
    assert result.verdict is not Verdict.PASS_FILTER
    assert any("null" in reason for reason in result.caution_reasons)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["death_rate"] is None
    assert stored["details"]["death_rate_null"] is True


def test_incomplete_graph_is_unknown_not_pass(tmp_path):
    graph = FakeGraphSource(
        txs=[],
        funders={},
        complete=False,
        rpc_partial_failure=True,
        notes=["rpc miss"],
    )
    result = _scan(tmp_path, graph=graph)
    assert result.layer_b.bundle_detected is CheckStatus.UNKNOWN
    assert result.layer_b.same_funder_as_known_bad is CheckStatus.UNKNOWN
    assert result.layer_b.early_consolidation is CheckStatus.UNKNOWN
    assert result.layer_b.bundle_detected is not CheckStatus.PASS
    assert result.verdict is not Verdict.PASS_FILTER
    assert result.verdict is Verdict.CAUTION
    assert any("UNKNOWN" in reason for reason in result.caution_reasons)


def test_early_consolidation_fail(tmp_path):
    buyers = ["b1", "b2", "b3"]
    sink = "consolidator"
    moves = [
        TokenMove(tx_id="c1", slot=3, sender="b1", receiver=sink),
        TokenMove(tx_id="c1", slot=3, sender="b2", receiver=sink),
        TokenMove(tx_id="c1", slot=3, sender="b3", receiver=sink),
    ]
    graph = FakeGraphSource(
        txs=[
            _tx("t0", 1, buyers),
            _tx("c1", 3, [], moves),
        ],
        funders=_unique_funders(buyers + [DEPLOYER]),
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    assert result.layer_b.early_consolidation is CheckStatus.FAIL
    assert result.verdict is not Verdict.PASS_FILTER


def test_memory_json_written_on_every_scan(tmp_path):
    graph = FakeGraphSource(
        txs=[_tx("t0", 1, ["buyer0"])],
        funders=_unique_funders(["buyer0", DEPLOYER]),
        complete=True,
    )
    result = _scan(tmp_path, graph=graph)
    path = deployer_path(tmp_path, DEPLOYER)
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["deployer"] == DEPLOYER
    assert payload["death_rate"] is None
    assert any(row.get("token") == TOKEN for row in payload["tokens"])
    assert result.memory.details.get("death_rate_null") or result.memory.death_rate is None


def test_memory_reset_requires_i_understand(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = deployer_path(tmp_path, DEPLOYER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"deployer": DEPLOYER, "death_rate": None, "tokens": []}), encoding="utf-8")

    refused = runner.invoke(app, ["memory", "reset"])
    assert refused.exit_code == 1
    assert "refusing to delete memory without --i-understand" in refused.output
    assert path.is_file()

    deleted = runner.invoke(app, ["memory", "reset", "--i-understand"])
    assert deleted.exit_code == 0, deleted.output
    assert not path.is_file()
    assert "BUY" not in deleted.output


def test_solana_rpc_bundle_via_shorthand_tx(tmp_path):
    mint = pubkey_from_byte(80)
    buyers = [pubkey_from_byte(81 + i) for i in range(5)]
    sig = "sig-bundle"
    rpc = FakeSolanaRpc(
        {mint: spl_account(mint_authorities_revoked())},
        signatures_by_address={mint: [sig]},
        transactions={
            sig: {"slot": 100, "graph": {"buyers": buyers, "moves": []}},
        },
    )
    result = run_scan(Chain.solana, mint, tmp_path, solana_rpc=rpc)
    assert result.layer_b.bundle_detected is CheckStatus.FAIL
    assert result.verdict is not Verdict.PASS_FILTER
    assert (tmp_path / "memory" / "deployers").is_dir()


def test_layer_a_veto_skips_graph_still_avoid(tmp_path):
    from tests.helpers import layer_a_all

    called = {"graph": False}

    class TrackingSource(FakeGraphSource):
        def fetch_early_txs(self, token, limit):
            called["graph"] = True
            return super().fetch_early_txs(token, limit)

    layer_a = layer_a_all(CheckStatus.FAIL, details={"creator": DEPLOYER})
    result = run_scan(
        Chain.solana,
        TOKEN,
        tmp_path,
        scanner=FixtureScanner(layer_a),
        graph_source=TrackingSource(complete=True),
    )
    assert called["graph"] is False
    assert result.verdict is Verdict.AVOID
    assert result.layer_b.details.get("graph_skipped") is True
    assert deployer_path(tmp_path, DEPLOYER).is_file()
