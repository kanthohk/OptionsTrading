"""Idempotent schema migrations for an existing ``trading.db``.

``db_creation.py`` only runs ``CREATE TABLE IF NOT EXISTS`` — it will not
alter a table that already exists, so a database created before a schema
change keeps the old columns silently. This script brings such a database
up to date and is safe to re-run: every step checks current state first.

Migrations applied:

1. ``option_snapshot.underlying`` — added for multi-underlying support.
   Historical rows are backfilled as ``'NIFTY'`` because that is factually
   what they are: the recorder was NIFTY-only until this change (it then
   hard-coded ``UNDERLYING = "NIFTY"``, and lived at
   ``MarketAnalysis/record_snapshots.py`` before being renamed to
   ``fetch_db_data.py``). The backfill is done in one UPDATE inside the
   same transaction as the ALTER, so the column is never left
   half-populated.

2. ``option_snapshot.volume`` — the day's traded quantity, added so
   liquidity checks can tell "nobody holds this strike" (zero OI) from
   "nobody trades this strike" (zero volume). Historical rows stay NULL:
   the value isn't recoverable, and NULL correctly reads as unknown.

3. ``idx_option_snapshot_underlying_time`` — the composite index every
   read-side query now needs.

4. ``kite_trades.fill_timestamp`` / ``exchange_timestamp`` — rewritten from
   RFC 1123 ("Fri, 03 Jul 2026 09:20:21 GMT") to ``'YYYY-MM-DD HH:MM:SS'``.
   ``read_trades`` orders by that TEXT column, and RFC 1123 sorts by weekday
   NAME, so the ledger came back in weekday blocks instead of chronologically
   — which silently corrupts FIFO realised-P&L matching.

Run: ``python migrate_db.py`` (optionally ``python migrate_db.py <path>``)
"""
import os
import sqlite3
import sys

from helpers.basic import normalise_broker_timestamp, parse_broker_timestamp

DB_FILE = "trading.db"
BACKFILL_UNDERLYING = "NIFTY"


def _columns(conn, table):
    """Set of column names on ``table``, empty if the table is absent."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def migrate_option_snapshot_underlying(conn):
    """Add ``option_snapshot.underlying`` and backfill historical rows.

    Returns a short status string describing what happened, so the caller
    can report accurately rather than always printing "migrated".
    """
    if not _table_exists(conn, "option_snapshot"):
        return "option_snapshot: table absent — run db_creation.py first"

    if "underlying" in _columns(conn, "option_snapshot"):
        # Column already present. Still backfill any NULLs — this covers a
        # database where a previous run added the column but died before
        # the UPDATE committed.
        cur = conn.execute(
            "UPDATE option_snapshot SET underlying = ? WHERE underlying IS NULL",
            (BACKFILL_UNDERLYING,),
        )
        if cur.rowcount:
            return (f"option_snapshot.underlying: already present; "
                    f"backfilled {cur.rowcount} NULL row(s) as {BACKFILL_UNDERLYING}")
        return "option_snapshot.underlying: already present, no NULLs — nothing to do"

    conn.execute("ALTER TABLE option_snapshot ADD COLUMN underlying TEXT")
    cur = conn.execute(
        "UPDATE option_snapshot SET underlying = ? WHERE underlying IS NULL",
        (BACKFILL_UNDERLYING,),
    )
    return (f"option_snapshot.underlying: column added; "
            f"backfilled {cur.rowcount} historical row(s) as {BACKFILL_UNDERLYING}")


def migrate_option_snapshot_volume(conn):
    """Add ``option_snapshot.volume`` (day's traded quantity, in shares).

    Deliberately NOT backfilled. Unlike ``underlying`` — where the historical
    value was knowable, because the recorder was NIFTY-only — nobody knows
    what volume those rows had, and the data isn't recoverable. They stay
    NULL, which reads as "not captured".

    That distinction is the point of this column. Liquidity gates treat NULL
    as unknown and zero as genuinely untraded; backfilling 0 here would
    declare every historical strike dead and is exactly the conflation the
    column exists to remove.
    """
    if not _table_exists(conn, "option_snapshot"):
        return "option_snapshot.volume: table absent — run db_creation.py first"
    if "volume" in _columns(conn, "option_snapshot"):
        return "option_snapshot.volume: already present — nothing to do"
    conn.execute("ALTER TABLE option_snapshot ADD COLUMN volume INTEGER")
    return ("option_snapshot.volume: column added; historical rows left NULL "
            "(unknown, not zero)")


def migrate_adjustments_client_key(conn):
    """Add ``adjustments.client_key`` plus its UNIQUE index.

    The adjustment write moved from a local INSERT to an HTTP call, so a
    response lost after the row landed would be retried into a duplicate
    audit row — which double-counts in the dashboard's P&L chart. The key is
    generated by the caller and carried through the retry.

    Historical rows stay NULL. SQLite treats NULLs as distinct in a UNIQUE
    index, so any number of them coexist and no backfill is needed.

    Skipped entirely when the table doesn't exist: it is lazy-created on the
    first adjustment (``_ensure_adjustments_table``), which builds the column
    and index itself.
    """
    if not _table_exists(conn, "adjustments"):
        return "adjustments.client_key: table absent (no adjustment has fired yet)"
    added = False
    if "client_key" not in _columns(conn, "adjustments"):
        conn.execute("ALTER TABLE adjustments ADD COLUMN client_key TEXT")
        added = True
    before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_adjustments_client_key'"
    ).fetchone()
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_adjustments_client_key "
        "ON adjustments(client_key)"
    )
    parts = ["column added" if added else "column already present",
             "index created" if not before else "index already present"]
    return "adjustments.client_key: " + "; ".join(parts)


def migrate_option_snapshot_index(conn):
    """Create the (underlying, snapshot_time) index if missing."""
    if not _table_exists(conn, "option_snapshot"):
        return "idx_option_snapshot_underlying_time: skipped (no table)"
    before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_option_snapshot_underlying_time'"
    ).fetchone()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_option_snapshot_underlying_time "
        "ON option_snapshot(underlying, snapshot_time)"
    )
    if before:
        return "idx_option_snapshot_underlying_time: already present"
    return "idx_option_snapshot_underlying_time: created"


def migrate_kite_trades_timestamps(conn):
    """Normalise ``kite_trades`` timestamps to ``'YYYY-MM-DD HH:MM:SS'``.

    ``read_trades`` orders by ``fill_timestamp``, a TEXT column. Kite's client
    parses these fields into datetimes and Flask then serialises them as
    RFC 1123 ("Fri, 03 Jul 2026 09:20:21 GMT"), so that is what landed in the
    DB. A TEXT sort over RFC 1123 orders by WEEKDAY NAME — Fri, Mon, Sat, Sun,
    Thu, Tue, Wed — so the ledger came back in seven weekday blocks rather
    than chronologically.

    That matters because FIFO matching is order-dependent: with three or more
    fills on a symbol it picks *which* lot a close is matched against, so a
    scrambled ledger silently produces wrong realised P&L. (A simple two-fill
    round trip is unaffected — ``(sell - buy) * qty`` is direction-agnostic.)

    ``persist_trades`` now normalises on write; this fixes the rows written
    before that. Idempotent: already-normalised values re-parse to themselves,
    and unparseable ones are left alone rather than destroyed.
    """
    if not _table_exists(conn, "kite_trades"):
        return "kite_trades timestamps: table absent - nothing to do"

    rows = conn.execute(
        "SELECT trade_id, fill_timestamp, exchange_timestamp FROM kite_trades"
    ).fetchall()
    if not rows:
        return "kite_trades timestamps: table empty - nothing to do"

    updates, unparseable, already = [], 0, 0
    for trade_id, fill_ts, exch_ts in rows:
        new_fill = normalise_broker_timestamp(fill_ts)
        new_exch = normalise_broker_timestamp(exch_ts)
        if new_fill == fill_ts and new_exch == exch_ts:
            already += 1
            # Distinguish "already ISO" from "could not be parsed": the latter
            # also compares equal, and must not be reported as fine.
            if fill_ts and parse_broker_timestamp(fill_ts) is None:
                unparseable += 1
                already -= 1
            continue
        updates.append((new_fill, new_exch, trade_id))

    if updates:
        conn.executemany(
            "UPDATE kite_trades SET fill_timestamp = ?, exchange_timestamp = ? "
            "WHERE trade_id = ?",
            updates,
        )

    parts = [f"kite_trades timestamps: {len(updates)} row(s) normalised"]
    if already:
        parts.append(f"{already} already canonical")
    if unparseable:
        parts.append(f"{unparseable} UNPARSEABLE and left untouched - "
                     f"these will sort first and pair wrongly in FIFO")
    return "; ".join(parts)


MIGRATIONS = (
    migrate_option_snapshot_underlying,
    migrate_option_snapshot_volume,
    migrate_adjustments_client_key,
    migrate_option_snapshot_index,
    migrate_kite_trades_timestamps,
)


def run(db_file=DB_FILE):
    """Apply every migration to ``db_file`` in a single transaction.

    Any failure rolls the whole thing back — a half-migrated schema is
    worse than an un-migrated one, because the recorder would start
    writing rows the read side can't interpret.
    """
    if not os.path.exists(db_file):
        raise SystemExit(f"{db_file} not found. Run db_creation.py first.")

    conn = sqlite3.connect(db_file)
    try:
        results = [m(conn) for m in MIGRATIONS]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return results


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DB_FILE
    for line in run(target):
        print(line)
    print(f"Migration complete: {target}")
