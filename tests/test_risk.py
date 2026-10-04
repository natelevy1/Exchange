from app.risk import Exposure, available_cash, worst_loss

BIN = dict(lo=0, hi=100)


def test_flat_has_no_risk():
    assert worst_loss(Exposure(**BIN)) == 0


def test_long_binary_worst_case_is_at_min():
    # bought 10 @ 60: cash already paid 600; settles at 0 in the worst case
    e = Exposure(**BIN, pos=10)
    assert worst_loss(e) == 0
    assert available_cash(10_000 - 600, [e]) == 9_400


def test_short_binary_worst_case_is_at_max():
    # sold 10 @ 60: received 600, pays 100 each at max -> loses 400
    e = Exposure(**BIN, pos=-10)
    assert worst_loss(e) == 1_000
    assert available_cash(10_000 + 600, [e]) == 9_600


def test_short_sold_at_max_has_zero_loss():
    e = Exposure(**BIN, pos=-10)
    assert available_cash(10_000 + 10 * 100, [e]) == 10_000


def test_short_sold_at_min_loses_full_range():
    e = Exposure(**BIN, pos=-10)
    assert available_cash(10_000 + 10 * 0, [e]) == 9_000


def test_short_numeric_with_negative_min_loses_at_max_not_min():
    # range [-50, 50], sold 10 @ 0
    e = Exposure(lo=-50, hi=50, pos=-10)
    assert available_cash(10_000, [e]) == 10_000 - 500


def test_short_numeric_sold_at_min_end():
    # sold 10 @ -50 (paid 500 to go short); worst at max: -10*50 = -500
    e = Exposure(lo=-50, hi=50, pos=-10)
    assert available_cash(10_000 - 500, [e]) == 9_000


def test_long_numeric_loses_at_min():
    e = Exposure(lo=-50, hi=50, pos=10)  # bought 10 @ 0
    assert available_cash(10_000, [e]) == 9_500


def test_guaranteed_minimum_is_credited():
    # dice sum [2, 12]: buy 1000 @ 7 -> cash 3000, but worst case pays 2 each
    e = Exposure(lo=2, hi=12, pos=1_000)
    assert available_cash(3_000, [e]) == 5_000


def test_resting_bid_reserves_price_minus_min():
    e = Exposure(**BIN, bids=[(40, 10)])
    assert worst_loss(e) == 400


def test_resting_ask_reserves_max_minus_price():
    e = Exposure(**BIN, asks=[(40, 10)])
    assert worst_loss(e) == 600


def test_bids_and_asks_do_not_offset_each_other():
    e = Exposure(**BIN, bids=[(40, 10)], asks=[(60, 10)])
    assert worst_loss(e) == 400  # max(400, 400), not the sum and not a credit


def test_multiple_resting_orders_same_side_add_up():
    e = Exposure(**BIN, bids=[(40, 10), (30, 5)])
    assert worst_loss(e) == 400 + 150


def test_closing_order_adds_no_risk():
    # long 10 bought @ 60, offering them out @ 70
    base = Exposure(**BIN, pos=10)
    closing = Exposure(**BIN, pos=10, asks=[(70, 10)])
    assert worst_loss(closing) == worst_loss(base) == 0
    # but offering more than the position does add short risk
    # at max: +10*100 from the position, 40*(70-100) from the asks -> -200
    over = Exposure(**BIN, pos=10, asks=[(70, 40)])
    assert worst_loss(over) == 200


def test_position_plus_same_side_orders_compound():
    e = Exposure(**BIN, pos=-10, asks=[(80, 10)])
    # at max: -1000 + 10*(80-100) = -1200
    assert worst_loss(e) == 1_200


def test_available_sums_across_contracts():
    a = Exposure(**BIN, bids=[(50, 10)])          # 500
    b = Exposure(lo=2, hi=12, asks=[(7, 100)])    # 500
    assert available_cash(1_000, [a, b]) == 0
    assert available_cash(999, [a, b]) == -1
