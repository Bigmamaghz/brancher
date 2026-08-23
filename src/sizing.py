from __future__ import annotations


def qty_for_hit(
    hit: float,
    base: int,
    max_mult: int,
    min_hit: float = 0.75,
) -> int:
    """Linear sizing: 75% hit = 1× base, 100% hit = max_mult× base."""
    if hit < min_hit:
        return 0
    if hit >= 1.0:
        return base * max_mult
    t = (hit - min_hit) / (1.0 - min_hit)
    mult = 1 + t * (max_mult - 1)
    return max(1, round(base * mult))
