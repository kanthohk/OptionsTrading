"""Read-side queries against trading.db (market_snapshot, option_snapshot).

Pure SQLite functions — no Flask, no analytics, no broker. Designed to be
called from main.py's Flask endpoints, ad-hoc scripts, and notebooks alike.
All rows are returned as dicts so callers can json.dumps() them directly.
"""
import sqlite3
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone('Asia/Kolkata')
DB_FILE = "trading.db"


def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # rows as dict-like
    return conn


def _today_ist():
    return datetime.now(IST).strftime('%Y-%m-%d')


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
    conn.commit()


def record_adjustment(**kwargs):
    """Insert one audit row into ``adjustments``. Unknown column names
    are silently ignored so the caller can pass a wide kwargs dict
    without worrying about schema drift.

    Returns the new row's ``id`` on success or ``None`` on failure.
    """
    row = {k: kwargs.get(k) for k in _ADJUSTMENT_COLUMNS}
    with _connect() as conn:
        _ensure_adjustments_table(conn)
        cols = list(row.keys())
        placeholders = ", ".join("?" * len(cols))
        cur = conn.execute(
            f"INSERT INTO adjustments ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(row.values()),
        )
        conn.commit()
        return cur.lastrowid


def read_adjustments(limit=200, expiry=None):
    """Read recent ``adjustments`` rows, newest first.

    Args:
        limit: max rows to return (default 200).
        expiry: optional ``YYYY-MM-DD`` filter; when set, only
            adjustments on that expiry are returned.

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
        if expiry:
            cur = conn.execute(
                "SELECT * FROM adjustments WHERE expiry = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (expiry, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM adjustments ORDER BY timestamp DESC LIMIT ?",
                (limit,),
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
            t.get('fill_timestamp'),
            t.get('exchange_timestamp'),
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
              returned. When omitted, every user's fills are returned.
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
                "WHERE user = ? "
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


def get_strikes_in_delta_band(min_abs_delta_pct=10.0, max_abs_delta_pct=15.0):
    """Latest option_snapshot rows whose ``|delta|`` (decimal) falls inside
    ``[min/100, max/100]``, grouped by expiry and option_type.

    Thresholds are provided in **percent** to match the dashboard's display
    convention (10 means ``|delta|>=0.10``). Returns::

        {
          'snapshot_time': '2026-06-18 14:30:00',
          'min_abs_delta_pct': 10.0,
          'max_abs_delta_pct': 15.0,
          'expiries': [
            {'expiry': '2026-06-23',
             'CE': [{'strike': N, 'symbol': '...', 'ltp': X, 'delta': 0.12}, ...],
             'PE': [...]},
            {'expiry': '2026-06-30', 'CE': [...], 'PE': [...]},
          ]
        }

    The expiries are sorted ascending — index 0 is the nearest weekly,
    index 1 the next. Each option-type list is sorted by strike. Empty
    ``expiries`` indicates no data in the DB (recorder not yet run).
    """
    min_d = float(min_abs_delta_pct) / 100.0
    max_d = float(max_abs_delta_pct) / 100.0
    with _connect() as conn:
        latest_row = conn.execute(
            "SELECT MAX(snapshot_time) AS t FROM option_snapshot"
        ).fetchone()
        latest = latest_row["t"] if latest_row else None
        if not latest:
            return {
                "snapshot_time": None,
                "min_abs_delta_pct": min_abs_delta_pct,
                "max_abs_delta_pct": max_abs_delta_pct,
                "expiries": [],
            }
        cur = conn.execute(
            "SELECT expiry, symbol, strike, option_type, ltp, delta "
            "FROM option_snapshot "
            "WHERE snapshot_time = ? "
            "  AND ABS(delta) BETWEEN ? AND ? "
            "ORDER BY expiry ASC, option_type ASC, strike ASC",
            (latest, min_d, max_d),
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
        })
    return {
        "snapshot_time": latest,
        "min_abs_delta_pct": min_abs_delta_pct,
        "max_abs_delta_pct": max_abs_delta_pct,
        "expiries": [{"expiry": exp, **data} for exp, data in sorted(by_expiry.items())],
    }


def list_latest_snapshot_expiries():
    """All distinct ``expiry`` values present in ``option_snapshot`` at the
    most recent ``snapshot_time``, sorted ascending.

    Used by the dashboard to populate the expiry dropdown independently of
    any delta-band filter — every expiry the recorder is currently writing
    should appear, even if its strikes don't happen to fall inside the
    iron-condor band that ``get_iron_condor_preset`` searches.
    """
    with _connect() as conn:
        latest_row = conn.execute(
            "SELECT MAX(snapshot_time) AS t FROM option_snapshot"
        ).fetchone()
        latest = latest_row["t"] if latest_row else None
        if not latest:
            return []
        cur = conn.execute(
            "SELECT DISTINCT expiry FROM option_snapshot "
            "WHERE snapshot_time = ? ORDER BY expiry ASC",
            (latest,),
        )
        return [r["expiry"] for r in cur.fetchall()]


def get_iron_condor_preset(delta_target_pct=10, hedge_points=200, expiry=None):
    """Build an iron-condor leg set for the requested ``expiry`` (or the
    next-weekly by default).

    Picks the short PE / short CE strikes whose ``|delta|`` is closest to
    ``delta_target_pct`` (default 10%), then constructs hedge legs
    ``hedge_points`` further OTM (PE: below short, CE: above short).

    Args:
        delta_target_pct: short-leg target |delta| in percent.
        hedge_points:     hedge distance in strike points.
        expiry:           YYYY-MM-DD to build for. When omitted, defaults to
                          the second nearest weekly (next-weekly), falling
                          back to the nearest if only one is present.

    Returns::

        {
          'snapshot_time', 'expiry', 'delta_target_pct', 'hedge_points',
          'available_expiries': ['YYYY-MM-DD', ...],
          'short_pe', 'short_ce', 'long_pe', 'long_ce'
        }

    ``available_expiries`` is sourced from every expiry the recorder wrote
    at the latest snapshot — NOT just those with strikes inside the delta
    band — so the dashboard dropdown stays in sync with whatever the
    recorder is covering even when a far-dated expiry has no candidate
    strikes for an iron-condor preset.
    """
    available_expiries = list_latest_snapshot_expiries()

    # Pull a generous band around the target so we have plenty of candidates
    band = get_strikes_in_delta_band(
        min_abs_delta_pct=max(2, delta_target_pct - 5),
        max_abs_delta_pct=delta_target_pct + 10,
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

    base_payload = {
        'snapshot_time': band.get('snapshot_time'),
        'expiry': chosen_expiry,
        'available_expiries': available_expiries,
        'delta_target_pct': delta_target_pct,
        'hedge_points': hedge_points,
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

    ce_list = next_exp.get('CE') or []
    pe_list = next_exp.get('PE') or []

    target = delta_target_pct / 100.0

    def _closest(candidates):
        if not candidates:
            return None
        return min(candidates, key=lambda r: abs(abs(r['delta']) - target))

    short_ce = _closest(ce_list)
    short_pe = _closest(pe_list)
    if not short_ce or not short_pe:
        return base_payload

    long_ce_strike = int(short_ce['strike']) + int(hedge_points)
    long_pe_strike = int(short_pe['strike']) - int(hedge_points)
    long_ce_symbol = short_ce['symbol'].replace(str(short_ce['strike']), str(long_ce_strike))
    long_pe_symbol = short_pe['symbol'].replace(str(short_pe['strike']), str(long_pe_strike))

    # Look up hedge LTP/delta from the same snapshot, if recorded.
    with _connect() as conn:
        latest_row = conn.execute(
            "SELECT MAX(snapshot_time) AS t FROM option_snapshot"
        ).fetchone()
        latest = latest_row["t"] if latest_row else None

        def _fetch_leg(symbol, fallback_strike):
            if not latest:
                return {'symbol': symbol, 'strike': fallback_strike,
                        'ltp': None, 'delta': None}
            cur = conn.execute(
                "SELECT symbol, strike, ltp, delta FROM option_snapshot "
                "WHERE snapshot_time = ? AND symbol = ? LIMIT 1",
                (latest, symbol),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            return {'symbol': symbol, 'strike': fallback_strike,
                    'ltp': None, 'delta': None}

        long_ce = _fetch_leg(long_ce_symbol, long_ce_strike)
        long_pe = _fetch_leg(long_pe_symbol, long_pe_strike)

    base_payload.update({
        'short_pe': short_pe,
        'short_ce': short_ce,
        'long_pe': long_pe,
        'long_ce': long_ce,
    })
    return base_payload


def _is_fresh(snapshot_time_str, max_age_seconds):
    """True iff ``snapshot_time_str`` (a ``YYYY-MM-DD HH:MM:SS`` IST string
    written by the recorder) is within ``max_age_seconds`` of now."""
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


def get_index_summary():
    """Return open/close/change/horizon-% for NIFTY 50 and INDIA VIX,
    derived from ``market_snapshot``.

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
          },
          'INDIA VIX': { ... same shape ... },
        }

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
            "ltp, iv, delta, theta, gamma, vega, oi "
            "FROM option_snapshot WHERE symbol = ? "
            "ORDER BY snapshot_time DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
    if not row or not _is_fresh(row["snapshot_time"], max_age_seconds):
        return None
    return dict(row)


def read_latest_option_snapshot(date_str=None):
    """Latest ``option_snapshot`` rows for the given date (IST today by
    default). Returns ``{'snapshot_time': <iso>, 'rows': [...]}`` where each
    row carries strike, option_type, expiry, ltp, iv, delta, theta, gamma,
    vega, oi. Returns ``{'snapshot_time': None, 'rows': []}`` when no data.

    Sort order: expiry ASC, strike ASC, option_type ASC (CE before PE) so the
    consumer can render a clean per-expiry chain.
    """
    date_str = date_str or _today_ist()
    with _connect() as conn:
        latest = _latest_snapshot_time(conn, date_str)
        if not latest:
            return {"snapshot_time": None, "rows": []}
        cur = conn.execute(
            "SELECT snapshot_time, symbol, strike, option_type, expiry, "
            "ltp, iv, delta, theta, gamma, vega, oi "
            "FROM option_snapshot "
            "WHERE snapshot_time = ? "
            "ORDER BY expiry ASC, strike ASC, option_type ASC",
            (latest,),
        )
        return {
            "snapshot_time": latest,
            "rows": [dict(row) for row in cur.fetchall()],
        }
