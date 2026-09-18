"""Solana JSON-RPC via httpx. Missing config or transport failure is an error, not PASS."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx

from filter_floor.adapters.b58 import b58decode

DEFAULT_TIMEOUT_S = 8.0
GPA_TIMEOUT_S = 8.0
GET_TRANSACTION_ATTEMPTS = 3
GET_TRANSACTION_BACKOFF_S = (1.0, 2.0)
RETRYABLE_HTTP = frozenset({429, 503})
RETRYABLE_RPC_CODES = frozenset({-32005})
VERSION_RPC_CODES = frozenset({-32015})

_URL_RE = re.compile(r"https?://\S+", re.I)
_API_KEY_RE = re.compile(r"(api[-_]?key=)[^&\s]+", re.I)


class RpcError(Exception):
    """Transport, HTTP, or JSON-RPC failure. Callers must map this to UNKNOWN.

    Messages must not include the RPC URL or API key.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rpc_code: int | None = None,
    ) -> None:
        super().__init__(sanitize_rpc_text(message))
        self.status_code = status_code
        self.rpc_code = rpc_code


def sanitize_rpc_text(text: str) -> str:
    text = _URL_RE.sub("[rpc]", text)
    return _API_KEY_RE.sub(r"\1[redacted]", text)


def is_retryable_rpc_error(exc: RpcError) -> bool:
    if exc.status_code in RETRYABLE_HTTP:
        return True
    return exc.rpc_code in RETRYABLE_RPC_CODES


def classify_tx_rpc_error(exc: RpcError) -> str:
    """Return ratelimit | version | error. Never includes URL."""
    if is_retryable_rpc_error(exc):
        return "ratelimit"
    blob = f"{exc.rpc_code or ''} {exc}".lower()
    if exc.rpc_code in VERSION_RPC_CODES or (
        "maxsupportedtransactionversion" in blob or "transaction version" in blob
    ):
        return "version"
    return "error"


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
            self._http = httpx.Client(
                timeout=httpx.Timeout(
                    connect=min(5.0, self._timeout_s),
                    read=self._timeout_s,
                    write=self._timeout_s,
                    pool=5.0,
                )
            )
        return self._http

    def _call(self, method: str, params: list, *, timeout_s: float | None = None) -> object:
        if not self.url:
            raise RpcError("SOLANA_RPC_URL is not set")
        timeout = timeout_s if timeout_s is not None else self._timeout_s
        try:
            response = self._client().post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else None
            raise RpcError(f"{method} failed: HTTP {code}", status_code=code) from None
        except (httpx.HTTPError, ValueError):
            raise RpcError(f"{method} failed: transport error") from None
        if not isinstance(body, dict):
            raise RpcError(f"{method} returned a non-object")
        error = body.get("error")
        if error:
            rpc_code = None
            short = "rpc error"
            if isinstance(error, dict):
                raw_code = error.get("code")
                if isinstance(raw_code, int):
                    rpc_code = raw_code
                msg = error.get("message")
                if isinstance(msg, str) and msg.strip():
                    short = sanitize_rpc_text(msg.strip())[:200]
                elif rpc_code is not None:
                    short = f"code {rpc_code}"
            raise RpcError(
                f"{method} json-rpc error: {short}",
                rpc_code=rpc_code,
            )
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

    def get_transaction(self, signature: str, *, sleep_fn=time.sleep) -> dict | None:
        last: RpcError | None = None
        for attempt in range(GET_TRANSACTION_ATTEMPTS):
            try:
                result = self._call(
                    "getTransaction",
                    [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 1,
                        },
                    ],
                )
            except RpcError as exc:
                last = exc
                if attempt < GET_TRANSACTION_ATTEMPTS - 1 and is_retryable_rpc_error(exc):
                    delay = GET_TRANSACTION_BACKOFF_S[
                        min(attempt, len(GET_TRANSACTION_BACKOFF_S) - 1)
                    ]
                    sleep_fn(delay)
                    continue
                raise
            if result is None:
                return None
            if not isinstance(result, dict):
                raise RpcError("getTransaction: malformed result")
            return result
        if last is not None:
            raise last
        return None

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
        result = self._call(
            "getProgramAccounts",
            [program_id, opts],
            timeout_s=GPA_TIMEOUT_S,
        )
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
