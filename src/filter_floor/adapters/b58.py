"""Bitcoin/Solana base58 (no extra dependency)."""

from __future__ import annotations

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    if n == 0:
        encoded = ""
    else:
        chars: list[str] = []
        while n:
            n, rem = divmod(n, 58)
            chars.append(ALPHABET[rem])
        encoded = "".join(reversed(chars))
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return ("1" * pad) + encoded


def b58decode(value: str) -> bytes:
    if not value:
        return b""
    n = 0
    for ch in value:
        try:
            n = n * 58 + _INDEX[ch]
        except KeyError as exc:
            raise ValueError(f"invalid base58 character: {ch!r}") from exc
    if n == 0:
        raw = b""
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw
