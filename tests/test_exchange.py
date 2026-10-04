import pytest

from app.exchange import Exchange, ExchangeError


@pytest.fixture
def ex(tmp_path):
    return Exchange(str(tmp_path / "t.db"))


@pytest.fixture
def game(ex):
    s = ex.create_session("TEST", 1_000)
    a, b, c = (ex.join("TEST", n) for n in ("alice", "bob", "carol"))
    binary = ex.create_contract(s.id, "Heads", kind="binary")
    dice = ex.create_contract(s.id, "Dice", kind="numeric", lo=2, hi=12)
    for ct in (binary, dice):
        ex.set_contract_state(ct.id, "open")
    return ex, s, a, b, c, binary, dice


def test_orders_rejected_unless_open(ex):
    s = ex.create_session("X1", 1_000)
    u = ex.join("X1", "u")
    ct = ex.create_contract(s.id, "C")
    with pytest.raises(ExchangeError, match="pending"):
        ex.place_order(u.id, ct.id, "buy", 50, 1)
    ex.set_contract_state(ct.id, "open")
    ex.place_order(u.id, ct.id, "buy", 50, 1)
    ex.set_contract_state(ct.id, "halted")
    with pytest.raises(ExchangeError, match="halted"):
        ex.place_order(u.id, ct.id, "buy", 50, 1)
    # halting keeps resting orders, and cancels are still allowed
    assert ex.books[ct.id].levels("buy") == [(50, 1)]
    assert ex.cancel_all(u.id) == 1


def test_price_must_be_on_tick_and_in_range(ex):
    s = ex.create_session("X2", 1_000)
    u = ex.join("X2", "u")
    ct = ex.create_contract(s.id, "C", kind="numeric", lo=0, hi=100, tick=5)
    ex.set_contract_state(ct.id, "open")
    for bad in (-5, 105, 7):
        with pytest.raises(ExchangeError, match="tick"):
            ex.place_order(u.id, ct.id, "buy", bad, 1)
    ex.place_order(u.id, ct.id, "buy", 10, 1)


def test_bid_rejected_when_margin_exceeds_cash(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "buy", 50, 20)          # reserves 1000 exactly
    assert ex.available(a.id) == 0
    with pytest.raises(ExchangeError, match="not enough available cash"):
        ex.place_order(a.id, binary.id, "buy", 1, 1)


def test_short_at_max_end_rejected(game):
    ex, s, a, b, c, binary, dice = game
    # selling at 0 risks 100 per lot at max: 10 lots = 1000 OK, 11 not
    with pytest.raises(ExchangeError, match="needs 1,100"):
        ex.place_order(a.id, binary.id, "sell", 0, 11)
    ex.place_order(a.id, binary.id, "sell", 0, 10)
    assert ex.available(a.id) == 0


def test_short_fills_reserve_at_max_and_long_at_min(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 60, 10)
    ex.place_order(b.id, binary.id, "buy", 60, 10)
    assert ex.users[a.id].cash == 1_600 and ex.available(a.id) == 600   # loses 400 at 100
    assert ex.users[b.id].cash == 400 and ex.available(b.id) == 400     # loses 600 at 0


def test_closing_order_allowed_at_zero_available(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 100, 10)
    ex.place_order(b.id, binary.id, "buy", 100, 10)   # b spent all 1000; available 0
    assert ex.available(b.id) == 0
    ex.place_order(b.id, binary.id, "sell", 90, 10)   # flattening adds no risk
    # going net short is allowed while the long covers it at max: 1000 - n*10 >= 0
    ex.place_order(b.id, binary.id, "sell", 90, 90)
    with pytest.raises(ExchangeError):
        ex.place_order(b.id, binary.id, "sell", 90, 1)


def test_dice_long_credits_guaranteed_minimum(game):
    ex, s, a, b, c, binary, dice = game
    # buying at 12 with min 2 risks 10 a lot -> 100 lots uses all 1000
    ex.place_order(a.id, dice.id, "sell", 12, 100)
    ex.place_order(b.id, dice.id, "buy", 12, 100)
    assert ex.users[b.id].cash == 1_000 - 1_200
    assert ex.available(b.id) == 0


def test_ioc_risk_uses_actual_fills(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 10, 5)
    # market buy (IOC at max) for 50: only 5 fill at 10 -> risk 50, accepted
    r = ex.place_order(b.id, binary.id, "buy", 100, 50, tif="ioc")
    assert r["filled"] == 5 and r["remaining"] == 0 and r["status"] == "cancelled"
    assert ex.books[binary.id].orders == {}
    assert ex.available(b.id) == 1_000 - 50


def test_self_trade_prevention_persisted(game):
    ex, s, a, b, c, binary, dice = game
    o = ex.place_order(a.id, binary.id, "sell", 50, 5)["order_id"]
    r = ex.place_order(a.id, binary.id, "buy", 50, 5)
    assert r["self_trade_cancelled"] == [o] and r["filled"] == 0
    row = ex.conn.execute("SELECT status, cancel_reason FROM orders WHERE id=?", (o,)).fetchone()
    assert tuple(row) == ("cancelled", "self_trade")


def test_settlement_pays_longs_and_charges_shorts(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 40, 10)
    ex.place_order(b.id, binary.id, "buy", 40, 10)
    ex.place_order(c.id, binary.id, "buy", 30, 3)     # resting, cancelled at settlement
    ex.settle(binary.id, 100)
    assert ex.users[a.id].cash == 1_000 + 400 - 1_000
    assert ex.users[b.id].cash == 1_000 - 400 + 1_000
    assert ex.users[c.id].cash == 1_000
    assert ex.books[binary.id].orders == {}
    assert ex.contracts[binary.id].state == "settled"
    with pytest.raises(ExchangeError):
        ex.place_order(a.id, binary.id, "buy", 50, 1)
    with pytest.raises(ExchangeError, match="within"):
        ex.settle(dice.id, 13)


def test_cannot_end_session_with_unsettled_contracts(game):
    ex, s, a, b, c, binary, dice = game
    with pytest.raises(ExchangeError, match="Settle"):
        ex.end_session(s.id)
    ex.settle(binary.id, 0)
    ex.settle(dice.id, 7)
    ex.end_session(s.id)
    with pytest.raises(ExchangeError, match="ended"):
        ex.join("TEST", "dave")
    new = ex.create_session("NEXT")
    assert new.starting_cash == 10_000


def test_names_unique_case_insensitive(game):
    ex = game[0]
    with pytest.raises(ExchangeError, match="taken"):
        ex.join("test", "ALICE")


def test_state_survives_reload(game, tmp_path):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 55, 4)
    ex.place_order(b.id, binary.id, "buy", 55, 1)
    ex.place_order(c.id, binary.id, "sell", 55, 2)
    ex.announce(s.id, "Die 1: 4")
    ex2 = Exchange(str(tmp_path / "t.db"))
    assert ex2.books[binary.id].levels("sell") == [(55, 5)]
    assert ex2.users[a.id].cash == ex.users[a.id].cash
    assert ex2.positions == ex.positions
    assert ex2.public_state(s.id) == ex.public_state(s.id)
    # time priority survives: a's remaining 3 fill before c's 2
    ex2.place_order(b.id, binary.id, "buy", 55, 3)
    assert ex2.positions[(a.id, binary.id)] == -4
    assert (c.id, binary.id) not in ex2.positions


def test_failed_write_leaves_memory_untouched(game, monkeypatch):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 50, 5)
    before = (ex.books[binary.id].levels("sell"), dict(ex.positions),
              {u.id: u.cash for u in ex.users.values()})
    ex.conn.execute("CREATE TRIGGER boom BEFORE INSERT ON trades BEGIN SELECT RAISE(ABORT, 'x'); END")
    with pytest.raises(Exception):
        ex.place_order(b.id, binary.id, "buy", 50, 5)
    after = (ex.books[binary.id].levels("sell"), dict(ex.positions),
             {u.id: u.cash for u in ex.users.values()})
    assert before == after
    assert ex.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_leaderboard_marks_to_mid_then_last(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 50, 10)
    ex.place_order(b.id, binary.id, "buy", 50, 10)
    # no book: mark to last trade 50 -> flat P&L
    pnl = {r["name"]: r["pnl"] for r in ex.public_state(s.id)["leaderboard"]}
    assert pnl["alice"] == 0 and pnl["bob"] == 0
    ex.place_order(c.id, binary.id, "buy", 60, 1)
    ex.place_order(c.id, binary.id, "sell", 71, 1)    # mid 65.5
    pnl = {r["name"]: r["pnl"] for r in ex.public_state(s.id)["leaderboard"]}
    assert pnl["bob"] == 155 and pnl["alice"] == -155


def test_csv_exports(game):
    ex, s, a, b, c, binary, dice = game
    ex.place_order(a.id, binary.id, "sell", 50, 2)
    ex.place_order(b.id, binary.id, "buy", 50, 2)
    ex.settle(binary.id, 100)
    trades = ex.export_csv(s.id, "trades").splitlines()
    assert len(trades) == 2 and "alice" in trades[1] and "bob" in trades[1]
    assert len(ex.export_csv(s.id, "orders").splitlines()) == 3
    results = ex.export_csv(s.id, "results").splitlines()
    assert results[1].startswith("1,bob,1000,1100,100.0")
