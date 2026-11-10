"""Read-side queries against trading.db (market_snapshot, option_snapshot).

Pure SQLite functions — no Flask, no analytics, no broker. Designed to be
called from main.py's Flask endpoints, ad-hoc scripts, and notebooks alike.
All rows are returned as dicts so callers can json.dumps() them directly.
"""
import sqlite3
from datetime import datetime, timedelta
import pytz

from helpers.basic import (hedge_distance_for_dte, reprice_option_symbol,
                           normalise_broker_timestamp, wilder_rsi, RSI_PERIOD)
from helpers import underlyings as ul

IST = pytz.timezone('Asia/Kolkata')
DB_FILE = "trading.db"

# Liquidity floors. A strike clears them if EITHER measure does — see
# is_strike_liquid for why it is an OR and not an AND.
#
# Both are in shares, not lots (NIFTY lot 65, BANKNIFTY 30 as of Aug 2026),
# because that is the unit the broker reports.
MIN_OI_FOR_LTP = 1000
MIN_VOLUME_FOR_LTP = 1000


def is_strike_liquid(oi, volume, min_oi=None, min_volume=None):
    """Tri-state liquidity verdict for a strike: ``True`` / ``False`` /
    ``None`` (unknown).

    Open interest and volume answer different questions and neither alone is
    sufficient:

      - ``oi`` is the STOCK of outstanding positions. It is zero on a strike
        the exchange listed this morning, on every strike of a newly opened
        expiry cycle, and on anything already settled — none of which means
        untradeable. Index options are market-maker quoted, so a fresh strike
        is tradable from its first tick with no OI behind it at all.
      - ``volume`` is the FLOW: did anyone trade this today. It catches the
        new-strike case that OI misses, but is itself zero early in a session
        on a perfectly liquid strike.

    Hence OR: clearing either is evidence someone is actually there. Failing
    BOTH — nothing held and nothing traded — is the case worth refusing.

    ``None`` is returned only when both figures are missing, i.e. the row
    predates the ``volume`` migration or the broker sent neither field.
    Callers must not treat unknown as illiquid; a missing measurement is not
    a measurement of absence.
    """
    min_oi = MIN_OI_FOR_LTP if min_oi is None else min_oi
    min_volume = MIN_VOLUME_FOR_LTP if min_volume is None else min_volume
    known = False
    for value, floor in ((oi, min_oi), (volume, min_volume)):
        if value is None:
            continue
        known = True
        try:
            if int(value) >= floor:
                return True
        except (TypeError, ValueError):
            continue
    return False if known else None


def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # rows as dict-like
    return conn


def _today_ist():
    return datetime.now(IST).strftime('%Y-%m-%d')


_HAS_UNDERLYING_COLUMN = None


def _has_underlying_column(conn):
    """Whether ``option_snapshot`` carries the ``underlying`` column.

    Cached for the process lifetime — the schema doesn't change under a
    running server. A database that predates ``migrate_db.py`` lacks the
    column; rather than raising OperationalError on every dashboard poll,
    callers degrade to treating the whole table as the default underlying,
    which is factually what such a table holds.
    """
    global _HAS_UNDERLYING_COLUMN
    if _HAS_UNDERLYING_COLUMN is None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(option_snapshot)")}
        _HAS_UNDERLYING_COLUMN = 'underlying' in cols
    return _HAS_UNDERLYING_COLUMN


_HAS_VOLUME_COLUMN = None


def _has_volume_column(conn):
    """Whether ``option_snapshot`` carries the ``volume`` column.

    Same shape and same process-lifetime cache as ``_has_underlying_column``
    (including its restart-after-migration caveat), kept as a separate global
    rather than folded into a generic column cache so that function's
    existing monkeypatch points keep working unchanged.

    A database that predates the volume migration must still serve reads:
    ``_volume_select`` drops the column from the projection and rows come
    back without it, which ``is_strike_liquid`` reads as unknown.
    """
    global _HAS_VOLUME_COLUMN
    if _HAS_VOLUME_COLUMN is None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(option_snapshot)")}
        _HAS_VOLUME_COLUMN = 'volume' in cols
    return _HAS_VOLUME_COLUMN


def _volume_select(conn):
    """``", volume"`` when the column exists, else ``""`` — spliced into the
    SELECT list of every query that wants it."""
    return ", volume" if _has_volume_column(conn) else ""


def _underlying_filter(conn, underlying):
    """SQL fragment + params restricting ``option_snapshot`` to one underlying.

    Returns ``('', [])`` on a pre-migration schema. For the default
    underlying, NULL is matched too, so rows written between the ALTER and
    the backfill still resolve instead of vanishing from the dashboard.
    """
    name = ul.normalize(underlying)
    if not _has_underlying_column(conn):
        return "", []
    if name == ul.DEFAULT_UNDERLYING:
        return " AND (underlying = ? OR underlying IS NULL)", [name]
    return " AND underlying = ?", [name]


def _latest_time_for_underlying(conn, underlying):
    """Most recent ``option_snapshot`` timestamp FOR THIS UNDERLYING.

    Distinct from ``_latest_snapshot_time(conn, date_str)`` further down,
    which scopes by date instead — hence the different name.

    Scoped per underlying on purpose: if one index's capture fails for a
    cycle, a global ``MAX(snapshot_time)`` would point at the other index's
    newer row and this underlying would appear to have no data at all,
    rather than showing its last good snapshot.
    """
    clause, params = _underlying_filter(conn, underlying)
    row = conn.execute(
        f"SELECT MAX(snapshot_time) AS t FROM option_snapshot WHERE 1=1{clause}",
        params,
    ).fetchone()
    return row["t"] if row else None


def list_recorded_underlyings():
    """Distinct underlyings present in ``option_snapshot``, sorted with the
    default first so the dashboard's selector opens on NIFTY.

    Drives the underlying dropdown, so it reflects what the recorder is
    actually writing rather than what the registry could theoretically
    support.
    """
    with _connect() as conn:
        if not _has_underlying_column(conn):
            return [ul.DEFAULT_UNDERLYING]
        cur = conn.execute(
            "SELECT DISTINCT underlying FROM option_snapshot "
            "WHERE underlying IS NOT NULL AND underlying != ''"
        )
        found = sorted({r["underlying"] for r in cur.fetchall()})
    if not found:
        return [ul.DEFAULT_UNDERLYING]
    # Default first, remainder alphabetical.
    return ([ul.DEFAULT_UNDERLYING] if ul.DEFAULT_UNDERLYING in found else []) + \
           [u for u in found if u != ul.DEFAULT_UNDERLYING]


def list_snapshot_dates():
    """Distinct dates (newest first) that have at least one market_snapshot
    row. Drives the date picker on the UI."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT DISTINCT DATE(snapshot_time) AS d "
            "FROM market_snapshot "
            "ORDER BY d DESC"
        )
        return [row["d"] for row in cur.fetchall() if row["d"]]


_ADJUSTMENT_COLUMNS = (
    # Caller-generated idempotency key. The write now arrives over HTTP, so a
    # response lost after the row landed would otherwise be retried into a
    # duplicate audit row — which double-counts in the P&L chart. Unique
    # index; NULL for rows written before this existed (SQLite permits many
    # NULLs in a UNIQUE index, so no backfill is needed).
    'client_key',
    'timestamp', 'user', 'underlying', 'expiry', 'side', 'adjustment_type',
    'short_strike_before', 'short_strike_after',
    'short_delta_before',  'short_delta_after',
    'hedge_strike_before', 'hedge_strike_after',
    'hedge_delta_before',  'hedge_delta_after',
    'nifty_spot', 'india_vix', 'order_ids', 'realized_pnl',
)


def _ensure_adjustments_table(conn):
    """Lazy-create the ``adjustments`` table on existing deployments
    that haven't re-run db_creation.py. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS adjustments (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_adjustments_expiry "
        "ON adjustments(expiry)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_adjustments_timestamp "
        "ON adjustments(timestamp)"
    )
    # Self-heal the column on a table that predates it, so a write can't fail
    # just because migrate_db.py hasn't been run on this host yet. CREATE
    # TABLE IF NOT EXISTS above is a no-op on an existing table and would
    # leave the column missing.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(adjustments)")}
    if 'client_key' not in cols:
        conn.execute("ALTER TABLE adjustments ADD COLUMN client_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_adjustments_client_key "
        "ON adjustments(client_key)"
    )
    conn.commit()


def record_adjustment(**kwargs):
    """Insert one audit row into ``adjustments``. Unknown column names
    are silently ignored so the caller can pass a wide kwargs dict
    without worrying about schema drift.

    Returns ``(row_id, created)``. ``created`` is False when ``client_key``
    matched a row already present — the write is then a no-op and the
    existing id comes back, so a retried request is safe. Callers that pass
    no ``client_key`` always insert (and always get ``created=True``), which
    is the pre-existing behaviour for local writes.

    Returns ``(None, False)`` if the insert somehow affected nothing.
    """
    row = {k: kwargs.get(k) for k in _ADJUSTMENT_COLUMNS}
    key = row.get('client_key')
    with _connect() as conn:
        _ensure_adjustments_table(conn)
        if key:
            prior = conn.execute(
                "SELECT id FROM adjustments WHERE client_key = ?", (key,)
            ).fetchone()
            if prior:
                return prior['id'], False
        cols = list(row.keys())
        placeholders = ", ".join("?" * len(cols))
        # OR IGNORE so a concurrent retry that slipped past the check above
        # loses the race quietly rather than raising IntegrityError.
        cur = conn.execute(
            f"INSERT OR IGNORE INTO adjustments ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            tuple(row.values()),
        )
        conn.commit()
        if cur.rowcount:
            return cur.lastrowid, True
        if key:
            prior = conn.execute(
                "SELECT id FROM adjustments WHERE client_key = ?", (key,)
            ).fetchone()
            if prior:
                return prior['id'], False
        return None, False


def read_adjustments(limit=200, expiry=None, user=None):
    """Read recent ``adjustments`` rows, newest first.

    Args:
        limit: max rows to return (default 200).
        expiry: optional ``YYYY-MM-DD`` filter; when set, only
            adjustments on that expiry are returned.
        user: optional account filter. ``adjustments`` is per-account data
            — every row records which account the roll was placed for — so
            callers serving one account's dashboard must pass this. Omitting
            it returns EVERY account's rows, which is only right for
            operator tooling that means to look across accounts.

    Matching on ``user`` is case-insensitive. Reader and writer now resolve
    the name through one helper (``helpers.basic.trading_account``), so they
    agree by construction — but rows already in the table keep whatever
    casing was configured when they were written. Recasing the account in
    config would otherwise orphan its own history, and an empty audit panel
    reads as "nothing happened" rather than as a mismatch.

    Returns ``[]`` when the table doesn't exist yet (fresh deployment
    where no adjustment has fired) instead of erroring.
    """
    with _connect() as conn:
        guard = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='adjustments'"
        ).fetchone()
        if not guard:
            return []
        clauses, params = [], []
        if user:
            clauses.append("user = ? COLLATE NOCASE")
            params.append(user)
        if expiry:
            clauses.append("expiry = ?")
            params.append(expiry)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = conn.execute(
            f"SELECT * FROM adjustments{where} "
            "ORDER BY timestamp DESC LIMIT ?",
            params + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


def ensure_kite_trades_table(conn):
    """Lazy-create the ``kite_trades`` table + indexes. Idempotent —
    safe to call on every persist attempt. Used by both the daily 15:35
    recorder hook and the per-cycle persist call from
    ``Monitor/options.py``."""
    conn.execute("""
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kite_trades_symbol "
        "ON kite_trades(tradingsymbol)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kite_trades_fill "
        "ON kite_trades(fill_timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kite_trades_user "
        "ON kite_trades(user)"
    )
    conn.commit()


def persist_trades(trades, user, fetched_at=None):
    """``INSERT OR IGNORE`` the supplied trade fills into ``kite_trades``.

    Primary key is Kite's ``trade_id`` so repeated calls within the
    same day are no-ops — the daily 15:35 recorder fetch and the
    per-cycle fetch from ``Monitor/options.py`` can both run without
    duplicating rows.

    Args:
        trades: list of trade dicts as returned by ``kite.trades()``
            (via the broker proxy's ``/get_trades`` endpoint).
        user: account identifier the trades belong to.
        fetched_at: optional ISO-ish timestamp string; defaults to now
            in IST so callers don't need to pass one.

    Returns the number of newly-inserted rows (0 if every ``trade_id``
    was already present).
    """
    if not trades:
        return 0
    if fetched_at is None:
        fetched_at = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for t in trades:
        tid = t.get('trade_id')
        if not tid:
            continue
        rows.append((
            tid, user,
            t.get('tradingsymbol'), t.get('exchange'),
            t.get('transaction_type'),
            int(t.get('quantity') or 0),
            float(t.get('average_price') or 0),
            t.get('order_id'),
            # Normalised to 'YYYY-MM-DD HH:MM:SS' because read_trades orders
            # by this TEXT column. Kite's datetimes arrive here as RFC 1123
            # ("Fri, 03 Jul 2026 ...") after Flask serialises them, and that
            # sorts alphabetically by WEEKDAY NAME — which silently breaks
            # FIFO matching, since FIFO is order-dependent. See
            # helpers.basic.normalise_broker_timestamp.
            normalise_broker_timestamp(t.get('fill_timestamp')),
            normalise_broker_timestamp(t.get('exchange_timestamp')),
            t.get('product'),
            fetched_at,
        ))
    if not rows:
        return 0
    with _connect() as conn:
        ensure_kite_trades_table(conn)
        cur = conn.executemany("""
            INSERT OR IGNORE INTO kite_trades
            (trade_id, user, tradingsymbol, exchange, transaction_type,
             quantity, average_price, order_id, fill_timestamp,
             exchange_timestamp, product, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        return cur.rowcount


def read_trades(user=None):
    """All persisted Kite trade fills from the ``kite_trades`` ledger,
    sorted chronologically (FIFO matching requires this order).

    Returns an empty list when the table doesn't exist yet — fresh
    deployments hit this until the first 15:35 trade-fetch hook runs.

    Args:
        user: optional filter; when set, only that user's fills are
              returned. When omitted, every user's fills are returned —
              which would silently mix two accounts' lots into one FIFO
              chain, so every caller serving one account passes it.

    Matched case-insensitively, for the same reason as ``read_adjustments``:
    fills are written with whatever casing ``api_info.users`` carries, and
    read back against a hardcoded or lowercased name. An exact match would
    turn a config capitalisation into an empty ledger — and an empty ledger
    reads as "no realised P&L", not as a fault.
    """
    with _connect() as conn:
        # Defensive: the recorder lazy-creates this table on first
        # write. Until then a query against it would crash.
        guard = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='kite_trades'"
        ).fetchone()
        if not guard:
            return []
        if user:
            cur = conn.execute(
                "SELECT * FROM kite_trades "
                "WHERE user = ? COLLATE NOCASE "
                "ORDER BY fill_timestamp ASC, trade_id ASC",
                (user,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM kite_trades "
                "ORDER BY fill_timestamp ASC, trade_id ASC"
            )
        return [dict(r) for r in cur.fetchall()]


def read_market_snapshots(date_str=None):
    """All ``market_snapshot`` rows for the given YYYY-MM-DD date (IST today
    by default), ordered chronologically. Each row is a dict matching the
    table columns."""
    date_str = date_str or _today_ist()
    with _connect() as conn:
        cur = conn.execute(
            "SELECT snapshot_time, spot_price, fut_price, india_vix, "
            "weekly_atm_iv, monthly_atm_iv, banknifty_spot "
            "FROM market_snapshot "
            "WHERE DATE(snapshot_time) = ? "
            "ORDER BY snapshot_time ASC",
            (date_str,),
        )
        return [dict(row) for row in cur.fetchall()]


# ``market_snapshot`` is a WIDE table: one column per index rather than one row
# per underlying. Only these two have a spot column, so an expiry settlement
# can only be derived for them — anything else must be reported as unknown
# rather than guessed at.
SPOT_COLUMN_BY_UNDERLYING = {
    'NIFTY': 'spot_price',
    'BANKNIFTY': 'banknifty_spot',
}


def settlement_spot(underlying, date_str):
    """Last recorded spot for ``underlying`` on ``date_str`` (YYYY-MM-DD).

    Used to value options that expired rather than being closed by a trade:
    expiry produces no fill, so the only way to realise that P&L is to price
    the residual position at its intrinsic value on expiry day.

    Returns ``{'spot': float, 'snapshot_time': str}`` or ``None`` when the
    underlying has no spot column, the date has no rows, or every value on
    that date is NULL. Returning None (rather than a fallback) is deliberate:
    inventing a settlement price would silently book a wrong realised P&L into
    a live risk decision.

    CAVEAT: this is the last *recorded snapshot*, not NSE's official settlement
    price, which is the weighted average of the final 30 minutes. For strikes
    far from spot the difference is immaterial; for near-ATM strikes at expiry
    it is not.
    """
    column = SPOT_COLUMN_BY_UNDERLYING.get(underlying)
    if not column or not date_str:
        return None
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT snapshot_time, {column} AS spot FROM market_snapshot "
            f"WHERE DATE(snapshot_time) = ? AND {column} IS NOT NULL "
            f"ORDER BY snapshot_time DESC LIMIT 1",
            (date_str,),
        )
        row = cur.fetchone()
    if not row or row['spot'] is None:
        return None
    return {'spot': float(row['spot']), 'snapshot_time': row['snapshot_time']}


def _latest_snapshot_time(conn, date_str):
    cur = conn.execute(
        "SELECT MAX(snapshot_time) AS t "
        "FROM option_snapshot "
        "WHERE DATE(snapshot_time) = ?",
        (date_str,),
    )
    row = cur.fetchone()
    return row["t"] if row else None


def purge_option_snapshots_before(date_str):
    """Delete ``option_snapshot`` rows whose snapshot date is strictly before
    ``date_str`` (YYYY-MM-DD). Used by the daily post-market hook to keep
    only today's option history — older rows are not needed for any current
    UI surface and otherwise grow the table by tens of thousands of rows
    per trading day.

    ``market_snapshot`` is untouched (dashboard charts depend on multi-day
    history).
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM option_snapshot WHERE DATE(snapshot_time) < ?",
            (date_str,),
        )
        conn.commit()
        return cur.rowcount


def thin_option_snapshots_to_5min():
    """Delete ``option_snapshot`` rows not on a 5-minute boundary.

    Run once a day after market close to compact the day's 30-second captures
    down to one row per 5-minute mark per (symbol, snapshot_time). Idempotent:
    rerunning is a no-op once the table is already thinned.

    Kept rows satisfy ``minute % 5 == 0 AND seconds == 0`` (i.e. 09:15:00,
    09:20:00, …). ``market_snapshot`` is untouched.

    Returns the number of rows deleted.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM option_snapshot "
            "WHERE NOT ("
            "    CAST(STRFTIME('%M', snapshot_time) AS INTEGER) % 5 = 0 "
            "    AND STRFTIME('%S', snapshot_time) = '00'"
            ")"
        )
        conn.commit()
        return cur.rowcount


def get_strikes_in_delta_band(min_abs_delta_pct=10.0, max_abs_delta_pct=15.0,
                              underlying=None):
    """Latest option_snapshot rows whose ``|delta|`` (decimal) falls inside
    ``[min/100, max/100]``, grouped by expiry and option_type.

    Thresholds are provided in **percent** to match the dashboard's display
    convention (10 means ``|delta|>=0.10``). ``underlying`` scopes the query
    to one index (default NIFTY); the returned ``snapshot_time`` is that
    underlying's own latest capture. Returns::

        {
          'snapshot_time': '2026-06-18 14:30:00',
          'underlying': 'NIFTY',
          'min_abs_delta_pct': 10.0,
          'max_abs_delta_pct': 15.0,
          'expiries': [
            {'expiry': '2026-06-23',
             'CE': [{'strike': N, 'symbol': '...', 'ltp': X, 'delta': 0.12,
                     'oi': 1234000, 'volume': 56000}, ...],
             'PE': [...]},
            {'expiry': '2026-06-30', 'CE': [...], 'PE': [...]},
          ]
        }

    The expiries are sorted ascending — index 0 is the nearest weekly,
    index 1 the next. Each option-type list is sorted by strike. Empty
    ``expiries`` indicates no data in the DB (recorder not yet run).
    """
    name = ul.normalize(underlying)
    min_d = float(min_abs_delta_pct) / 100.0
    max_d = float(max_abs_delta_pct) / 100.0
    with _connect() as conn:
        latest = _latest_time_for_underlying(conn, name)
        if not latest:
            return {
                "snapshot_time": None,
                "underlying": name,
                "min_abs_delta_pct": min_abs_delta_pct,
                "max_abs_delta_pct": max_abs_delta_pct,
                "expiries": [],
            }
        clause, u_params = _underlying_filter(conn, name)
        cur = conn.execute(
            "SELECT expiry, symbol, strike, option_type, ltp, delta, oi"
            f"{_volume_select(conn)} "
            "FROM option_snapshot "
            "WHERE snapshot_time = ? "
            "  AND ABS(delta) BETWEEN ? AND ? "
            f"{clause} "
            "ORDER BY expiry ASC, option_type ASC, strike ASC",
            [latest, min_d, max_d] + u_params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    by_expiry = {}
    for r in rows:
        bucket = by_expiry.setdefault(r["expiry"], {"CE": [], "PE": []})
        bucket[r["option_type"]].append({
            "strike": r["strike"],
            "symbol": r["symbol"],
            "ltp": r["ltp"],
            "delta": r["delta"],
            "oi": r["oi"],
            # Absent on a pre-migration schema — stays None, i.e. unknown.
            "volume": r["volume"] if "volume" in r.keys() else None,
        })
    return {
        "snapshot_time": latest,
        "underlying": name,
        "min_abs_delta_pct": min_abs_delta_pct,
        "max_abs_delta_pct": max_abs_delta_pct,
        "expiries": [{"expiry": exp, **data} for exp, data in sorted(by_expiry.items())],
    }


def list_latest_snapshot_expiries(underlying=None):
    """All distinct ``expiry`` values present in ``option_snapshot`` at the
    most recent ``snapshot_time`` for ``underlying``, sorted ascending.

    Used by the dashboard to populate the expiry dropdown independently of
    any delta-band filter — every expiry the recorder is currently writing
    should appear, even if its strikes don't happen to fall inside the
    iron-condor band that ``get_iron_condor_preset`` searches.
    """
    name = ul.normalize(underlying)
    with _connect() as conn:
        latest = _latest_time_for_underlying(conn, name)
        if not latest:
            return []
        clause, u_params = _underlying_filter(conn, name)
        cur = conn.execute(
            "SELECT DISTINCT expiry FROM option_snapshot "
            f"WHERE snapshot_time = ?{clause} ORDER BY expiry ASC",
            [latest] + u_params,
        )
        return [r["expiry"] for r in cur.fetchall()]


def _suppress_illiquid_ltp(leg):
    """Return ``leg`` with ``ltp`` forced to 0 when neither its open interest
    nor its day volume clears the floors — see ``is_strike_liquid``.

    A copy is returned rather than mutating in place, so the delta-band rows
    the short legs came from keep reporting what the recorder actually saw.
    ``illiquid`` is set alongside so the dashboard can say why an LTP is 0
    instead of leaving it to look like a free option.

    An UNKNOWN verdict is left alone. Both figures missing means the row
    predates the volume migration or the broker sent neither — zeroing on
    absent data would blank legs that are perfectly tradable, and a missing
    measurement is not a measurement of absence.
    """
    if not leg:
        return leg
    if is_strike_liquid(leg.get('oi'), leg.get('volume')) is False:
        return {**leg, 'ltp': 0, 'illiquid': True}
    return leg


def _has_tradable_ltp(leg):
    """True when ``leg`` would show a non-zero LTP in the preset — a
    recorded price above 0 AND not suppressed by ``_suppress_illiquid_ltp``.

    The preset only builds on such strikes: a leg with nothing to fill at is
    no use as a short (no credit) or as a hedge (no protection price).
    """
    if not leg:
        return False
    ltp = _suppress_illiquid_ltp(leg).get('ltp')
    try:
        return ltp is not None and float(ltp) > 0
    except (TypeError, ValueError):
        return False


def get_iron_condor_preset(delta_target_pct=10, hedge_points=None, expiry=None,
                           hedge_cfg=None, underlying=None):
    """Build an iron-condor leg set for the requested ``expiry`` (or the
    next-weekly by default).

    Picks the short PE / short CE strikes whose ``|delta|`` is closest to
    ``delta_target_pct`` (default 10%), then the hedge legs at least
    ``hedge_points`` further OTM: the lowest CE strike >= short CE +
    hedge_points, and the highest PE strike <= short PE - hedge_points.
    Every leg must have a tradable LTP (> 0 and not illiquid-suppressed,
    see ``_has_tradable_ltp``); strikes that don't are skipped. A hedge with
    no qualifying strike is returned as ``None``.

    Args:
        delta_target_pct: short-leg target |delta| in percent.
        hedge_points:     hedge distance in strike points. When ``None``
                          (the default), it is derived from the chosen
                          expiry's days-to-expiry via
                          ``hedge_distance_for_dte`` using the ``hedge_cfg``
                          distances/boundaries, then snapped UP to a whole
                          number of strike steps for this underlying. Pass
                          an explicit int to override (it is still snapped —
                          an off-grid hedge strike does not exist).
        hedge_cfg:        the ``hedge_info`` block from Monitor/config.yaml
                          (near/mid/far distances + DTE boundaries). Missing
                          keys fall back to 100/200/300 @ 7/14 DTE.
        expiry:           YYYY-MM-DD to build for. When omitted, defaults to
                          the second nearest weekly (next-weekly), falling
                          back to the nearest if only one is present.
        underlying:       index to build for (default NIFTY). Governs both
                          which snapshot rows are searched and the strike
                          grid the hedge distance is snapped to.

    Returns::

        {
          'snapshot_time', 'expiry', 'underlying', 'strike_step',
          'delta_target_pct', 'hedge_points', 'min_oi', 'min_volume',
          'available_expiries': ['YYYY-MM-DD', ...],
          'short_pe', 'short_ce', 'long_pe', 'long_ce'
        }

    Each leg carries ``symbol``, ``strike``, ``ltp``, ``delta``, ``oi`` and
    ``volume``. A leg that clears NEITHER floor (see ``is_strike_liquid``) is
    reported with ``ltp`` 0 and ``illiquid`` True — its printed price is a
    leftover from an old trade, not a level anyone will fill at, so the
    preset must not imply a credit that isn't collectable. Clearing either
    floor is enough: a strike listed this morning has no OI yet and is still
    perfectly tradable.

    ``available_expiries`` is sourced from every expiry the recorder wrote
    at the latest snapshot — NOT just those with strikes inside the delta
    band — so the dashboard dropdown stays in sync with whatever the
    recorder is covering even when a far-dated expiry has no candidate
    strikes for an iron-condor preset.
    """
    name = ul.normalize(underlying)
    step = ul.strike_step(name)
    available_expiries = list_latest_snapshot_expiries(name)

    # Pull a generous band around the target so we have plenty of candidates
    band = get_strikes_in_delta_band(
        min_abs_delta_pct=max(2, delta_target_pct - 5),
        max_abs_delta_pct=delta_target_pct + 10,
        underlying=name,
    )
    band_expiries = band.get('expiries') or []

    if not available_expiries and not band_expiries:
        return None

    # Pick the expiry to build legs for. Honor the caller's choice when
    # provided; otherwise default to the second-nearest (next-weekly).
    chosen_expiry = None
    if expiry and expiry in available_expiries:
        chosen_expiry = expiry
    elif available_expiries:
        chosen_expiry = available_expiries[1] if len(available_expiries) > 1 else available_expiries[0]

    next_exp = next((e for e in band_expiries if e.get('expiry') == chosen_expiry), None)

    # Derive the hedge distance from the chosen expiry's DTE unless the
    # caller pinned an explicit value. Falls back to 200 (via
    # hedge_distance_for_dte) when the expiry can't be parsed.
    if hedge_points is None:
        dte = None
        if chosen_expiry:
            try:
                exp_date = datetime.strptime(chosen_expiry, '%Y-%m-%d').date()
                dte = (exp_date - datetime.now(IST).date()).days
            except (ValueError, TypeError):
                dte = None
        hedge_points = hedge_distance_for_dte(dte, hedge_cfg)

    # Snap the hedge distance to a whole number of strike steps. hedge_info
    # is configured in NIFTY-shaped points (100/200/300), which happen to be
    # multiples of NIFTY's 50-pt grid — but an underlying on a coarser grid
    # would otherwise get an off-grid hedge strike and a symbol for a
    # contract that does not exist. Rounds UP so the wing never ends up
    # narrower than configured.
    hedge_points = int(hedge_points)
    if step > 0 and hedge_points % step:
        hedge_points = ((hedge_points // step) + 1) * step

    base_payload = {
        'snapshot_time': band.get('snapshot_time'),
        'expiry': chosen_expiry,
        'underlying': name,
        'strike_step': step,
        'available_expiries': available_expiries,
        'delta_target_pct': delta_target_pct,
        'hedge_points': hedge_points,
        'min_oi': MIN_OI_FOR_LTP,
        'min_volume': MIN_VOLUME_FOR_LTP,
        'short_pe': None,
        'short_ce': None,
        'long_pe': None,
        'long_ce': None,
    }

    if next_exp is None:
        # The chosen expiry has no strikes inside the delta band — keep the
        # dropdown populated but return an empty preset so the dashboard
        # renders "—" for every leg instead of silently switching expiry.
        return base_payload

    # Only strikes with a tradable LTP are short-leg candidates.
    ce_list = [r for r in (next_exp.get('CE') or []) if _has_tradable_ltp(r)]
    pe_list = [r for r in (next_exp.get('PE') or []) if _has_tradable_ltp(r)]

    target = delta_target_pct / 100.0

    def _closest(candidates):
        if not candidates:
            return None
        return min(candidates, key=lambda r: abs(abs(r['delta']) - target))

    short_ce = _closest(ce_list)
    short_pe = _closest(pe_list)
    if not short_ce or not short_pe:
        return base_payload

    # Hedges sit AT LEAST hedge_points further OTM: the nearest strike with a
    # tradable LTP at or beyond short ± hedge_points. None when no strike in
    # that direction qualifies — the dashboard then refuses to deploy rather
    # than buy a hedge with no price.
    with _connect() as conn:
        latest = _latest_time_for_underlying(conn, name)
        clause, u_params = _underlying_filter(conn, name)

        def _find_hedge(option_type, bound, outward_op, order):
            if not latest:
                return None
            cur = conn.execute(
                "SELECT symbol, strike, ltp, delta, oi"
                f"{_volume_select(conn)} FROM option_snapshot "
                "WHERE snapshot_time = ? AND expiry = ? AND option_type = ? "
                f"  AND strike {outward_op} ?{clause} "
                f"ORDER BY strike {order}",
                [latest, chosen_expiry, option_type, bound] + u_params,
            )
            for row in cur:
                leg = {'volume': None, **dict(row)}
                if _has_tradable_ltp(leg):
                    return leg
            return None

        long_ce = _find_hedge('CE', int(short_ce['strike']) + int(hedge_points),
                              '>=', 'ASC')
        long_pe = _find_hedge('PE', int(short_pe['strike']) - int(hedge_points),
                              '<=', 'DESC')

    base_payload.update({
        'short_pe': _suppress_illiquid_ltp(short_pe),
        'short_ce': _suppress_illiquid_ltp(short_ce),
        'long_pe': _suppress_illiquid_ltp(long_pe),
        'long_ce': _suppress_illiquid_ltp(long_ce),
    })
    return base_payload


def _is_fresh(snapshot_time_str, max_age_seconds):
    """True iff ``snapshot_time_str`` (a ``YYYY-MM-DD HH:MM:SS`` IST string
    written by the recorder) is within ``max_age_seconds`` of now.

    ``max_age_seconds=None`` disables the check entirely — the row is
    always considered fresh. Used by callers that want the LAST available
    value regardless of age (e.g. showing an open position's delta after
    the recorder stops writing at 15:30), as opposed to the live-quote
    path which must fall through to KITE when the DB row goes stale.
    """
    if max_age_seconds is None:
        return True
    try:
        ts = datetime.strptime(snapshot_time_str, '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return False
    ts = IST.localize(ts)
    return (datetime.now(IST) - ts).total_seconds() <= max_age_seconds


def get_latest_market_snapshot(max_age_seconds=120):
    """Most recent ``market_snapshot`` row, or ``None`` if absent or stale.

    Stale = older than ``max_age_seconds`` (default 120s — recorder samples
    every 60s, so anything beyond 2 minutes signals the recorder is down
    and live broker data should be preferred).
    """
    with _connect() as conn:
        cur = conn.execute(
            "SELECT snapshot_time, spot_price, fut_price, india_vix, "
            "weekly_atm_iv, monthly_atm_iv, banknifty_spot "
            "FROM market_snapshot ORDER BY snapshot_time DESC LIMIT 1"
        )
        row = cur.fetchone()
    if not row or not _is_fresh(row["snapshot_time"], max_age_seconds):
        return None
    return dict(row)


_SUMMARY_COLUMNS = ("spot_price", "india_vix", "banknifty_spot")


def _check_column(column):
    """Allow only the known snapshot columns to be interpolated into raw SQL.
    Guards the f-string usage below against accidental injection."""
    if column not in _SUMMARY_COLUMNS:
        raise ValueError(f"unsupported snapshot column: {column!r}")


def _last_price_on_or_before(conn, date_str, column):
    """Latest snapshot value of ``column`` whose DATE is <= ``date_str``."""
    _check_column(column)
    cur = conn.execute(
        f"SELECT {column} AS v FROM market_snapshot "
        f"WHERE DATE(snapshot_time) <= ? AND {column} IS NOT NULL "
        f"ORDER BY snapshot_time DESC LIMIT 1",
        (date_str,),
    )
    row = cur.fetchone()
    return row["v"] if row else None


def _first_price_on_date(conn, date_str, column):
    """Earliest snapshot value of ``column`` on the given date."""
    _check_column(column)
    cur = conn.execute(
        f"SELECT {column} AS v FROM market_snapshot "
        f"WHERE DATE(snapshot_time) = ? AND {column} IS NOT NULL "
        f"ORDER BY snapshot_time ASC LIMIT 1",
        (date_str,),
    )
    row = cur.fetchone()
    return row["v"] if row else None


def _last_price_strictly_before(conn, date_str, column):
    """Latest snapshot value of ``column`` whose DATE is strictly < ``date_str``."""
    _check_column(column)
    cur = conn.execute(
        f"SELECT {column} AS v FROM market_snapshot "
        f"WHERE DATE(snapshot_time) < ? AND {column} IS NOT NULL "
        f"ORDER BY snapshot_time DESC LIMIT 1",
        (date_str,),
    )
    row = cur.fetchone()
    return row["v"] if row else None


# Calendar-day window of daily closes fed to the RSI. ~180 calendar days is
# ~120 trading days, far more than the 15 bars RSI(14) needs — the surplus is
# there so Wilder smoothing has converged and the dashboard figure matches a
# broker chart. Bounded (rather than "all history") to keep this a small,
# predictable scan: market_snapshot gets ~750 rows a day and has no index on
# snapshot_time, and this runs on every 30s dashboard poll.
RSI_LOOKBACK_DAYS = 180


def _daily_closes(conn, column, lookback_days=RSI_LOOKBACK_DAYS):
    """Daily closing values of ``column``, oldest first, over the last
    ``lookback_days`` calendar days.

    "Close" is the last snapshot of each day that actually carried a value
    for this column. The ``IS NOT NULL`` filter is what makes that true and
    is not cosmetic: the recorder writes one row per 30s cycle and any single
    field can come back empty (a failed VIX fetch, say). Taking the final row
    of the day unconditionally would then hand back a NULL close, and one
    NULL poisons the whole difference series.

    Note the last entry is TODAY's latest snapshot mid-session, i.e. a live
    price rather than a settled close — deliberate, so the reading moves
    during the day like the one on a broker's chart does.

    Relies on SQLite's documented bare-column behaviour: with a single
    ``MAX()`` in the select list, the unaggregated columns come from the row
    that produced that maximum.
    """
    _check_column(column)
    cutoff = (datetime.now(IST) - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    cur = conn.execute(
        f"SELECT DATE(snapshot_time) AS d, MAX(snapshot_time), {column} AS v "
        f"FROM market_snapshot "
        f"WHERE snapshot_time >= ? AND {column} IS NOT NULL "
        f"GROUP BY d ORDER BY d ASC",
        (cutoff,),
    )
    return [row["v"] for row in cur.fetchall()]


def get_index_summary():
    """Return open/close/change/horizon-%/RSI for NIFTY 50, BANKNIFTY and
    INDIA VIX, derived from ``market_snapshot``.

    Shape::

        {
          'NIFTY 50': {
            'open':         float | None,   # first snapshot of today (IST)
            'close':        float | None,   # latest snapshot today
            'change':       float | None,   # close - prev day's close
            'change_pct':   float | None,
            'change_3d_pct':  float | None,
            'change_1w_pct':  float | None,
            'change_1m_pct':  float | None,
            'change_3m_pct':  float | None,
            'rsi':            float | None, # Wilder RSI of daily closes
            'rsi_period':     int,
          },
          'INDIA VIX': { ... same shape ... },
        }

    ``rsi`` is ``None`` until this database holds ``rsi_period + 1`` days of
    closes for that column — which for BANKNIFTY means days since
    ``banknifty_spot`` started being recorded, not days since the table
    existed. The period is returned alongside the value so the UI can label
    it without hard-coding 14 in two places.

    "N days ago" walks calendar days back from today; the chosen price is
    the latest snapshot from the closest trading day on or before that
    target. Missing data anywhere along the chain yields ``None`` for that
    field — UI should render it as ``—``.
    """
    now = datetime.now(IST)
    today = now.strftime('%Y-%m-%d')
    horizons = (
        ('change_3d_pct', 3),
        ('change_1w_pct', 7),
        ('change_1m_pct', 30),
        ('change_3m_pct', 90),
    )

    result = {}
    with _connect() as conn:
        for label, column in (('NIFTY 50', 'spot_price'),
                              ('BANKNIFTY', 'banknifty_spot'),
                              ('INDIA VIX', 'india_vix')):
            open_today = _first_price_on_date(conn, today, column)
            close_today = _last_price_on_or_before(conn, today, column)
            prev_close = _last_price_strictly_before(conn, today, column)

            change = None
            change_pct = None
            if close_today is not None and prev_close is not None and prev_close != 0:
                change = close_today - prev_close
                change_pct = (change / prev_close) * 100.0

            entry = {
                'open': open_today,
                'close': close_today,
                'change': change,
                'change_pct': change_pct,
                'rsi': wilder_rsi(_daily_closes(conn, column)),
                'rsi_period': RSI_PERIOD,
            }

            for key, days in horizons:
                target = (now - timedelta(days=days)).strftime('%Y-%m-%d')
                past = _last_price_on_or_before(conn, target, column)
                if close_today is not None and past is not None and past != 0:
                    entry[key] = ((close_today - past) / past) * 100.0
                else:
                    entry[key] = None

            result[label] = entry

    return result


def get_latest_option_quote(symbol, max_age_seconds=120):
    """Most recent ``option_snapshot`` row for the given trading symbol
    (e.g. ``NIFTY25SEP24500PE``). Returns ``None`` if absent or stale."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT snapshot_time, symbol, strike, option_type, expiry, "
            f"ltp, iv, delta, theta, gamma, vega, oi{_volume_select(conn)} "
            "FROM option_snapshot WHERE symbol = ? "
            "ORDER BY snapshot_time DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
    if not row or not _is_fresh(row["snapshot_time"], max_age_seconds):
        return None
    return dict(row)


def read_latest_option_snapshot(date_str=None, underlying=None):
    """Latest ``option_snapshot`` rows for the given date (IST today by
    default). Returns ``{'snapshot_time': <iso>, 'underlying': ..., 'rows':
    [...]}`` where each row carries strike, option_type, expiry, ltp, iv,
    delta, theta, gamma, vega, oi, volume. Returns an empty ``rows`` when no
    data. ``volume`` is omitted entirely on a pre-migration schema.

    Scoped to a single ``underlying`` (default NIFTY) — without the filter
    a multi-underlying table would interleave two indices' chains in one
    strike-sorted list, where a 24500 NIFTY strike and a 24500 BANKNIFTY
    strike are indistinguishable to the reader.

    Sort order: expiry ASC, strike ASC, option_type ASC (CE before PE) so the
    consumer can render a clean per-expiry chain.
    """
    date_str = date_str or _today_ist()
    name = ul.normalize(underlying)
    with _connect() as conn:
        latest = _latest_snapshot_time(conn, date_str)
        if not latest:
            return {"snapshot_time": None, "underlying": name, "rows": []}
        clause, u_params = _underlying_filter(conn, name)
        cur = conn.execute(
            "SELECT snapshot_time, symbol, strike, option_type, expiry, "
            f"ltp, iv, delta, theta, gamma, vega, oi{_volume_select(conn)} "
            "FROM option_snapshot "
            f"WHERE snapshot_time = ?{clause} "
            "ORDER BY expiry ASC, strike ASC, option_type ASC",
            [latest] + u_params,
        )
        return {
            "snapshot_time": latest,
            "underlying": name,
            "rows": [dict(row) for row in cur.fetchall()],
        }
