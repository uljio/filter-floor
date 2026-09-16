"""Solana Layer A: mint/freeze/supply/decimals/Token-2022. Layer B runs in the pipeline graph."""

from __future__ import annotations

from solders.pubkey import Pubkey

from filter_floor.adapters.rpc import RpcError, SolanaRpc
from filter_floor.adapters.rugcheck import fetch_rugcheck_report
from filter_floor.adapters.spl_mint import ParsedMint, parse_mint_account
from filter_floor.models import CheckStatus, LayerA, LayerB
from filter_floor.scanners.base import Scanner, unknown_layer_a, unknown_layer_b
from filter_floor.scanners.pumpfun import read_bonding_curve

LAYER_A_TODO = (
    "M1: lp_locked_or_burned and honeypot_or_unsellable not fetched "
    "(no Raydium lock / sell sim). UNKNOWN, never PASS."
)

METADATA_PROGRAM_ID = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


def scan_solana_layer_a(
    token: str,
    *,
    rpc: SolanaRpc | None = None,
    pump_hint: bool = False,
    rugcheck_http=None,
) -> LayerA:
    token = token.strip()
    details: dict = {"token": token, "TODO(verify)": LAYER_A_TODO}
    if pump_hint:
        details["sources"] = ["solana", "pumpfun"]
        details["pump_hint"] = True

    try:
        Pubkey.from_string(token)
    except Exception:
        details["TODO(verify)"] = "invalid Solana pubkey; Layer A not fetched"
        return unknown_layer_a(details)

    client = rpc or SolanaRpc.from_env()
    try:
        info = client.get_account_info(token)
    except RpcError as exc:
        details["rpc_partial_failure"] = True
        details["rpc_error"] = str(exc)
        details["TODO(verify)"] = f"RPC miss: {exc}"
        return unknown_layer_a(details)

    if info is None:
        details["account_missing"] = True
        details["TODO(verify)"] = "mint account missing; Layer A UNKNOWN, never PASS"
        return unknown_layer_a(details)

    parsed = parse_mint_account(info.owner, info.data)
    _apply_parsed_details(details, parsed)

    pump = read_bonding_curve(client, token)
    if pump.get("rpc_partial_failure"):
        details["rpc_partial_failure"] = True
    if pump.get("is_pumpfun"):
        details["is_pumpfun"] = True
        details["bonding_curve"] = pump.get("bonding_curve")
        details["bonding_curve_inventory"] = pump.get("bonding_curve_inventory")
        details["pumpfun_complete"] = pump.get("complete")
        if pump.get("creator"):
            details["creator"] = pump["creator"]
        sources = list(details.get("sources") or ["solana"])
        if "pumpfun" not in sources:
            sources.append("pumpfun")
        details["sources"] = sources
        for key in (
            "virtual_token_reserves",
            "virtual_sol_reserves",
            "real_token_reserves",
            "real_sol_reserves",
            "token_total_supply",
        ):
            if key in pump:
                details[key] = pump[key]
    elif pump_hint:
        details["is_pumpfun"] = pump.get("is_pumpfun", False)
        details["bonding_curve"] = pump.get("bonding_curve")
        details["bonding_curve_inventory"] = pump.get("bonding_curve_inventory") or (
            "pump hint set but curve unread; caution, not auto-AVOID"
        )
        if pump.get("TODO(verify)"):
            details["pump_TODO(verify)"] = pump["TODO(verify)"]

    meta_note = _try_metadata_mutability(client, token)
    details.update(meta_note)

    rug = fetch_rugcheck_report(token, http=rugcheck_http)
    if rug is not None:
        details["rugcheck"] = rug
        sources = list(details.get("sources") or ["solana"])
        if "rugcheck" not in sources:
            sources.append("rugcheck")
        details["sources"] = sources

    if parsed.program_kind == "unknown" or parsed.mint_authority_status is CheckStatus.UNKNOWN:
        # Not a mint we can classify: keep unread mechanical checks UNKNOWN.
        if parsed.supply is None:
            return unknown_layer_a(details)

    return LayerA(
        mint_authority_revoked=parsed.mint_authority_status,
        freeze_authority_revoked=parsed.freeze_authority_status,
        lp_locked_or_burned=CheckStatus.UNKNOWN,
        honeypot_or_unsellable=CheckStatus.UNKNOWN,
        owner_or_upgrade_risk=parsed.owner_or_upgrade_status,
        token2022_or_hook_risk=parsed.token2022_status,
        details=details,
    )


def _apply_parsed_details(details: dict, parsed: ParsedMint) -> None:
    details["token_program"] = parsed.owner
    details["program_kind"] = parsed.program_kind
    if parsed.supply is not None:
        details["supply"] = str(parsed.supply)
    if parsed.decimals is not None:
        details["decimals"] = parsed.decimals
    details["mint_authority"] = parsed.mint_authority
    details["freeze_authority"] = parsed.freeze_authority
    if parsed.extensions:
        details["token2022_extensions"] = parsed.extensions
    if parsed.dangerous_extensions:
        details["token2022_dangerous_extensions"] = parsed.dangerous_extensions
    if parsed.parse_notes:
        details["mint_parse_notes"] = parsed.parse_notes
        # Keep a top-level TODO(verify) if the parser produced one.
        todos = [n for n in parsed.parse_notes if n.startswith("TODO(verify)")]
        if todos:
            details["TODO(verify)"] = "; ".join(todos + [LAYER_A_TODO])


def _try_metadata_mutability(rpc: SolanaRpc, mint: str) -> dict:
    """Cheap Metaplex is_mutable read. Parse failure is a detail, not a PASS/FAIL guess."""
    try:
        mint_pk = Pubkey.from_string(mint)
        program = Pubkey.from_string(METADATA_PROGRAM_ID)
        pda, _ = Pubkey.find_program_address(
            [b"metadata", bytes(program), bytes(mint_pk)],
            program,
        )
        info = rpc.get_account_info(str(pda))
    except (RpcError, Exception) as exc:
        return {
            "metadata_pda_error": str(exc),
            "metadata_mutable": None,
            "metadata_TODO(verify)": "metadata mutability not read",
        }
    if info is None:
        return {
            "metadata_pda": str(pda),
            "metadata_mutable": None,
            "metadata_TODO(verify)": "metadata account missing",
        }
    mutable = _parse_metaplex_is_mutable(info.data)
    out = {"metadata_pda": str(pda), "metadata_mutable": mutable}
    if mutable is None:
        out["metadata_TODO(verify)"] = "metadata layout not parsed; not guessing PASS"
    return out


def _parse_metaplex_is_mutable(data: bytes) -> bool | None:
    # key u8 + update_authority 32 + mint 32 + Data {name, symbol, uri} +
    # seller_fee_basis_points u16 + creators COption + primary_sale_happened + is_mutable
    if len(data) < 65:
        return None
    offset = 1 + 32 + 32
    for _ in range(3):
        parsed = _borsh_string(data, offset)
        if parsed is None:
            return None
        _s, offset = parsed
    if offset + 2 > len(data):
        return None
    offset += 2  # seller_fee_basis_points
    if offset >= len(data):
        return None
    option = data[offset]
    offset += 1
    if option == 1:
        if offset + 4 > len(data):
            return None
        count = int.from_bytes(data[offset : offset + 4], "little")
        offset += 4
        # creator: pubkey 32 + verified u8 + share u8
        skip = count * 34
        if offset + skip > len(data):
            return None
        offset += skip
    elif option not in (0, 1):
        return None
    if offset + 2 > len(data):
        return None
    # primary_sale_happened, is_mutable
    mutable = data[offset + 1]
    if mutable not in (0, 1):
        return None
    return bool(mutable)


def _borsh_string(data: bytes, offset: int) -> tuple[bytes, int] | None:
    if offset + 4 > len(data):
        return None
    n = int.from_bytes(data[offset : offset + 4], "little")
    offset += 4
    if n > 1024 or offset + n > len(data):
        return None
    return data[offset : offset + n], offset + n


class SolanaScanner(Scanner):
    name = "solana"

    def __init__(self, rpc: SolanaRpc | None = None, rugcheck_http=None) -> None:
        self.rpc = rpc
        self.rugcheck_http = rugcheck_http

    def scan_layer_a(self, token: str) -> LayerA:
        return scan_solana_layer_a(
            token, rpc=self.rpc, pump_hint=False, rugcheck_http=self.rugcheck_http
        )

    def scan_layer_b(self, token: str) -> LayerB:
        # Pipeline runs filter_floor.graph after Layer A; scanner method stays UNKNOWN.
        return unknown_layer_b(
            {
                "token": token,
                "TODO(verify)": "Layer B is computed in the pipeline graph, not the scanner",
            }
        )
