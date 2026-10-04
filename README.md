# Exchange

A small play-money exchange for live trading games (≈10 players, 30–90 minutes, one admin).
The backend is FastAPI with SQLite and WebSockets. The frontend is plain HTML/JS with no build step.

- Players: `http://<host>/`, where they join with the session code and a display name.
- Admin: `http://<host>/admin`, which uses the password from `ADMIN_PASSWORD`.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
ADMIN_PASSWORD=changeme .venv/bin/uvicorn app.main:app_from_env --factory --port 8000
```

Data goes to `exchange.db`, or to the path in `DB_PATH`. Restarting the server keeps everything: books, cash, positions and history are reloaded from SQLite.

## Tests and simulation

```bash
.venv/bin/python -m pytest -q                 # engine, risk, exchange and API tests
.venv/bin/python -m sim.simulate --ops 200    # 8 bots × 200 actions × 2 phases
```

The simulation starts a real server on a temporary database. It then runs two phases:
1. 8 bots trade concurrently over HTTP while an admin bot halts and resumes contracts.
2. 8 OS threads call the exchange object directly, to contend for the lock.

After every step it takes an atomic snapshot and asserts four things:
- Net position is zero for every contract.
- Total cash is unchanged by trading.
- Every player's available cash is ≥ 0 and matches an independent recomputation.
- No book is crossed.

It then settles every contract at a random value. It checks that each player's final cash equals a replay of the exported trade log plus settlement payoffs, and that no trade is a self-trade.

## Running a session (admin)

1. **Start a session.** Choose a join code and starting cash (default 10,000).
2. **Create contracts.** A contract can be binary (fixed range [0, 100]) or numeric (any [min, max]), with a tick size. New contracts start as *pending*, which players can't see. Click **Open** to start trading.
3. **During play.** Post announcements such as "Die 2: 4". **Halt** pauses trading and keeps resting orders; players can still cancel. **Resume** reopens it.
4. **Settle.** Enter a value in [min, max] and click **Settle** twice to confirm. Resting orders are cancelled, longs receive `value × qty`, and shorts pay it.
5. **End the session.** You can't end it while any opened contract is unsettled. Ended sessions stay in the session dropdown as read-only, and you can still export trades, orders and final results as CSV. Players' links still show the ended session read-only.

Name and description can be edited until a contract settles. Type, range and tick can only change while the contract is pending.

## Rules the engine enforces

- Prices are integers on the grid `min + k·tick`. Money is `price × qty`, also integers.
- Matching uses price-time priority, and trades execute at the resting order's price.
- Order types are limit (good until cancelled) and IOC. **Market** sends an IOC at the worst price in the range.
- Self-trade prevention: if an incoming order would match your own resting order, that resting order is cancelled and matching continues.
- **Risk.** For each contract, the worst case is checked at both ends of the range:
  - At `min`, your position plus all your bids filled.
  - At `max`, your position plus all your asks filled.
  - Asks only help at `min` and bids only help at `max`, so the two sides never offset each other.

  Available cash = cash + Σ(worst-case settlement value) = your worst-case cash after everything settles. An order is rejected if accepting it would make this negative. For IOC and market orders, only the actual fills count. Guaranteed minimum payouts are credited, so on a contract whose min is above 0 (e.g. dice sum [2, 12]), displayed cash can dip below 0. Available cash, and therefore final cash, never can.
- **Leaderboard.** P&L = cash − starting cash + positions marked to mid. With no two-sided market, positions are marked to the last trade. Once everything is settled, P&L is final and realized.

## Deploy

This app must run as **one instance with one worker**: the order books live in memory behind a single lock. It also needs a **persistent volume at `/data`** for the SQLite file. The Docker image reads `PORT`, `ADMIN_PASSWORD` and `DB_PATH` (default `/data/exchange.db`).

### Fly.io (recommended: cheap, and has volumes)

```bash
fly launch --no-deploy            # detects the Dockerfile; pick a name/region
fly volumes create data --size 1
fly secrets set ADMIN_PASSWORD='something-long'
```

Add this to the generated `fly.toml`:

```toml
[mounts]
  source = "data"
  destination = "/data"

[http_service]
  internal_port = 8000
  auto_stop_machines = "off"     # keep WebSockets alive during a game
  min_machines_running = 1
```

Then run `fly deploy && fly scale count 1`.

### Render

New → Web Service → choose the repo. Render detects the Dockerfile. Set the env var `ADMIN_PASSWORD`, and add a **Disk** mounted at `/data` (this needs a paid instance). Without a disk the database is wiped on every redeploy or restart, and free instances sleep when idle.

### Railway

New Project → Deploy from repo. Railway detects the Dockerfile. Add a **Volume** mounted at `/data`, and set `ADMIN_PASSWORD`.

## Layout

```
app/engine.py    order book + matching (pure, no I/O)
app/risk.py      worst-case / available-cash math (pure)
app/exchange.py  single-lock service: validate → plan → risk → one SQLite tx → apply
app/db.py        schema
app/main.py      REST + WebSocket hub (debounced full-state pushes; snapshot on connect)
static/          player page (index.html, app.js), admin page (admin.html, admin.js)
tests/           pytest suites
sim/simulate.py  concurrent invariant simulation (testing only)
```
