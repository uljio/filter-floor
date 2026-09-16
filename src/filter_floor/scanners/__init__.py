"""Chain → scanner dispatch. Solana and Base Layer A are live; Robinhood is gated."""

from filter_floor.adapters.rpc import SolanaRpc
from filter_floor.models import Chain
from filter_floor.scanners.base import Scanner
from filter_floor.scanners.clanker import ClankerScanner
from filter_floor.scanners.pons import PonsScanner
from filter_floor.scanners.solana import SolanaScanner


def get_scanner(chain: Chain, *, solana_rpc: SolanaRpc | None = None) -> Scanner:
    if chain is Chain.solana:
        return SolanaScanner(rpc=solana_rpc)
    if chain is Chain.base:
        return ClankerScanner.from_chain_config()
    if chain is Chain.robinhood:
        return PonsScanner.from_chain_config()
    raise ValueError(f"unsupported chain: {chain}")
