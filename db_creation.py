"""Create trading.db's tables and indexes. Safe to re-run.

Every statement here is ``IF NOT EXISTS``, so this is idempotent — but note
what that means for an EXISTING table: ``CREATE TABLE IF NOT EXISTS`` is a
no-op, so a column added to the DDL below does NOT appear on a database that
already has that table. Adding columns to an existing database is
``migrate_db.py``'s job, and that split is deliberate.

The consequence bit us: an index over a column this script may not have been
able to create would fail outright on an older database ("no such column:
client_key"). ``_create_index`` therefore checks the columns exist first and
reports what it skipped, so re-running this on a live database is safe and
tells you when a migration is outstanding.
"""
import sqlite3

from helpers.user_store import USERS_DDL

conn = sqlite3.connect("trading.db")

cursor = conn.cursor()

# Indexes skipped because their columns aren't on this database yet.
_skipped = []


def _columns(table):
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}


def _create_index(name, table, columns, unique=False):
    """Create an index, unless the database lacks one of its columns.

    Skipping rather than failing keeps this script re-runnable against a
    database that predates a column. The migration adds the column AND the
    index, so a skip here is a reminder, not a lost index.
    """
    present = _columns(table)
    missing = [c for c in columns if c not in present]
    if missing:
        _skipped.append((name, table, missing))
        return
    cursor.execute(
        f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {name} "
        f"ON {table}({', '.join(columns)})"
    )

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

# ``underlying`` is the Kite derivatives prefix (NIFTY, BANKNIFTY, ...).
# Without it NIFTY and BANKNIFTY rows are indistinguishable, since both
# land in this table keyed only by symbol/strike/expiry — and a 24500
# strike is a plausible strike for either index. Existing databases are
# migrated (column added, historical rows backfilled as NIFTY) by
# migrate_db.py.
cursor.execute("""
CREATE TABLE IF NOT EXISTS option_snapshot (
    snapshot_time DATETIME,
    underlying TEXT,
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
    oi INTEGER,
    -- Day's traded quantity, in shares (not lots). Recorded alongside oi
    -- because the two answer different questions: oi is the stock of
    -- outstanding positions, volume is whether anyone traded the strike
    -- TODAY. A newly listed strike has zero oi and can still be perfectly
    -- tradable, so liquidity gates test both. NULL means "not captured",
    -- which is not the same as zero — see helpers/liquidity notes in
    -- MarketAnalysis/db_queries.is_strike_liquid.
    volume INTEGER
)
""")
# Every read-side query filters on (underlying, snapshot_time) — the
# dashboard asks for "latest snapshot for BANKNIFTY" on each 30s poll.
_create_index("idx_option_snapshot_underlying_time", "option_snapshot",
              ["underlying", "snapshot_time"])

# Persistent ledger of executed trades pulled from Kite once per trading
# day (see fetch_db_data._maybe_fetch_trades_today). Primary key is
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
_create_index("idx_kite_trades_symbol", "kite_trades", ["tradingsymbol"])
_create_index("idx_kite_trades_fill", "kite_trades", ["fill_timestamp"])
_create_index("idx_kite_trades_user", "kite_trades", ["user"])

# Audit trail of automated adjustments — one row per successful
# inward / outward roll or partial-reduce. Used by snapshots.html to
# correlate adjustment decisions with subsequent P&L per expiry.
# ``order_ids`` is a comma-separated list of Kite order IDs returned by
# /place_order, joinable to ``kite_trades.order_id`` after the next
# 15:35 trade-ledger fetch.
cursor.execute("""
CREATE TABLE IF NOT EXISTS adjustments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Caller-generated idempotency key, unique. The adjustment write arrives
    -- over HTTP from the trading server, so a response lost after the row
    -- landed would otherwise retry into a duplicate audit row. NULL is
    -- allowed (and repeatable) for rows written before this column existed.
    client_key            TEXT,
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
_create_index("idx_adjustments_expiry", "adjustments", ["expiry"])
_create_index("idx_adjustments_timestamp", "adjustments", ["timestamp"])
# UNIQUE: this is what makes a retried adjustment write a no-op rather than a
# duplicate audit row. SQLite treats NULLs as distinct, so pre-existing rows
# without a key coexist freely.
_create_index("idx_adjustments_client_key", "adjustments", ["client_key"],
              unique=True)

# Dashboard logins (see helpers/user_store.py). The DDL lives there so the
# table this script creates and the one db_api creates on demand can't drift.
# Add users with tools/manage_users.py.
cursor.execute(USERS_DDL)

conn.commit()
conn.close()

print("Database created successfully")
if _skipped:
    print("\nThe following indexes were SKIPPED — this database predates the "
          "columns they cover:")
    for name, table, missing in _skipped:
        print(f"  {name}: {table} has no {', '.join(missing)}")
    print("\nRun `python migrate_db.py` to add the columns and their indexes. "
          "This script only creates tables, never alters them.")