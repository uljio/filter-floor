"""Optional RugCheck adapter. Off unless RUGCHECK_ENABLED=1. Never required for Layer A."""

from __future__ import annotations

import os

import httpx

DEFAULT_URL_TEMPLATE = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
DEFAULT_TIMEOUT_S = 8.0


def rugcheck_enabled(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get("RUGCHECK_ENABLED", "0") == "1"


def fetch_rugcheck_report(
    mint: str,
    *,
    enabled: bool | None = None,
    http: httpx.Client | None = None,
    url_template: str | None = None,
) -> dict | None:
    """Return a details dict, or None if disabled.

    Failures become a details payload with error + TODO(verify). They must not
    override on-chain mint/freeze FAIL, and they must not invent PASS.
    """
    if enabled is None:
        enabled = rugcheck_enabled()
    if not enabled:
        return None

    template = (
        url_template
        or os.environ.get("RUGCHECK_API_URL", "").strip()
        or DEFAULT_URL_TEMPLATE
    )
    url = template.replace("{mint}", mint)
    owns = http is None
    client = http or httpx.Client(timeout=DEFAULT_TIMEOUT_S)
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "enabled": True,
            "error": str(exc),
            "TODO(verify)": "optional RugCheck adapter missed; core scan unchanged",
        }
    finally:
        if owns:
            client.close()

    if not isinstance(payload, dict):
        return {
            "enabled": True,
            "error": "non-object JSON",
            "TODO(verify)": "RugCheck body not classified; core scan unchanged",
        }

    summary = {
        "enabled": True,
        "score": payload.get("score"),
        "rugged": payload.get("rugged"),
        "tokenMeta": payload.get("tokenMeta"),
        "source": "rugcheck",
    }
    risks = payload.get("risks")
    if isinstance(risks, list):
        names = []
        for item in risks:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str):
                names.append(item)
        summary["risks"] = names
    return summary
