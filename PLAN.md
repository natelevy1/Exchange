# Plan

## Layout
```
app/
  engine.py     pure in-memory order book + matching (no I/O)
  risk.py       pure worst-case / available-cash math
  db.py         SQLite schema + connection helper
  exchange.py   Exchange service: single lock, owns books/cash/positions in memory,
                risk check -> match -> write-through to SQLite in ONE transaction
                (on any DB error the transaction rolls back and memory is reloaded from DB)
  main.py       FastAPI routes, auth, WebSocket hub (debounced state push)
static/         index.html (player), admin.html, app.js, admin.js, style.css
tests/          test_engine.py, test_risk.py, test_api.py
sim/simulate.py concurrent random-order simulation with invariant checks
Dockerfile, requirements.txt, README.md
```

Build order: engine + tests -> risk + tests -> exchange/API -> WebSockets -> player UI ->
admin UI -> simulation.

## Data model (SQLite)
```
sessions      id, code UNIQUE, starting_cash, status('active'|'ended'), created_at, ended_at
users         id, session_id, name, token UNIQUE, cash, created_at   UNIQUE(session_id, name)
contracts     id, session_id, name, description, kind('binary'|'numeric'),
              min_value, max_value, tick, state('pending'|'open'|'halted'|'settled'),
              settlement_value NULL, created_at, settled_at
orders        id, session_id, contract_id, user_id, side('buy'|'sell'), tif('limit'|'ioc'),
              price, qty, remaining, status('open'|'filled'|'cancelled'),
              cancel_reason NULL ('user'|'self_trade'|'ioc_remainder'|'settled'|'session_end'),
              created_at, updated_at
trades        id, session_id, contract_id, price, qty, buyer_id, seller_id,
              buy_order_id, sell_order_id, aggressor_side, created_at
positions     user_id, contract_id, qty            PK(user_id, contract_id)
settlements   contract_id, user_id, qty, amount     (one row per holder at settlement)
announcements id, session_id, text, created_at
```
Order ids are monotonically increasing, so (price, id) gives price-time priority.

## Key rules / interpretations
- Prices are integers; valid prices are `min + k*tick` within [min, max]; (max-min) must be
  divisible by tick. Money = price * qty, all integers.
- Risk, per user and unsettled contract, at each endpoint V of the range:
  worst value at V=min = pos*min + sum over bids q*(min - p)   (asks only help at min)
  worst value at V=max = pos*max + sum over asks q*(p - max)   (bids only help at max)
  i.e. "all resting orders fully filled" is taken per side; filling both sides at once
  would let opposite-side orders offset each other and understate risk.
- An order is checked against the state that would result from accepting it (its fills at
  actual prices, self-trade cancels, and any limit remainder resting). For IOC/market this
  means only the actual fills count. Counterparties' available cash can only rise.
- Cancels are allowed while halted. Settling (from open or halted) cancels resting orders,
  pays pos*value, zeroes positions, and records rows in `settlements`.
- Available cash = cash + sum over unsettled contracts of min(value at min, value at max)
  = worst-case cash after everything settles. (Decision: guaranteed minimum payouts are
  credited, so displayed cash may dip below 0 on contracts with min > 0; available cash,
  and therefore final cash, never does.)
- One active session at a time; ended sessions are read-only but viewable/exportable.
  Ending is refused while any contract that has been opened is unsettled (decision).
