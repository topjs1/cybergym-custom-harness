from __future__ import annotations

import random

BOUNDARY_BYTES = (0x00, 0x01, 0x7F, 0x80, 0xFE, 0xFF)
_STRATEGIES = (
    "boundary_byte",
    "zero_byte",
    "max_byte",
    "increment",
    "decrement",
    "bit_flip",
    "dword_boundary",
    "swap_pair",
)


def _trigger_positions(length: int, rng: random.Random, count: int) -> list[int]:
    if length <= 0:
        return []
    start = max(0, length - max(1, length // 3))
    region = list(range(start, length))
    rng.shuffle(region)
    chosen = region[:count]
    if len(chosen) < count:
        extra = list(range(0, start))
        rng.shuffle(extra)
        chosen.extend(extra[: count - len(chosen)])
    return chosen


def generate_mutations(
    data: bytes,
    *,
    max_mutations: int = 8,
    seed: int = 0,
) -> list[tuple[bytes, str]]:
    """Deterministic, zero-token byte mutations focused on the trigger region."""
    if not data or max_mutations <= 0:
        return []
    rng = random.Random(seed)
    buffer = bytearray(data)
    positions = _trigger_positions(len(buffer), rng, max_mutations)
    results: list[tuple[bytes, str]] = []
    seen: set[bytes] = {bytes(buffer)}
    for index, position in enumerate(positions):
        strategy = _STRATEGIES[index % len(_STRATEGIES)]
        candidate = bytearray(buffer)
        explanation = _apply(candidate, position, strategy, rng)
        if explanation is None:
            continue
        blob = bytes(candidate)
        if blob in seen:
            continue
        seen.add(blob)
        results.append((blob, f"{strategy} at offset {position}: {explanation}"))
        if len(results) >= max_mutations:
            break
    return results


def _apply(buffer: bytearray, position: int, strategy: str, rng: random.Random) -> str | None:
    original = buffer[position]
    if strategy == "boundary_byte":
        new = BOUNDARY_BYTES[rng.randrange(len(BOUNDARY_BYTES))]
        buffer[position] = new
        return f"{original:#04x}->{new:#04x}"
    if strategy == "zero_byte":
        buffer[position] = 0x00
        return f"{original:#04x}->0x00"
    if strategy == "max_byte":
        buffer[position] = 0xFF
        return f"{original:#04x}->0xff"
    if strategy == "increment":
        buffer[position] = (original + 1) & 0xFF
        return "increment"
    if strategy == "decrement":
        buffer[position] = (original - 1) & 0xFF
        return "decrement"
    if strategy == "bit_flip":
        buffer[position] = original ^ (1 << rng.randrange(8))
        return "bit_flip"
    if strategy == "dword_boundary":
        if position + 4 <= len(buffer):
            buffer[position:position + 4] = (0xFF).to_bytes(4, "little")
            return "dword->0xffffffff"
        buffer[position] = 0xFF
        return "byte->0xff"
    if strategy == "swap_pair" and position + 1 < len(buffer):
        buffer[position], buffer[position + 1] = buffer[position + 1], buffer[position]
        return "swap_pair"
    return None
