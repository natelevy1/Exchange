import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    starting_cash INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'ended')),
    created_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    name TEXT NOT NULL COLLATE NOCASE,
    token TEXT NOT NULL UNIQUE,
    cash INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (session_id, name)
);
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('binary', 'numeric')),
    min_value INTEGER NOT NULL,
    max_value INTEGER NOT NULL,
    tick INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'open', 'halted', 'settled')),
    settlement_value INTEGER,
    created_at INTEGER NOT NULL,
    settled_at INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    tif TEXT NOT NULL CHECK (tif IN ('limit', 'ioc')),
    price INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled')),
    cancel_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_open ON orders (status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS orders_session ON orders (session_id);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    price INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    buyer_id INTEGER NOT NULL REFERENCES users(id),
    seller_id INTEGER NOT NULL REFERENCES users(id),
    buy_order_id INTEGER NOT NULL REFERENCES orders(id),
    sell_order_id INTEGER NOT NULL REFERENCES orders(id),
    aggressor_side TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_contract ON trades (contract_id, id);
CREATE TABLE IF NOT EXISTS positions (
    user_id INTEGER NOT NULL REFERENCES users(id),
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    qty INTEGER NOT NULL,
    PRIMARY KEY (user_id, contract_id)
);
CREATE TABLE IF NOT EXISTS settlements (
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    qty INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (contract_id, user_id)
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
