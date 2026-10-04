"""Worst-case risk math. Pure integer functions.

For one user and one unsettled contract with settlement range [lo, hi]:

    value(V, S) = pos*V + sum_{bids in S} q*(V - p) + sum_{asks in S} q*(p - V)

is the cash the position (plus a set S of filled resting orders) adds at settlement
value V. It is linear in V, so its minimum is at an endpoint. At V=lo every bid
hurts and every ask helps, so the worst case fills all bids and no asks; at V=hi
the reverse. Hence

    worst = min(pos*lo + sum_bids q*(lo - p),  pos*hi + sum_asks q*(p - hi))

and available cash = cash + sum over contracts of worst, which is the user's
worst-case cash once everything settles. Orders are rejected if it would go < 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Exposure:
    lo: int
    hi: int
    pos: int = 0
    bids: list[tuple[int, int]] = field(default_factory=list)  # (price, qty)
    asks: list[tuple[int, int]] = field(default_factory=list)


def worst_value(e: Exposure) -> int:
    at_lo = e.pos * e.lo + sum(q * (e.lo - p) for p, q in e.bids)
    at_hi = e.pos * e.hi + sum(q * (p - e.hi) for p, q in e.asks)
    return min(at_lo, at_hi)


def worst_loss(e: Exposure) -> int:
    """Reservation for this contract (negative = guaranteed payout credited)."""
    return -worst_value(e)


def available_cash(cash: int, exposures) -> int:
    return cash + sum(worst_value(e) for e in exposures)
