"""Standalone market snapshot recorder.

Runs every ``SAMPLE_SECONDS`` (default 30s) during NSE market hours
(09:15-15:30 IST, weekdays) and appends one row to ``market_snapshot``
plus N rows to ``option_snapshot`` in ``trading.db``. Uses the Flask proxy at ``url`` from Monitor/config.yaml
(same standalone pattern as autotrade.py and fetch_data.py) so it does not
need its own Kite session.

market_snapshot columns:
    snapshot_time, spot_price, fut_price, india_vix,
    weekly_atm_iv   -- ATM IV of the nearest weekly NIFTY expiry,
    monthly_atm_iv  -- ATM IV of the *next* weekly NIFTY expiry (column
                       kept by that name to avoid a DB migration;
                       semantics changed from monthly to next-weekly),
    banknifty_spot  -- BANKNIFTY spot LTP (no options recorded).

option_snapshot columns (one row per strike per expiry per option_type):
    snapshot_time, symbol, strike, option_type, expiry,
    ltp, iv, delta, theta, gamma, vega, oi

Kite does not return IV or Greeks; both are computed locally via Black-
Scholes (Brent's method for IV, closed-form for the Greeks) using
``RISK_FREE_RATE``. IV is stored as a percentage to match the conventional
display (e.g. 15.5 for 15.5%); theta is per-day, vega is per-1% sigma.

Run: ``python MarketAnalysis/record_snapshots.py``
Prereq: run ``db_creation.py`` once so the tables exist.
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
from MarketAnalysis.db_queries import (
    thin_option_snapshots_to_5min,
    purge_option_snapshots_before,
    ensure_kite_trades_table,
    persist_trades,
)

IST = pytz.timezone('Asia/Kolkata')
DB_FILE = "trading.db"
CONFIG_FILE = "Monitor/config.yaml"


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
    logfile = f"{daystr}_recorder.log"

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

UNDERLYING = "NIFTY"
NSE_INDEX_SYMBOL = "NIFTY 50"
NSE_VIX_SYMBOL = "INDIA VIX"
NSE_BANK_SYMBOL = "NIFTY BANK"  # BANKNIFTY spot (no options recorded — spot only)
STRIKE_STEP = 50
# Maximum strikes on each side of ATM to query from Kite. Acts as a hard
# safety bound (in case delta never drops below the threshold) and caps the
# size of the kite.quote() batch. Actual rows written are filtered by delta
# below.
MAX_STRIKES_AROUND_ATM = 30
# How many consecutive weekly expiries to record (starting with the nearest
# Tuesday on/after today). NIFTY weeklies + the monthly that lands on the
# last Tuesday of the month are both covered — symbol format is decided
# per-expiry by ``_is_monthly_expiry``.
#
# The cycle's total Kite-quote instrument count is roughly
# ``WEEKLY_EXPIRIES_COUNT * (2 * MAX_STRIKES_AROUND_ATM + 1) * 2 + 1``,
# and ``kite.quote()`` caps at 500 per call — the broker proxy chunks
# transparently when this exceeds the cap.
WEEKLY_EXPIRIES_COUNT = 5
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


def _option_inst(expiry_date, strike, opt_type, monthly):
    return f"NFO:{UNDERLYING}{kite_expiry_code(expiry_date, monthly)}{strike}{opt_type}"


def _futures_inst(expiry_date):
    return f"NFO:{UNDERLYING}{kite_expiry_code(expiry_date, monthly=True)}FUT"


# ---- Capture cycle ---------------------------------------------------------

def _atm_strike(spot):
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def _capture(conn):
    now = datetime.now(IST)
    snapshot_time = now.strftime('%Y-%m-%d %H:%M:%S')

    # ``live=true`` bypasses main.py's DB-first cache so the recorder
    # always pulls fresh broker prices — otherwise it would read back its
    # own prior writes and the table would stop advancing.
    spot_payload = proxy_get(f"/get_current_price/{NSE_INDEX_SYMBOL}/stock",
                             params={'live': 'true'})
    vix_payload = proxy_get(f"/get_current_price/{NSE_VIX_SYMBOL}/stock",
                            params={'live': 'true'})
    bank_payload = proxy_get(f"/get_current_price/{NSE_BANK_SYMBOL}/stock",
                             params={'live': 'true'})
    spot = spot_payload and spot_payload.get(f"NSE:{NSE_INDEX_SYMBOL}", {}).get('last_price')
    vix = vix_payload and vix_payload.get(f"NSE:{NSE_VIX_SYMBOL}", {}).get('last_price')
    banknifty = bank_payload and bank_payload.get(f"NSE:{NSE_BANK_SYMBOL}", {}).get('last_price')
    if not spot:
        logger.warning(f"[{snapshot_time}] spot unavailable; skipping cycle")
        return

    today = now.date()
    # ``WEEKLY_EXPIRIES_COUNT`` consecutive weekly expiries starting with
    # the nearest Tuesday on/after today. When any of them happens to be
    # the last Tuesday of its month, the Kite symbol uses the monthly
    # format — handled per-expiry by ``_is_monthly_expiry``.
    near_weekly_exp = _next_weekday(today, weekday=1)
    weekly_expiries = [near_weekly_exp + timedelta(days=7 * i)
                       for i in range(WEEKLY_EXPIRIES_COUNT)]

    # Futures only have monthly expiries — that hasn't changed.
    fut_inst = _futures_inst(_nearest_monthly_expiry(today))
    fut_price = None

    atm = _atm_strike(spot)
    strikes = [atm + i * STRIKE_STEP for i in range(-MAX_STRIKES_AROUND_ATM, MAX_STRIKES_AROUND_ATM + 1)]

    expiry_meta = [(exp, _is_monthly_expiry(exp)) for exp in weekly_expiries]
    # The first two expiries are still used for the ``weekly_atm_iv`` /
    # ``monthly_atm_iv`` columns on ``market_snapshot`` below — keep
    # convenience aliases so that block reads cleanly.
    near_weekly_exp = weekly_expiries[0]
    next_weekly_exp = weekly_expiries[1] if len(weekly_expiries) > 1 else weekly_expiries[0]

    instruments = [fut_inst]
    inst_meta = {}  # option inst -> (strike, opt_type, expiry_date, monthly)
    for exp_date, monthly in expiry_meta:
        for strike in strikes:
            for opt_type in ('CE', 'PE'):
                inst = _option_inst(exp_date, strike, opt_type, monthly)
                instruments.append(inst)
                inst_meta[inst] = (strike, opt_type, exp_date, monthly)

    quotes = proxy_get("/get_quotes",
                       params={'instruments': ','.join(instruments), 'live': 'true'}) or {}

    # Futures LTP
    fut_q = quotes.get(fut_inst)
    if fut_q:
        fut_price = fut_q.get('last_price')

    def atm_iv_pct(exp_date, monthly):
        """Average ATM CE/PE IV in percent, or None when neither solves."""
        t = max((exp_date - today).days / 365.0, 1.0 / 365.0)
        ivs = []
        for opt_type in ('CE', 'PE'):
            q = quotes.get(_option_inst(exp_date, atm, opt_type, monthly))
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

    # ``weekly_atm_iv`` column stores the nearest weekly's ATM IV;
    # ``monthly_atm_iv`` column now stores the *next* weekly's ATM IV
    # (rename pending — keeping the column name to avoid a DB migration).
    near_iv = atm_iv_pct(near_weekly_exp, _is_monthly_expiry(near_weekly_exp))
    next_iv = atm_iv_pct(next_weekly_exp, _is_monthly_expiry(next_weekly_exp))

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO market_snapshot "
        "(snapshot_time, spot_price, fut_price, india_vix, weekly_atm_iv, "
        "monthly_atm_iv, banknifty_spot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (snapshot_time, spot, fut_price, vix, near_iv, next_iv, banknifty),
    )

    rows = []
    skipped_low_delta = 0
    # Per-expiry diagnostic counts. Helps spot when an expiry (typically
    # the far-dated one) returns no quotes from Kite because the contracts
    # aren't listed yet — without this you'd just see one missing expiry
    # in the dashboard with no log trail.
    per_expiry = {exp.isoformat(): {'written': 0, 'missing_quote': 0,
                                    'zero_ltp': 0, 'low_delta': 0}
                  for exp, _ in expiry_meta}
    for inst, (strike, opt_type, exp_date, monthly) in inst_meta.items():
        exp_key = exp_date.isoformat()
        q = quotes.get(inst)
        if not q:
            per_expiry[exp_key]['missing_quote'] += 1
            continue
        ltp = q.get('last_price', 0)
        if ltp <= 0:
            per_expiry[exp_key]['zero_ltp'] += 1
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
            per_expiry[exp_key]['low_delta'] += 1
            continue
        sym = inst.split(':', 1)[1]
        rows.append((snapshot_time, sym, strike, opt_type, exp_date.isoformat(),
                     ltp, sigma * 100.0, delta, theta, gamma, vega, q.get('oi', 0)))
        per_expiry[exp_key]['written'] += 1

    if rows:
        cur.executemany(
            "INSERT INTO option_snapshot "
            "(snapshot_time, symbol, strike, option_type, expiry, "
            "ltp, iv, delta, theta, gamma, vega, oi) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()
    # Compact per-expiry breakdown — flags expiries that returned nothing
    # so it's easy to tell why the dashboard is short an expiry.
    expiry_breakdown = " | ".join(
        f"{exp}: written={c['written']}"
        + (f" missing_quote={c['missing_quote']}" if c['missing_quote'] else "")
        + (f" zero_ltp={c['zero_ltp']}" if c['zero_ltp'] else "")
        + (f" low_delta={c['low_delta']}" if c['low_delta'] else "")
        for exp, c in per_expiry.items()
    )
    logger.info(f"[{snapshot_time}] spot={spot} fut={fut_price} vix={vix} "
                f"banknifty={banknifty} "
                f"near_weekly_iv={near_iv} next_weekly_iv={next_iv} "
                f"options_recorded={len(rows)} (skipped_low_delta={skipped_low_delta})")
    logger.info(f"    expiry breakdown: {expiry_breakdown}")
    for exp, counts in per_expiry.items():
        if counts['written'] == 0:
            logger.warning(f"    expiry {exp} wrote 0 rows — "
                           f"likely Kite hasn't listed these contracts yet")


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
    logger.info(f"Snapshot recorder started; sampling every {SAMPLE_SECONDS}s during 09:15-15:30 IST")
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

main()
