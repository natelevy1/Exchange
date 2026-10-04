"""In-memory limit order book with price-time priority.

Pure data structure: no I/O, no locking, no risk. Matching is split into
`plan` (read-only: what would happen) and `apply` (mutate the book), so the
caller can risk-check and persist the outcome before committing it.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

BUY = "buy"
SELL = "sell"


@dataclass
class Order:
    id: int  # monotonically increasing; doubles as time priority
    user_id: int
    side: str
    price: int
    remaining: int


@dataclass
class Fill:
    resting_id: int
    resting_user_id: int
    price: int  # always the resting order's price
    qty: int


@dataclass
class MatchPlan:
    side: str
    price: int
    qty: int
    user_id: int
    fills: list[Fill] = field(default_factory=list)
    self_cancels: list[int] = field(default_factory=list)  # resting ids cancelled by STP

    @property
    def filled(self) -> int:
        return sum(f.qty for f in self.fills)

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


class OrderBook:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        # price -> FIFO list of resting orders
        self._levels: dict[str, dict[int, list[Order]]] = {BUY: {}, SELL: {}}
        # ascending price lists per side
        self._prices: dict[str, list[int]] = {BUY: [], SELL: []}

    # ---- queries -------------------------------------------------------
    def best_bid(self) -> int | None:
        p = self._prices[BUY]
        return p[-1] if p else None

    def best_ask(self) -> int | None:
        p = self._prices[SELL]
        return p[0] if p else None

    def levels(self, side: str) -> list[tuple[int, int]]:
        """Aggregated (price, qty), best price first."""
        prices = self._prices[side]
        ordered = reversed(prices) if side == BUY else prices
        return [(p, sum(o.remaining for o in self._levels[side][p])) for p in ordered]

    def user_orders(self, user_id: int) -> list[Order]:
        return [o for o in self.orders.values() if o.user_id == user_id]

    def _opposite_in_priority(self, side: str):
        """Yield resting orders on the side opposite `side`, best price first, then FIFO."""
        opp = SELL if side == BUY else BUY
        prices = self._prices[opp]
        ordered = list(prices) if opp == SELL else list(reversed(prices))
        for p in ordered:
            for o in self._levels[opp][p]:
                yield o

    # ---- matching ------------------------------------------------------
    def plan(self, side: str, price: int, qty: int, user_id: int) -> MatchPlan:
        """Compute fills and self-trade cancels for an incoming order without mutating."""
        if side not in (BUY, SELL):
            raise ValueError("side must be buy or sell")
        plan = MatchPlan(side=side, price=price, qty=qty, user_id=user_id)
        left = qty
        for o in self._opposite_in_priority(side):
            if left == 0:
                break
            crosses = o.price <= price if side == BUY else o.price >= price
            if not crosses:
                break
            if o.user_id == user_id:
                plan.self_cancels.append(o.id)
                continue
            q = min(left, o.remaining)
            plan.fills.append(Fill(o.id, o.user_id, o.price, q))
            left -= q
        return plan

    def apply(self, plan: MatchPlan, rest_id: int | None = None) -> Order | None:
        """Apply a plan produced by `plan` (with no book changes in between).

        If `rest_id` is given and quantity remains, the remainder rests under that id.
        """
        for oid in plan.self_cancels:
            self.cancel(oid)
        for f in plan.fills:
            o = self.orders[f.resting_id]
            o.remaining -= f.qty
            if o.remaining == 0:
                self._remove(o)
        if rest_id is not None and plan.remaining > 0:
            o = Order(rest_id, plan.user_id, plan.side, plan.price, plan.remaining)
            self.add_resting(o)
            return o
        return None

    def submit(self, order_id: int, side: str, price: int, qty: int, user_id: int,
               ioc: bool = False) -> MatchPlan:
        """Convenience: plan + apply. Limit remainder rests; IOC remainder is dropped."""
        plan = self.plan(side, price, qty, user_id)
        self.apply(plan, None if ioc else order_id)
        return plan

    # ---- maintenance ---------------------------------------------------
    def add_resting(self, o: Order) -> None:
        if o.id in self.orders:
            raise ValueError(f"duplicate order id {o.id}")
        lvl = self._levels[o.side]
        if o.price not in lvl:
            lvl[o.price] = []
            bisect.insort(self._prices[o.side], o.price)
        q = lvl[o.price]
        # keep FIFO by id even when loading out of order
        if q and q[-1].id > o.id:
            bisect.insort(q, o, key=lambda x: x.id)
        else:
            q.append(o)
        self.orders[o.id] = o

    def cancel(self, order_id: int) -> Order:
        o = self.orders[order_id]
        self._remove(o)
        return o

    def _remove(self, o: Order) -> None:
        del self.orders[o.id]
        q = self._levels[o.side][o.price]
        q.remove(o)
        if not q:
            del self._levels[o.side][o.price]
            prices = self._prices[o.side]
            prices.pop(bisect.bisect_left(prices, o.price))
