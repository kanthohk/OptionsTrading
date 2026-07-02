"""Daily market analysis + position-state nudge.

Runs once at startup and once at 08:00 IST every working day (Mon-Fri).
Each pass logs to ``{YYYYMMDD}_brain.log`` and:

1. Pulls recent ``market_snapshot`` rows from ``trading.db`` and computes:
     - NIFTY + India VIX % change over 1d / 5d / 10d / 20d horizons.
     - A short trend label combining spot direction and VIX direction.
     - Classical floor-trader pivot levels (S2 / S1 / P / R1 / R2) from
       yesterday's H / L / C.
     - Rolling 5d / 10d / 20d highs and lows as additional S&R candidates.

2. Fetches open broker positions through the existing
   ``PositionProcessor`` so the (underlying, expiry) grouping and the
   IRON_CONDOR / SHORT_STRANGLE classification stay consistent with the
   risk engine.

3. Pulls executed trades from Kite via ``/get_trades`` and runs a
   First-In-First-Out match between buys and sells per symbol to
   compute realised P&L per expiry (formula: ``(sell_price -
   buy_price) * matched_qty`` for each FIFO pair, with no
   brokerage / STT / GST subtracted). Kite serves only the current
   trading day's trades — multi-day history requires local
   persistence or Selenium scraping of Console.

4. For each tradable group it logs the strategy, expiry,
   days-to-expiry, the FIFO realised P&L, and writes that realised
   value to ``<week_block>.pnl`` in ``Monitor/config.yaml`` (only
   when it materially differs from the existing value).

5. For any tradable group whose expiry is <=6 calendar days away,
   sets the matching week block's ``close_on_trailprofit`` flag to
   ``true``. All config writes for the pass are batched into one
   atomic ``put_config`` call.

Run: ``python brain.py``
Prereq: ``trading.db`` (run ``MarketAnalysis/db_creation.py`` once) and
``Monitor/config.yaml`` populated with at least ``api_info`` and the
week blocks.
"""
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
import pytz

from helpers.basic import get_config, put_config, split_symbol
from helpers.positions_processing import PositionProcessor
from MarketAnalysis.db_queries import read_trades

IST = pytz.timezone('Asia/Kolkata')

DB_FILE = "trading.db"
CONFIG_FILE = "Monitor/config.yaml"

# 08:00 IST on weekdays.
WAKEUP_HOUR = 8
WAKEUP_MINUTE = 5

# Lookback window for market_snapshot history. 30 calendar days ~ 22
# trading days — enough headroom for 20-day rolling stats with margin
# for weekends / holidays.
LOOKBACK_DAYS = 30

# Days-to-expiry threshold at or below which we auto-enable
# ``close_on_trailprofit`` for that group's week block. 6 covers an
# expiry happening this week regardless of which weekday we run on.
TRAIL_PROFIT_TRIGGER_DAYS = 6

# Flat-rate approximation of broker fees per executed trade fill (₹).
# Covers Zerodha brokerage + STT + exchange transaction + GST + stamp
# duty + SEBI fees. Charged on every fill (i.e., every row in
# ``kite_trades``); subtracted from gross FIFO realised P&L when
# computing the value brain persists to ``<week>.pnl``.
CHARGE_PER_TRADE = 40


# ---- Logger setup ----------------------------------------------------------

def _setup_logger():
    """Per-day file logger writing to ``{YYYYMMDD}_brain.log``.

    Same pattern as ``MarketAnalysis/record_snapshots.py``. Brain.py is a
    long-lived process; the logfile name is fixed at boot — restart
    daily if you want one file per day.
    """
    daystr = datetime.now(IST).strftime('%Y%m%d')
    logger = logging.getLogger(f"brain_{daystr}")
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()
    fh = logging.FileHandler(f"{daystr}_brain.log", mode='a')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)
    logger.propagate = False
    return logger


# ---- Market history & trend -----------------------------------------------

def _load_market_history(days):
    """Load ``(snapshot_time, spot_price, india_vix)`` rows for the most
    recent ``days`` calendar days, oldest first.
    """
    cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT snapshot_time, spot_price, india_vix "
            "FROM market_snapshot "
            "WHERE snapshot_time >= ? "
            "ORDER BY snapshot_time ASC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


def _per_day_summary(rows):
    """Collapse intraday snapshots into one OHLC + VIX-close row per date.

    Returns a list of ``(YYYY-MM-DD, summary_dict)`` tuples sorted
    chronologically. Rows with missing spot are skipped.
    """
    by_day = {}
    for r in rows:
        ts = r.get('snapshot_time')
        spot = r.get('spot_price')
        if not ts or spot is None:
            continue
        day = ts[:10]
        vix = r.get('india_vix')
        bucket = by_day.get(day)
        if bucket is None:
            by_day[day] = {
                'open': spot, 'high': spot, 'low': spot, 'close': spot,
                'vix_open': vix, 'vix_close': vix,
            }
        else:
            if spot > bucket['high']:
                bucket['high'] = spot
            if spot < bucket['low']:
                bucket['low'] = spot
            bucket['close'] = spot
            bucket['vix_close'] = vix
    return sorted(by_day.items())


def _pct_change(latest, past):
    if not past:
        return None
    return (latest - past) / past * 100.0


def _trend_summary(daily, horizons=(1, 5, 10, 20)):
    """Compute % change over each horizon for spot and VIX. ``daily`` is
    the sorted output of ``_per_day_summary``.
    """
    if not daily:
        return None
    latest_spot = daily[-1][1]['close']
    latest_vix = daily[-1][1]['vix_close']
    summary = {'latest_spot': latest_spot, 'latest_vix': latest_vix}
    for h in horizons:
        if len(daily) > h:
            summary[f'spot_{h}d_pct'] = _pct_change(latest_spot, daily[-(h + 1)][1]['close'])
            past_vix = daily[-(h + 1)][1]['vix_close']
            summary[f'vix_{h}d_pct'] = _pct_change(latest_vix, past_vix) if past_vix else None
    return summary


def _classify_trend(spot_pct, vix_pct):
    """Coarse 5d label: spot direction + VIX direction. ``unknown`` if no
    spot data; VIX direction omitted if missing."""
    if spot_pct is None:
        return 'unknown'
    spot = 'up' if spot_pct > 1.5 else 'down' if spot_pct < -1.5 else 'flat'
    if vix_pct is None:
        return spot
    vix = 'rising' if vix_pct > 5 else 'falling' if vix_pct < -5 else 'flat'
    return f"{spot}, vix {vix}"


def _pivot_levels(daily):
    """Floor-trader pivots from the latest USABLE day's H / L / C.

    A "usable" day has ``high != low`` — i.e., the recorder captured
    more than one tick and the intraday range is non-degenerate. Days
    with a collapsed range (H == L, typically because only a single
    snapshot was written that session) are skipped because the
    canonical pivot formula then degenerates to a single value::

        P = (H+L+C)/3 = C   when H = L = C
        R1 = 2P - L = C,   S1 = 2P - H = C
        R2 = P + (H-L) = C, S2 = P - (H-L) = C

    Walking backwards through ``daily`` finds the most recent fully-
    captured session — at 08:00 IST that's almost always yesterday, but
    if the recorder also wrote a row from this morning we skip it and
    fall through to the previous day.

    Returns ``(pivots_dict, day_str)`` so the caller can log which
    session was used, or ``None`` when no usable day is in the window.
    """
    for day_str, bucket in reversed(daily):
        h, l, c = bucket.get('high'), bucket.get('low'), bucket.get('close')
        if not all([h, l, c]):
            continue
        if h == l:
            continue  # single-tick day — degenerate pivots
        p = (h + l + c) / 3.0
        return ({
            'P': p,
            'R1': 2 * p - l,
            'R2': p + (h - l),
            'S1': 2 * p - h,
            'S2': p - (h - l),
        }, day_str)
    return None


def _rolling_extrema(daily, windows=(5, 10, 20)):
    """For each window, return ``(rolling_high, rolling_low)`` over the
    last N days. Days shorter than the window are skipped.
    """
    out = {}
    for w in windows:
        if len(daily) < w:
            continue
        slice_ = [d[1] for d in daily[-w:]]
        highs = [d['high'] for d in slice_ if d['high'] is not None]
        lows = [d['low'] for d in slice_ if d['low'] is not None]
        if highs and lows:
            out[w] = (max(highs), min(lows))
    return out


# ---- Open positions -------------------------------------------------------

def _fifo_realized_pnl(trades):
    """Per-symbol realised P&L from a chronological list of trades using
    First-In-First-Out matching.

    Each trade dict must carry ``tradingsymbol``, ``transaction_type``
    (``"BUY"`` / ``"SELL"``), ``quantity`` (in shares) and
    ``average_price`` (per share). Multipliers don't need to be applied
    because Kite already reports quantity in shares (lot_size *
    num_lots), so ``(price_diff) * matched_qty`` is the rupee P&L.

    Algorithm: maintain a per-symbol queue of open lots. Each new trade
    either appends (same direction as the open lots) or pops/matches
    against the oldest opposite-direction lot. Realised P&L uses the
    canonical formula::

        realised = (sell_price - buy_price) * matched_qty

    independent of whether the position was opened long or short. A
    residual quantity after matching becomes a new open lot.

    Returns ``{tradingsymbol: realised_pnl}``.

    NOTE: Brokerage / STT / GST are NOT subtracted — Kite's trades
    endpoint doesn't include charges. The returned number is gross P&L.
    """
    open_lots = defaultdict(deque)
    realized = defaultdict(float)
    for t in trades:
        sym = t.get("tradingsymbol")
        side = (t.get("transaction_type") or "").upper()
        qty = float(t.get("quantity") or 0)
        price = float(t.get("average_price") or 0)
        if not sym or side not in ("BUY", "SELL") or qty <= 0:
            continue

        q = open_lots[sym]
        # Match against opposite-direction lots while we still have qty
        # to absorb. ``q[0]`` is the OLDEST lot — FIFO.
        while qty > 0 and q and q[0][2] != side:
            lot_qty, lot_price, lot_side = q[0]
            matched = min(qty, lot_qty)
            if side == "SELL":
                # closing a long position: sold at price, bought at lot_price
                realized[sym] += (price - lot_price) * matched
            else:
                # closing a short position: bought at price, sold at lot_price
                realized[sym] += (lot_price - price) * matched
            qty -= matched
            if matched < lot_qty:
                q[0] = (lot_qty - matched, lot_price, lot_side)
            else:
                q.popleft()

        # Residual quantity becomes a new same-direction open lot.
        if qty > 0:
            q.append((qty, price, side))

    return dict(realized)


def _aggregate_realized_by_expiry(per_symbol_pnl):
    """Group a ``{tradingsymbol: realised}`` dict by ``(underlying,
    expiry_date)`` using ``split_symbol``. Unparseable symbols are
    dropped with no log (the FIFO ledger keeps them under symbol-key).
    """
    by_expiry = defaultdict(float)
    for sym, pnl in per_symbol_pnl.items():
        try:
            parsed = split_symbol(sym)
        except Exception:
            continue
        if not parsed or len(parsed) != 4:
            continue
        underlying, expiry_dt, _, _ = parsed
        key = (underlying, expiry_dt.date() if hasattr(expiry_dt, "date") else expiry_dt)
        by_expiry[key] += pnl
    return dict(by_expiry)


def _compute_realized_pnl_by_expiry(user, logger):
    """Read every persisted trade for ``user`` from the local
    ``kite_trades`` ledger, run FIFO matching per symbol, aggregate per
    ``(underlying, expiry_date)``, and subtract ``CHARGE_PER_TRADE`` ×
    ``num_trades`` to approximate net-of-brokerage realised P&L.

    The ledger is populated by ``MarketAnalysis/record_snapshots.py``'s
    ``_maybe_fetch_trades_today`` hook (15:35 IST on weekdays). Until
    that hook has run at least once on a given deployment, the table
    won't exist and ``read_trades`` returns ``[]`` — brain falls
    through with no realised numbers, which is the correct behaviour.

    Returns ``{(underlying, expiry_date): net_realised_pnl}``. Includes
    expiries that only have opens (gross 0, but charges accrued) so the
    operator sees the negative-realised footprint even before any
    closes land.
    """
    trades = read_trades(user=user)
    nfo_trades = [t for t in trades if t.get("exchange") == "NFO"]
    if not nfo_trades:
        logger.info("FIFO ledger: kite_trades is empty (or no NFO fills yet)")
        return {}

    # ``read_trades`` already sorts by fill_timestamp / trade_id, so the
    # chronological order FIFO needs is preserved — no re-sort here.
    logger.info(
        f"FIFO ledger: {len(nfo_trades)} NFO trade(s) from local kite_trades"
    )
    per_symbol_gross = _fifo_realized_pnl(nfo_trades)
    gross_by_expiry = _aggregate_realized_by_expiry(per_symbol_gross)

    # Per-trade charges accrue on every executed fill (not just matched
    # ones), so count by expiry across the FULL trade list — not just
    # the symbols that produced realised P&L.
    trade_count_by_expiry = defaultdict(int)
    for t in nfo_trades:
        sym = t.get('tradingsymbol')
        if not sym:
            continue
        try:
            parsed = split_symbol(sym)
        except Exception:
            continue
        if not parsed or len(parsed) != 4:
            continue
        underlying, expiry_dt, _, _ = parsed
        key = (underlying, expiry_dt.date() if hasattr(expiry_dt, 'date') else expiry_dt)
        trade_count_by_expiry[key] += 1

    # Union the two keysets so expiries with only opens (no FIFO
    # matches → not in gross_by_expiry) still get their charges
    # accounted for.
    net_by_expiry = {}
    for key in set(gross_by_expiry) | set(trade_count_by_expiry):
        gross = gross_by_expiry.get(key, 0.0)
        n_trades = trade_count_by_expiry.get(key, 0)
        charges = n_trades * CHARGE_PER_TRADE
        net = gross - charges
        net_by_expiry[key] = net
        logger.info(
            f"  {key[0]}@{key[1]}: gross={gross:+.2f}, "
            f"trades={n_trades}, charges={charges}, net={net:+.2f}"
        )
    return net_by_expiry


def _build_processor(user, config, logger):
    """Construct a PositionProcessor using the new ``api_info`` config
    layout. ``alert_fn`` is intentionally a no-op so brain.py runs silent
    on the push channel — it logs, it doesn't ping the operator.
    """
    api_info = config.get('api_info') or {}
    api_url = api_info.get('url')
    if not api_url:
        raise RuntimeError("api_info.url missing from config")
    return PositionProcessor(
        user=user,
        api_url=api_url,
        config=config,
        logger=logger,
        config_file=CONFIG_FILE,
        alert_fn=None,
    )


def _analyze_positions(logger, config):
    """Run process+analyze, log per-group state, return a ``{dotted_key:
    value}`` dict of config updates the caller should persist.

    Two kinds of updates may end up in the returned dict:

    - ``<week_block>.close_on_trailprofit``: two-way sync against
      ``dte`` for groups with open legs. Set ``True`` when
      ``dte <= TRAIL_PROFIT_TRIGGER_DAYS``, ``False`` when
      ``dte > TRAIL_PROFIT_TRIGGER_DAYS``. Empty / rolled-over blocks
      (no open legs anymore) are not touched.
    - ``<week_block>.pnl``: written when net FIFO realised P&L from
      the local ``kite_trades`` ledger materially differs from the
      value already in config.
    """
    api_info = config.get('api_info') or {}
    users_raw = str(api_info.get('users') or '').strip()
    user = users_raw.split(',')[0].strip() if users_raw else ''
    if not user:
        logger.error("api_info.users is empty — cannot fetch positions; skipping group analysis")
        return {}

    try:
        processor = _build_processor(user, config, logger)
    except Exception as e:
        logger.exception(f"Could not build PositionProcessor: {e}")
        return {}

    if not processor.process_positions():
        logger.info("Broker returned no open positions (or fetch failed). Nothing to analyze.")
        return {}
    processor.analyze_positions()

    # Net realised P&L per expiry, computed by FIFO-matching the
    # persistent ``kite_trades`` ledger and subtracting ``CHARGE_PER_TRADE``
    # × num_trades. The ledger is populated by record_snapshots's 15:35
    # daily hook — until that runs once, this is ``{}`` and brain just
    # skips the bucket-write path.
    realized_by_expiry = _compute_realized_pnl_by_expiry(user, logger)

    today = datetime.now(IST).date()
    # All config writes for this pass accumulate here and get flushed in
    # a single batched ``put_config`` call at the end. Keys are dotted
    # paths into the nested config layout.
    pending_updates = {}
    for group in processor.groups:
        if group.strategy not in ('IRON_CONDOR', 'SHORT_STRANGLE'):
            logger.info(
                f"Group {group.underlying}@{group.expiry.date()}: "
                f"strategy={group.strategy or 'unclassified'} — skipped"
            )
            continue

        block_name = processor._group_block_name(group)
        group_cfg = processor.get_group_config(group)
        bucket_pnl = group_cfg.get('pnl', 0)
        live_pnl = processor.compute_group_pnl(group)
        mark = live_pnl - bucket_pnl  # current cycle's mark-to-market component
        dte = (group.expiry.date() - today).days

        # NOTE: ``bucket_pnl`` is the LIFETIME banked realized P&L for this
        # week-slot — not a strict 30-day window. We don't currently keep
        # a daily P&L history table; if you want true 30-day deltas, add
        # a daily snapshot of these bucket values and diff them here.
        logger.info(
            f"{group.strategy} @ {group.expiry.date()} "
            f"({block_name or 'no-block'}, dte={dte}): "
            f"bucket(lifetime)={bucket_pnl:+.2f}  "
            f"mark={mark:+.2f}  live={live_pnl:+.2f}"
        )

        # Realised P&L for this expiry, FIFO-matched from the persistent
        # kite_trades ledger and net of ``CHARGE_PER_TRADE`` per fill.
        # ``None`` means no fills for legs belonging to this (underlying,
        # expiry) anywhere in the ledger — typical for a position carried
        # in from outside the ledger's coverage window.
        realized = realized_by_expiry.get((group.underlying, group.expiry.date()))
        if realized is not None:
            logger.info(
                f"  FIFO realised (net of ₹{CHARGE_PER_TRADE}/trade): {realized:+.2f}"
            )
        else:
            logger.info(
                "  FIFO realised: n/a "
                "(no fills for this expiry in kite_trades)"
            )

        if block_name is None:
            logger.warning(
                f"  expiry {group.expiry.date()} is past WEEK_GROUPS[-1] — "
                f"close_on_trailprofit flag cannot be set (no config block)"
            )
            continue

        # Sync the week block's ``pnl`` to FIFO-computed net realised P&L
        # (gross realised − ₹CHARGE_PER_TRADE × num_fills). Unrealised
        # MTM is intentionally excluded — it swings with intraday market
        # moves and would make the persisted bucket volatile rather than
        # reflecting locked-in P&L. Only writes when there are ledger
        # fills for this expiry AND the new value materially differs
        # from what's in config (avoids churn).
        if realized is not None:
            new_pnl = round(float(realized), 2)
            current_pnl = float(group_cfg.get('pnl') or 0)
            if abs(new_pnl - current_pnl) > 0.005:
                logger.info(
                    f"  queueing {block_name}.pnl := {new_pnl:+.2f} "
                    f"(net FIFO realised; was {current_pnl:+.2f}, "
                    f"Δ={new_pnl - current_pnl:+.2f})"
                )
                pending_updates[f"{block_name}.pnl"] = new_pnl
            else:
                logger.info(
                    f"  {block_name}.pnl already {current_pnl:+.2f} — "
                    f"matches net FIFO realised within rounding, no change"
                )

        # Two-way sync of ``close_on_trailprofit`` for this week block,
        # gated on the group having open legs (the for-loop above only
        # yields groups present in ``processor.groups``, so we don't
        # touch blocks that are empty / just rolled over).
        if dte <= TRAIL_PROFIT_TRIGGER_DAYS:
            already_on = group_cfg.get('close_on_trailprofit') is True
            if already_on:
                logger.info(
                    f"  dte={dte} <= {TRAIL_PROFIT_TRIGGER_DAYS} but "
                    f"{block_name}.close_on_trailprofit already true — no change"
                )
            else:
                logger.info(
                    f"  dte={dte} <= {TRAIL_PROFIT_TRIGGER_DAYS} — "
                    f"queueing {block_name}.close_on_trailprofit := true"
                )
                pending_updates[f"{block_name}.close_on_trailprofit"] = True
        else:
            # dte > 6: the active position has plenty of room, so the
            # aggressive trail-profit close should be OFF. Catches the
            # rollover case where a flag set TRUE on the prior trade's
            # last few days has carried over into the new trade.
            already_off = group_cfg.get('close_on_trailprofit') is False
            if already_off:
                logger.info(
                    f"  dte={dte} > {TRAIL_PROFIT_TRIGGER_DAYS} and "
                    f"{block_name}.close_on_trailprofit already false — no change"
                )
            else:
                logger.info(
                    f"  dte={dte} > {TRAIL_PROFIT_TRIGGER_DAYS} — "
                    f"queueing {block_name}.close_on_trailprofit := false"
                )
                pending_updates[f"{block_name}.close_on_trailprofit"] = False

    return pending_updates


def _persist_pending_updates(logger, updates):
    """Flush brain's queued config updates in a single batched, file-locked
    ``put_config`` call. Keys may be dotted paths into the nested config
    layout; ``put_config`` walks the path and writes only that leaf, so
    sibling flags inside each block are preserved across concurrent
    writers (e.g. ``Monitor/options.py`` updating ``monitor_info.last_watched``)."""
    if not updates:
        return
    ok, info = put_config(CONFIG_FILE, updates=updates)
    if ok:
        for key, value in updates.items():
            logger.info(f"  persisted {key} = {value}")
    else:
        logger.error(f"  failed to persist {len(updates)} update(s): {info}")


# ---- Single pass ----------------------------------------------------------

def _process_pass(logger):
    logger.info("=" * 70)
    logger.info(f"Brain pass starting at {datetime.now(IST):%Y-%m-%d %H:%M:%S} IST")

    # 1. Trend / S&R from market_snapshot history.
    rows = _load_market_history(LOOKBACK_DAYS)
    daily = _per_day_summary(rows)
    if not daily:
        logger.warning(
            f"No market_snapshot rows in the last {LOOKBACK_DAYS} days — "
            f"skipping trend analysis"
        )
    else:
        logger.info(f"Loaded {len(daily)} trading day(s) of market history.")
        trend = _trend_summary(daily)
        spot_parts = ", ".join(
            f"{h}d={trend[f'spot_{h}d_pct']:+.2f}%"
            for h in (1, 5, 10, 20)
            if trend.get(f'spot_{h}d_pct') is not None
        )
        vix_parts = ", ".join(
            f"{h}d={trend[f'vix_{h}d_pct']:+.2f}%"
            for h in (1, 5, 10, 20)
            if trend.get(f'vix_{h}d_pct') is not None
        )
        logger.info(f"Spot latest={trend['latest_spot']:.2f}  changes: {spot_parts or 'n/a'}")
        logger.info(f"VIX  latest={trend['latest_vix']:.2f}  changes: {vix_parts or 'n/a'}")
        logger.info(
            "Trend (5d): "
            + _classify_trend(trend.get('spot_5d_pct'), trend.get('vix_5d_pct'))
        )

        pivot_result = _pivot_levels(daily)
        if pivot_result:
            pivot, pivot_day = pivot_result
            logger.info(
                f"Pivot levels (from {pivot_day} H/L/C): "
                f"S2={pivot['S2']:.0f}  S1={pivot['S1']:.0f}  "
                f"P={pivot['P']:.0f}  "
                f"R1={pivot['R1']:.0f}  R2={pivot['R2']:.0f}"
            )
        else:
            logger.info(
                "Pivot levels: not computable (no usable day in lookback; "
                "every recent day was missing H/L/C or had a collapsed range)"
            )

        rolling = _rolling_extrema(daily)
        for w, (hi, lo) in rolling.items():
            logger.info(f"Rolling {w:>2}d:  high={hi:.0f}  low={lo:.0f}")

    # 2 + 3. Open positions, P&L per group, close_on_trailprofit nudge,
    # Kite-sourced pnl sync into <week>.pnl.
    config = get_config(CONFIG_FILE)
    pending_updates = _analyze_positions(logger, config)
    if pending_updates:
        logger.info(f"Persisting {len(pending_updates)} config update(s):")
        _persist_pending_updates(logger, pending_updates)
    else:
        logger.info("No config updates needed this pass.")

    logger.info("Brain pass complete.")
    logger.info("=" * 70)


# ---- Scheduling -----------------------------------------------------------

def _seconds_to_next_run(now):
    """Seconds until the next 08:00 IST on a working day, plus the
    absolute target datetime. If today's 08:00 has already passed,
    rolls forward day-by-day, skipping Sat/Sun.
    """
    target = now.replace(hour=WAKEUP_HOUR, minute=WAKEUP_MINUTE,
                         second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:  # 5=Sat, 6=Sun
        target += timedelta(days=1)
    return (target - now).total_seconds(), target


def main():
    logger = _setup_logger()
    if not os.path.exists(DB_FILE):
        logger.error(f"DB file {DB_FILE} not found. Run db_creation.py first.")
        sys.exit(1)
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file {CONFIG_FILE} not found.")
        sys.exit(1)

    logger.info(
        f"brain.py started; first pass now, then daily at "
        f"{WAKEUP_HOUR:02d}:{WAKEUP_MINUTE:02d} IST (weekdays)"
    )
    try:
        try:
            _process_pass(logger)
        except Exception as e:
            logger.exception(f"Initial brain pass crashed: {e}")

        while True:
            now = datetime.now(IST)
            secs, when = _seconds_to_next_run(now)
            logger.info(
                f"Next pass at {when:%Y-%m-%d %H:%M} IST ({int(secs)}s away)"
            )
            # Sleep in <=30-minute chunks so Ctrl+C stays responsive and
            # mid-day clock changes don't strand us in a stale long sleep.
            while secs > 0:
                time.sleep(min(secs, 1800))
                secs = (when - datetime.now(IST)).total_seconds()

            try:
                _process_pass(logger)
            except Exception as e:
                logger.exception(f"Brain pass crashed: {e}")
    except KeyboardInterrupt:
        logger.info("Interrupted; exiting.")


if __name__ == "__main__":
    main()
