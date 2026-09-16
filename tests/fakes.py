"""In-memory RPC / graph feeds for tests. No mainnet."""

from __future__ import annotations

from filter_floor.adapters.rpc import AccountInfo, RpcError
from filter_floor.graph.types import EarlyTxFetch, FundingHop, ParsedTokenTx


class FakeSolanaRpc:
    def __init__(
        self,
        accounts: dict[str, AccountInfo | None] | None = None,
        signatures: list[str] | None = None,
        transactions: dict[str, dict | None] | None = None,
        fail: bool = False,
        fail_on: set[str] | None = None,
        signatures_by_address: dict[str, list[str]] | None = None,
    ) -> None:
        self.accounts = dict(accounts or {})
        self.signatures = list(signatures or [])
        self.signatures_by_address = dict(signatures_by_address or {})
        self.transactions = dict(transactions or {})
        self.fail = fail
        self.fail_on = set(fail_on or [])
        self.calls: list[tuple[str, str]] = []
        self.program_accounts: dict[str, list[tuple[str, AccountInfo]]] = {}
        self.token_largest: dict[str, list[dict]] = {}

    def get_account_info(self, address: str) -> AccountInfo | None:
        self.calls.append(("get_account_info", address))
        if self.fail or "get_account_info" in self.fail_on:
            raise RpcError("mocked getAccountInfo failure")
        if address not in self.accounts:
            return None
        return self.accounts[address]

    def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 20,
        until: str | None = None,
    ) -> list[str]:
        self.calls.append(("get_signatures_for_address", address))
        if self.fail or "get_signatures_for_address" in self.fail_on:
            raise RpcError("mocked getSignaturesForAddress failure")
        _ = until
        if address in self.signatures_by_address:
            return list(self.signatures_by_address[address])[:limit]
        return list(self.signatures)[:limit]

    def get_transaction(self, signature: str) -> dict | None:
        self.calls.append(("get_transaction", signature))
        if self.fail or "get_transaction" in self.fail_on:
            raise RpcError("mocked getTransaction failure")
        if signature not in self.transactions:
            return None
        return self.transactions[signature]

    def get_program_accounts(
        self,
        program_id: str,
        *,
        filters: list | None = None,
    ) -> list[tuple[str, AccountInfo]]:
        self.calls.append(("get_program_accounts", program_id))
        if self.fail or "get_program_accounts" in self.fail_on:
            raise RpcError("mocked getProgramAccounts failure")
        _ = filters
        return list(self.program_accounts.get(program_id, []))

    def get_token_largest_accounts(self, mint: str) -> list[dict]:
        self.calls.append(("get_token_largest_accounts", mint))
        if self.fail or "get_token_largest_accounts" in self.fail_on:
            raise RpcError("mocked getTokenLargestAccounts failure")
        return list(self.token_largest.get(mint, []))


class FakeGraphSource:
    """Deterministic Layer B feed. Incomplete unless complete=True."""

    def __init__(
        self,
        txs: list[ParsedTokenTx] | None = None,
        funders: dict[str, str | None] | None = None,
        *,
        complete: bool = True,
        funder_complete: bool | dict[str, bool] | None = None,
        rpc_partial_failure: bool = False,
        notes: list[str] | None = None,
    ) -> None:
        self.txs = list(txs or [])
        self.funders = dict(funders or {})
        self.complete = complete
        self.funder_complete = funder_complete
        self.rpc_partial_failure = rpc_partial_failure
        self.notes = list(notes or [])

    def fetch_early_txs(self, token: str, limit: int) -> EarlyTxFetch:
        _ = token
        return EarlyTxFetch(
            txs=list(self.txs)[:limit],
            complete=self.complete and not self.rpc_partial_failure,
            rpc_partial_failure=self.rpc_partial_failure,
            notes=list(self.notes),
        )

    def first_inbound_native(self, wallet: str) -> FundingHop:
        if self.rpc_partial_failure:
            return FundingHop(wallet=wallet, funder=None, complete=False)
        funder = self.funders.get(wallet)
        if isinstance(self.funder_complete, dict):
            complete = bool(self.funder_complete.get(wallet, False))
            return FundingHop(wallet=wallet, funder=funder, complete=complete)
        if self.funder_complete is False:
            return FundingHop(wallet=wallet, funder=funder, complete=False)
        return FundingHop(wallet=wallet, funder=funder, complete=bool(self.complete))
