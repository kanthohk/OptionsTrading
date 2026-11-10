"""Standalone market snapshot recorder.

Runs every ``SAMPLE_SECONDS`` (default 30s) during NSE market hours
(09:15-15:30 IST, weekdays) and appends one row to ``market_snapshot``
plus N rows to ``option_snapshot`` in ``trading.db``. Uses the Flask proxy at ``url`` from Monitor/config.yaml
(same standalone pattern as autotrade.py and fetch_data.py) so it does not
need its own Kite session.

Which underlyings are captured is driven by ``underlyings.record`` in
Monitor/config.yaml (default ``[NIFTY]`` when the block is absent, which
preserves the historical NIFTY-only behaviour). Per-underlying facts —
strike step, NSE spot symbol, lot size — come from
``helpers/underlyings.py`` rather than literals in this file.

market_snapshot columns (ONE row per cycle, NIFTY-centric):
    snapshot_time, spot_price, fut_price, india_vix,
    weekly_atm_iv   -- ATM IV of the nearest weekly NIFTY expiry,
    monthly_atm_iv  -- ATM IV of the *next* weekly NIFTY expiry (column
                       kept by that name to avoid a DB migration;
                       semantics changed from monthly to next-weekly),
    banknifty_spot  -- BANKNIFTY spot LTP.

    This table stays single-index shaped on purpose. It backs the market
    overview cards and the DB-first quote cache, both of which want one
    row per timestamp. BANKNIFTY futures price and ATM IV are therefore
    NOT recorded — only its spot. If those are needed later, the honest
    fix is a per-underlying market table, not more prefixed columns.

option_snapshot columns (one row per strike per expiry per option_type
PER UNDERLYING):
    snapshot_time, underlying, symbol, strike, option_type, expiry,
    ltp, iv, delta, theta, gamma, vega, oi, volume

``oi`` and ``volume`` are stored exactly as the broker sent them, NULL
included. A missing field is unknown liquidity, which is not the same as a
strike nobody holds or trades — downstream gates rely on being able to tell
those apart, so neither is defaulted to 0 here.

Kite does not return IV or Greeks; both are computed locally via Black-
Scholes (Brent's method for IV, closed-form for the Greeks) using
``RISK_FREE_RATE``. IV is stored as a percentage to match the conventional
display (e.g. 15.5 for 15.5%); theta is per-day, vega is per-1% sigma.

Run: ``python fetch_db_data.py`` (this is the ``fetch_db_data`` scheduled job).
Prereq: run ``db_creation.py`` once so the tables exist.

Lives at the project root, not under ``MarketAnalysis/``, on purpose: running
``python fetch_db_data.py`` puts the root on ``sys.path[0]``, so the
``helpers.*`` / ``MarketAnalysis.*`` imports below resolve no matter which
directory the job is launched from. Under ``MarketAnalysis/`` they only
resolved when invoked as ``python -m``, and getting that wrong failed
silently. (Formerly ``MarketAnalysis/record_snapshots.py``.)
"""
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
import pytz

from helpers.market_data import proxy_get, kite_expiry_code, implied_vol, greeks
from helpers.basic import get_config
from helpers import underlyings as ul
from MarketAnalysis.db_queries import (
    thin_option_snapshots_to_5min,
    purge_option_snapshots_before,
    ensure_kite_trades_table,
    persist_trades,
)

IST = pytz.timezone('Asia/Kolkata')

# Every path below is resolved against this file's own directory rather than
# the process's current working directory. This runs as a scheduled job, and
# a job launched from some other directory would otherwise look for a
# trading.db that isn't there and then write its complaint into a log file in
# that same wrong directory — a real failure that presents as total silence.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(PROJECT_ROOT, "trading.db")
CONFIG_FILE = os.path.join(PROJECT_ROOT, "Monitor", "config.yaml")


def _setup_logger():
    """Per-day file logger writing to ``{YYYYMMDD}_recorder.log``.

    Mirrors ``helpers.basic.setup_logger`` shape (date-prefixed filename,
    cleared handlers on re-call) but uses its own logger name / file so it
    doesn't collide with the per-user trading logs. The recorder runs as
    a long-lived process; if it crosses midnight the file name stays at
    boot's date — restart daily or call this again to roll.
    """
    daystr = datetime.now(IST).strftime('%Y%m%d')
    logger_name = f"recorder_{daystr}"
    # Root-anchored, so the log always lands beside the code regardless of
    # the job's working directory. Filename pattern deliberately unchanged
    # from the record_snapshots.py era so existing log tooling keeps working.
    logfile = os.path.join(PROJECT_ROOT, f"{daystr}_recorder.log")

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()
    fh = logging.FileHandler(logfile, mode='a')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)
    logger.propagate = False
    return logger


logger = _setup_logger()

# The underlying backing the ``market_snapshot`` row (spot/fut/IV columns)
# and the market-overview cards. Distinct from the set of underlyings whose
# option chains get recorded — that is config-driven, see RECORD_UNDERLYINGS.
PRIMARY_UNDERLYING = "NIFTY"
NSE_INDEX_SYMBOL = ul.nse_symbol(PRIMARY_UNDERLYING)   # 'NIFTY 50'
NSE_VIX_SYMBOL = "INDIA VIX"
NSE_BANK_SYMBOL = ul.nse_symbol("BANKNIFTY")           # 'NIFTY BANK'

# Underlyings whose option chains are recorded each cycle. Populated from
# ``underlyings.record`` in config at startup by ``_load_underlyings()``;
# the module-level default keeps NIFTY-only behaviour if that never runs
# (e.g. a test importing a helper directly).
RECORD_UNDERLYINGS = [ul.DEFAULT_UNDERLYING]

# HARD CAP on strikes each side of ATM, not the operating value. The actual
# ladder width is per-underlying, configured in POINTS via `ladder_points`
# in helpers/underlyings.py (overridable from config) and converted to a
# strike count by `ul.strikes_each_side`. Points rather than strike counts
# because the same count spans different market distance on a 50-pt and a
# 100-pt grid.
#
# This cap exists only so a mistyped `ladder_points` can't blow up the
# kite.quote() batch — at 60 the widest single-underlying batch is
# 10 expiries x 121 strikes x 2 = 2,420 instruments. Rows actually written
# are still filtered by MIN_ABS_DELTA below.
MAX_STRIKES_AROUND_ATM = 60
# CAP on how many weekly-cadence expiries to record, counting forward from
# today. This is a ceiling on the LISTED set (see ``_expiry_plan``), not a
# generator: the dates themselves come from Kite's instrument dump, so the
# count no longer has to guess how many contracts exist.
#
# It used to be exactly that guess, and it was wrong in both directions.
# NSE lists 5 weeklies plus 3 monthlies for NIFTY, so ten consecutive
# Tuesdays asked for three contracts that were never listed while missing
# the third monthly — and separately it could not express a weekly that NSE
# had shifted off its Tuesday for a holiday.
#
# The cycle's total Kite-quote instrument count is roughly
# ``expiries * (2 * MAX_STRIKES_AROUND_ATM + 1) * 2 + 1``, and
# ``kite.quote()`` caps at 500 per call — the broker proxy chunks
# transparently when this exceeds the cap.
WEEKLY_EXPIRIES_COUNT = 10
# How many consecutive MONTHLY expiries to record for underlyings that don't
# list weeklies (BANKNIFTY since NSE's 2024 rationalisation). Three covers the
# current month plus two ahead — roughly the same forward window in calendar
# terms as the 10 weeklies above, at a tenth of the instrument count.
#
# Applied per-underlying via ``_expiries_for``. Handing a monthly-only
# underlying the weekly list instead is not a visible error: every non-monthly
# Tuesday becomes a symbol for a contract that was never listed, Kite returns
# nothing for it, and the underlying records little or nothing at all.
MONTHLY_EXPIRIES_COUNT = 3
# Forward cut-off applied to the LISTED expiry set. Kite lists NIFTY options
# out to roughly three years — quarterly and half-yearly contracts sitting
# well beyond the near weeklies and monthlies. Those carry no open interest
# we would ever trade against, and quoting them would add ~122 instruments
# each to every 30-second cycle.
#
# 120 days comfortably covers NSE's 5 weeklies + 3 monthlies with room for
# the listing window to move, while excluding the long-dated tail. The count
# caps above still apply on top of this; whichever binds first, wins.
EXPIRY_HORIZON_DAYS = 120
# Only persist option rows whose absolute Black-Scholes delta exceeds this
# threshold (0.02 ≈ 2 on the percent scale most chains display). Deep-OTM
# strikes below this are skipped — they have no actionable greek and just
# bloat the table. Lowered from 0.04 so the low-delta adjustment trigger
# (delta_low_close in Monitor/config.yaml, currently 5) can still see
# strikes around |delta|≈3%–5% in the DB and react before they roll off.
MIN_ABS_DELTA = 0.02

MARKET_START_HM = (9, 15)
MARKET_STOP_HM = (15, 30)
# Daily trade-ledger fetch: 5 minutes after market close on every weekday.
# Pulls the day's executed trades from Kite via /get_trades and persists
# them to ``kite_trades`` for the FIFO realised-P&L ledger in brain.py.
FETCH_TRADES_HM = (15, 35)
SAMPLE_SECONDS = 30  # capture cadence; must divide 60 evenly so boundaries align


# ---- Expiry helpers --------------------------------------------------------

def _next_weekday(today, weekday):
    """Next date (inclusive) whose weekday matches ``weekday`` (Mon=0 .. Sun=6)."""
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta)


def _last_weekday_of_month(year, month, weekday=1):
    """Last given weekday (default Tuesday) of (year, month)."""
    if month == 12:
        first_of_next = datetime(year + 1, 1, 1).date()
    else:
        first_of_next = datetime(year, month + 1, 1).date()
    d = first_of_next - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _nearest_monthly_expiry(today):
    """Nearest monthly expiry on/after ``today`` (last Tuesday of the month).
    Used for the NIFTY futures symbol — futures only have monthly expiries."""
    candidate = _last_weekday_of_month(today.year, today.month)
    if candidate >= today:
        return candidate
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    return _last_weekday_of_month(year, month)


def _is_monthly_expiry(expiry_date):
    """True iff ``expiry_date`` is the last Tuesday of its month. Used to
    choose the Kite symbol format: monthly weeklies serialize as
    ``YY+MON3`` (e.g. ``26JUN``), regular weeklies as ``YY+M+DD``
    (e.g. ``26623``)."""
    return expiry_date == _last_weekday_of_month(expiry_date.year, expiry_date.month, 1)


def _weekly_expiry_list(today, count):
    """``count`` consecutive weekly expiries from the nearest Tuesday on or
    after ``today``. Some of them will happen to be the last Tuesday of
    their month; ``_is_monthly_expiry`` decides the symbol format
    per-expiry, so no special-casing is needed here."""
    first = _next_weekday(today, weekday=1)
    return [first + timedelta(days=7 * i) for i in range(count)]


def _monthly_expiry_list(today, count):
    """``count`` consecutive MONTHLY expiries from the nearest one on or
    after ``today`` (each the last Tuesday of its month).

    Used for underlyings that don't list weeklies. Before this existed the
    recorder handed every underlying the same weekly Tuesday list, which
    for BANKNIFTY meant ~8 of 10 expiries were contracts that were never
    listed: Kite returns no quote, nothing is written, and the failure
    looks like an empty dropdown rather than an error.
    """
    out = []
    cursor = _nearest_monthly_expiry(today)
    for _ in range(count):
        out.append(cursor)
        # Step into the following month and take its last Tuesday.
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        cursor = _last_weekday_of_month(year, month)
    return out


def _expiries_for(underlying, today):
    """GENERATED expiry list for ``underlying``, per its registry cadence.

    Weekly underlyings get ``WEEKLY_EXPIRIES_COUNT`` Tuesdays; monthly-only
    ones get ``MONTHLY_EXPIRIES_COUNT`` last-Tuesdays.

    This is now the FALLBACK, not the primary source — see ``_expiry_plan``.
    Generated dates cannot know about a contract NSE shifted off its usual
    weekday, and they invent expiries NSE never listed. Kept because the
    recorder must keep capturing when the instrument dump is unreachable,
    and a mostly-right chain beats no chain at all.
    """
    if ul.has_weekly_expiries(underlying):
        return _weekly_expiry_list(today, WEEKLY_EXPIRIES_COUNT)
    return _monthly_expiry_list(today, MONTHLY_EXPIRIES_COUNT)


# ---- Listed expiries (the authoritative source) ---------------------------

# Cache of the proxy's answer, keyed by (underlying, segment) -> (day, dates).
# Kite regenerates the instrument dump once a day, so one call per key per
# day is enough; at a 30s cadence that is ~780 calls/day saved.
#
# A FAILED lookup is not cached. A transient proxy error on the first cycle
# of the session must not pin the recorder to generated dates until midnight.
_LISTED_EXPIRY_CACHE = {}


def _fetch_listed_expiries(underlying, segment):
    """Ask the proxy which expiries Kite lists. Returns a sorted list of
    ``date`` objects, or None when the answer is unavailable.

    None and ``[]`` are both returned as None on purpose here: an underlying
    with genuinely nothing listed and an underlying we could not ask about
    lead to the same decision at the call site (fall back and say so), and
    conflating them keeps that decision in one place.
    """
    payload = proxy_get(f"/get_expiries/{underlying}",
                        params={'segment': segment}, logger=logger)
    if not payload:
        return None
    out = []
    for raw in payload.get('expiries') or []:
        try:
            out.append(datetime.strptime(raw, '%Y-%m-%d').date())
        except (TypeError, ValueError):
            logger.warning(f"{underlying}: ignoring unparseable expiry "
                           f"{raw!r} from /get_expiries")
    return sorted(out) or None


def _listed_expiries(underlying, today, segment="NFO-OPT"):
    """Per-day cached ``_fetch_listed_expiries``. None when unavailable."""
    key = (underlying, segment)
    cached_day, cached = _LISTED_EXPIRY_CACHE.get(key, (None, None))
    if cached_day == today and cached:
        return cached
    expiries = _fetch_listed_expiries(underlying, segment)
    if not expiries:
        return None
    _LISTED_EXPIRY_CACHE[key] = (today, expiries)
    logger.info(f"{underlying} {segment}: Kite lists {len(expiries)} expiries; "
                f"next 10: "
                f"{', '.join(e.isoformat() for e in expiries[:10])}")
    return expiries


def _monthly_expiry_flags(expiries):
    """``{expiry: is_monthly}``, decided by POSITION IN THE LISTED SET.

    Kite serialises the last expiry of a calendar month as ``YY+MON3``
    (``26OCT``) and every other one as ``YY+M+DD`` (``26O19``). Deriving
    that from the listed set rather than from calendar arithmetic is what
    makes holiday shifts a non-event in both directions:

      * a WEEKLY shifted off its Tuesday (19-Oct-2026, moved by Dussehra) is
        still not the last October expiry, so it still formats as a weekly;
      * a MONTHLY shifted off its last Tuesday is still the last expiry of
        its month, so it still formats as a monthly.

    ``_is_monthly_expiry`` gets the second case wrong — it asks "is this the
    last Tuesday", which a shifted monthly is not, and would then emit a
    weekly-format symbol for a contract Kite names with MON3.

    Must be computed over the FULL listed set, BEFORE any horizon cut:
    truncating first would make the last survivor of the final month look
    like that month's monthly.
    """
    last_of_month = {}
    for exp in expiries:
        key = (exp.year, exp.month)
        if key not in last_of_month or exp > last_of_month[key]:
            last_of_month[key] = exp
    return {exp: last_of_month[(exp.year, exp.month)] == exp
            for exp in expiries}


def _expiry_plan(underlying, today):
    """``[(expiry_date, is_monthly), ...]`` to quote for ``underlying``.

    Prefers what Kite actually lists; falls back to generated cadence dates
    and says so loudly, because the fallback is precisely the behaviour that
    loses holiday-shifted expiries.
    """
    listed = _listed_expiries(underlying, today)
    if listed:
        # Flags over the full set, then trim — see _monthly_expiry_flags.
        flags = _monthly_expiry_flags(listed)
        horizon = today + timedelta(days=EXPIRY_HORIZON_DAYS)
        cap = (WEEKLY_EXPIRIES_COUNT if ul.has_weekly_expiries(underlying)
               else MONTHLY_EXPIRIES_COUNT)
        chosen = [e for e in listed if today <= e <= horizon][:cap]
        if chosen:
            return [(e, flags[e]) for e in chosen]
        logger.warning(
            f"{underlying}: Kite lists {len(listed)} expiries but none fall "
            f"between today and +{EXPIRY_HORIZON_DAYS}d. Raise "
            f"EXPIRY_HORIZON_DAYS if this underlying is genuinely only "
            f"listed further out."
        )
    logger.warning(
        f"{underlying}: could not read listed expiries from the proxy — "
        f"falling back to GENERATED dates. Any expiry NSE shifted off its "
        f"usual weekday (a holiday-adjusted contract) will be quoted on the "
        f"wrong date and record nothing until the dump is readable again."
    )
    return [(e, _is_monthly_expiry(e)) for e in _expiries_for(underlying, today)]


def _futures_expiry(underlying, today):
    """Nearest listed futures expiry, or last-Tuesday arithmetic as fallback.

    Futures shift on holidays exactly like options do; when they did, the
    generated symbol named a contract that doesn't exist and
    ``market_snapshot.fut_price`` silently went NULL for that stretch.
    """
    listed = _listed_expiries(underlying, today, segment="NFO-FUT")
    if listed:
        upcoming = [e for e in listed if e >= today]
        if upcoming:
            return upcoming[0]
    return _nearest_monthly_expiry(today)


def _option_inst(underlying, expiry_date, strike, opt_type, monthly):
    return (f"NFO:{underlying}{kite_expiry_code(expiry_date, monthly)}"
            f"{strike}{opt_type}")


def _futures_inst(underlying, expiry_date):
    return f"NFO:{underlying}{kite_expiry_code(expiry_date, monthly=True)}FUT"


# ---- Capture cycle ---------------------------------------------------------

def _atm_strike(underlying, spot):
    """Nearest tradable strike — 50-pt grid for NIFTY, 100-pt for
    BANKNIFTY. Delegates to the registry so the step lives in one place."""
    return ul.atm_strike(underlying, spot)


def _load_underlyings():
    """Read the ``underlyings`` config block into module state.

    Applies operator overrides (lot size, strike step) to the registry and
    sets ``RECORD_UNDERLYINGS``. Best-effort: a config read failure leaves
    the NIFTY-only default in place and logs, rather than preventing the
    recorder from starting.
    """
    global RECORD_UNDERLYINGS
    try:
        cfg = get_config(CONFIG_FILE)
    except Exception as e:
        logger.warning(f"Could not read underlyings config, defaulting to "
                       f"{RECORD_UNDERLYINGS}: {e}")
        return RECORD_UNDERLYINGS
    ul.load_underlying_overrides(cfg)
    RECORD_UNDERLYINGS = ul.configured_underlyings(cfg)
    return RECORD_UNDERLYINGS


def _capture_options(underlying, quotes, inst_meta, spot, today, snapshot_time):
    """Turn a quote payload into ``option_snapshot`` row tuples.

    Split out of ``_capture`` so each underlying's chain is processed
    independently — a BANKNIFTY failure can't lose the NIFTY rows for the
    same cycle. Returns ``(rows, per_expiry_counts, skipped_low_delta)``.
    """
    rows = []
    skipped_low_delta = 0
    per_expiry = {}
    for inst, (strike, opt_type, exp_date, monthly) in inst_meta.items():
        exp_key = exp_date.isoformat()
        counts = per_expiry.setdefault(
            exp_key, {'written': 0, 'missing_quote': 0,
                      'zero_ltp': 0, 'low_delta': 0})
        q = quotes.get(inst)
        if not q:
            counts['missing_quote'] += 1
            continue
        ltp = q.get('last_price', 0)
        if ltp <= 0:
            counts['zero_ltp'] += 1
            continue
        t = max((exp_date - today).days / 365.0, 1.0 / 365.0)
        sigma = implied_vol(spot, strike, t, ltp, opt_type)
        delta, theta, gamma, vega = greeks(spot, strike, t, sigma, opt_type)
        # Filter out deep-OTM strikes — anything with |delta| <= MIN_ABS_DELTA
        # adds no analytic value and is skipped at insert time. The wider
        # MAX_STRIKES_AROUND_ATM query above just ensures we have enough
        # candidates on both sides.
        if abs(delta) <= MIN_ABS_DELTA:
            skipped_low_delta += 1
            counts['low_delta'] += 1
            continue
        sym = inst.split(':', 1)[1]
        # oi/volume are stored as-is, INCLUDING None. They used to default to
        # 0, which made "the broker didn't send this field" indistinguishable
        # from "nothing is open / nothing traded" — and downstream liquidity
        # gates read that 0 as a dead strike. NULL means unknown; let it.
        #
        # kite.quote() names the day's traded quantity ``volume``; the ticker
        # payload calls the same figure ``volume_traded``. Accept either so a
        # quote sourced from the websocket path doesn't silently record NULL.
        volume = q.get('volume')
        if volume is None:
            volume = q.get('volume_traded')
        rows.append((snapshot_time, underlying, sym, strike, opt_type,
                     exp_date.isoformat(), ltp, sigma * 100.0,
                     delta, theta, gamma, vega, q.get('oi'), volume))
        counts['written'] += 1
    return rows, per_expiry, skipped_low_delta


# Whether option_snapshot carries the ``volume`` column. Probed once per
# process — the schema doesn't change under a running recorder.
_HAS_VOLUME_COLUMN = None


def _insert_option_rows(conn, rows, logger):
    """Write option_snapshot rows, dropping ``volume`` on a database that
    predates ``migrate_db.py``'s volume migration.

    The recorder is the one job that must not die on a schema gap: losing a
    column costs a field, losing the process costs the whole day's capture.
    So an un-migrated DB keeps recording everything else and says so in the
    log, once, rather than raising OperationalError every cycle.
    """
    global _HAS_VOLUME_COLUMN
    if _HAS_VOLUME_COLUMN is None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(option_snapshot)")}
        _HAS_VOLUME_COLUMN = 'volume' in cols
        if not _HAS_VOLUME_COLUMN:
            logger.warning(
                "option_snapshot has no 'volume' column — recording without "
                "it. Run migrate_db.py to enable volume-based liquidity "
                "checks; adjustments will fall back to OI alone until then."
            )
    if _HAS_VOLUME_COLUMN:
        conn.executemany(
            "INSERT INTO option_snapshot "
            "(snapshot_time, underlying, symbol, strike, option_type, expiry, "
            "ltp, iv, delta, theta, gamma, vega, oi, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return
    conn.executemany(
        "INSERT INTO option_snapshot "
        "(snapshot_time, underlying, symbol, strike, option_type, expiry, "
        "ltp, iv, delta, theta, gamma, vega, oi) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [r[:-1] for r in rows],
    )


def _spot_of(underlying):
    """Live spot LTP for ``underlying``, or None.

    ``live=true`` bypasses main.py's DB-first cache so the recorder always
    pulls fresh broker prices — otherwise it would read back its own prior
    writes and the table would stop advancing.
    """
    nse = ul.nse_symbol(underlying)
    # logger= so a failure says WHY. A missing spot aborts the whole cycle
    # ("spot unavailable; skipping cycle"), and that message on its own
    # cannot distinguish "the trading server is unreachable from here" from
    # "the broker refused the quote" — which is exactly the distinction
    # needed when the recorder and the proxy live on different hosts.
    payload = proxy_get(f"/get_current_price/{nse}/stock",
                        params={'live': 'true'}, logger=logger)
    return payload and payload.get(f"NSE:{nse}", {}).get('last_price')


def _capture_underlying(conn, underlying, snapshot_time, today, spot):
    """Quote and persist one underlying's option chain for this cycle.

    The expiry list is derived here, per underlying, rather than being
    handed down from ``_capture`` — NIFTY gets weeklies, BANKNIFTY gets
    monthlies. See ``_expiries_for``.

    Returns ``(written, atm, fut_price, near_iv, next_iv)``. The IV/futures
    values are only consumed for the primary underlying's market_snapshot
    row; they're computed here because this is where the quote payload is.
    """
    step = ul.strike_step(underlying)
    atm = _atm_strike(underlying, spot)
    # Ladder width is per-underlying and expressed in points: NIFTY ±1,500,
    # BANKNIFTY ±5,000. BANKNIFTY needs the wider span because |delta| 5%
    # (delta_low_close, the inward-roll trigger) sits ~3,100 points OTM at a
    # 54,000 spot — a ±3,000 ladder stops just short of it, so the engine
    # would have no candidate strike to roll into.
    n_side = ul.strikes_each_side(underlying, hard_cap=MAX_STRIKES_AROUND_ATM)
    strikes = [atm + i * step for i in range(-n_side, n_side + 1)]

    # Expiries Kite actually lists, with the symbol format decided from the
    # same set — see _expiry_plan. Generated Tuesdays are the fallback only.
    expiry_meta = _expiry_plan(underlying, today)
    if not expiry_meta:
        logger.error(f"{underlying}: no expiries to quote this cycle; "
                     f"skipping this underlying.")
        return 0, atm, None, None, None
    expiries = [exp for exp, _ in expiry_meta]
    monthly_of = dict(expiry_meta)
    # "near"/"next" are just the first two expiries of whatever cadence this
    # underlying uses. Only PRIMARY_UNDERLYING's values reach market_snapshot.
    near_weekly_exp = expiries[0]
    next_weekly_exp = expiries[1] if len(expiries) > 1 else expiries[0]

    # Futures only have monthly expiries, but those shift on holidays too —
    # _futures_expiry prefers the listed date over last-Tuesday arithmetic.
    fut_inst = (_futures_inst(underlying, _futures_expiry(underlying, today))
                if ul.has_futures(underlying) else None)
    fut_price = None

    instruments = [fut_inst] if fut_inst else []
    inst_meta = {}  # option inst -> (strike, opt_type, expiry_date, monthly)
    for exp_date, monthly in expiry_meta:
        for strike in strikes:
            for opt_type in ('CE', 'PE'):
                inst = _option_inst(underlying, exp_date, strike, opt_type, monthly)
                instruments.append(inst)
                inst_meta[inst] = (strike, opt_type, exp_date, monthly)

    quotes = proxy_get("/get_quotes",
                       params={'instruments': ','.join(instruments),
                               'live': 'true'}) or {}

    if fut_inst:
        fut_q = quotes.get(fut_inst)
        if fut_q:
            fut_price = fut_q.get('last_price')

    def atm_iv_pct(exp_date, monthly):
        """Average ATM CE/PE IV in percent, or None when neither solves."""
        t = max((exp_date - today).days / 365.0, 1.0 / 365.0)
        ivs = []
        for opt_type in ('CE', 'PE'):
            q = quotes.get(_option_inst(underlying, exp_date, atm, opt_type, monthly))
            if not q:
                continue
            price = q.get('last_price', 0)
            if price <= 0:
                continue
            iv = implied_vol(spot, atm, t, price, opt_type)
            if iv > 0:
                ivs.append(iv)
        if not ivs:
            return None
        return (sum(ivs) / len(ivs)) * 100.0

    near_iv = atm_iv_pct(near_weekly_exp, monthly_of[near_weekly_exp])
    next_iv = atm_iv_pct(next_weekly_exp, monthly_of[next_weekly_exp])

    rows, per_expiry, skipped_low_delta = _capture_options(
        underlying, quotes, inst_meta, spot, today, snapshot_time)

    if rows:
        _insert_option_rows(conn, rows, logger)

    # Compact per-expiry breakdown — flags expiries that returned nothing
    # so it's easy to tell why the dashboard is short an expiry.
    expiry_breakdown = " | ".join(
        f"{exp}: written={c['written']}"
        + (f" missing_quote={c['missing_quote']}" if c['missing_quote'] else "")
        + (f" zero_ltp={c['zero_ltp']}" if c['zero_ltp'] else "")
        + (f" low_delta={c['low_delta']}" if c['low_delta'] else "")
        for exp, c in per_expiry.items()
    )
    cadence = ul.expiry_cadence(underlying)
    logger.info(f"[{snapshot_time}] {underlying}: spot={spot} atm={atm} "
                f"step={step} ladder=+/-{n_side * step}pts cadence={cadence} "
                f"expiries={len(expiries)} "
                f"instruments={len(instruments)} fut={fut_price} "
                f"options_recorded={len(rows)} "
                f"(skipped_low_delta={skipped_low_delta})")
    logger.info(f"    {underlying} expiry breakdown: {expiry_breakdown}")
    for exp, counts in per_expiry.items():
        if counts['written'] == 0:
            # The old text ("Kite hasn't listed these contracts yet") was a
            # guess that happened to cover two unrelated causes — a shifted
            # expiry and an unlisted one — and so explained neither. Now that
            # every date here came from the listed set, an all-missing expiry
            # means the SYMBOL is wrong, not the date; all-zero-LTP just means
            # nobody has traded it yet, which is normal for a far weekly.
            if counts['missing_quote'] and not counts['zero_ltp']:
                logger.warning(
                    f"    {underlying} expiry {exp} wrote 0 rows and Kite "
                    f"returned no quote for any of its "
                    f"{counts['missing_quote']} symbols. The expiry date came "
                    f"from Kite's own instrument dump, so suspect the symbol "
                    f"FORMAT (monthly vs weekly serialisation) or "
                    f"strike_step={step}, not the date."
                )
            else:
                logger.info(
                    f"    {underlying} expiry {exp} wrote 0 rows "
                    f"(zero_ltp={counts['zero_ltp']}, "
                    f"low_delta={counts['low_delta']}) — listed but untraded "
                    f"so far, which is normal for a far-dated expiry."
                )

    # Whole-underlying health check. An individual expiry writing nothing is
    # normal (far-dated contracts aren't listed yet). Nearly EVERY quote
    # coming back missing is a different failure: the symbols being generated
    # don't correspond to listed contracts at all — wrong strike step, or the
    # wrong expiry cadence. That previously surfaced only as an index quietly
    # absent from the dashboard dropdown, with no line in this log to explain
    # it. Say it plainly instead.
    total_requested = sum(sum(c.values()) for c in per_expiry.values())
    total_missing = sum(c['missing_quote'] for c in per_expiry.values())
    if total_requested and total_missing / total_requested >= 0.9:
        logger.error(
            f"    {underlying}: {total_missing}/{total_requested} quotes missing "
            f"({total_missing / total_requested:.0%}) — the generated symbols "
            f"most likely don't match listed contracts. The expiry DATES now "
            f"come from Kite's instrument dump, so check the other two "
            f"ingredients: strike_step={step} and expiry_cadence={cadence} "
            f"for {underlying} in helpers/underlyings.py or the config "
            f"override."
        )

    return len(rows), atm, fut_price, near_iv, next_iv


def _capture(conn):
    now = datetime.now(IST)
    snapshot_time = now.strftime('%Y-%m-%d %H:%M:%S')

    vix_payload = proxy_get(f"/get_current_price/{NSE_VIX_SYMBOL}/stock",
                            params={'live': 'true'}, logger=logger)
    vix = vix_payload and vix_payload.get(f"NSE:{NSE_VIX_SYMBOL}", {}).get('last_price')

    # One spot fetch per underlying we're recording, plus BANKNIFTY's even
    # when it isn't being recorded — market_snapshot.banknifty_spot has
    # always been populated and the overview card depends on it.
    spots = {}
    for name in RECORD_UNDERLYINGS:
        spots[name] = _spot_of(name)
    if 'BANKNIFTY' not in spots:
        spots['BANKNIFTY'] = _spot_of('BANKNIFTY')

    spot = spots.get(PRIMARY_UNDERLYING)
    if not spot:
        logger.warning(f"[{snapshot_time}] {PRIMARY_UNDERLYING} spot unavailable; "
                       f"skipping cycle")
        return

    today = now.date()
    # Expiry lists are derived per underlying inside _capture_underlying, from
    # each one's registry cadence — NIFTY weeklies, BANKNIFTY monthlies. They
    # used to share one Tuesday list built here, which silently generated
    # never-listed contracts for any monthly-only index.

    total_written = 0
    primary_fut = primary_near_iv = primary_next_iv = None
    for name in RECORD_UNDERLYINGS:
        u_spot = spots.get(name)
        if not u_spot:
            logger.warning(f"[{snapshot_time}] {name} spot unavailable; "
                           f"skipping this underlying for this cycle")
            continue
        try:
            written, _atm, fut_price, near_iv, next_iv = _capture_underlying(
                conn, name, snapshot_time, today, u_spot)
        except Exception as e:
            # One underlying failing must not cost the others their rows
            # for this cycle — log and move on rather than aborting.
            logger.exception(f"[{snapshot_time}] {name} capture failed: {e}")
            continue
        total_written += written
        if name == PRIMARY_UNDERLYING:
            primary_fut, primary_near_iv, primary_next_iv = fut_price, near_iv, next_iv

    # ``weekly_atm_iv`` column stores the nearest weekly's ATM IV;
    # ``monthly_atm_iv`` column now stores the *next* weekly's ATM IV
    # (rename pending — keeping the column name to avoid a DB migration).
    # Both are PRIMARY_UNDERLYING only; see the module docstring for why
    # market_snapshot stays single-index shaped.
    conn.execute(
        "INSERT INTO market_snapshot "
        "(snapshot_time, spot_price, fut_price, india_vix, weekly_atm_iv, "
        "monthly_atm_iv, banknifty_spot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (snapshot_time, spot, primary_fut, vix, primary_near_iv,
         primary_next_iv, spots.get('BANKNIFTY')),
    )
    conn.commit()

    logger.info(f"[{snapshot_time}] spot={spot} fut={primary_fut} vix={vix} "
                f"banknifty={spots.get('BANKNIFTY')} "
                f"near_weekly_iv={primary_near_iv} next_weekly_iv={primary_next_iv} "
                f"underlyings={','.join(RECORD_UNDERLYINGS)} "
                f"options_recorded={total_written}")


# ---- Scheduling ------------------------------------------------------------

def _holiday_set():
    """Set of ``YYYY-MM-DD`` strings flagged as NSE trading holidays in
    ``monitor_info.trading_holidays`` of the config.

    Reads the config every time it's called (per-cycle ~30s during
    market hours, much less frequently off-hours). Robust against
    accidental ``date`` values in YAML by coercing each entry to its
    string form before set membership tests.
    """
    try:
        cfg = get_config(CONFIG_FILE)
    except Exception as e:
        logger.warning(f"Could not read trading_holidays from config: {e}")
        return set()
    monitor_info = (cfg or {}).get('monitor_info') or {}
    holidays = monitor_info.get('trading_holidays') or []
    out = set()
    for h in holidays:
        if h is None:
            continue
        # YAML may parse "2026-01-26" as a date object; str() normalises
        # to "2026-01-26" either way.
        s = str(h).strip()
        if s:
            out.add(s)
    return out


def _is_trading_holiday(now):
    """True iff today's IST date is in the configured holiday list."""
    return now.strftime('%Y-%m-%d') in _holiday_set()


def _in_market_hours(now):
    if now.weekday() >= 5:  # Sat/Sun
        return False
    if _is_trading_holiday(now):  # NSE-declared holiday
        return False
    start = now.replace(hour=MARKET_START_HM[0], minute=MARKET_START_HM[1],
                        second=0, microsecond=0)
    stop = now.replace(hour=MARKET_STOP_HM[0], minute=MARKET_STOP_HM[1],
                       second=0, microsecond=0)
    return start <= now <= stop


def _sleep_to_next_sample_boundary():
    """Sleep until the next clock tick that is a multiple of ``SAMPLE_SECONDS``
    past the minute, so captures land on stable, predictable marks across
    days (e.g. HH:MM:00 / HH:MM:30 when SAMPLE_SECONDS=30)."""
    now = datetime.now(IST)
    secs = now.minute * 60 + now.second + now.microsecond / 1_000_000.0
    next_secs = (int(secs // SAMPLE_SECONDS) + 1) * SAMPLE_SECONDS
    delta = next_secs - secs
    time.sleep(max(0.5, delta))


def _seconds_to_next_market_open(now):
    """Compute seconds until the next 09:15 IST on a trading day —
    skipping both weekends and configured trading holidays.

    Returns ``(seconds, target_datetime)``. Reads the holiday set once
    per call (the cost is negligible compared to the multi-minute sleep
    that follows).
    """
    candidate = now.replace(hour=MARKET_START_HM[0], minute=MARKET_START_HM[1],
                            second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    holidays = _holiday_set()
    while candidate.weekday() >= 5 or candidate.strftime('%Y-%m-%d') in holidays:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds(), candidate


def _maybe_fetch_trades_today(conn, now, last_fetched_date):
    """Once per weekday, on or after ``FETCH_TRADES_HM``, fetch the day's
    executed trades for every configured user via ``proxy_get`` and
    persist into ``kite_trades``.

    Returns the new ``last_fetched_date`` (unchanged on no-op or failure
    so the next loop iteration retries).
    """
    today = now.date()
    if last_fetched_date == today:
        return last_fetched_date
    if now.weekday() >= 5:  # don't run on weekends
        return last_fetched_date
    if (now.hour, now.minute) < FETCH_TRADES_HM:
        return last_fetched_date

    try:
        cfg = get_config(CONFIG_FILE)
    except Exception as e:
        logger.exception(f"Trade fetch aborted; could not read config: {e}")
        return last_fetched_date

    api_info = (cfg or {}).get('api_info') or {}
    users_raw = str(api_info.get('users') or '').strip()
    users = [u.strip() for u in users_raw.split(',') if u.strip()]
    if not users:
        logger.warning(
            "Trade fetch skipped: api_info.users is empty in config"
        )
        return last_fetched_date

    ensure_kite_trades_table(conn)
    fetched_at = now.strftime('%Y-%m-%d %H:%M:%S')
    total_inserted = 0
    for user in users:
        try:
            payload = proxy_get("/get_trades", params={'user': user})
        except Exception as e:
            logger.exception(f"/get_trades failed for {user}: {e}")
            return last_fetched_date  # retry next loop tick
        if payload is None:
            logger.warning(f"/get_trades returned no payload for {user}")
            return last_fetched_date
        user_trades = (payload or {}).get(user) or []
        try:
            inserted = persist_trades(user_trades, user, fetched_at)
        except Exception as e:
            logger.exception(f"Failed to persist {user}'s trades: {e}")
            return last_fetched_date
        total_inserted += inserted
        logger.info(
            f"[{user}] fetched={len(user_trades)} trade(s); "
            f"inserted={inserted} new row(s) into kite_trades"
        )

    logger.info(
        f"[{now:%Y-%m-%d %H:%M:%S}] daily trade fetch complete; "
        f"{total_inserted} new row(s) total"
    )
    return today


def _maybe_thin_today(now, last_thinned_date):
    """Once per trading day, after market close:

    1. Compact today's option_snapshot rows from 30s captures down to one
       row per 5-minute boundary.
    2. Purge ALL option_snapshot rows older than today — the dashboard only
       needs today's intraday history, so anything earlier is dead weight.

    Returns the new ``last_thinned_date`` (unchanged on no-op or failure).
    """
    today = now.date()
    if last_thinned_date == today:
        return last_thinned_date
    if now.weekday() >= 5:  # don't run on weekends
        return last_thinned_date
    if (now.hour, now.minute) < MARKET_STOP_HM:  # only after market close
        return last_thinned_date
    today_str = today.strftime('%Y-%m-%d')
    try:
        thinned = thin_option_snapshots_to_5min()
        purged = purge_option_snapshots_before(today_str)
        logger.info(f"[{now:%Y-%m-%d %H:%M:%S}] option_snapshot housekeeping: "
                    f"thinned {thinned} row(s) to 5-min, purged {purged} pre-{today_str} row(s).")
    except Exception as e:
        logger.exception(f"[{now:%Y-%m-%d %H:%M:%S}] Daily housekeeping failed: {e}")
        return last_thinned_date  # retry next iteration
    return today


def main():
    if not os.path.exists(DB_FILE):
        logger.error(f"DB file {DB_FILE} not found. Run db_creation.py first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_FILE)
    _load_underlyings()
    # Fail loudly rather than writing rows the read side can't attribute:
    # without this column every underlying's chain lands indistinguishable.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(option_snapshot)")}
    if 'underlying' not in cols:
        logger.error("option_snapshot has no 'underlying' column. "
                     "Run migrate_db.py before starting the recorder.")
        conn.close()
        sys.exit(1)
    logger.info(f"Snapshot recorder started; sampling every {SAMPLE_SECONDS}s "
                f"during 09:15-15:30 IST; "
                f"recording {', '.join(RECORD_UNDERLYINGS)}")
    last_thinned_date = None
    last_trades_fetched_date = None
    try:
        while True:
            now = datetime.now(IST)
            if _in_market_hours(now):
                try:
                    _capture(conn)
                except Exception as e:
                    logger.exception(f"[{datetime.now(IST):%H:%M:%S}] capture error: {e}")
                _sleep_to_next_sample_boundary()
            else:
                last_thinned_date = _maybe_thin_today(now, last_thinned_date)
                last_trades_fetched_date = _maybe_fetch_trades_today(
                    conn, now, last_trades_fetched_date
                )
                secs, when = _seconds_to_next_market_open(now)
                reason = (
                    "trading holiday" if _is_trading_holiday(now)
                    else "weekend"     if now.weekday() >= 5
                    else "outside market hours"
                )
                logger.info(
                    f"[{now:%Y-%m-%d %H:%M:%S}] {reason}; "
                    f"next open {when:%Y-%m-%d %H:%M} IST ({int(secs)}s)"
                )
                # Cap each sleep so Ctrl+C remains responsive.
                time.sleep(min(secs, 300))
    except KeyboardInterrupt:
        logger.info("Interrupted; closing DB.")
    finally:
        conn.close()


# Guarded so the module's helpers (_capture_options, _option_inst, ...) can
# be imported by tests without launching the recorder loop as a side effect.
if __name__ == "__main__":
    main()
