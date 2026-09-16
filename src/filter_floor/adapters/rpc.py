"""Solana JSON-RPC via httpx. Missing config or transport failure is an error, not PASS."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from filter_floor.adapters.b58 import b58decode

DEFAULT_TIMEOUT_S = 8.0


class RpcError(Exception):
    """Transport, HTTP, or JSON-RPC failure. Callers must map this to UNKNOWN."""


@dataclass(frozen=True)
class AccountInfo:
    owner: str
    data: bytes
    lamports: int = 0
    executable: bool = False


class SolanaRpc:
    """Minimal Solana JSON-RPC client. Inject a fake in tests; do not hit mainnet in CI."""

    def __init__(
        self,
        url: str | None,
        http: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.url = (url or "").strip() or None
        self._http = http
        self._timeout_s = timeout_s
        self._owns_http = http is None

    @classmethod
    def from_env(cls) -> SolanaRpc:
        return cls(url=os.environ.get("SOLANA_RPC_URL", "").strip() or None)

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout_s)
        return self._http

    def _call(self, method: str, params: list) -> object:
        if not self.url:
            raise RpcError("SOLANA_RPC_URL is not set")
        try:
            response = self._client().post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RpcError(f"{method} failed: {exc}") from exc
        if not isinstance(body, dict):
            raise RpcError(f"{method} returned a non-object")
        error = body.get("error")
        if error:
            raise RpcError(f"{method} json-rpc error: {error}")
        return body.get("result")

    def get_account_info(self, address: str) -> AccountInfo | None:
        """Return account bytes, or None if the account does not exist.

        Transport failures raise RpcError (UNKNOWN, never PASS).
        """
        result = self._call(
            "getAccountInfo",
            [address, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not isinstance(result, dict):
            raise RpcError("getAccountInfo: malformed result")
        value = result.get("value")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RpcError("getAccountInfo: malformed value")
        return _parse_account_value(value)

    def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 20,
        until: str | None = None,
    ) -> list[str]:
        opts: dict = {"limit": limit, "commitment": "confirmed"}
        if until:
            opts["until"] = until
        result = self._call("getSignaturesForAddress", [address, opts])
        if result is None:
            return []
        if not isinstance(result, list):
            raise RpcError("getSignaturesForAddress: malformed result")
        signatures: list[str] = []
        for item in result:
            if isinstance(item, dict) and item.get("signature"):
                signatures.append(str(item["signature"]))
            elif isinstance(item, str):
                signatures.append(item)
        return signatures

    def get_transaction(self, signature: str) -> dict | None:
        result = self._call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RpcError("getTransaction: malformed result")
        return result

    def get_program_accounts(
        self,
        program_id: str,
        *,
        filters: list | None = None,
    ) -> list[tuple[str, AccountInfo]]:
        """Return (pubkey, account) pairs. Transport failure raises RpcError."""
        opts: dict = {"encoding": "base64", "commitment": "confirmed"}
        if filters:
            opts["filters"] = filters
        result = self._call("getProgramAccounts", [program_id, opts])
        if result is None:
            return []
        if not isinstance(result, list):
            raise RpcError("getProgramAccounts: malformed result")
        out: list[tuple[str, AccountInfo]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            pubkey = item.get("pubkey")
            account = item.get("account")
            if not isinstance(pubkey, str) or not isinstance(account, dict):
                continue
            out.append((pubkey, _parse_account_value(account)))
        return out

    def get_token_largest_accounts(self, mint: str) -> list[dict]:
        """Largest token accounts for a mint. Transport failure raises RpcError."""
        result = self._call(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        )
        if not isinstance(result, dict):
            raise RpcError("getTokenLargestAccounts: malformed result")
        value = result.get("value")
        if value is None:
            return []
        if not isinstance(value, list):
            raise RpcError("getTokenLargestAccounts: malformed value")
        rows: list[dict] = []
        for item in value:
            if not isinstance(item, dict) or not item.get("address"):
                continue
            rows.append(
                {
                    "address": str(item["address"]),
                    "amount": str(item.get("amount") or "0"),
                }
            )
        return rows


def _parse_account_value(value: dict) -> AccountInfo:
    owner = value.get("owner")
    if not isinstance(owner, str) or not owner:
        raise RpcError("getAccountInfo: missing owner")
    data_field = value.get("data")
    raw = _decode_account_data(data_field)
    lamports = int(value.get("lamports") or 0)
    executable = bool(value.get("executable") or False)
    return AccountInfo(
        owner=owner, data=raw, lamports=lamports, executable=executable
    )


def _decode_account_data(data_field: object) -> bytes:
    if isinstance(data_field, list) and len(data_field) >= 1:
        blob = data_field[0]
        encoding = data_field[1] if len(data_field) > 1 else "base64"
        if not isinstance(blob, str):
            raise RpcError("getAccountInfo: data blob is not a string")
        if encoding == "base64":
            import base64

            try:
                return base64.b64decode(blob)
            except (ValueError, TypeError) as exc:
                raise RpcError("getAccountInfo: invalid base64") from exc
        if encoding == "base58":
            try:
                return b58decode(blob)
            except ValueError as exc:
                raise RpcError("getAccountInfo: invalid base58") from exc
        raise RpcError(f"getAccountInfo: unsupported encoding {encoding}")
    if isinstance(data_field, str):
        import base64

        try:
            return base64.b64decode(data_field)
        except (ValueError, TypeError) as exc:
            raise RpcError("getAccountInfo: invalid base64") from exc
    raise RpcError("getAccountInfo: missing data")
