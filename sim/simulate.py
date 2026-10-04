"""Load/invariant simulation. Testing only; not part of the game.

Starts a real server on a temp DB, creates a session with three contracts, joins
8 fake players and then fires random orders and cancels concurrently:

  phase 1: 8 asyncio clients over real HTTP (+ an admin halting/resuming contracts)
  phase 2: 8 OS threads calling the Exchange object directly, to stress the lock

After every step it takes an atomic snapshot and asserts:
  1. net position across users is zero for every contract
  2. total cash is unchanged by trading
  3. no user's available cash is negative (and it matches an independent recomputation)
  4. no crossed book at rest
Then it settles every contract, re-checks, and verifies each player's final cash
against a replay of the exported trade log plus settlement payoffs.

    python -m sim.simulate [--ops 400] [--seed 1]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
import random
import socket
import sys
import tempfile
import threading
import time

import httpx
import uvicorn
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.exchange import ExchangeError  # noqa: E402
from app.main import create_app  # noqa: E402

PLAYERS = 8
START = 5_000
PASSWORD = "sim-password"
CONTRACTS = [
    dict(name="Coin", kind="binary", tick=1),
    dict(name="Dice sum", kind="numeric", min=2, max=12, tick=1),
    dict(name="Swing", kind="numeric", min=-50, max=50, tick=5),
]
EXPECTED_REJECTIONS = ("not enough available cash", "orders are only accepted while open",
                       "Order is not open")


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.checks = 0

    def bump(self, key: str, n: int = 1) -> None:
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + n


stats = Stats()


# ---------------------------------------------------------------- invariants
def worst_value(lo, hi, pos, bids, asks):
    at_lo = pos * lo + sum(q * (lo - p) for p, q in bids)
    at_hi = pos * hi + sum(q * (p - hi) for p, q in asks)
    return min(at_lo, at_hi)


def check(st: dict, expected_total_cash: int) -> None:
    """Assert invariants on one atomic admin snapshot."""
    contracts = {c["id"]: c for c in st["contracts"]}
    users = st["users"]
    # 1. net position zero
    for cid in contracts:
        net = sum(int(u["positions"].get(str(cid), u["positions"].get(cid, 0))) for u in users)
        assert net == 0, f"net position {net} on contract {cid}"
    # 2. cash conserved
    total = sum(u["cash"] for u in users)
    assert total == expected_total_cash, f"total cash {total} != {expected_total_cash}"
    # 3. available >= 0, and matches an independent recomputation
    for u in users:
        mine = [o for o in st["open_orders"] if o["user_id"] == u["id"]]
        recomputed = u["cash"]
        for cid, c in contracts.items():
            if c["state"] == "settled":
                continue
            pos = int(u["positions"].get(str(cid), u["positions"].get(cid, 0)))
            bids = [(o["price"], o["remaining"]) for o in mine if o["contract_id"] == cid and o["side"] == "buy"]
            asks = [(o["price"], o["remaining"]) for o in mine if o["contract_id"] == cid and o["side"] == "sell"]
            recomputed += worst_value(c["min"], c["max"], pos, bids, asks)
        assert u["available"] == recomputed, f"{u['name']}: available {u['available']} != {recomputed}"
        assert u["available"] >= 0, f"{u['name']} has negative available cash {u['available']}"
    # 4. not crossed; book levels agree with open orders
    for cid, c in contracts.items():
        if c["bids"] and c["asks"]:
            assert c["bids"][0][0] < c["asks"][0][0], f"crossed book on {c['name']}: {c['bids'][0]} / {c['asks'][0]}"
        for side, levels in (("buy", c["bids"]), ("sell", c["asks"])):
            agg: dict[int, int] = {}
            for o in st["open_orders"]:
                if o["contract_id"] == cid and o["side"] == side:
                    agg[o["price"]] = agg.get(o["price"], 0) + o["remaining"]
            assert {p: q for p, q in levels} == agg, f"book/orders mismatch on {c['name']} {side}"
        if c["state"] == "settled":
            assert not c["bids"] and not c["asks"], f"resting orders after settlement on {c['name']}"
    with stats.lock:
        stats.checks += 1


# ---------------------------------------------------------------- order flow
class Market:
    """Hidden drifting fair values so random orders cluster and actually trade."""

    def __init__(self, contracts, rng):
        self.c = contracts
        self.fair = {c["id"]: (c["min"] + c["max"]) / 2 for c in contracts}
        self.rng = rng
        self.lock = threading.Lock()

    def random_action(self, rng: random.Random, my_open_ids: list[int]):
        r = rng.random()
        if my_open_ids and r < 0.18:
            return ("cancel", rng.choice(my_open_ids))
        if r < 0.21:
            return ("cancel_all", rng.choice([None] + [c["id"] for c in self.c]))
        c = rng.choice(self.c)
        width = c["max"] - c["min"]
        with self.lock:
            f = self.fair[c["id"]] = min(c["max"], max(c["min"], self.fair[c["id"]] + rng.gauss(0, width / 100)))
        side = rng.choice(["buy", "sell"])
        qty = rng.randint(1, 25)
        k = rng.random()
        if k < 0.08:  # market button: IOC at worst price
            return ("order", c["id"], side, c["max"] if side == "buy" else c["min"], qty, "ioc")
        edge = rng.gauss(0, width / 15) + (-1 if side == "buy" else 1) * width / 40
        p = f + edge
        p = c["min"] + round((p - c["min"]) / c["tick"]) * c["tick"]
        p = min(c["max"], max(c["min"], p))
        return ("order", c["id"], side, int(p), qty, "ioc" if k < 0.2 else "limit")


def expected_error(msg: str) -> bool:
    return any(e in msg for e in EXPECTED_REJECTIONS)


# ---------------------------------------------------------------- phase 1: HTTP
async def http_player(base, token, market: Market, ops: int, seed: int, expected_cash: int, admin_h):
    rng = random.Random(seed)
    h = {"X-Token": token}
    async with httpx.AsyncClient(base_url=base, timeout=30) as cl:
        me = (await cl.get("/api/state", headers=h)).json()["me"]
        for _ in range(ops):
            act = market.random_action(rng, [o["id"] for o in me["orders"]])
            if act[0] == "order":
                _, cid, side, price, qty, tif = act
                r = await cl.post("/api/orders", headers=h, json=dict(
                    contract_id=cid, side=side, price=price, qty=qty, tif=tif))
                kind = f"order_{tif}"
            elif act[0] == "cancel":
                r = await cl.delete(f"/api/orders/{act[1]}", headers=h)
                kind = "cancel"
            else:
                r = await cl.post("/api/cancel_all", headers=h, json={"contract_id": act[1]})
                kind = "cancel_all"
            if r.status_code == 200:
                stats.bump(kind)
                if kind.startswith("order") and r.json()["filled"]:
                    stats.bump("orders_with_fills")
            else:
                err = r.json().get("error", r.text)
                assert r.status_code in (400, 404) and expected_error(err), f"unexpected {r.status_code}: {err}"
                stats.bump("rejected: " + next(e for e in EXPECTED_REJECTIONS if e in err))
            # invariants after every step (atomic snapshot taken under the exchange lock)
            st = (await cl.get(f"/api/admin/sessions/{market.sid}/state", headers=admin_h)).json()
            check(st, expected_cash)
            me = (await cl.get("/api/state", headers=h)).json()["me"]


async def http_admin_halter(base, admin_h, contracts, stop: asyncio.Event, seed: int):
    rng = random.Random(seed)
    async with httpx.AsyncClient(base_url=base, timeout=30) as cl:
        while not stop.is_set():
            await asyncio.sleep(rng.uniform(0.3, 1.0))
            c = rng.choice(contracts)
            r = await cl.post(f"/api/admin/contracts/{c['id']}/state", headers=admin_h, json={"state": "halted"})
            assert r.status_code == 200, r.text
            stats.bump("halts")
            await asyncio.sleep(rng.uniform(0.05, 0.3))
            r = await cl.post(f"/api/admin/contracts/{c['id']}/state", headers=admin_h, json={"state": "open"})
            assert r.status_code == 200, r.text


async def ws_resync_check(base, token):
    """Connect, drop, reconnect: the server must send a full fresh snapshot each time."""
    url = base.replace("http", "ws") + f"/ws?token={token}"
    async with websockets.connect(url) as ws:
        first = await ws.recv()
    async with websockets.connect(url) as ws:
        again = __import__("json").loads(await ws.recv())
    async with httpx.AsyncClient(base_url=base) as cl:
        now = (await cl.get("/api/state", headers={"X-Token": token})).json()
    assert first and again["type"] == "state"
    assert again["me"] == now["me"], "resynced snapshot differs from current state"
    stats.bump("ws_resyncs")


# ---------------------------------------------------------------- phase 2: threads
def thread_player(ex, uid, sid, market: Market, ops: int, seed: int, expected_cash: int, errors: list):
    rng = random.Random(seed)
    try:
        for _ in range(ops):
            with ex.lock:
                mine = [oid for oid, cid in ex.order_contract.items()
                        if ex.books[cid].orders[oid].user_id == uid]
            act = market.random_action(rng, mine)
            try:
                if act[0] == "order":
                    _, cid, side, price, qty, tif = act
                    r = ex.place_order(uid, cid, side, price, qty, tif)
                    stats.bump(f"order_{tif}")
                    if r["filled"]:
                        stats.bump("orders_with_fills")
                elif act[0] == "cancel":
                    ex.cancel_order(uid, act[1])
                    stats.bump("cancel")
                else:
                    ex.cancel_all(uid, act[1])
                    stats.bump("cancel_all")
            except ExchangeError as e:
                assert expected_error(e.message), f"unexpected error: {e.message}"
                stats.bump("rejected: " + next(x for x in EXPECTED_REJECTIONS if x in e.message))
            check(ex.admin_state(sid), expected_cash)
    except Exception as e:  # surface failures from worker threads
        errors.append(e)


# ---------------------------------------------------------------- settlement
def replay_final_cash(trades_csv: str, names: list[str], settle: dict[str, int]) -> dict[str, int]:
    cash = {n: START for n in names}
    pos: dict[tuple[str, str], int] = {}
    for row in csv.DictReader(io.StringIO(trades_csv)):
        p, q, b, s, c = int(row["price"]), int(row["qty"]), row["buyer"], row["seller"], row["contract"]
        cash[b] -= p * q
        cash[s] += p * q
        pos[(b, c)] = pos.get((b, c), 0) + q
        pos[(s, c)] = pos.get((s, c), 0) - q
    for (n, c), q in pos.items():
        cash[n] += q * settle[c]
    return cash


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main(ops: int, seed: int) -> None:
    rng = random.Random(seed)
    tmp = tempfile.mkdtemp()
    app = create_app(os.path.join(tmp, "sim.db"), PASSWORD)
    ex = app.state.ex
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        await asyncio.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    t0 = time.time()

    async with httpx.AsyncClient(base_url=base, timeout=30) as cl:
        admin_h = {"X-Admin-Token": (await cl.post("/api/admin/login", json={"password": PASSWORD})).json()["token"]}
        sid = (await cl.post("/api/admin/sessions", headers=admin_h,
                             json={"code": "SIM", "starting_cash": START})).json()["id"]
        contracts = []
        for spec in CONTRACTS:
            cid = (await cl.post(f"/api/admin/sessions/{sid}/contracts", headers=admin_h, json=spec)).json()["id"]
            await cl.post(f"/api/admin/contracts/{cid}/state", headers=admin_h, json={"state": "open"})
        st = (await cl.get(f"/api/admin/sessions/{sid}/state", headers=admin_h)).json()
        contracts = [{"id": c["id"], "name": c["name"], "min": c["min"], "max": c["max"], "tick": c["tick"]}
                     for c in st["contracts"]]
        players = []
        for i in range(PLAYERS):
            r = (await cl.post("/api/join", json={"code": "sim", "name": f"bot{i}"})).json()
            players.append((r["user_id"], r["token"], f"bot{i}"))
    total_cash = START * PLAYERS
    market = Market(contracts, rng)
    market.sid = sid

    # phase 1: concurrent HTTP clients
    stop = asyncio.Event()
    halter = asyncio.create_task(http_admin_halter(base, admin_h, contracts, stop, seed + 99))
    await asyncio.gather(*(http_player(base, tok, market, ops, seed * 100 + i, total_cash, admin_h)
                           for i, (_, tok, _) in enumerate(players)))
    stop.set()
    await halter
    async with httpx.AsyncClient(base_url=base) as cl:  # leave every contract open for phase 2
        for c in contracts:
            await cl.post(f"/api/admin/contracts/{c['id']}/state", headers=admin_h, json={"state": "open"})
    await ws_resync_check(base, players[0][1])
    print(f"phase 1 (HTTP) done in {time.time() - t0:.1f}s")

    # phase 2: raw threads against the exchange object, contending for the lock
    t1 = time.time()
    errors: list = []
    threads = [threading.Thread(target=thread_player,
                                args=(ex, uid, sid, market, ops, seed * 1000 + i, total_cash, errors))
               for i, (uid, _, _) in enumerate(players)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    print(f"phase 2 (threads) done in {time.time() - t1:.1f}s")

    # settlement
    settle_values = {}
    async with httpx.AsyncClient(base_url=base, timeout=30) as cl:
        for c in contracts:
            v = c["min"] + rng.randrange((c["max"] - c["min"]) // c["tick"] + 1) * c["tick"]
            settle_values[c["name"]] = v
            r = await cl.post(f"/api/admin/contracts/{c['id']}/settle", headers=admin_h, json={"value": v})
            assert r.status_code == 200, r.text
            check((await cl.get(f"/api/admin/sessions/{sid}/state", headers=admin_h)).json(),
                  total_cash)  # payoffs net to zero because net position is zero
        st = (await cl.get(f"/api/admin/sessions/{sid}/state", headers=admin_h)).json()
        trades_csv = (await cl.get(f"/api/admin/sessions/{sid}/export/trades.csv", headers=admin_h)).text
        r = await cl.post(f"/api/admin/sessions/{sid}/end", headers=admin_h)
        assert r.status_code == 200, r.text

    self_trades = [r for r in csv.DictReader(io.StringIO(trades_csv)) if r["buyer"] == r["seller"]]
    assert not self_trades, f"{len(self_trades)} self-trades slipped past self-trade prevention"
    expected = replay_final_cash(trades_csv, [n for _, _, n in players], settle_values)
    actual = {u["name"]: u["cash"] for u in st["users"]}
    assert actual == expected, f"final cash mismatch:\n actual   {actual}\n expected {expected}"
    assert sum(actual.values()) == sum(expected.values()) == total_cash
    assert all(not u["positions"] for u in st["users"]), "positions remain after settlement"
    assert not st["open_orders"], "open orders remain after settlement"
    assert st["final"], "leaderboard not final after settlement"
    for u in st["users"]:
        assert u["available"] == u["cash"] >= 0

    server.should_exit = True
    n_trades = len(trades_csv.splitlines()) - 1
    print(f"settled at {settle_values}")
    print(f"trades: {n_trades}, invariant checks: {stats.checks}")
    for k in sorted(stats.counts):
        print(f"  {k}: {stats.counts[k]}")
    print("final cash:", dict(sorted(actual.items())))
    print("ALL INVARIANTS HELD")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ops", type=int, default=200, help="actions per player per phase")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    asyncio.run(main(a.ops, a.seed))
