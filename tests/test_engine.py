import itertools

import pytest

from app.engine import BUY, SELL, OrderBook


@pytest.fixture
def book():
    return OrderBook()


ids = itertools.count(1)


def sub(book, user, side, price, qty, ioc=False):
    return book.submit(next(ids), side, price, qty, user, ioc=ioc)


def fills(plan):
    return [(f.resting_user_id, f.price, f.qty) for f in plan.fills]


def test_resting_orders_build_book(book):
    sub(book, 1, BUY, 40, 5)
    sub(book, 1, BUY, 42, 3)
    sub(book, 2, SELL, 50, 7)
    sub(book, 2, SELL, 48, 1)
    assert book.levels(BUY) == [(42, 3), (40, 5)]
    assert book.levels(SELL) == [(48, 1), (50, 7)]
    assert book.best_bid() == 42 and book.best_ask() == 48


def test_full_fill(book):
    sub(book, 1, SELL, 50, 10)
    p = sub(book, 2, BUY, 50, 10)
    assert fills(p) == [(1, 50, 10)]
    assert p.remaining == 0
    assert book.levels(SELL) == [] and book.levels(BUY) == []


def test_partial_fill_of_resting(book):
    sub(book, 1, SELL, 50, 10)
    p = sub(book, 2, BUY, 50, 4)
    assert fills(p) == [(1, 50, 4)]
    assert book.levels(SELL) == [(50, 6)]
    assert book.levels(BUY) == []


def test_partial_fill_of_incoming_rests_remainder(book):
    sub(book, 1, SELL, 50, 4)
    p = sub(book, 2, BUY, 52, 10)
    assert fills(p) == [(1, 50, 4)]
    assert p.remaining == 6
    assert book.levels(BUY) == [(52, 6)]
    assert book.levels(SELL) == []


def test_multiple_price_levels_trade_at_resting_prices(book):
    sub(book, 1, SELL, 51, 2)
    sub(book, 1, SELL, 50, 3)
    sub(book, 1, SELL, 53, 5)
    p = sub(book, 2, BUY, 52, 10)
    assert fills(p) == [(1, 50, 3), (1, 51, 2)]
    assert book.levels(BUY) == [(52, 5)]
    assert book.levels(SELL) == [(53, 5)]


def test_sell_sweeps_bids_best_first(book):
    sub(book, 1, BUY, 40, 1)
    sub(book, 1, BUY, 45, 1)
    sub(book, 1, BUY, 42, 1)
    p = sub(book, 2, SELL, 0, 3)
    assert [f.price for f in p.fills] == [45, 42, 40]


def test_price_time_priority(book):
    sub(book, 1, SELL, 50, 5)  # first at 50
    sub(book, 2, SELL, 49, 5)  # better price, later
    sub(book, 3, SELL, 50, 5)  # second at 50
    p = sub(book, 4, BUY, 50, 12)
    assert fills(p) == [(2, 49, 5), (1, 50, 5), (3, 50, 2)]
    assert book.levels(SELL) == [(50, 3)]
    assert list(book.orders.values())[0].user_id == 3


def test_no_match_when_not_crossing(book):
    sub(book, 1, SELL, 50, 5)
    p = sub(book, 2, BUY, 49, 5)
    assert p.fills == []
    assert book.best_bid() == 49 and book.best_ask() == 50


def test_self_trade_prevention_cancels_resting_and_continues(book):
    sub(book, 1, SELL, 50, 5)       # user 1's own resting
    o2 = next(ids); book.submit(o2, SELL, 51, 5, 2)
    p = sub(book, 1, BUY, 51, 3)
    assert p.self_cancels and fills(p) == [(2, 51, 3)]
    assert book.levels(SELL) == [(51, 2)]
    assert all(o.user_id != 1 or o.side == BUY for o in book.orders.values())
    assert book.levels(BUY) == []


def test_self_trade_prevention_only_for_orders_that_would_match(book):
    sub(book, 2, SELL, 50, 5)
    sub(book, 1, SELL, 51, 5)  # own order behind; never reached
    p = sub(book, 1, BUY, 51, 5)
    assert fills(p) == [(2, 50, 5)]
    assert p.self_cancels == []
    assert book.levels(SELL) == [(51, 5)]


def test_self_trade_then_rest(book):
    sub(book, 1, SELL, 50, 5)
    p = sub(book, 1, BUY, 50, 5)
    assert p.fills == [] and len(p.self_cancels) == 1
    assert book.levels(SELL) == []
    assert book.levels(BUY) == [(50, 5)]


def test_cancel(book):
    oid = next(ids)
    book.submit(oid, BUY, 40, 5, 1)
    sub(book, 2, BUY, 40, 2)
    book.cancel(oid)
    assert book.levels(BUY) == [(40, 2)]
    with pytest.raises(KeyError):
        book.cancel(oid)


def test_cancel_last_order_removes_level(book):
    oid = next(ids)
    book.submit(oid, SELL, 60, 5, 1)
    book.cancel(oid)
    assert book.best_ask() is None and book.levels(SELL) == []


def test_ioc_remainder_cancelled(book):
    sub(book, 1, SELL, 50, 3)
    p = sub(book, 2, BUY, 55, 10, ioc=True)
    assert fills(p) == [(1, 50, 3)]
    assert p.remaining == 7
    assert book.levels(BUY) == [] and book.levels(SELL) == []


def test_ioc_no_liquidity_does_nothing(book):
    p = sub(book, 2, BUY, 55, 10, ioc=True)
    assert p.fills == [] and book.orders == {}


def test_plan_does_not_mutate(book):
    sub(book, 1, SELL, 50, 3)
    sub(book, 2, SELL, 50, 3)
    before = book.levels(SELL)
    p = book.plan(BUY, 50, 4, 1)
    assert len(p.self_cancels) == 1 and fills(p) == [(2, 50, 3)]
    assert book.levels(SELL) == before


def test_load_out_of_order_keeps_time_priority(book):
    from app.engine import Order
    book.add_resting(Order(20, 1, SELL, 50, 1))
    book.add_resting(Order(10, 2, SELL, 50, 1))
    p = sub(book, 3, BUY, 50, 1)
    assert fills(p) == [(2, 50, 1)]
