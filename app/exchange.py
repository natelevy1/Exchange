"""Exchange service: the only place state changes.

Every mutating method takes `self.lock`, validates, computes the outcome without
touching memory, writes it to SQLite in a single transaction, and only after the
commit applies it to the in-memory books/cash/positions. A failed write therefore
leaves memory untouched.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db
from .engine import BUY, SELL, Order, OrderBook
from .risk import Exposure, available_cash, worst_value

MAX_QTY = 1_000_000
MAX_ABS_PRICE = 1_000_000_000
TAPE_LEN = 50


class ExchangeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Session:
    id: int
    code: str
    starting_cash: int
    status: str
    created_at: int
    ended_at: int | None


@dataclass
class User:
    id: int
    session_id: int
    name: str
    token: str
    cash: int
    created_at: int


@dataclass
class Contract:
    id: int
    session_id: int
    name: str
    description: str
    kind: str
    lo: int
    hi: int
    tick: int
    state: str
    settlement_value: int | None
    created_at: int
    settled_at: int | None


def _int(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ExchangeError(f"{what} must be an integer")
    return v


def validate_spec(kind: str, lo, hi, tick) -> tuple[int, int, int]:
    tick = _int(tick, "Tick size")
    if tick < 1:
        raise ExchangeError("Tick size must be at least 1")
    if kind == "binary":
        lo, hi = 0, 100
    elif kind == "numeric":
        lo, hi = _int(lo, "Min"), _int(hi, "Max")
        if not lo < hi:
            raise ExchangeError("Min must be less than max")
        if abs(lo) > MAX_ABS_PRICE or abs(hi) > MAX_ABS_PRICE:
            raise ExchangeError("Range is too large")
    else:
        raise ExchangeError("Type must be binary or numeric")
    if (hi - lo) % tick:
        raise ExchangeError(f"Range width {hi - lo} is not a multiple of tick size {tick}")
    return lo, hi, tick


class Exchange:
    def __init__(self, db_path: str):
        self.lock = threading.RLock()
        self.conn = db.connect(db_path)
        self.listeners: list = []  # callables(session_id) invoked after each change
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        c = self.conn
        self.sessions = {r["id"]: Session(**dict(r)) for r in c.execute("SELECT * FROM sessions")}
        self.users = {r["id"]: User(**dict(r)) for r in c.execute("SELECT * FROM users")}
        self.tokens = {u.token: u.id for u in self.users.values()}
        self.contracts = {}
        for r in c.execute("SELECT * FROM contracts"):
            d = dict(r)
            d["lo"], d["hi"] = d.pop("min_value"), d.pop("max_value")
            self.contracts[d["id"]] = Contract(**d)
        self.books = {cid: OrderBook() for cid in self.contracts}
        self.order_contract: dict[int, int] = {}
        for r in c.execute("SELECT * FROM orders WHERE status = 'open' ORDER BY id"):
            self.books[r["contract_id"]].add_resting(
                Order(r["id"], r["user_id"], r["side"], r["price"], r["remaining"]))
            self.order_contract[r["id"]] = r["contract_id"]
        self.positions = {(r["user_id"], r["contract_id"]): r["qty"]
                          for r in c.execute("SELECT * FROM positions WHERE qty != 0")}
        self.settled = {(r["user_id"], r["contract_id"]): (r["qty"], r["amount"])
                        for r in c.execute("SELECT * FROM settlements")}
        self.tape: dict[int, deque] = {cid: deque(maxlen=TAPE_LEN) for cid in self.contracts}
        for cid in self.contracts:
            rows = c.execute("SELECT * FROM trades WHERE contract_id = ? ORDER BY id DESC LIMIT ?",
                             (cid, TAPE_LEN)).fetchall()
            for r in reversed(rows):
                self.tape[cid].append(self._tape_row(r["id"], r["price"], r["qty"],
                                                     r["aggressor_side"], r["created_at"]))
        self.announcements: dict[int, list] = {sid: [] for sid in self.sessions}
        for r in c.execute("SELECT * FROM announcements ORDER BY id"):
            self.announcements[r["session_id"]].append(
                {"id": r["id"], "text": r["text"], "ts": r["created_at"]})
        row = c.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()
        self.next_order_id = row[0] + 1

    @staticmethod
    def _tape_row(tid, price, qty, side, ts):
        return {"id": tid, "price": price, "qty": qty, "side": side, "ts": ts}

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _changed(self, session_id: int) -> None:
        for fn in self.listeners:
            fn(session_id)

    # -------------------------------------------------------------- sessions
    def active_session(self) -> Session | None:
        return next((s for s in self.sessions.values() if s.status == "active"), None)

    def create_session(self, code: str, starting_cash: int = 10_000) -> Session:
        code = (code or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{2,20}", code):
            raise ExchangeError("Join code must be 2-20 letters, digits or dashes")
        starting_cash = _int(starting_cash, "Starting cash")
        if not 0 < starting_cash <= MAX_ABS_PRICE:
            raise ExchangeError("Starting cash must be positive")
        with self.lock:
            if self.active_session():
                raise ExchangeError("End the current session before starting a new one")
            if any(s.code == code for s in self.sessions.values()):
                raise ExchangeError("That join code has been used before; pick another")
            ts = now_ms()
            with self._tx() as c:
                cur = c.execute("INSERT INTO sessions (code, starting_cash, status, created_at) "
                                "VALUES (?, ?, 'active', ?)", (code, starting_cash, ts))
            s = Session(cur.lastrowid, code, starting_cash, "active", ts, None)
            self.sessions[s.id] = s
            self.announcements[s.id] = []
        self._changed(s.id)
        return s

    def end_session(self, session_id: int) -> None:
        with self.lock:
            s = self._session(session_id)
            if s.status != "active":
                raise ExchangeError("Session already ended")
            live = [c.name for c in self._session_contracts(s.id) if c.state in ("open", "halted")]
            if live:
                raise ExchangeError("Settle these contracts before ending the session: "
                                    + ", ".join(live))
            ts = now_ms()
            with self._tx() as c:
                c.execute("UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ?",
                          (ts, s.id))
            s.status, s.ended_at = "ended", ts
        self._changed(s.id)

    def _session(self, session_id: int) -> Session:
        s = self.sessions.get(session_id)
        if not s:
            raise ExchangeError("No such session", 404)
        return s

    def _session_contracts(self, session_id: int) -> list[Contract]:
        return sorted((c for c in self.contracts.values() if c.session_id == session_id),
                      key=lambda c: c.id)

    def _session_users(self, session_id: int) -> list[User]:
        return sorted((u for u in self.users.values() if u.session_id == session_id),
                      key=lambda u: u.id)

    # ----------------------------------------------------------------- users
    def join(self, code: str, name: str) -> User:
        code = (code or "").strip().upper()
        name = " ".join((name or "").split())
        if not 1 <= len(name) <= 24:
            raise ExchangeError("Name must be 1-24 characters")
        with self.lock:
            s = next((s for s in self.sessions.values() if s.code == code), None)
            if not s:
                raise ExchangeError("Unknown join code", 404)
            if s.status != "active":
                raise ExchangeError("That session has ended")
            if any(u.name.lower() == name.lower() for u in self._session_users(s.id)):
                raise ExchangeError("That name is taken in this session")
            token = secrets.token_urlsafe(24)
            ts = now_ms()
            with self._tx() as c:
                cur = c.execute("INSERT INTO users (session_id, name, token, cash, created_at) "
                                "VALUES (?, ?, ?, ?, ?)", (s.id, name, token, s.starting_cash, ts))
            u = User(cur.lastrowid, s.id, name, token, s.starting_cash, ts)
            self.users[u.id] = u
            self.tokens[token] = u.id
        self._changed(s.id)
        return u

    def user_by_token(self, token: str | None) -> User | None:
        uid = self.tokens.get(token or "")
        return self.users.get(uid) if uid else None

    # ------------------------------------------------------------- contracts
    def create_contract(self, session_id: int, name: str, description: str = "",
                        kind: str = "binary", lo=None, hi=None, tick=1) -> Contract:
        name = (name or "").strip()
        if not 1 <= len(name) <= 40:
            raise ExchangeError("Contract name must be 1-40 characters")
        description = (description or "").strip()
        lo, hi, tick = validate_spec(kind, lo, hi, tick)
        with self.lock:
            s = self._session(session_id)
            if s.status != "active":
                raise ExchangeError("Session has ended")
            ts = now_ms()
            with self._tx() as c:
                cur = c.execute(
                    "INSERT INTO contracts (session_id, name, description, kind, min_value, "
                    "max_value, tick, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (s.id, name, description, kind, lo, hi, tick, ts))
            ct = Contract(cur.lastrowid, s.id, name, description, kind, lo, hi, tick,
                          "pending", None, ts, None)
            self.contracts[ct.id] = ct
            self.books[ct.id] = OrderBook()
            self.tape[ct.id] = deque(maxlen=TAPE_LEN)
        self._changed(s.id)
        return ct

    def _contract(self, contract_id: int) -> Contract:
        ct = self.contracts.get(contract_id)
        if not ct:
            raise ExchangeError("No such contract", 404)
        return ct

    def edit_contract(self, contract_id: int, **fields) -> Contract:
        """Name/description editable until settled; type/range/tick only while pending."""
        with self.lock:
            ct = self._contract(contract_id)
            if ct.state == "settled" or self.sessions[ct.session_id].status != "active":
                raise ExchangeError("This contract can no longer be edited")
            name = fields.get("name", ct.name)
            name = (name or "").strip()
            if not 1 <= len(name) <= 40:
                raise ExchangeError("Contract name must be 1-40 characters")
            description = (fields.get("description", ct.description) or "").strip()
            kind = fields.get("kind", ct.kind)
            lo, hi, tick = validate_spec(kind, fields.get("lo", ct.lo), fields.get("hi", ct.hi),
                                         fields.get("tick", ct.tick))
            if (kind, lo, hi, tick) != (ct.kind, ct.lo, ct.hi, ct.tick) and ct.state != "pending":
                raise ExchangeError("Type, range and tick can only change while pending")
            with self._tx() as c:
                c.execute("UPDATE contracts SET name=?, description=?, kind=?, min_value=?, "
                          "max_value=?, tick=? WHERE id=?",
                          (name, description, kind, lo, hi, tick, ct.id))
            ct.name, ct.description, ct.kind, ct.lo, ct.hi, ct.tick = (
                name, description, kind, lo, hi, tick)
        self._changed(ct.session_id)
        return ct

    def set_contract_state(self, contract_id: int, state: str) -> Contract:
        allowed = {"open": ("pending", "halted"), "halted": ("open",)}
        if state not in allowed:
            raise ExchangeError("State must be open or halted (use settle to settle)")
        with self.lock:
            ct = self._contract(contract_id)
            if self.sessions[ct.session_id].status != "active":
                raise ExchangeError("Session has ended")
            if ct.state not in allowed[state]:
                raise ExchangeError(f"Cannot go from {ct.state} to {state}")
            with self._tx() as c:
                c.execute("UPDATE contracts SET state = ? WHERE id = ?", (state, ct.id))
            ct.state = state
        self._changed(ct.session_id)
        return ct

    def settle(self, contract_id: int, value) -> Contract:
        value = _int(value, "Settlement value")
        with self.lock:
            ct = self._contract(contract_id)
            if ct.state not in ("open", "halted"):
                raise ExchangeError(f"Cannot settle a {ct.state} contract")
            if not ct.lo <= value <= ct.hi:
                raise ExchangeError(f"Settlement value must be within [{ct.lo}, {ct.hi}]")
            book = self.books[ct.id]
            resting = list(book.orders)
            holders = [(uid, q) for (uid, cid), q in self.positions.items()
                       if cid == ct.id and q != 0]
            ts = now_ms()
            with self._tx() as c:
                c.executemany("UPDATE orders SET status='cancelled', cancel_reason='settled', "
                              "updated_at=? WHERE id=?", [(ts, oid) for oid in resting])
                for uid, q in holders:
                    c.execute("UPDATE users SET cash = cash + ? WHERE id = ?", (q * value, uid))
                    c.execute("INSERT INTO settlements (contract_id, user_id, qty, amount) "
                              "VALUES (?, ?, ?, ?)", (ct.id, uid, q, q * value))
                    c.execute("UPDATE positions SET qty = 0 WHERE user_id = ? AND contract_id = ?",
                              (uid, ct.id))
                c.execute("UPDATE contracts SET state='settled', settlement_value=?, settled_at=? "
                          "WHERE id=?", (value, ts, ct.id))
            for oid in resting:
                book.cancel(oid)
                del self.order_contract[oid]
            for uid, q in holders:
                self.users[uid].cash += q * value
                self.settled[(uid, ct.id)] = (q, q * value)
                del self.positions[(uid, ct.id)]
            ct.state, ct.settlement_value, ct.settled_at = "settled", value, ts
        self._changed(ct.session_id)
        return ct

    # ---------------------------------------------------------- announcements
    def announce(self, session_id: int, text: str) -> dict:
        text = (text or "").strip()
        if not 1 <= len(text) <= 500:
            raise ExchangeError("Announcement must be 1-500 characters")
        with self.lock:
            s = self._session(session_id)
            if s.status != "active":
                raise ExchangeError("Session has ended")
            ts = now_ms()
            with self._tx() as c:
                cur = c.execute("INSERT INTO announcements (session_id, text, created_at) "
                                "VALUES (?, ?, ?)", (s.id, text, ts))
            a = {"id": cur.lastrowid, "text": text, "ts": ts}
            self.announcements[s.id].append(a)
        self._changed(s.id)
        return a

    # ------------------------------------------------------------------ risk
    def _exposure(self, uid: int, ct: Contract) -> Exposure:
        e = Exposure(ct.lo, ct.hi, self.positions.get((uid, ct.id), 0))
        for o in self.books[ct.id].user_orders(uid):
            (e.bids if o.side == BUY else e.asks).append((o.price, o.remaining))
        return e

    def _exposures(self, uid: int, skip: int | None = None) -> list[Exposure]:
        u = self.users[uid]
        return [self._exposure(uid, ct) for ct in self._session_contracts(u.session_id)
                if ct.state != "settled" and ct.id != skip]

    def available(self, uid: int) -> int:
        with self.lock:
            return available_cash(self.users[uid].cash, self._exposures(uid))

    # ---------------------------------------------------------------- orders
    def place_order(self, uid: int, contract_id: int, side: str, price, qty,
                    tif: str = "limit") -> dict:
        if side not in (BUY, SELL):
            raise ExchangeError("Side must be buy or sell")
        if tif not in ("limit", "ioc"):
            raise ExchangeError("Order type must be limit or ioc")
        price, qty = _int(price, "Price"), _int(qty, "Quantity")
        if not 1 <= qty <= MAX_QTY:
            raise ExchangeError(f"Quantity must be between 1 and {MAX_QTY}")
        with self.lock:
            u = self.users.get(uid)
            if not u:
                raise ExchangeError("Unknown user", 401)
            if self.sessions[u.session_id].status != "active":
                raise ExchangeError("Session has ended")
            ct = self.contracts.get(contract_id)
            if not ct or ct.session_id != u.session_id:
                raise ExchangeError("No such contract", 404)
            if ct.state != "open":
                raise ExchangeError(f"{ct.name} is {ct.state}; orders are only accepted while open")
            if not ct.lo <= price <= ct.hi or (price - ct.lo) % ct.tick:
                raise ExchangeError(f"Price must be in [{ct.lo}, {ct.hi}] on a tick of {ct.tick}")

            book = self.books[ct.id]
            plan = book.plan(side, price, qty, uid)
            sign = 1 if side == BUY else -1
            notional = sum(f.price * f.qty for f in plan.fills)
            rest = plan.remaining if tif == "limit" else 0

            # Risk: the state that would exist after accepting this order.
            cancelled = set(plan.self_cancels)
            e = Exposure(ct.lo, ct.hi, self.positions.get((uid, ct.id), 0) + sign * plan.filled)
            for o in book.user_orders(uid):
                if o.id not in cancelled:
                    (e.bids if o.side == BUY else e.asks).append((o.price, o.remaining))
            if rest:
                (e.bids if side == BUY else e.asks).append((price, rest))
            others = self._exposures(uid, skip=ct.id)
            after = available_cash(u.cash - sign * notional, others + [e])
            if after < 0:
                before = available_cash(u.cash, others + [self._exposure(uid, ct)])
                raise ExchangeError(
                    f"Rejected: not enough available cash. This order needs {before - after:,} "
                    f"in worst-case margin but you have {before:,} available.")

            # Persist.
            oid = self.next_order_id
            ts = now_ms()
            if plan.remaining == 0:
                status, reason = "filled", None
            elif tif == "limit":
                status, reason = "open", None
            else:
                status, reason = "cancelled", "ioc_remainder"
            cash_delta: dict[int, int] = {uid: -sign * notional}
            pos_delta: dict[int, int] = {uid: sign * plan.filled}
            trades = []
            with self._tx() as c:
                c.execute("INSERT INTO orders (id, session_id, contract_id, user_id, side, tif, "
                          "price, qty, remaining, status, cancel_reason, created_at, updated_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (oid, u.session_id, ct.id, uid, side, tif, price, qty, plan.remaining,
                           status, reason, ts, ts))
                c.executemany("UPDATE orders SET status='cancelled', cancel_reason='self_trade', "
                              "updated_at=? WHERE id=?", [(ts, x) for x in plan.self_cancels])
                for f in plan.fills:
                    left = book.orders[f.resting_id].remaining - f.qty
                    c.execute("UPDATE orders SET remaining=?, status=?, updated_at=? WHERE id=?",
                              (left, "filled" if left == 0 else "open", ts, f.resting_id))
                    buyer, seller = (uid, f.resting_user_id) if side == BUY else (f.resting_user_id, uid)
                    bo, so = (oid, f.resting_id) if side == BUY else (f.resting_id, oid)
                    cur = c.execute(
                        "INSERT INTO trades (session_id, contract_id, price, qty, buyer_id, "
                        "seller_id, buy_order_id, sell_order_id, aggressor_side, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (u.session_id, ct.id, f.price, f.qty, buyer, seller, bo, so, side, ts))
                    trades.append(self._tape_row(cur.lastrowid, f.price, f.qty, side, ts))
                    cash_delta[f.resting_user_id] = cash_delta.get(f.resting_user_id, 0) + sign * f.price * f.qty
                    pos_delta[f.resting_user_id] = pos_delta.get(f.resting_user_id, 0) - sign * f.qty
                for k, d in cash_delta.items():
                    if d:
                        c.execute("UPDATE users SET cash = cash + ? WHERE id = ?", (d, k))
                for k, d in pos_delta.items():
                    if d:
                        c.execute("INSERT INTO positions (user_id, contract_id, qty) VALUES (?, ?, ?) "
                                  "ON CONFLICT (user_id, contract_id) DO UPDATE SET qty = qty + excluded.qty",
                                  (k, ct.id, d))

            # Apply to memory (after commit).
            self.next_order_id += 1
            filled_resting = [f.resting_id for f in plan.fills
                              if book.orders[f.resting_id].remaining == f.qty]
            book.apply(plan, oid if tif == "limit" else None)
            for x in plan.self_cancels + filled_resting:
                del self.order_contract[x]
            if rest:
                self.order_contract[oid] = ct.id
            for k, d in cash_delta.items():
                self.users[k].cash += d
            for k, d in pos_delta.items():
                q = self.positions.get((k, ct.id), 0) + d
                if q:
                    self.positions[(k, ct.id)] = q
                else:
                    self.positions.pop((k, ct.id), None)
            self.tape[ct.id].extend(trades)
        self._changed(ct.session_id)
        return {"order_id": oid, "status": status, "filled": plan.filled,
                "remaining": rest, "fills": [{"price": f.price, "qty": f.qty} for f in plan.fills],
                "self_trade_cancelled": plan.self_cancels}

    def cancel_order(self, uid: int, order_id: int) -> None:
        with self.lock:
            cid = self.order_contract.get(order_id)
            if cid is None or self.books[cid].orders[order_id].user_id != uid:
                raise ExchangeError("Order is not open", 404)
            self._cancel([order_id], "user")
        self._changed(self.contracts[cid].session_id)

    def cancel_all(self, uid: int, contract_id: int | None = None) -> int:
        with self.lock:
            u = self.users[uid]
            ids = [oid for oid, cid in self.order_contract.items()
                   if (contract_id is None or cid == contract_id)
                   and self.books[cid].orders[oid].user_id == uid]
            self._cancel(ids, "user")
        if ids:
            self._changed(u.session_id)
        return len(ids)

    def _cancel(self, ids: list[int], reason: str) -> None:
        if not ids:
            return
        ts = now_ms()
        with self._tx() as c:
            c.executemany("UPDATE orders SET status='cancelled', cancel_reason=?, updated_at=? "
                          "WHERE id=? AND status='open'", [(reason, ts, oid) for oid in ids])
        for oid in ids:
            self.books[self.order_contract.pop(oid)].cancel(oid)

    # ------------------------------------------------------------- snapshots
    def _mark2(self, ct: Contract) -> int | None:
        """Twice the mark price (mid can be a half-integer)."""
        book = self.books[ct.id]
        bb, ba = book.best_bid(), book.best_ask()
        if bb is not None and ba is not None:
            return bb + ba
        if self.tape[ct.id]:
            return 2 * self.tape[ct.id][-1]["price"]
        return None

    def _pnl(self, u: User) -> float:
        start = self.sessions[u.session_id].starting_cash
        total2 = 2 * (u.cash - start)
        for ct in self._session_contracts(u.session_id):
            q = self.positions.get((u.id, ct.id), 0)
            if q and ct.state != "settled":
                m2 = self._mark2(ct)
                total2 += q * (m2 if m2 is not None else ct.lo + ct.hi)
        return total2 / 2

    def public_state(self, session_id: int) -> dict:
        with self.lock:
            s = self._session(session_id)
            contracts = []
            for ct in self._session_contracts(s.id):
                book = self.books[ct.id]
                tape = self.tape[ct.id]
                contracts.append({
                    "id": ct.id, "name": ct.name, "description": ct.description, "kind": ct.kind,
                    "min": ct.lo, "max": ct.hi, "tick": ct.tick, "state": ct.state,
                    "settlement_value": ct.settlement_value,
                    "bids": book.levels(BUY), "asks": book.levels(SELL),
                    "last": tape[-1]["price"] if tape else None,
                    "trades": list(reversed(tape))[:30],
                })
            board = sorted(({"user_id": u.id, "name": u.name, "pnl": self._pnl(u)}
                            for u in self._session_users(s.id)), key=lambda r: -r["pnl"])
            all_settled = all(c["state"] in ("settled", "pending") for c in contracts)
            return {
                "session": {"id": s.id, "code": s.code, "status": s.status,
                            "starting_cash": s.starting_cash},
                "contracts": contracts,
                "announcements": list(reversed(self.announcements[s.id])),
                "leaderboard": board,
                "final": all_settled and not any(
                    q for (uid, cid), q in self.positions.items()
                    if self.contracts[cid].session_id == s.id),
            }

    def _orders_view(self, orders_by_contract) -> list[dict]:
        out = []
        for cid, o in orders_by_contract:
            out.append({"id": o.id, "contract_id": cid, "user_id": o.user_id, "side": o.side,
                        "price": o.price, "remaining": o.remaining})
        return sorted(out, key=lambda r: r["id"])

    def private_state(self, uid: int) -> dict:
        with self.lock:
            u = self.users[uid]
            positions = []
            for ct in self._session_contracts(u.session_id):
                if ct.state == "settled":
                    if (uid, ct.id) in self.settled:
                        q, amt = self.settled[(uid, ct.id)]
                        positions.append({"contract_id": ct.id, "qty": q, "settled": True,
                                          "payout": amt})
                elif self.positions.get((uid, ct.id)):
                    positions.append({"contract_id": ct.id, "qty": self.positions[(uid, ct.id)],
                                      "settled": False})
            orders = self._orders_view(
                (cid, self.books[cid].orders[oid]) for oid, cid in self.order_contract.items()
                if self.books[cid].orders[oid].user_id == uid)
            return {"user": {"id": u.id, "name": u.name, "cash": u.cash,
                             "available": self.available(uid), "pnl": self._pnl(u)},
                    "orders": orders, "positions": positions}

    def admin_state(self, session_id: int) -> dict:
        with self.lock:
            pub = self.public_state(session_id)
            users = []
            for u in self._session_users(session_id):
                users.append({
                    "id": u.id, "name": u.name, "cash": u.cash, "available": self.available(u.id),
                    "pnl": self._pnl(u),
                    "positions": {cid: q for (uid, cid), q in self.positions.items() if uid == u.id},
                })
            orders = self._orders_view(
                (cid, self.books[cid].orders[oid]) for oid, cid in self.order_contract.items()
                if self.contracts[cid].session_id == session_id)
            log = [dict(r) for r in self.conn.execute(
                "SELECT o.*, u.name AS user_name FROM orders o JOIN users u ON u.id = o.user_id "
                "WHERE o.session_id = ? ORDER BY o.id DESC LIMIT 200", (session_id,))]
            return {**pub, "users": users, "open_orders": orders, "order_log": log}

    def list_sessions(self) -> list[dict]:
        with self.lock:
            return [{"id": s.id, "code": s.code, "status": s.status,
                     "starting_cash": s.starting_cash, "created_at": s.created_at,
                     "ended_at": s.ended_at,
                     "players": len(self._session_users(s.id))}
                    for s in sorted(self.sessions.values(), key=lambda s: -s.id)]

    # ---------------------------------------------------------------- export
    def export_csv(self, session_id: int, what: str) -> str:
        with self.lock:
            s = self._session(session_id)
            buf = io.StringIO()
            w = csv.writer(buf)
            q = self.conn.execute
            if what == "trades":
                w.writerow(["trade_id", "time", "contract", "price", "qty", "buyer", "seller",
                            "aggressor_side", "buy_order_id", "sell_order_id"])
                for r in q("SELECT t.*, c.name AS cname, b.name AS bname, sl.name AS sname "
                           "FROM trades t JOIN contracts c ON c.id = t.contract_id "
                           "JOIN users b ON b.id = t.buyer_id JOIN users sl ON sl.id = t.seller_id "
                           "WHERE t.session_id = ? ORDER BY t.id", (s.id,)):
                    w.writerow([r["id"], iso(r["created_at"]), r["cname"], r["price"], r["qty"],
                                r["bname"], r["sname"], r["aggressor_side"], r["buy_order_id"],
                                r["sell_order_id"]])
            elif what == "orders":
                w.writerow(["order_id", "time", "updated", "player", "contract", "side", "type",
                            "price", "qty", "remaining", "status", "cancel_reason"])
                for r in q("SELECT o.*, c.name AS cname, u.name AS uname FROM orders o "
                           "JOIN contracts c ON c.id = o.contract_id JOIN users u ON u.id = o.user_id "
                           "WHERE o.session_id = ? ORDER BY o.id", (s.id,)):
                    w.writerow([r["id"], iso(r["created_at"]), iso(r["updated_at"]), r["uname"],
                                r["cname"], r["side"], r["tif"], r["price"], r["qty"],
                                r["remaining"], r["status"], r["cancel_reason"] or ""])
            elif what == "results":
                cts = self._session_contracts(s.id)
                head = ["rank", "player", "starting_cash", "cash", "pnl"]
                for ct in cts:
                    head += [f"{ct.name} settle", f"{ct.name} position", f"{ct.name} payout"]
                w.writerow(head)
                users = sorted(self._session_users(s.id), key=lambda u: -self._pnl(u))
                for i, u in enumerate(users, 1):
                    row = [i, u.name, s.starting_cash, u.cash, self._pnl(u)]
                    for ct in cts:
                        if ct.state == "settled":
                            qty, amt = self.settled.get((u.id, ct.id), (0, 0))
                            row += [ct.settlement_value, qty, amt]
                        else:
                            row += ["", self.positions.get((u.id, ct.id), 0), ""]
                    w.writerow(row)
            else:
                raise ExchangeError("Unknown export", 404)
            return buf.getvalue()
