import sqlite3

conn = sqlite3.connect("trading.db")

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS market_snapshot (
    snapshot_time DATETIME,
    spot_price REAL,
    fut_price REAL,
    india_vix REAL,
    weekly_atm_iv REAL,
    monthly_atm_iv REAL,
    banknifty_spot REAL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS option_snapshot (
    snapshot_time DATETIME,
    symbol TEXT,
    strike INTEGER,
    option_type TEXT,
    expiry DATE,
    ltp REAL,
    iv REAL,
    delta REAL,
    theta REAL,
    gamma REAL,
    vega REAL,
    oi INTEGER
)
""")

# Persistent ledger of executed trades pulled from Kite once per trading
# day (see record_snapshots._maybe_fetch_trades_today). Primary key is
# Kite's trade_id so the daily pull is idempotent — re-runs INSERT OR
# IGNORE without duplicating rows.
cursor.execute("""
CREATE TABLE IF NOT EXISTS kite_trades (
    trade_id           TEXT PRIMARY KEY,
    user               TEXT NOT NULL,
    tradingsymbol      TEXT NOT NULL,
    exchange           TEXT NOT NULL,
    transaction_type   TEXT NOT NULL,
    quantity           INTEGER NOT NULL,
    average_price      REAL NOT NULL,
    order_id           TEXT,
    fill_timestamp     TEXT,
    exchange_timestamp TEXT,
    product            TEXT,
    fetched_at         TEXT NOT NULL
)
""")
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_kite_trades_symbol "
    "ON kite_trades(tradingsymbol)"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_kite_trades_fill "
    "ON kite_trades(fill_timestamp)"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_kite_trades_user "
    "ON kite_trades(user)"
)

# Audit trail of automated adjustments — one row per successful
# inward / outward roll or partial-reduce. Used by snapshots.html to
# correlate adjustment decisions with subsequent P&L per expiry.
# ``order_ids`` is a comma-separated list of Kite order IDs returned by
# /place_order, joinable to ``kite_trades.order_id`` after the next
# 15:35 trade-ledger fetch.
cursor.execute("""
CREATE TABLE IF NOT EXISTS adjustments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL,
    user                  TEXT NOT NULL,
    underlying            TEXT NOT NULL,
    expiry                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    adjustment_type       TEXT NOT NULL,
    short_strike_before   INTEGER,
    short_strike_after    INTEGER,
    short_delta_before    REAL,
    short_delta_after     REAL,
    hedge_strike_before   INTEGER,
    hedge_strike_after    INTEGER,
    hedge_delta_before    REAL,
    hedge_delta_after     REAL,
    nifty_spot            REAL,
    india_vix             REAL,
    order_ids             TEXT,
    realized_pnl          REAL
)
""")
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_adjustments_expiry "
    "ON adjustments(expiry)"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_adjustments_timestamp "
    "ON adjustments(timestamp)"
)

conn.commit()
conn.close()

print("Database created successfully")