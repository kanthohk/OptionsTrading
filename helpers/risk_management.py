"""Delta-driven adjustment risk management.

Extracted from ``Monitor/options.py`` so the same logic can be reused outside
``handle_options`` (backtesting, dry-run replays, integration tests). The
manager reads positional state from a ``PositionProcessor`` and places orders
via an injected callable, so it has no direct dependency on the broker
plumbing or the orchestrator.

This module used to carry two more risk paths alongside the adjustments:
``check_stop_loss`` (close the book when a VIX-scaled loss threshold was hit,
the strikes were near spot, AND a short had doubled) and ``trail_profit``
(close it when P&L gave back a high-water mark). Both are gone — the
adjustments engine now manages the trade end to end, rolling a threatened
short out and trimming a losing side rather than flattening the book. Their
config blocks went with them; ``stoploss_info.quantity_per_lot`` is the one
survivor, because ``_lot_size_for`` is what sizes partial-reduce and re-add.
"""
import time
from datetime import datetime
import pytz

from helpers.basic import (api_get, ApiError, hedge_distance_for_dte,
                           split_symbol, reprice_option_symbol,
                           snap_to_strike_grid)
from helpers import underlyings as ul
# is_strike_liquid is pure logic with no DB access, so it stays on db_queries
# even though this module runs on the trading server. record_adjustment does
# touch the database, so it comes from the remote client.
from MarketAnalysis.db_queries import is_strike_liquid
# Pure classification, no DB access — safe to import on the trading server
# for the same reason as is_strike_liquid above.
from MarketAnalysis.trendanalysis import (current_trend, rsi_band,
                                          NON_DIRECTIONAL, DOWNWARDS, UPWARDS)
# Strategy names only — positions_processing does not import this module, so
# there is no cycle. Importing them beats re-spelling the literals: the
# hedge-roll condition below turns on exact equality with these.
from helpers.positions_processing import IRON_CONDOR, DIRECTIONAL
from helpers.db_client import record_adjustment

IST = pytz.timezone('Asia/Kolkata')


class RiskManager:
    """Encapsulates the per-group adjustment decisions for a strategy.

    Why this exists as a separate class: the adjustment paths need the broker
    proxy, the order placer, the alerter and the position processor wired
    together as one unit, and keeping that wiring out of the orchestrator
    lets it stay focused on cycle sequencing.

    Deliberately stateless between cycles. It held three per-expiry dicts
    (``lock_profit_by_expiry``, ``trail_profit_hit_count_by_expiry``,
    ``nearing_strike_by_expiry``) for the stop-loss and trail-profit paths;
    with those gone every adjustment decision is recomputed from live deltas
    and quantities each cycle, so a restart mid-session loses nothing.
    """

    def __init__(self, user, api_url, config, logger, processor,
                 place_order_fn, alert_fn=None):
        self.user = user
        self.api_url = api_url
        self.config = config
        self.logger = logger
        self.processor = processor
        self._place_order = place_order_fn
        self._alert = alert_fn or (lambda heading, message, priority=5: None)
        # {(underlying, expiry_date, side): (short_qty, hedge_qty)} last
        # alerted as over-hedged. Lets the block log every cycle but ring
        # the phone once per distinct state rather than every 30s.
        self._overhedge_alerted = {}

    def _spot_price(self, underlying):
        """Fetch current spot for ``underlying`` via the broker proxy.

        Isolated here so each adjustment branch (inward roll boundary,
        partial-reduce hedge floor) can fetch spot for itself. Returns
        ``None`` on any failure — callers treat that as "skip the
        min_distance check this cycle" rather than fail the whole
        adjustment.
        """
        if underlying == 'NIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY 50/stock"
            key = 'NSE:NIFTY 50'
        elif underlying == 'BANKNIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY BANK/stock"
            key = 'NSE:NIFTY BANK'
        else:
            url = f"{self.api_url}/get_current_price/{underlying}/stock"
            key = f'NSE:{underlying}'
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"Spot fetch for {underlying} failed: {e}")
            return None
        if not ok:
            self.logger.warning(f"Broker reported failure on spot for {underlying}: {payload}")
            return None
        try:
            return int(payload[key]['last_price'])
        except (KeyError, TypeError, ValueError):
            return None

    def _fetch_option_quote(self, symbol):
        """Fetch a single option's quote dict via the broker proxy.

        Returns:
            dict — quote payload (``last_price``, ``delta`` etc.) on
                success.
            ``{}`` — broker responded successfully but had no data for
                this exact symbol (strike not listed by the exchange).
            ``None`` — transport failure or broker reported failure;
                caller must distinguish this from "no data" because in
                the unverifiable case we don't want to silently fall
                back as if the strike were missing.
        """
        url = f"{self.api_url}/get_current_price/{symbol}/option"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"  LTP check for {symbol} failed: {e}")
            return None
        if not ok:
            return None
        return payload.get(f"NFO:{symbol}") or {}

    def _india_vix(self):
        """Fetch the current India VIX last_price via the broker proxy.

        Companion to ``_spot_price`` — used when recording an adjustment
        audit row so the market-vol context is preserved. Returns
        ``None`` on any failure; the row will then carry NULL for
        ``india_vix`` rather than failing the whole write.
        """
        url = f"{self.api_url}/get_current_price/INDIA VIX/stock"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"VIX fetch failed: {e}")
            return None
        if not ok:
            return None
        try:
            return float(payload['NSE:INDIA VIX']['last_price'])
        except (KeyError, TypeError, ValueError):
            return None

    # How an underlying is named in get_index_summary's payload. NOT simply
    # ``ul.nse_symbol``: that maps BANKNIFTY to 'NIFTY BANK', but the summary
    # labels the same series 'BANKNIFTY'. Only NIFTY's label is the NSE
    # symbol, and getting this wrong reads as "no RSI for this index" — which
    # silently stands the trend gate down rather than erroring.
    _INDEX_SUMMARY_LABELS = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'BANKNIFTY'}

    def _rsi_for(self, underlying):
        """Latest Wilder RSI for ``underlying``, or ``None``.

        Reads ``/api/index_summary`` on the broker proxy, which forwards to
        the DB server — the same figure the dashboard's index cards show, so
        an operator can always see what the engine decided on.

        ``None`` on any failure (transport, unknown index, RSI not yet
        computable). Callers must treat that as "trend unknown" and stand
        down, never as "non-directional".
        """
        label = self._INDEX_SUMMARY_LABELS.get(underlying)
        if label is None:
            self.logger.warning(
                f"No index_summary label known for {underlying}; trend "
                f"cannot be determined. Add it to _INDEX_SUMMARY_LABELS."
            )
            return None
        url = f"{self.api_url}/api/index_summary"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"RSI fetch failed for {underlying}: {e}")
            return None
        if not ok or not isinstance(payload, dict):
            self.logger.warning(
                f"RSI fetch for {underlying} returned no usable payload"
            )
            return None
        entry = payload.get(label)
        if not isinstance(entry, dict):
            self.logger.warning(
                f"index_summary has no entry for {label!r}; cannot determine "
                f"the trend for {underlying}"
            )
            return None
        rsi = entry.get('rsi')
        if rsi is None:
            self.logger.warning(
                f"index_summary reports no RSI for {label} yet — the DB needs "
                f"{entry.get('rsi_period', '?')} + 1 days of closes for it."
            )
        return rsi

    def _rsi_band(self):
        """``(low, high)`` non-directional RSI band from ``adjustment_info``.

        Re-read per call, like the delta thresholds, which is what lets an
        operator retune without restarting the monitors. Parsing and its
        fallbacks live in ``trendanalysis.rsi_band`` because the dashboard
        reads the same two keys to colour the RSI rail.
        """
        return rsi_band(self.config.get('adjustment_info'),
                        on_warning=self.logger.warning)

    def _trend_for(self, underlying):
        """``(trend, rsi)`` for ``underlying``. ``trend`` is None if unknown.

        Returns the RSI alongside the verdict so every caller can log the
        number it decided on — a gate that only says "trending" leaves an
        operator no way to check the call after the fact.
        """
        rsi = self._rsi_for(underlying)
        low, high = self._rsi_band()
        return current_trend(underlying, rsi, low=low, high=high), rsi

    def _is_non_directional(self, underlying, action, trend=None, rsi=None):
        """Trend gate shared by the two non-directional actions.

        True only for an explicit non-directional reading. An UNKNOWN trend
        refuses, and says so: the alternative — treating "no RSI" as
        range-bound — turns a data gap into a live trade.

        ``trend``/``rsi`` let a caller that has already read them pass them
        in. Each read is an HTTP hop to the DB server, and the directional
        path needs the verdict before it can choose WHICH action to take —
        without this it would fetch the same RSI twice per cycle.
        """
        if trend is None:
            trend, rsi = self._trend_for(underlying)
        rsi_text = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "unavailable"
        if trend == NON_DIRECTIONAL:
            self.logger.info(
                f"  trend gate: {underlying} RSI={rsi_text} -> {trend}; "
                f"{action} may proceed"
            )
            return True
        if trend is None:
            self.logger.warning(
                f"  trend gate: {underlying} RSI={rsi_text} -> trend UNKNOWN; "
                f"skipping {action}. An unknown trend is not a range-bound "
                f"one, so this holds rather than trading on a data gap."
            )
            return False
        self.logger.info(
            f"  trend gate: {underlying} RSI={rsi_text} -> {trend}; skipping "
            f"{action} — it is only valid in a non-directional market."
        )
        return False

    # Kite order statuses that mean the order has reached a FINAL state and
    # is no longer working in the book. Anything NOT in this set (OPEN,
    # TRIGGER PENDING, VALIDATION PENDING, PUT ORDER REQ RECEIVED, MODIFY
    # PENDING, AMO REQ RECEIVED, …) is treated as still-working — so an
    # unrecognised status fails SAFE toward "pending".
    _TERMINAL_ORDER_STATUSES = frozenset({'COMPLETE', 'REJECTED', 'CANCELLED'})

    def _fetch_open_orders(self):
        """Fetch this user's order book via the broker proxy ``/get_orders``.

        Returns the raw list of order dicts on success, or ``None`` when
        the book can't be retrieved (transport failure, broker error, or
        the broker returned no order list for this user). Callers must
        treat ``None`` as "state UNKNOWN" — never as "no orders".
        """
        url = f"{self.api_url}/get_orders?user={self.user}"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"Order-book fetch failed: {e}")
            return None
        if not ok:
            self.logger.warning(f"Broker reported failure for /get_orders: {payload}")
            return None
        orders = (payload or {}).get(self.user)
        if orders is None:
            self.logger.warning("Broker returned no order list for this user on /get_orders")
            return None
        return orders

    def has_pending_orders(self, group):
        """True if this ``(underlying, expiry)`` book has a working order.

        Guards the per-cycle risk pipeline against re-issuing orders that
        are still sitting in the broker queue. ``place_order`` returns as
        soon as the broker ACCEPTS an order — not when it FILLS — so an
        order that hangs PENDING leaves the positions unchanged, and the
        next cycle re-evaluates the same delta trigger and would place the
        SAME orders again. Checking the live
        order book for any non-terminal order on this book's instruments
        closes that gap.

        Orders are matched to the group by parsing each order's
        tradingsymbol into ``(underlying, expiry)`` and comparing against
        the group — so a working order on next week's book never blocks
        this week's, and vice versa.

        Fails SAFE: if the order book can't be read, returns ``True`` (hold
        risk actions this cycle) — consistent with ``place_order``'s policy
        of not acting when the broker state is UNKNOWN. The trade-off is
        that a genuine order-book outage also pauses protective closes, so
        the condition is surfaced via an alert.
        """
        orders = self._fetch_open_orders()
        if orders is None:
            self._alert(
                "OrderBookUnavailable",
                f"Could not read order book for {group.underlying} "
                f"({group.expiry.date()}); holding risk actions this cycle to "
                f"avoid re-issuing orders that may already be pending."
            )
            return True

        group_expiry = group.expiry.date()
        working = []
        for o in orders:
            status = str(o.get('status') or '').upper()
            if status in self._TERMINAL_ORDER_STATUSES:
                continue
            symbol = o.get('tradingsymbol')
            if not symbol:
                continue
            try:
                parsed = split_symbol(symbol)
            except Exception:
                # Unparseable (equity holding, unknown format) — can't be
                # one of this option book's legs, so it can't be a dup.
                continue
            if not parsed or len(parsed) != 4:
                continue
            o_underlying, o_expiry_dt, _strike, _opt = parsed
            try:
                same_expiry = o_expiry_dt.date() == group_expiry
            except AttributeError:
                continue
            if o_underlying == group.underlying and same_expiry:
                working.append(f"{symbol}[{status or 'UNKNOWN'}]")

        if working:
            self.logger.info(
                f"[{group_expiry}] {group.underlying}: {len(working)} working "
                f"order(s) still in the broker queue — holding risk actions "
                f"this cycle to avoid duplicate orders: {', '.join(working)}"
            )
            return True
        return False

    def _record_adjustment_safely(self, **kwargs):
        """Wrap ``record_adjustment`` in try/except so a DB error in
        the audit path never crashes the live trading loop. The
        adjustment already happened at the broker by the time we get
        here — losing the audit row is regrettable but recoverable;
        crashing the cycle would be worse.

        This is now a network call (``helpers.db_client``), so failure is
        appreciably more likely than it was against local SQLite — which
        makes swallowing it more important, not less. It is also why the row
        carries an idempotency key: unlike the ledger, an audit row has no
        natural unique column, so a retry after a lost response would
        otherwise double-count in the dashboard's P&L chart.
        """
        try:
            return record_adjustment(**kwargs)
        except Exception as e:
            self.logger.exception(
                f"Failed to persist adjustment audit row: {e}. "
                f"kwargs={kwargs}"
            )
            self._alert(
                "AdjustmentAuditFailed",
                f"DB write failed for adjustment audit; orders went through. "
                f"Detail: {e}"
            )
            return None

    _MAX_WALK_STEPS = 30  # safety cap when walking strikes toward ATM

    def _underlying_cfg(self, underlying):
        """The ``underlyings.<NAME>`` config block, or ``{}``.

        Holds this underlying's RISK parameters (``investment``), as opposed
        to ``helpers/underlyings.py`` which holds its CONTRACT facts (strike
        step, lot size). Keeping the two apart means an operator tuning risk
        never has to edit code, and a contract-spec change never hides in a
        risk block.
        """
        block = (self.config.get('underlyings') or {})
        entry = block.get(ul.normalize(underlying)) if isinstance(block, dict) else None
        return entry if isinstance(entry, dict) else {}

    def _lot_size_for(self, underlying):
        """Exchange lot size for ``underlying``.

        Precedence, most specific first:

        1. ``underlyings.<NAME>.lot_size`` — the operator's per-underlying
           setting for this exact index.
        2. ``stoploss_info.quantity_per_lot`` — the operator's global
           setting. Preserves the behaviour of every deployment that
           predates per-underlying config.
        3. ``helpers/underlyings.py`` — the built-in default, last resort.

        Config outranks the registry deliberately. The registry is a
        hardcoded convenience; ``quantity_per_lot`` is something the
        operator typed. Letting a built-in silently override an explicit
        setting would change position sizing under them with no signal —
        and sizing is what every partial-reduce and re-add tranche is
        computed from.

        ``stoploss_info.quantity_per_lot`` is the only key left in that
        block now that the stop-loss itself is gone. The block name is a
        misnomer kept deliberately: renaming it would need a coordinated
        edit of the config file on every host that has a copy.

        Returns 0 when nothing is available. Callers must treat that as
        "cannot size" and skip, never as a usable quantity.
        """
        candidates = (
            self._underlying_cfg(underlying).get('lot_size'),
            (self.config.get('stoploss_info') or {}).get('quantity_per_lot'),
            ul.lot_size(underlying),
        )
        for value in candidates:
            try:
                size = int(value or 0)
            except (TypeError, ValueError):
                continue
            if size > 0:
                return size
        return 0

    def _hedge_cfg_for(self, underlying):
        """``hedge_info`` for ``underlying``, falling back to the global block.

        Per-underlying because the distances are absolute POINTS, and the
        same point count is a very different wing width across indices: 200
        points is 0.8% of a 25,000 NIFTY but only 0.37% of a 54,000
        BANKNIFTY. Sharing one block would give BANKNIFTY wings less than
        half as wide in the terms that actually govern max loss.
        """
        own = self._underlying_cfg(underlying).get('hedge_info')
        if isinstance(own, dict) and own:
            return own
        return self.config.get('hedge_info') or {}

    def _hedge_distance_for(self, underlying, dte):
        """DTE-derived hedge distance in points, snapped to the strike grid.

        ``hedge_distance_for_dte`` alone can return an off-grid value (650
        on a 100-point grid), which would name a strike that doesn't exist.
        The dashboard preset already snaps; this brings the adjustments
        engine in line so both produce the same wing.
        """
        raw = hedge_distance_for_dte(dte, self._hedge_cfg_for(underlying))
        return snap_to_strike_grid(raw, ul.strike_step(underlying))

    # _investment_for() was removed with check_stop_loss, its only caller. It
    # resolved the premium-per-lot figure (underlyings.<NAME>.investment, else
    # stoploss_info.investment) that scaled the stop-loss threshold; nothing
    # else in the engine sizes off premium, so both config keys went too.

    @staticmethod
    def _underlying_of(symbol):
        """Underlying prefix of a tradingsymbol, e.g. ``'BANKNIFTY'``.

        Derived from the symbol rather than taken from the group, because
        the symbol is the contract's own statement of what it is — it can't
        drift out of sync with the leg actually being traded, and it's
        available on code paths where ``group`` is optional.
        """
        try:
            parsed = split_symbol(symbol)
        except Exception:
            return ul.DEFAULT_UNDERLYING
        if not parsed or not parsed[0]:
            return ul.DEFAULT_UNDERLYING
        return parsed[0]

    def _strike_step_for(self, symbol):
        """Strike interval for the symbol's underlying — 50 for NIFTY, 100
        for BANKNIFTY.

        Was a hard-coded ``_STRIKE_STEP = 50`` class constant. On a 100-pt
        grid that made every other probed strike a contract that doesn't
        exist: the delta walk would quietly skip them as "delta unknown" and
        cover half the intended distance, so adjustments would under-fire
        with nothing in the log naming the cause.
        """
        return ul.strike_step(self._underlying_of(symbol))

    def _liquidity_floor(self, key, default):
        """``adjustment_info[key]`` as an int, falling back to ``default``."""
        adjustment_info = self.config.get('adjustment_info') or {}
        try:
            return int(adjustment_info.get(key, default))
        except (TypeError, ValueError):
            self.logger.warning(
                f"adjustment_info.{key} is not an integer; using {default}"
            )
            return default

    def _min_oi(self):
        """Open-interest floor for a tradable strike. ``adjustment_info.min_oi``,
        default 1000 shares.

        Delta alone cannot see liquidity: a strike with almost no open
        interest has a perfectly reasonable delta and a printed LTP that is
        the residue of some earlier trade rather than a level anyone is
        standing behind. Selling into it collects less than the screen says;
        buying into it leaves a wing you cannot exit when the wing is the
        only thing protecting the book.
        """
        return self._liquidity_floor('min_oi', 1000)

    def _min_volume(self):
        """Day-volume floor for a tradable strike.
        ``adjustment_info.min_volume``, default 1000 shares.

        The second half of the gate, because OI alone rejects strikes that
        are fine: a strike the exchange listed this morning, or every strike
        of a freshly opened expiry cycle, carries zero OI and is quoted by
        market makers regardless. Volume catches those — somebody traded it
        today. Either measure clearing is enough (see ``_is_tradable``).
        """
        return self._liquidity_floor('min_volume', 1000)

    def _is_tradable(self, quote):
        """Tri-state liquidity verdict for a quote: ``True`` / ``False`` /
        ``None`` (neither OI nor volume reported).

        Delegates to ``db_queries.is_strike_liquid`` so the adjustment engine
        and the dashboard's iron-condor preset apply one rule. They differ
        only in the floors: these come from ``adjustment_info`` because the
        engine is placing orders, where the dashboard is drawing a table.
        """
        if not quote:
            return None
        return is_strike_liquid(quote.get('oi'), quote.get('volume'),
                                min_oi=self._min_oi(),
                                min_volume=self._min_volume())

    @staticmethod
    def _liquidity_text(quote):
        """``'OI 4200 / vol 900'`` for logs, with '?' for absent figures."""
        quote = quote or {}
        oi = quote.get('oi')
        volume = quote.get('volume')
        return (f"OI {oi if oi is not None else '?'} / "
                f"vol {volume if volume is not None else '?'}")

    def _hedge_search_steps(self):
        """How many strike steps either side of a proposed hedge the selector
        may search when the proposal itself fails its gates.
        ``adjustment_info.hedge_search_steps``, default 2.

        Deliberately small. The proposed strike is the wing width the DTE
        rules asked for; drifting far from it to find liquidity would quietly
        deliver a different structure than the one configured, so past this
        distance the adjustment is refused instead.
        """
        adjustment_info = self.config.get('adjustment_info') or {}
        try:
            return max(0, int(adjustment_info.get('hedge_search_steps', 2)))
        except (TypeError, ValueError):
            self.logger.warning(
                "adjustment_info.hedge_search_steps is not an integer; using 2"
            )
            return 2

    def _strike_at_delta(self, current_symbol, current_strike, opt_type,
                         target_abs_delta, direction,
                         spot_price=None, min_distance=None):
        """Walk strikes from ``current_strike`` looking for one whose
        ``|delta|`` meets the target.

        ``direction='inward'`` (toward ATM): PE walks UP, CE walks DOWN.
            Matches the first strike where ``|delta| >= target_abs_delta``.
            Used when a short leg drifted too far OTM and needs to be
            brought closer to ATM.

        ``direction='outward'`` (away from ATM): PE walks DOWN, CE walks UP.
            Matches the first strike where ``|delta| <= target_abs_delta``.
            Used when a short leg got too close to ATM and needs to be
            backed off.

        Per-strike delta is read off ``/get_current_price`` which serves
        from the DB snapshot table when the recorder has covered the strike.
        Strikes outside the recorder's delta band come back with
        ``delta=None`` and the walk skips them — never matched as a target.

        A strike that meets the delta target must ALSO be tradable before it
        is selected (``_is_tradable``: OI **or** day volume clears its floor).
        Delta says the strike is at the right distance; OI and volume say
        somebody is actually there. A strike failing both is stepped past, so
        the roll lands on a slightly different strike rather than into one
        nobody trades.

        A strike reporting NEITHER figure is selected anyway, with a warning.
        Unknown is not thin, and refusing everything unmeasurable would
        silently switch adjustments off on a database predating the volume
        migration — a short drifting unrolled costs far more than a slightly
        worse fill.

        Returns ``((symbol, strike, quote), None)`` on success — the quote
        comes back so the caller can price the hedge against the short it
        will protect without re-fetching — or ``(None, reason)`` on failure,
        where ``reason`` names what actually stopped the walk. The caller
        puts that in its alert: "no strike found" plus the search criteria
        is not diagnosable on its own, because the walk gives up for several
        unrelated reasons (see ``_walk_failure_reason``).

        When ``spot_price`` and ``min_distance`` are both supplied AND
        the direction is ``'inward'``, the walk also stops the first
        time a candidate strike would come within ``min_distance`` of
        spot — i.e., the rule "don't bring the gaining leg too close to
        spot" is enforced on the leg actively being moved. Outward
        walks ignore the boundary (they move AWAY from spot).
        """
        strike_step = self._strike_step_for(current_symbol)
        min_oi, min_volume = self._min_oi(), self._min_volume()
        if direction == 'inward':
            step_signed = strike_step if opt_type == 'PE' else -strike_step
            def meets(d): return d >= target_abs_delta
            cmp_sym = '>='
        elif direction == 'outward':
            step_signed = -strike_step if opt_type == 'PE' else strike_step
            def meets(d): return d <= target_abs_delta
            cmp_sym = '<='
        else:
            raise ValueError(f"direction must be 'inward' or 'outward', got {direction!r}")

        # Inward-only spot boundary: PE strike must stay <= spot - min_dist,
        # CE strike must stay >= spot + min_dist. Stops the walk before
        # the first strike that would cross.
        enforce_boundary = (
            direction == 'inward'
            and spot_price is not None
            and min_distance is not None
        )

        # Why each candidate was passed over, so a failed walk can say what
        # actually stopped it. Without this the caller can only report the
        # search criteria, which reads as "your strikes are illiquid" even
        # when the real cause was the spot boundary or a missing delta.
        tally = {}
        best_delta = None       # (abs_delta, strike) closest to qualifying
        probed = []

        def _note(reason):
            tally[reason] = tally.get(reason, 0) + 1

        for n in range(1, self._MAX_WALK_STEPS + 1):
            new_strike = current_strike + step_signed * n
            if enforce_boundary:
                crosses = (opt_type == 'PE' and new_strike > spot_price - min_distance) or \
                          (opt_type == 'CE' and new_strike < spot_price + min_distance)
                if crosses:
                    self.logger.info(
                        f"  delta-walk (inward): {new_strike} would come within "
                        f"{min_distance} pts of spot {spot_price}; stopping "
                        f"walk before crossing the safety boundary"
                    )
                    return None, self._walk_failure_reason(
                        'boundary', probed, tally, best_delta,
                        target_abs_delta, cmp_sym,
                        boundary=(new_strike, min_distance, spot_price))
            probed.append(new_strike)
            new_symbol = reprice_option_symbol(current_symbol, current_strike,
                                               new_strike)
            url = f"{self.api_url}/get_current_price/{new_symbol}/option"
            try:
                ok, payload = api_get(url, logger=self.logger)
            except ApiError as e:
                self.logger.warning(f"  delta-walk: {new_symbol} fetch failed: {e}")
                _note('quote fetch failed')
                continue
            if not ok:
                self.logger.info(f"  delta-walk: {new_symbol} broker reported failure")
                _note('broker reported failure')
                continue
            quote = payload.get(f"NFO:{new_symbol}", {})
            delta = quote.get('delta')
            if delta is None:
                self.logger.info(f"  delta-walk: {new_symbol} delta unknown — skipping")
                _note('delta unknown (outside recorder band, or quote came '
                      'from the broker LTP path which carries no greeks)')
                continue
            abs_d = abs(delta)
            if best_delta is None or abs(abs_d - target_abs_delta) < abs(best_delta[0] - target_abs_delta):
                best_delta = (abs_d, new_strike)
            if not meets(abs_d):
                self.logger.info(
                    f"  delta-walk ({direction}): {new_symbol} |delta|={abs_d:.3f} "
                    f"not {cmp_sym} {target_abs_delta:.2f} — continuing"
                )
                _note(f'|delta| not {cmp_sym} {target_abs_delta:.2f}')
                continue
            # Delta qualifies; now the liquidity gate. Skipping rather than
            # failing here is deliberate — the next strike out is usually
            # thicker, and the caller only gives up once the whole walk is
            # exhausted (which it already reports as an AdjustmentSkipped).
            liquid = self._is_tradable(quote)
            if liquid is False:
                self.logger.info(
                    f"  delta-walk ({direction}): {new_symbol} |delta|={abs_d:.3f} "
                    f"{cmp_sym} {target_abs_delta:.2f} but "
                    f"{self._liquidity_text(quote)} clears neither floor "
                    f"({min_oi} / {min_volume}) — too thin to sell into, skipping"
                )
                _note(f'OI/volume below {min_oi}/{min_volume}')
                continue
            if liquid is None:
                # Neither figure reported. Unknown is not thin — a strike we
                # can't measure is still the strike the delta target picked,
                # and refusing every one of them would silently disable
                # adjustments on a DB predating the volume migration.
                self.logger.warning(
                    f"  delta-walk ({direction}): {new_symbol} reports neither "
                    f"OI nor volume — selecting on delta alone. Run "
                    f"migrate_db.py if this persists."
                )
            self.logger.info(
                f"  delta-walk ({direction}): {new_symbol} |delta|={abs_d:.3f} "
                f"{cmp_sym} {target_abs_delta:.2f}, {self._liquidity_text(quote)}"
                f" — selected"
            )
            return (new_symbol, new_strike, quote), None
        return None, self._walk_failure_reason(
            'exhausted', probed, tally, best_delta, target_abs_delta, cmp_sym)

    @staticmethod
    def _walk_failure_reason(outcome, probed, tally, best_delta,
                             target_abs_delta, cmp_sym, boundary=None):
        """Human-readable account of why a delta walk found nothing.

        The walk gives up for several unrelated reasons and they call for
        different responses — widen the search, fix the recorder, relax
        ``minimum_distance_pct``, lower a liquidity floor. Naming only the
        search criteria (as the caller's alert used to) points at the last
        thing that changed rather than the thing that actually stopped it.
        """
        if probed:
            span = f"{min(probed)}-{max(probed)}"
            head = f"probed {len(probed)} strike(s) {span}"
        else:
            head = "probed no strikes"

        if outcome == 'boundary':
            blocked, min_distance, spot = boundary
            head = (f"STOPPED at the spot safety boundary: {blocked} is within "
                    f"{min_distance} pts of spot {spot} "
                    f"(adjustment_info.minimum_distance_pct); " + head
                    + " before that")

        parts = [head]
        if tally:
            parts.append("rejections: " + ", ".join(
                f"{reason} x{count}" for reason, count in sorted(tally.items())))
        if best_delta is not None:
            parts.append(f"closest was |delta|={best_delta[0]:.3f} at "
                         f"{best_delta[1]} (needed {cmp_sym} "
                         f"{target_abs_delta:.2f})")
        else:
            parts.append("no candidate reported a delta at all")
        return "; ".join(parts)

    def _select_hedge_strike(self, side, direction, proposed_symbol,
                             proposed_strike, new_short_strike, short_ltp):
        """Pick the hedge (BUY) strike for a roll, or ``None`` when nothing
        within reach is usable.

        ``proposed_strike`` is the wing the DTE rules asked for. A hedge is
        only accepted when ALL of:

          - the broker quotes a non-zero ``last_price`` — the strike is
            actually listed and trading;
          - it is tradable by ``_is_tradable`` — OI **or** day volume clears
            its floor. A long nobody trades is the leg you cannot get out of,
            and the wing is the thing protecting the book. A strike reporting
            neither figure is allowed through with a warning (unknown is not
            thin; see ``_strike_at_delta``);
          - it costs LESS than the short it protects. A hedge priced at or
            above its own short makes the spread a net debit: the structure
            would pay premium out to open instead of collecting it, which is
            not the trade being placed.

        When the proposal fails, strikes within ``_hedge_search_steps()`` are
        tried in order of distance, taking the toward-the-short side first at
        each distance — that direction narrows the wing (cheaper, and where
        the liquidity sits) rather than widening it into the tail. Candidates
        landing within one strike of the new short are never offered; that is
        the pre-existing safety floor and it still holds.

        Returns ``(symbol, strike, quote)``. Callers must treat ``None`` as
        "refuse the whole adjustment" — this runs before any order goes out
        precisely so that refusal is free.
        """
        step = self._strike_step_for(proposed_symbol)
        min_oi, min_volume = self._min_oi(), self._min_volume()
        max_steps = self._hedge_search_steps()
        # "Toward the short" is UP for a PE hedge (its short sits at a higher
        # strike) and DOWN for a CE hedge.
        toward_short = step if side == 'PE' else -step
        floor_strike = (new_short_strike - step if side == 'PE'
                        else new_short_strike + step)

        # 0 first (the configured wing), then ±1, ±2, … — nearer proposals
        # before further ones, toward-short before away-from-short.
        offsets = [0]
        for n in range(1, max_steps + 1):
            offsets.extend([n, -n])

        if not short_ltp:
            self.logger.warning(
                f"  hedge-pick ({side}): new short's LTP is unknown; the "
                f"'hedge must cost less than its short' check cannot be "
                f"applied this cycle (OI and tradability still enforced)"
            )

        rejected = []
        for off in offsets:
            strike = proposed_strike + toward_short * off
            crossed = ((side == 'PE' and strike > floor_strike) or
                       (side == 'CE' and strike < floor_strike))
            if crossed:
                rejected.append(f"{strike}: inside the {floor_strike} safety "
                                f"floor (1 strike from short {new_short_strike})")
                continue
            symbol = reprice_option_symbol(proposed_symbol, proposed_strike,
                                           strike)
            quote = self._fetch_option_quote(symbol)
            if quote is None:
                # Transport / broker failure. Unverifiable is NOT the same as
                # acceptable — we move on rather than buying into a strike we
                # could not check.
                rejected.append(f"{strike}: quote unavailable (could not verify)")
                continue
            ltp = quote.get('last_price')
            if not ltp:
                rejected.append(f"{strike}: no LTP (strike not listed/traded)")
                continue
            liquid = self._is_tradable(quote)
            if liquid is False:
                rejected.append(
                    f"{strike}: {self._liquidity_text(quote)} clears neither "
                    f"floor ({min_oi} / {min_volume})")
                continue
            if short_ltp and float(ltp) >= float(short_ltp):
                rejected.append(
                    f"{strike}: LTP {ltp} >= short LTP {short_ltp} "
                    f"(would open the spread as a net debit)")
                continue
            if liquid is None:
                self.logger.warning(
                    f"  hedge-pick ({side}): {symbol} reports neither OI nor "
                    f"volume — accepting on price alone. Run migrate_db.py "
                    f"if this persists."
                )
            self.logger.info(
                f"  hedge-pick ({side}, {direction}): {symbol}@{strike} "
                f"LTP {ltp}, {self._liquidity_text(quote)}"
                + (f", below short LTP {short_ltp}" if short_ltp else "")
                + (" — selected (configured wing)" if off == 0 else
                   f" — selected, {abs(off)} strike(s) "
                   f"{'toward' if off > 0 else 'away from'} the short")
            )
            return symbol, strike, quote

        self.logger.error(
            f"  hedge-pick ({side}, {direction}): no usable hedge within "
            f"{max_steps} strike(s) of {proposed_symbol}@{proposed_strike}. "
            f"Rejected: " + "; ".join(rejected)
        )
        return None

    def _roll_short_leg(self, side, short_leg, hedge_leg, target_abs_delta,
                        direction, strategy, adjust_hedges,
                        spot_price=None, min_distance=None, group=None,
                        legs_closed=False):
        """Buy back the short leg and sell a new strike whose ``|delta|``
        meets the target.

        ``legs_closed=True`` is the re-open half of the high-delta path: the
        short and its hedge were already closed by ``_close_short_and_hedge``,
        so only the new hedge and new short are placed. The hedge is then
        re-bought whatever ``adjust_hedges`` says — the old one is gone, and
        the alternative is a naked short. A refusal leaves the side flat
        rather than "holding", and the audit row carries no realised P&L
        because the close already booked it.

        ``direction='inward'`` walks toward ATM (used when the short drifted
        too far OTM); ``direction='outward'`` walks away from ATM (used when
        the short got too close to ATM). When the strategy is IRON_CONDOR or
        DIRECTIONAL and ``adjust_hedges`` is set, the long hedge is rolled
        with the short so the spread width is preserved in either case.

        BOTH legs are chosen and validated before the first order goes out —
        the short by ``_strike_at_delta`` (delta target + OI floor), the hedge
        by ``_select_hedge_strike`` (tradable + OI floor + priced below its
        short). Either can refuse, and refusing has to be free: deciding the
        hedge after buying back the short would leave the book holding a wing
        with nothing selling against it.

        When ``group`` is supplied, a row is written to the ``adjustments``
        audit table after all orders succeed — with order_ids harvested
        from each ``_place_order`` call so the snapshot UI can join back
        to ``kite_trades`` and show what actually filled.
        """
        found, walk_reason = self._strike_at_delta(
            current_symbol=short_leg['symbol'],
            current_strike=short_leg['strike'],
            opt_type=side,
            target_abs_delta=target_abs_delta,
            direction=direction,
            spot_price=spot_price,
            min_distance=min_distance,
        )
        refused_outcome = (f"{side} side left flat — not re-opened"
                           if legs_closed else "Holding existing position")
        if not found:
            # State the criteria AND what actually stopped the walk. These
            # are different things: the search can end at the spot boundary,
            # on missing greeks, or simply out of range, none of which the
            # criteria line explains on its own.
            msg = (f"No {side} strike found for {short_leg['symbol']} "
                   f"(wanted |delta| {'>=' if direction == 'inward' else '<='} "
                   f"{target_abs_delta:.2f} with OI >= {self._min_oi()} or "
                   f"volume >= {self._min_volume()}, within "
                   f"{self._MAX_WALK_STEPS} steps). {walk_reason}. "
                   f"{refused_outcome}")
            self.logger.error(msg)
            self._alert("AdjustmentSkipped", msg)
            return

        new_short_symbol, new_short_strike, new_short_quote = found
        strike_shift = new_short_strike - short_leg['strike']
        # The hedge must come in cheaper than this — see _select_hedge_strike.
        new_short_ltp = (new_short_quote or {}).get('last_price')

        # Quantity for the new legs. INWARD rolls stay 1:1 with the
        # existing same-side qty — repositioning, not resizing. OUTWARD
        # rolls instead size the new legs to match the OPPOSITE side's
        # short qty, restoring the iron condor's symmetry when the
        # losing side has been previously trimmed by partial-reduce.
        # Falls back to same-side qty when there's no opposite leg to
        # reference (e.g. a one-sided strangle).
        new_short_qty = int(short_leg['quantity'])
        new_hedge_qty = (int(hedge_leg['quantity'])
                         if hedge_leg.get('quantity') else None)
        if direction == 'outward' and group is not None:
            all_legs = group.get_legs()
            opp_key = 'short_call' if side == 'PE' else 'short_put'
            opp = all_legs.get(opp_key) or {}
            opp_qty = opp.get('quantity')
            if opp_qty and int(opp_qty) > 0 and int(opp_qty) != new_short_qty:
                self.logger.info(
                    f"Outward roll: sizing new {side} legs to opposite "
                    f"{opp_key} qty {int(opp_qty)} (existing {side} short "
                    f"was {new_short_qty})"
                )
                new_short_qty = int(opp_qty)
                new_hedge_qty = int(opp_qty)

        # Pick the hedge BEFORE placing anything. The hedge distance is
        # derived from the group's DTE (100/200/300 pts for <=7 / <=14 / >14
        # days) rather than blindly preserving the old spread width — so the
        # wing tightens as expiry approaches. When no ``group`` context is
        # available (defensive: both callers pass one), fall back to
        # preserving the width by rolling the hedge the same distance as the
        # short. _select_hedge_strike then vets that proposal for
        # tradability, OI and price, searching a couple of strikes either
        # side; if it can't find one, the whole roll is refused here with no
        # orders placed.
        # DIRECTIONAL alongside IRON_CONDOR: a one-sided vertical is
        # structurally one wing of a condor, so its hedge has to follow its
        # short for exactly the same reason. Leaving it behind would widen
        # the spread by however far the short moved, silently increasing max
        # loss on a book the operator sized deliberately.
        #
        # SHORT_STRANGLE is still excluded, and correctly: it has no hedge to
        # move (hedge_leg['symbol'] is None there anyway).
        hedge_needed = (
            strategy in (IRON_CONDOR, DIRECTIONAL)
            and (adjust_hedges or legs_closed)
            and hedge_leg['symbol']
        )
        new_hedge_strike = None
        new_hedge_symbol = None
        if hedge_needed:
            if group is not None:
                dte = (group.expiry.date() - datetime.now(IST).date()).days
                hedge_distance = self._hedge_distance_for(
                    group.underlying, dte)
                proposed_hedge_strike = (new_short_strike - hedge_distance
                                         if side == 'PE'
                                         else new_short_strike + hedge_distance)
                self.logger.info(
                    f"  {side} hedge distance {hedge_distance}pt "
                    f"({group.underlying}, DTE={dte}): "
                    f"new short {new_short_strike} -> hedge {proposed_hedge_strike}"
                )
            else:
                proposed_hedge_strike = int(hedge_leg['strike']) + strike_shift
            # reprice_option_symbol rather than a bare str.replace: the
            # strike's digits also occur inside weekly expiry codes, and
            # rewriting the leftmost match yields a contract that was never
            # listed (see helpers/basic.py).
            proposed_hedge_symbol = reprice_option_symbol(
                hedge_leg['symbol'], hedge_leg['strike'], proposed_hedge_strike)
            picked = self._select_hedge_strike(
                side=side,
                direction=direction,
                proposed_symbol=proposed_hedge_symbol,
                proposed_strike=proposed_hedge_strike,
                new_short_strike=new_short_strike,
                short_ltp=new_short_ltp,
            )
            if not picked:
                msg = (f"No usable {side} hedge within "
                       f"{self._hedge_search_steps()} strike(s) of "
                       f"{proposed_hedge_symbol}@{proposed_hedge_strike} "
                       f"(needs OI >= {self._min_oi()} or volume >= "
                       f"{self._min_volume()}, plus LTP below the new short's "
                       f"{new_short_ltp}); {refused_outcome} "
                       f"— no orders placed")
                self.logger.error(msg)
                self._alert("AdjustmentSkipped", msg)
                return
            new_hedge_symbol, new_hedge_strike, _ = picked

        self.logger.info(
            f"Rolling {side} short: {short_leg['symbol']}@{short_leg['strike']} -> "
            f"{new_short_symbol}@{new_short_strike} (shift {strike_shift:+d}, "
            f"new qty {new_short_qty})"
        )
        self._alert(
            "Adjustment",
            f"Rolling {side} short {short_leg['symbol']} -> {new_short_symbol}"
        )

        order_ids = []

        # 1. Buy back the existing short leg (already done when legs_closed)
        if not legs_closed:
            result = self._place_order(symbol=short_leg['symbol'],
                                       quantity=short_leg['quantity'],
                                       transaction_type="BUY",
                                       exchange='NFO')
            self.logger.info(result)
            time.sleep(2)
            if not result:
                return
            order_ids.append(str(result))

        # 2-3. Reposition the hedge onto the strike chosen above (iron
        # condor only). No searching happens here — by this point the
        # strike is already known to be listed, liquid enough, and cheaper
        # than the short it sits under.
        if hedge_needed:
            self.logger.info(
                f"Rolling {side} hedge: {hedge_leg['symbol']}@{hedge_leg['strike']} -> "
                f"{new_hedge_symbol}@{new_hedge_strike}"
            )
            if not legs_closed:
                result = self._place_order(symbol=hedge_leg['symbol'],
                                           quantity=hedge_leg['quantity'],
                                           transaction_type="SELL",
                                           exchange='NFO')
                self.logger.info(result)
                time.sleep(2)
                if not result:
                    return
                order_ids.append(str(result))
            # New hedge sized to ``new_hedge_qty`` (set above): same as
            # the existing hedge for inward rolls; matches the opposite
            # side's short qty for outward rolls.
            result = self._place_order(symbol=new_hedge_symbol,
                                       quantity=(new_hedge_qty
                                                 if new_hedge_qty
                                                 else hedge_leg['quantity']),
                                       transaction_type="BUY",
                                       exchange='NFO')
            self.logger.info(result)
            time.sleep(2)
            if not result:
                return
            order_ids.append(str(result))

        # 4. Sell the new short leg (sized to ``new_short_qty``)
        result = self._place_order(symbol=new_short_symbol,
                                   quantity=new_short_qty,
                                   transaction_type="SELL",
                                   exchange='NFO')
        self.logger.info(result)
        if not result:
            return
        order_ids.append(str(result))

        # Audit row — write only when every order succeeded. On a
        # legs_closed re-open the close already booked the realised P&L in
        # its own row; repeating it here would double-count in the chart.
        if group is not None:
            realized_pnl = (None if legs_closed else
                            self._realized_pnl_on_close(
                                short_leg, hedge_leg, group,
                                include_hedge=hedge_needed))

            self._record_adjustment_safely(
                timestamp=datetime.now(IST).isoformat(),
                user=self.user,
                underlying=group.underlying,
                expiry=group.expiry.date().isoformat(),
                side=side,
                adjustment_type=direction,  # 'inward' or 'outward'
                short_strike_before=int(short_leg['strike']),
                short_strike_after=int(new_short_strike),
                short_delta_before=float(short_leg.get('delta') or 0),
                short_delta_after=None,
                hedge_strike_before=int(hedge_leg['strike']) if hedge_leg.get('strike') else None,
                hedge_strike_after=int(new_hedge_strike) if new_hedge_strike is not None else None,
                hedge_delta_before=float(hedge_leg.get('delta') or 0) if hedge_leg.get('delta') is not None else None,
                hedge_delta_after=None,
                nifty_spot=float(spot_price) if spot_price else None,
                india_vix=self._india_vix(),
                order_ids=",".join(order_ids),
                realized_pnl=realized_pnl,
            )

    def _realized_pnl_on_close(self, short_leg, hedge_leg, group, include_hedge):
        """Realised P&L of fully closing ``short_leg`` (and ``hedge_leg``
        when ``include_hedge``), for the audit row.

        Computed from the FIFO entry prices of the lots that were open on
        the legs, vs their last_price at adjustment time. ``open_lot_price``
        reads a cache populated at the top of the cycle, and
        ``group.positions`` is the pre-trade snapshot from
        process_positions, so both sides of the comparison predate the
        orders just placed — which is what this figure wants.

        Returns None rather than a number the ledger cannot support. The
        broker's blended averages were the earlier fallback; on a leg that
        has been reopened in the same symbol they overstate the gain, and a
        plausible-looking wrong figure in an audit table is worse than a
        NULL.
        """
        short_pos = next(
            (p for p in group.positions
             if p.get('tradingsymbol') == short_leg['symbol']),
            None,
        )
        hedge_pos = next(
            (p for p in group.positions
             if p.get('tradingsymbol') == hedge_leg.get('symbol')),
            None,
        )
        try:
            qty = abs(int(short_leg.get('quantity') or 0))
            rp = 0.0
            priced = True
            short_avg_sell = self.processor.open_lot_price(
                short_leg['symbol'],
                broker_qty=int(short_pos.get('quantity') or 0),
            ) if short_pos else None
            if short_avg_sell is None:
                priced = False
            else:
                rp += (short_avg_sell - float(short_leg.get('last_price') or 0)) * qty
            if include_hedge and hedge_pos:
                hedge_avg_buy = self.processor.open_lot_price(
                    hedge_leg['symbol'],
                    broker_qty=int(hedge_pos.get('quantity') or 0),
                )
                if hedge_avg_buy is None:
                    priced = False
                else:
                    hqty = abs(int(hedge_leg.get('quantity') or 0))
                    rp += (float(hedge_leg.get('last_price') or 0) - hedge_avg_buy) * hqty
            return float(rp) if priced else None
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            # KeyError included: every order has already filled by this
            # point, so a broker row missing sell_value/buy_value must
            # cost us the P&L figure only. Letting it escape would surface
            # as "AdjustmentFailed — may be partially adjusted" when the
            # adjustment in fact completed.
            self.logger.info(
                f"Could not compute realized_pnl for audit row: {e}"
            )
            return None

    def _close_short_and_hedge(self, side, short_leg, hedge_leg, group,
                               spot_price=None):
        """Close the losing ``side`` outright: buy back the short, then sell
        its hedge. Returns True only when every order went through.

        The close half of the high-delta path, and deliberately NOT gated on
        trend — a short at ``delta_high_close`` is cut whatever the market
        is doing. Whether to re-sell further out is decided afterwards (see
        ``adjustments``).

        Short first, hedge second, so the book is never short without its
        wing: a failure between the two leaves only a long option, which
        cannot lose more than it cost. The hedge is closed even when
        ``adjust_hedges`` is off — with its short gone it is protecting
        nothing.
        """
        order_ids = []

        result = self._place_order(symbol=short_leg['symbol'],
                                   quantity=short_leg['quantity'],
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            msg = (f"Close of {side} short {short_leg['symbol']} "
                   f"({group.expiry.date()}) was not placed — position "
                   f"unchanged, still above delta_high_close")
            self.logger.error(msg)
            self._alert("AdjustmentFailed", msg)
            return False
        order_ids.append(str(result))

        has_hedge = bool(hedge_leg.get('symbol') and hedge_leg.get('quantity'))
        if has_hedge:
            result = self._place_order(symbol=hedge_leg['symbol'],
                                       quantity=hedge_leg['quantity'],
                                       transaction_type="SELL",
                                       exchange='NFO')
            self.logger.info(result)
            time.sleep(2)
            if not result:
                msg = (f"{side} short {short_leg['symbol']} closed but its "
                       f"hedge {hedge_leg['symbol']} ({group.expiry.date()}) "
                       f"was not sold — a lone long remains; verify manually")
                self.logger.error(msg)
                self._alert("AdjustmentFailed", msg)
                return False
            order_ids.append(str(result))

        self._alert(
            "Adjustment",
            f"Closed {side} short {short_leg['symbol']}"
            + (f" and hedge {hedge_leg['symbol']}" if has_hedge else "")
            + f" ({group.expiry.date()}) at |delta| "
              f"{abs(short_leg.get('delta') or 0):.3f}"
        )
        self._record_adjustment_safely(
            timestamp=datetime.now(IST).isoformat(),
            user=self.user,
            underlying=group.underlying,
            expiry=group.expiry.date().isoformat(),
            side=side,
            adjustment_type='close_high',
            short_strike_before=int(short_leg['strike']),
            short_strike_after=None,
            short_delta_before=float(short_leg.get('delta') or 0),
            short_delta_after=None,
            hedge_strike_before=int(hedge_leg['strike']) if has_hedge and hedge_leg.get('strike') else None,
            hedge_strike_after=None,
            hedge_delta_before=float(hedge_leg['delta']) if has_hedge and hedge_leg.get('delta') is not None else None,
            hedge_delta_after=None,
            nifty_spot=float(spot_price) if spot_price else None,
            india_vix=self._india_vix(),
            order_ids=",".join(order_ids),
            realized_pnl=self._realized_pnl_on_close(
                short_leg, hedge_leg, group, include_hedge=has_hedge),
        )
        return True

    def _extra_hedge_qty(self, underlying):
        """Deliberate hedge surplus (shares) a partial-reduce leaves on top
        of 1:1 cover: ``adjustment_info.partial_extra_hedge_lots`` × lot
        size. 0 when unset, misconfigured, or the lot size is unknown."""
        adjustment_info = self.config.get('adjustment_info') or {}
        try:
            lots = max(0, int(adjustment_info.get('partial_extra_hedge_lots', 0) or 0))
        except (TypeError, ValueError):
            self.logger.warning(
                "adjustment_info.partial_extra_hedge_lots is not an integer; using 0")
            return 0
        return lots * max(0, self._lot_size_for(underlying)) if lots else 0

    def _overhedged_block(self, side, group, short_qty, hedge_qty, action):
        """True when ``side`` carries more hedge than it should — skip ``action``.

        A completed partial-reduce leaves the hedge at the residual short
        plus ``_extra_hedge_qty`` (the configured surplus), and a completed
        re-add leaves it 1:1. So a surplus ABOVE that allowance is the
        signature of an adjustment that stopped part-way: the short buyback
        filled but a hedge order didn't (surplus grows by ``q1``), or the
        new hedge was bought but the old one never sold (hedge roughly
        doubles). Trimming again from that state prices the unsold hedge's
        gain a second time — the gain that already funded the half-done
        trim — and re-adding could stack yet more hedges. Read off live
        quantities each cycle, so it holds across a monitor restart without
        any stored flag, and clears itself once the quantities are squared
        up by hand.

        Uses the CURRENT config value, so lowering
        ``partial_extra_hedge_lots`` while a side carries the old surplus
        will block that side — square it up first (the config says so).

        Under-hedged (hedge < short) is NOT blocked: it can be a deliberate
        manual shape, and trimming only moves it toward 1:1.
        """
        key = (group.underlying, group.expiry.date(), side)
        allowance = self._extra_hedge_qty(group.underlying)
        if hedge_qty - short_qty <= allowance:
            self._overhedge_alerted.pop(key, None)
            return False
        msg = (f"[{group.expiry.date()}] {group.underlying} {side} "
               f"OVER-HEDGED: hedge {hedge_qty} > short {short_qty} + "
               f"allowed surplus {allowance} — likely a half-done "
               f"adjustment. {action} BLOCKED on this side until the "
               f"surplus is within {allowance}; square it up manually.")
        self.logger.warning(msg)
        state = (short_qty, hedge_qty)
        if self._overhedge_alerted.get(key) != state:
            self._overhedge_alerted[key] = state
            self._alert("SideOverHedged", msg)
        return True

    def _report_incomplete_adjustment(self, side, group, adjustment_type,
                                      done, failed, order_ids, audit):
        """Alert + audit an adjustment that stopped after some orders filled.

        ``done`` lists the steps that went through, ``failed`` names the one
        that didn't. The audit row is written with ``adjustment_type``
        suffixed ``_incomplete`` and ``realized_pnl=None``: the snapshot
        estimate assumes every leg traded, so it would be wrong here, and
        the dashboard sums that column. Blocking the follow-up cycles is
        ``_overhedged_block``'s job — the partial fills leave the side
        over-hedged, which it detects from positions alone.
        """
        msg = (f"{side} {adjustment_type} INCOMPLETE ({group.underlying} "
               f"{group.expiry.date()}): done [{'; '.join(done)}], FAILED "
               f"[{failed}]. Side is unbalanced — verify and fix manually.")
        self.logger.error(f"  [{group.expiry.date()}] {msg}")
        self._alert("AdjustmentIncomplete", msg)
        self._record_adjustment_safely(
            **audit,
            adjustment_type=f'{adjustment_type}_incomplete',
            order_ids=",".join(order_ids),
            realized_pnl=None,
        )

    def _partial_reduce_losing_side(self, side, short_leg, hedge_leg, group):
        """Funding-conditional partial close of a losing iron-condor side.

        Fires when ``|short_delta|`` sits between ``delta_partial_close``
        and ``delta_high_close`` AND the hedge has accumulated enough
        profit to cover at least one lot of short-buyback loss. Outcome:
        a smaller short and a smaller, tighter hedge — the realised P&L
        of steps (a)+(b) is ≈ 0 by construction, step (c) is a fresh
        debit paid for forward protection of the remaining short.

        Mechanics, in execution order (b, c, a — the short is reduced and
        the new hedge bought before the old wing is given up, so the
        residual short is never naked):
          b. Partial-close ``q1`` of the short (BUY back), where
             ``q1 = floor(hedge_profit / per_share_short_loss / lot_size)
             * lot_size``. Lot-aligned, rounded DOWN so the funding
             constraint is conservative.
          a. Close the hedge fully (SELL — it's a long).
          c. Reopen the hedge at the SAME strike it was just closed at,
             sized to FULLY COVER the residual short (``short_open_qty
             − q1``) PLUS ``_extra_hedge_qty`` — the configured
             ``partial_extra_hedge_lots`` surplus, a paid bet that the move
             continues. The next re-add squares the side back to 1:1. With
             the surplus at 0 the post-trade shape is a smaller short with a
             1:1 hedge ratio at an unchanged wing width. (Earlier this
             rolled the hedge one strike step closer to the short;
             it now stays put per operator preference — the close+reopen
             exists only to resize the hedge down to the residual short.)

        Both legs' entry prices are the FIFO cost basis of their OPEN lots
        (``PositionProcessor.open_lot_price``), never the broker's
        day-cumulative averages — see the comment at the call below for why
        the distinction decides whether a second reduction is sized right.
        One approximation remains and is deliberate: the short basis is the
        weighted average across its open lots, while a buyback actually
        retires the OLDEST lots first. The two agree unless a re-add has put
        tranches on at different prices, and resolving it exactly would make
        ``q1`` depend on a basis that depends on ``q1``.

        Defensive skips (log + no orders): missing broker rows, zero
        open quantity on either leg, no ledger-corroborated basis for
        either leg, ``q1`` rounds to 0 lots, or ``q1`` would fully close
        the short (defer to the high-delta path).
        """
        short_pos = next(
            (p for p in group.positions
             if p.get('tradingsymbol') == short_leg['symbol']),
            None,
        )
        hedge_pos = next(
            (p for p in group.positions
             if p.get('tradingsymbol') == hedge_leg['symbol']),
            None,
        )
        if not short_pos or not hedge_pos:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"missing broker position(s); skipping"
            )
            return

        short_last = float(short_pos.get('last_price') or 0)
        hedge_last = float(hedge_pos.get('last_price') or 0)
        short_signed_qty = int(short_pos.get('quantity') or 0)
        hedge_signed_qty = int(hedge_pos.get('quantity') or 0)
        short_open_qty = abs(short_signed_qty)
        hedge_open_qty = abs(hedge_signed_qty)
        if short_open_qty <= 0 or hedge_open_qty <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"a leg has zero open qty; skipping"
            )
            return
        if self._overhedged_block(side, group, short_open_qty,
                                  hedge_open_qty, "Partial-reduce"):
            return

        # Entry prices come from the FIFO ledger's OPEN lots, NOT from the
        # broker's sell_value/sell_quantity and buy_value/buy_quantity.
        #
        # This is what makes a SECOND reduction correct. Those broker fields
        # are day-cumulative per tradingsymbol, and step (c) below reopens
        # the hedge at the SAME strike it just closed — so on the next trim
        # buy_value covers the original tranche and the reopened one
        # together. The blend sits below the reopened lot's true cost (the
        # hedge appreciated, which is why the first trim fired), so the
        # hedge reads more profitable than it is and the gain that already
        # funded q1 gets spent a second time. Gate 2 was documented as
        # rate-limiting repeat triggers on the grounds that a fresh hedge
        # shows ~0 profit; under the blend it does not, and reductions could
        # cascade on consecutive cycles with no new hedge appreciation at
        # all. The short side has the mirror of this once ``_readd_reduced_side``
        # has sold a second tranche into sell_value.
        #
        # Fail CLOSED: no corroborated basis, no trade. The alternative —
        # falling back to the broker average — is the exact bug this
        # replaces, and it would reappear silently. A skip costs one cycle;
        # the ledger re-ingests as soon as the broker's net qty changes.
        short_avg_sell = self.processor.open_lot_price(
            short_leg['symbol'], broker_qty=short_signed_qty)
        hedge_avg_buy = self.processor.open_lot_price(
            hedge_leg['symbol'], broker_qty=hedge_signed_qty)
        if short_avg_sell is None or hedge_avg_buy is None:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: no "
                f"corroborated FIFO open-lot basis for the "
                f"{'short' if short_avg_sell is None else 'hedge'} leg "
                f"(see the warning above); skipping rather than sizing off "
                f"the broker's blended average"
            )
            return

        per_share_short_loss = short_last - short_avg_sell
        per_share_hedge_profit = hedge_last - hedge_avg_buy
        short_total_loss = per_share_short_loss * short_open_qty
        hedge_total_profit = per_share_hedge_profit * hedge_open_qty

        lot_size = self._lot_size_for(group.underlying)
        if lot_size <= 0:
            self.logger.warning(
                f"  [{group.expiry.date()}] {side} partial-reduce: no lot size "
                f"for {group.underlying} (registry and "
                f"stoploss_info.quantity_per_lot both empty); skipping"
            )
            return
        per_lot_short_loss = per_share_short_loss * lot_size

        # Gate 1: side must be net-losing (else nothing to fix here).
        if short_total_loss <= hedge_total_profit:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"side is net-profitable (short loss {short_total_loss:+.2f} "
                f"<= hedge profit {hedge_total_profit:+.2f}); skipping"
            )
            return
        # Gate 2: hedge profit must cover ≥ one lot of short loss. This
        # gate rate-limits repeat triggers: right after a successful
        # partial-reduce the new hedge has ~0 profit, so the next cycle
        # skips here. The next trigger only fires once the freshly-opened
        # (smaller) hedge has appreciated enough to fund another lot of
        # short buyback.
        #
        # That property holds ONLY because hedge_avg_buy is the reopened
        # lot's own FIFO basis. Off the broker's blended average the fresh
        # hedge shows a large phantom profit — the closed tranche's gain,
        # prorated onto the surviving quantity — and this gate waves
        # through a cascade of reductions funded by money already spent.
        if hedge_total_profit <= per_lot_short_loss:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"hedge profit ({hedge_total_profit:+.2f}) doesn't cover one lot of "
                f"short loss ({per_lot_short_loss:+.2f}); skipping"
            )
            return
        # Gate 3: per-share short loss must be positive (sanity — if it's
        # not, the side isn't actually losing and earlier gates should
        # have caught it).
        if per_share_short_loss <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"per-share short loss non-positive ({per_share_short_loss:.2f}); skipping"
            )
            return

        # q1 (shares) where partial-short-loss == hedge-profit. Round
        # DOWN to lot-aligned shares (conservative — funded fully).
        q1_raw = hedge_total_profit / per_share_short_loss
        q1_lots = int(q1_raw // lot_size)
        q1 = q1_lots * lot_size
        if q1 <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"q1 ({q1_raw:.2f}) rounds to 0 lots; skipping"
            )
            return
        if q1 >= short_open_qty:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"q1 ({q1}) >= short open qty ({short_open_qty}); "
                f"deferring to high-delta close path"
            )
            return

        # New hedge strike: UNCHANGED. The hedge is closed and reopened
        # at the same strike it already occupies — the close+reopen only
        # resizes it down to cover the residual short. (Earlier versions
        # rolled it one STRIKE_STEP nearer the short; that ratcheting is
        # no longer done per operator preference.)
        new_hedge_strike = int(hedge_leg['strike'])
        new_hedge_symbol = hedge_leg['symbol']
        # Residual short cover plus the configured surplus (0 = exactly 1:1).
        extra_qty = self._extra_hedge_qty(group.underlying)
        new_hedge_qty = short_open_qty - q1 + extra_qty

        self.logger.info(
            f"  [{group.expiry.date()}] {side} partial-reduce TRIGGERED: "
            f"|delta|={abs(short_leg['delta']):.3f}, "
            f"short loss={short_total_loss:+.2f}, hedge profit={hedge_total_profit:+.2f}, "
            f"per-lot short loss={per_lot_short_loss:+.2f}; "
            f"closing hedge {hedge_open_qty}, partial-closing short {q1}/{short_open_qty}, "
            f"reopening hedge {new_hedge_symbol}@{new_hedge_strike} qty {new_hedge_qty}"
            f" (residual {short_open_qty - q1} + surplus {extra_qty})"
        )
        self._alert(
            "PartialReduce",
            f"{side} partial-reduce ({group.expiry.date()}): "
            f"close hedge {hedge_open_qty}, partial-close short {q1}, "
            f"reopen hedge {new_hedge_symbol} qty {new_hedge_qty}"
        )

        order_ids = []
        # Context for the audit row, shared by the success and the
        # incomplete paths.
        audit = dict(
            timestamp=datetime.now(IST).isoformat(),
            user=self.user,
            underlying=group.underlying,
            expiry=group.expiry.date().isoformat(),
            side=side,
            short_strike_before=int(short_leg['strike']),
            short_strike_after=int(short_leg['strike']),  # unchanged
            short_delta_before=float(short_leg.get('delta') or 0),
            short_delta_after=None,  # next cycle will see updated delta
            hedge_strike_before=int(hedge_leg['strike']),
            hedge_strike_after=int(new_hedge_strike),
            hedge_delta_before=float(hedge_leg.get('delta') or 0),
            hedge_delta_after=None,
            # Fetch spot just for the audit-row context — partial-reduce
            # no longer needs it for any sizing/placement decision.
            nifty_spot=self._spot_price(group.underlying),
            india_vix=self._india_vix(),
        )

        # (b) Partial-close the short FIRST — it's a short, so BUY back q1.
        # Reducing the short before the wing is touched means the book is
        # never less hedged than it already is. A failure here leaves the
        # book unchanged, so it needs no incomplete report.
        result = self._place_order(symbol=short_leg['symbol'],
                                   quantity=q1,
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            return
        order_ids.append(str(result))

        # (c) Open the new hedge sized to fully cover the remaining
        # short ((short_open_qty − q1)) plus the configured surplus, at the
        # hedge's current strike.
        # Placed BEFORE (a) so the residual short is never naked: closing
        # the whole wing first left it uncovered until (c) filled, and the
        # broker rejected that SELL on margin. Since the strike is
        # unchanged, the book briefly carries old + new hedge in the same
        # symbol; (a) then sells the old quantity. FIFO retires the oldest
        # lots on that SELL, so the surviving lot is the one bought here
        # and its basis still resets.
        done = [f"BUY {q1} short {short_leg['symbol']}"]
        result = self._place_order(symbol=new_hedge_symbol,
                                   quantity=new_hedge_qty,
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            self._report_incomplete_adjustment(
                side, group, 'partial_reduce', done,
                f"BUY {new_hedge_qty} new hedge {new_hedge_symbol}",
                order_ids, audit)
            return
        order_ids.append(str(result))
        done.append(f"BUY {new_hedge_qty} new hedge {new_hedge_symbol}")

        # (a) Close the old hedge fully — it's a long, so SELL.
        result = self._place_order(symbol=hedge_leg['symbol'],
                                   quantity=hedge_open_qty,
                                   transaction_type="SELL",
                                   exchange='NFO')
        self.logger.info(result)
        if not result:
            self._report_incomplete_adjustment(
                side, group, 'partial_reduce', done,
                f"SELL {hedge_open_qty} old hedge {hedge_leg['symbol']}",
                order_ids, audit)
            return
        order_ids.append(str(result))

        # Audit row: realized P&L on the close pair is ≈ 0 by
        # construction (q1 sized to make hedge-gain ≈ partial-short-loss),
        # but compute it from the snapshot prices so any rounding shows.
        realized_pnl = (per_share_hedge_profit * hedge_open_qty
                        - per_share_short_loss * q1)
        self._record_adjustment_safely(
            **audit,
            adjustment_type='partial_reduce',
            order_ids=",".join(order_ids),
            realized_pnl=float(realized_pnl),
        )

    def _readd_reduced_side(self, side, short_leg, hedge_leg, group, fraction):
        """Scale a previously-reduced side back up as its |delta| recovers.

        After ``_partial_reduce_losing_side`` trims one side of the condor,
        that side carries fewer short lots than the opposite side. When the
        short's |delta| falls back into a safer band we add short quantity
        back in two |delta| tranches (thresholds in ``adjustment_info``),
        and square the hedge (at its current strike) to EXACTLY the new
        short quantity — buying, or selling any surplus partial-reduce left
        via ``partial_extra_hedge_lots``:

          - ``fraction='half'`` (|delta| < ``delta_readd_half``): add half
            of the current shortfall vs the opposite side.
          - ``fraction='full'`` (|delta| < ``delta_readd_full``): add the
            entire remaining shortfall, restoring 1:1 symmetry.

        The shortfall (``opposite_short_qty − this_short_qty``) is recomputed
        live each cycle and is lot-aligned by construction (every reduction /
        re-add is lot-aligned). The 'half' tranche rounds DOWN to whole lots
        (conservative); recomputing off live quantities means the two
        tranches together restore symmetry without persisting any per-tranche
        state. No realised P&L is recorded (the short add is an opening
        trade; a surplus-hedge SELL is not estimated).

        Defensive skips (log + no orders): no hedge leg to add against, no
        opposite short to size against, the side isn't actually reduced
        (shortfall <= 0), ``quantity_per_lot`` misconfigured, or the tranche
        rounds to 0 lots.
        """
        if not hedge_leg.get('symbol'):
            self.logger.info(
                f"  [{group.expiry.date()}] {side} re-add ({fraction}): "
                f"no hedge leg to add against; skipping"
            )
            return

        all_legs = group.get_legs()
        opp_key = 'short_call' if side == 'PE' else 'short_put'
        opp = all_legs.get(opp_key) or {}
        opp_qty = int(opp.get('quantity') or 0)
        current_qty = int(short_leg.get('quantity') or 0)
        if self._overhedged_block(side, group, current_qty,
                                  int(hedge_leg.get('quantity') or 0),
                                  f"Re-add ({fraction})"):
            return
        if opp_qty <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} re-add ({fraction}): "
                f"no opposite short ({opp_key}) to size against; skipping"
            )
            return

        shortfall = opp_qty - current_qty
        if shortfall <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} re-add ({fraction}): "
                f"side not reduced (this short {current_qty} >= opposite "
                f"{opp_qty}); nothing to re-add"
            )
            return

        lot_size = self._lot_size_for(group.underlying)
        if lot_size <= 0:
            self.logger.warning(
                f"  [{group.expiry.date()}] {side} re-add ({fraction}): no lot "
                f"size for {group.underlying} (registry and "
                f"stoploss_info.quantity_per_lot both empty); skipping"
            )
            return

        if fraction == 'full':
            add_qty = shortfall
        else:  # 'half' — round DOWN to whole lots (conservative)
            add_qty = int((shortfall / 2) // lot_size) * lot_size
        if add_qty <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} re-add ({fraction}): "
                f"tranche rounds to 0 lots (shortfall {shortfall}, "
                f"lot {lot_size}); skipping"
            )
            return

        # Hedge ends EXACTLY 1:1 with the short after the re-add, whatever
        # surplus partial-reduce left (``partial_extra_hedge_lots``). Sized
        # off live quantities, so the order is a BUY when the hedge is
        # short of target, a SELL of the excess when the surplus already
        # covers more than this tranche adds, and nothing when it matches.
        hedge_qty = int(hedge_leg.get('quantity') or 0)
        target_hedge = current_qty + add_qty
        hedge_delta = target_hedge - hedge_qty
        if hedge_delta > 0:
            hedge_action, hedge_txn = f"+{hedge_delta}", "BUY"
        elif hedge_delta < 0:
            hedge_action, hedge_txn = f"-{-hedge_delta}", "SELL"
        else:
            hedge_action, hedge_txn = "+0", None

        self.logger.info(
            f"  [{group.expiry.date()}] {side} re-add ({fraction}) TRIGGERED: "
            f"|delta|={abs(short_leg.get('delta') or 0):.3f}, shortfall "
            f"{shortfall} (this {current_qty} vs opposite {opp_qty}); "
            f"adding short {add_qty} @ {short_leg['strike']}, hedge "
            f"{hedge_qty} -> {target_hedge} ({hedge_action}) @ {hedge_leg['strike']}"
        )
        self._alert(
            "ReAdd",
            f"{side} re-add ({fraction}, {group.expiry.date()}): "
            f"+{add_qty} short @{short_leg['strike']} and {hedge_action} hedge "
            f"@{hedge_leg['strike']} (hedge -> {target_hedge}, 1:1)"
        )

        order_ids = []
        done = []

        # (a) Square the hedge to the post-re-add short at its current
        # strike. Placed first so the added short is never naked: after a
        # BUY or a SELL of excess the hedge is target_hedge, which already
        # covers current_qty + add_qty. A failure here changes nothing.
        if hedge_txn:
            result = self._place_order(symbol=hedge_leg['symbol'],
                                       quantity=abs(hedge_delta),
                                       transaction_type=hedge_txn,
                                       exchange='NFO')
            self.logger.info(result)
            time.sleep(2)
            if not result:
                return
            order_ids.append(str(result))
            done.append(f"{hedge_txn} {abs(hedge_delta)} hedge {hedge_leg['symbol']}")

        audit = dict(
            timestamp=datetime.now(IST).isoformat(),
            user=self.user,
            underlying=group.underlying,
            expiry=group.expiry.date().isoformat(),
            side=side,
            short_strike_before=int(short_leg['strike']),
            short_strike_after=int(short_leg['strike']),  # unchanged
            short_delta_before=float(short_leg.get('delta') or 0),
            short_delta_after=None,
            hedge_strike_before=int(hedge_leg['strike']),
            hedge_strike_after=int(hedge_leg['strike']),  # unchanged
            hedge_delta_before=(float(hedge_leg['delta'])
                                if hedge_leg.get('delta') is not None
                                else None),
            hedge_delta_after=None,
            nifty_spot=self._spot_price(group.underlying),
            india_vix=self._india_vix(),
        )

        # (b) Add short quantity — the short is a SELL, so SELL more.
        result = self._place_order(symbol=short_leg['symbol'],
                                   quantity=add_qty,
                                   transaction_type="SELL",
                                   exchange='NFO')
        self.logger.info(result)
        if not result:
            if done:
                self._report_incomplete_adjustment(
                    side, group, f'readd_{fraction}', done,
                    f"SELL {add_qty} short {short_leg['symbol']}",
                    order_ids, audit)
            return
        order_ids.append(str(result))

        self._record_adjustment_safely(
            **audit,
            adjustment_type=f'readd_{fraction}',
            order_ids=",".join(order_ids),
            # Opening trades, bar a SELL of surplus hedges — no realised
            # P&L is estimated here.
            realized_pnl=None,
        )

    def _directional_sides(self, group, action):
        """``(held_side, new_side, short_leg, hedge_leg)`` for a one-sided
        book, or None when it isn't one.

        Shared by both directional paths: the one that completes the condor
        needs ``new_side`` and the one that rolls the short inward needs
        ``held_side``, but both first have to answer the same question —
        which side is actually held.
        """
        legs = group.get_legs()
        held_put, held_call = legs['short_put'], legs['short_call']
        if held_put['symbol'] and not held_call['symbol']:
            return 'PE', 'CE', held_put, legs['long_put']
        if held_call['symbol'] and not held_put['symbol']:
            return 'CE', 'PE', held_call, legs['long_call']
        # Defensive: is_directional already guarantees one short on one side.
        # Reaching here means the classification and the leg map disagree,
        # and the honest response to that is to place nothing.
        self.logger.warning(
            f"[{group.expiry.date()}] DIRECTIONAL book is not the expected "
            f"one-sided shape (short_put={held_put['symbol']}, "
            f"short_call={held_call['symbol']}); skipping {action}."
        )
        return None

    def _directional_adjustments(self, group):
        """Per-cycle handling for a DIRECTIONAL (one-sided vertical) book.

        The trend decides WHICH action is available, so it is read once here
        and passed down rather than re-fetched per branch:

        * **non-directional** — sell the missing wing and make the book an
          iron condor (``complete_directional_to_condor``). From the next
          cycle it classifies as IRON_CONDOR and the normal delta bands take
          over.
        * **downwards** (RSI < ``rsi_non_directional_low``) — if the short
          has drifted far OTM (``|delta| <= delta_low_close``), roll it
          INWARD to ``delta_low_open``, exactly as the condor path does.
          A short that far out has stopped earning; bringing it back to a
          working delta is the same repositioning trade either way. Either
          side qualifies.
        * **upwards** (RSI > ``rsi_non_directional_high``) — the same inward
          roll, but **CE books only**. A PE-side book in a rally is left
          alone.

        Completing the condor is NOT attempted in a trending market, and the
        inward roll is not attempted in a range-bound one; each branch is
        exclusive, so at most one action fires per cycle per book.

        Worth knowing about the upward branch: a rally pushes a CE short
        TOWARD the money, so its ``|delta|`` rises rather than falls — the
        leg that goes quiet in an uptrend is the PE. The CE-only rule
        therefore fires rarely, and when it does it rolls a call short down
        toward a rising market. That is the rule as specified; widening it
        to PE, or dropping the side filter as the downward branch has, is a
        change to the dispatch below.
        """
        trend, rsi = self._trend_for(group.underlying)
        rsi_text = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "unavailable"
        self.logger.info(
            f"[{group.expiry.date()}] DIRECTIONAL: {group.underlying} "
            f"RSI={rsi_text} -> {trend or 'UNKNOWN'}"
        )
        if trend is None:
            self.logger.warning(
                f"[{group.expiry.date()}] DIRECTIONAL: trend unknown, so "
                f"neither the condor completion nor the inward roll can be "
                f"justified. Holding."
            )
            return
        if trend == NON_DIRECTIONAL:
            self.complete_directional_to_condor(group, trend=trend, rsi=rsi)
            return
        if trend == DOWNWARDS:
            self._roll_directional_short_inward(group)
            return
        if trend == UPWARDS:
            self._roll_directional_short_inward(group, only_side='CE')
            return
        self.logger.info(
            f"[{group.expiry.date()}] DIRECTIONAL: trend is {trend}; no "
            f"action is configured for this direction."
        )

    def _roll_directional_short_inward(self, group, only_side=None):
        """Roll a one-sided book's short INWARD when it has drifted far OTM.

        Same trade, same thresholds and the same ``_roll_short_leg`` the iron
        condor uses: buy the short back, re-sell at the first strike whose
        ``|delta| >= delta_low_open``, and bring the hedge with it (subject to
        ``adjust_hedges``) so the spread width is preserved. The only
        difference is what leads here — a trend reading rather than the delta
        band alone.

        ``only_side`` restricts the roll to books held on that side ('CE' or
        'PE'); None accepts either. The uptrend branch passes 'CE' and the
        downtrend branch passes nothing, which is the one asymmetry between
        them — see ``_directional_adjustments``.

        Every refusal below leaves the book untouched with no orders placed.
        """
        group_cfg = self.processor.get_group_config(group)
        if not group_cfg.get('adjustment'):
            self.logger.info(
                f"[{group.expiry.date()}] DIRECTIONAL: adjustment flag is off "
                f"for this book; not rolling the short inward."
            )
            return

        sides = self._directional_sides(group, "the inward roll")
        if not sides:
            return
        side, _new_side, short_leg, hedge_leg = sides

        if only_side and side != only_side:
            self.logger.info(
                f"[{group.expiry.date()}] DIRECTIONAL book is {side}-side; "
                f"this trend only rolls {only_side} books. No action."
            )
            return

        adjustment_info = self.config.get('adjustment_info') or {}
        delta_low_close = adjustment_info.get('delta_low_close', 5) / 100.0
        delta_low_open = adjustment_info.get('delta_low_open', 10) / 100.0
        min_distance_pct = float(adjustment_info.get('minimum_distance_pct', 1.7))
        adjust_hedges = bool(group_cfg.get('adjust_hedges'))

        if short_leg.get('delta') is None:
            self.logger.info(
                f"[{group.expiry.date()}] DIRECTIONAL: delta unknown for "
                f"{short_leg['symbol']} (strike outside the recorder band); "
                f"cannot judge the inward roll."
            )
            return

        abs_delta = abs(short_leg['delta'])
        if abs_delta > delta_low_close:
            self.logger.info(
                f"[{group.expiry.date()}] DIRECTIONAL {side} short "
                f"{short_leg['symbol']} |delta|={abs_delta:.3f} > "
                f"{delta_low_close:.2f} — still working, no inward roll."
            )
            return

        # min_distance stops the walk bringing the short too close to spot.
        # Only the inward direction honours it (see _strike_at_delta), which
        # is exactly the direction taken here.
        spot_price = self._spot_price(group.underlying) if min_distance_pct > 0 else None
        if min_distance_pct > 0 and spot_price is None:
            self.logger.warning(
                f"[{group.expiry.date()}] DIRECTIONAL: spot fetch failed; "
                f"min_distance checks will be skipped this cycle"
            )
            min_distance = 0
        elif spot_price is not None:
            min_distance = int(round(spot_price * min_distance_pct / 100))
        else:
            min_distance = 0

        self.logger.info(
            f"[{group.expiry.date()}] DIRECTIONAL {side} short "
            f"{short_leg['symbol']} |delta|={abs_delta:.3f} <= "
            f"{delta_low_close:.2f} in a downward trend — rolling INWARD to "
            f"|delta| >= {delta_low_open:.2f}"
        )
        try:
            self._roll_short_leg(side, short_leg, hedge_leg,
                                 delta_low_open, 'inward',
                                 group.strategy, adjust_hedges,
                                 spot_price=spot_price,
                                 min_distance=min_distance,
                                 group=group)
        except Exception as e:
            self.logger.exception(f"Failed to roll {side} short: {e}")
            self._alert(
                "AdjustmentFailed",
                f"Directional roll {side} failed for {short_leg['symbol']} "
                f"({group.expiry.date()}): {e}. Position may be partially "
                f"adjusted — verify manually."
            )

    def complete_directional_to_condor(self, group, trend=None, rsi=None):
        """Sell the MISSING wing of a DIRECTIONAL book, making it an iron
        condor — but only while the market is non-directional.

        A directional group holds one short and its hedge on a single side
        (e.g. short 23500PE + long 23000PE). This adds the mirror pair on the
        other side, picking the new short at ``delta_high_open`` by the same
        delta walk the outward roll uses, and its hedge at the DTE-derived
        wing distance — so the new side is built to the same rules as every
        other leg the engine places.

        Gated on a non-directional trend because that is precisely the bet:
        an iron condor profits when spot goes nowhere. Selling the upside
        wing into an uptrend is selling the side the market is moving toward.

        Refuses, placing NO orders, when any of these hold:

        * the group's ``adjustment`` flag is off;
        * the book isn't the expected one-sided shape;
        * the trend is directional, or unknown;
        * spot is unavailable (there is no ATM to walk out from);
        * the delta walk finds no strike at the target;
        * no usable hedge is within reach of the wing distance.

        The hedge is BOUGHT BEFORE the short is SOLD, which is the opposite
        order to ``_roll_short_leg``. That function buys back an existing
        short first because it is CLOSING a hedged spread; this one is
        OPENING one. Selling the short first would leave a naked short
        between the two fills — briefly unhedged, and at a margin
        requirement the account may not meet, which is how the second order
        gets rejected and the book is left holding the naked leg.
        """
        group_cfg = self.processor.get_group_config(group)
        if not group_cfg.get('adjustment'):
            self.logger.info(
                f"[{group.expiry.date()}] DIRECTIONAL: adjustment flag is off "
                f"for this book; not completing the condor."
            )
            return

        sides = self._directional_sides(group, "completing the condor")
        if not sides:
            return
        held_side, new_side, held_short, held_hedge = sides

        quantity = int(held_short['quantity'] or 0)
        if quantity <= 0:
            self.logger.warning(
                f"[{group.expiry.date()}] DIRECTIONAL {held_side} short has "
                f"quantity {quantity}; nothing to mirror."
            )
            return

        adjustment_info = self.config.get('adjustment_info') or {}
        delta_high_open = adjustment_info.get('delta_high_open', 20) / 100.0

        self.logger.info(
            f"[{group.expiry.date()}] DIRECTIONAL {held_side} book "
            f"({held_short['symbol']} / {held_hedge.get('symbol')}) — "
            f"considering the {new_side} wing at |delta| <= "
            f"{delta_high_open:.2f} to complete the condor"
        )

        if not self._is_non_directional(
                group.underlying,
                f"completing the {group.expiry.date()} condor with a "
                f"{new_side} wing",
                trend=trend, rsi=rsi):
            return

        # The new side has no existing leg to walk from, so the walk starts
        # at ATM and moves out. Needs spot; without it there is no anchor and
        # guessing one would put the whole wing in the wrong place.
        spot_price = self._spot_price(group.underlying)
        if spot_price is None:
            self.logger.error(
                f"[{group.expiry.date()}] Cannot complete condor: spot for "
                f"{group.underlying} is unavailable, so the {new_side} walk "
                f"has no ATM to start from. No orders placed."
            )
            return
        atm_strike = ul.atm_strike(group.underlying, spot_price)

        # Build the ATM symbol on the NEW side by repricing the held leg's
        # strike and swapping its option type. Kite option tradingsymbols
        # always end in CE/PE — the same property split_symbol parses on —
        # so the last two characters are the type and nothing else.
        atm_same_side = reprice_option_symbol(
            held_short['symbol'], held_short['strike'], atm_strike)
        atm_symbol = atm_same_side[:-2] + new_side

        found, walk_reason = self._strike_at_delta(
            current_symbol=atm_symbol,
            current_strike=atm_strike,
            opt_type=new_side,
            target_abs_delta=delta_high_open,
            direction='outward',
        )
        if not found:
            msg = (f"No {new_side} strike found to complete the "
                   f"{group.expiry.date()} condor (wanted |delta| <= "
                   f"{delta_high_open:.2f} with OI >= {self._min_oi()} or "
                   f"volume >= {self._min_volume()}, walking out from ATM "
                   f"{atm_strike}). {walk_reason}. No orders placed")
            self.logger.error(msg)
            self._alert("AdjustmentSkipped", msg)
            return

        new_short_symbol, new_short_strike, new_short_quote = found
        new_short_ltp = (new_short_quote or {}).get('last_price')

        dte = (group.expiry.date() - datetime.now(IST).date()).days
        hedge_distance = self._hedge_distance_for(group.underlying, dte)
        proposed_hedge_strike = (new_short_strike - hedge_distance
                                 if new_side == 'PE'
                                 else new_short_strike + hedge_distance)
        self.logger.info(
            f"  {new_side} hedge distance {hedge_distance}pt "
            f"({group.underlying}, DTE={dte}): new short {new_short_strike} "
            f"-> hedge {proposed_hedge_strike}"
        )
        proposed_hedge_symbol = reprice_option_symbol(
            new_short_symbol, new_short_strike, proposed_hedge_strike)
        picked = self._select_hedge_strike(
            side=new_side,
            direction='outward',
            proposed_symbol=proposed_hedge_symbol,
            proposed_strike=proposed_hedge_strike,
            new_short_strike=new_short_strike,
            short_ltp=new_short_ltp,
        )
        if not picked:
            msg = (f"No usable {new_side} hedge within "
                   f"{self._hedge_search_steps()} strike(s) of "
                   f"{proposed_hedge_symbol}@{proposed_hedge_strike}; not "
                   f"completing the {group.expiry.date()} condor — no orders "
                   f"placed. The book stays one-sided rather than holding a "
                   f"naked {new_side} short.")
            self.logger.error(msg)
            self._alert("AdjustmentSkipped", msg)
            return
        new_hedge_symbol, new_hedge_strike, _ = picked

        self.logger.info(
            f"Completing condor on {group.expiry.date()}: adding "
            f"{new_side} short {new_short_symbol}@{new_short_strike} + hedge "
            f"{new_hedge_symbol}@{new_hedge_strike}, qty {quantity} "
            f"(mirroring the {held_side} side)"
        )
        self._alert(
            "Adjustment",
            f"Completing condor: adding {new_side} {new_short_symbol} + "
            f"{new_hedge_symbol} to the {held_side}-only "
            f"{group.expiry.date()} book"
        )

        order_ids = []

        # 1. Hedge FIRST — see the docstring. Nothing is naked at any point.
        result = self._place_order(symbol=new_hedge_symbol,
                                   quantity=quantity,
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            self.logger.error(
                f"Hedge BUY for {new_hedge_symbol} did not go through; "
                f"abandoning the condor completion. Book is unchanged."
            )
            return
        order_ids.append(str(result))

        # 2. Then the short it protects.
        result = self._place_order(symbol=new_short_symbol,
                                   quantity=quantity,
                                   transaction_type="SELL",
                                   exchange='NFO')
        self.logger.info(result)
        if not result:
            # The hedge filled and the short did not. That is a long option
            # with nothing sold against it — a bounded loss (the premium
            # paid), not a risk, but it is NOT the position anyone intended
            # and it will not be cleaned up automatically: next cycle the
            # book has two longs and one short and no longer classifies as
            # DIRECTIONAL. Say so loudly.
            msg = (f"Condor completion half-done on {group.expiry.date()}: "
                   f"hedge {new_hedge_symbol} was BOUGHT but the "
                   f"{new_short_symbol} SELL failed. The book now holds an "
                   f"unpaired long {new_side}. Close it or place the short "
                   f"manually.")
            self.logger.error(msg)
            self._alert("AdjustmentFailed", msg)
            return
        order_ids.append(str(result))

        self._record_adjustment_safely(
            timestamp=datetime.now(IST).isoformat(),
            user=self.user,
            underlying=group.underlying,
            expiry=group.expiry.date().isoformat(),
            side=new_side,
            adjustment_type='complete_condor',
            # No "before" strikes: this side did not exist until now.
            short_strike_before=None,
            short_strike_after=int(new_short_strike),
            short_delta_before=None,
            short_delta_after=None,
            hedge_strike_before=None,
            hedge_strike_after=int(new_hedge_strike),
            hedge_delta_before=None,
            hedge_delta_after=None,
            nifty_spot=float(spot_price),
            india_vix=self._india_vix(),
            order_ids=",".join(order_ids),
            # Opening legs only — nothing was closed, so nothing is realised.
            realized_pnl=None,
        )

    def adjustments(self, group):
        """Per-group delta-driven adjustments.

        Triggers, all in percent via ``adjustment_info`` in
        ``Monitor/config.yaml``:

        - **Low-delta (too far OTM):** |delta| <= ``delta_low_close``
          rolls the short INWARD to a strike with |delta| >=
          ``delta_low_open``. Hedge follows by the same strike distance
          when ``adjust_hedges`` is set.
        - **Mid-high (partial-reduce window):** ``delta_partial_close``
          <= |delta| < ``delta_high_close``. Fires the partial-reduce
          path (see ``_partial_reduce_losing_side``) iff the side is
          net-losing AND the hedge has enough profit to cover at least
          one lot of partial short close. Otherwise the leg is left
          alone for this cycle. Skipped entirely — band logged, no
          orders — when ``partial_close_enabled`` is false, which is
          the switch for cutting the brokerage this path generates.
        - **High-delta (close, then maybe reopen wide):** |delta| >=
          ``delta_high_close`` always closes the short and its hedge. Only
          on a non-directional trend reading is the side then re-sold
          OUTWARD at |delta| <= ``delta_high_open`` with a fresh hedge;
          otherwise it is left flat.
        - **Recovery (re-add):** for a side previously trimmed by
          partial-reduce, |delta| < ``delta_readd_half`` adds half the
          current short shortfall vs the opposite side, and |delta| <
          ``delta_readd_full`` (deeper) adds the entire remaining
          shortfall — each with an equal quantity of hedges (see
          ``_readd_reduced_side``). A no-op on a side that was never
          reduced.

        Legs whose |delta| sits inside the band ``[delta_readd_half,
        delta_partial_close)`` are left alone. The ``adjustment`` master
        switch and ``adjust_hedges`` are read off the group's per-week
        config block — so one week can be in active adjustment mode
        while another is frozen.

        DIRECTIONAL books take a different route entirely — see
        ``_directional_adjustments``. The bands below assume a two-sided
        book: they iterate PE and CE, and the partial-reduce and re-add
        paths size against the OPPOSITE side's short, which a one-sided
        book does not have.
        """
        if group.strategy == DIRECTIONAL:
            self._directional_adjustments(group)
            return

        group_cfg = self.processor.get_group_config(group)
        if not group_cfg.get('adjustment'):
            return

        adjustment_info = self.config.get('adjustment_info') or {}
        delta_low_close = adjustment_info.get('delta_low_close', 5) / 100.0
        delta_low_open = adjustment_info.get('delta_low_open', 10) / 100.0
        delta_partial_close = adjustment_info.get('delta_partial_close', 20) / 100.0
        delta_high_close = adjustment_info.get('delta_high_close', 25) / 100.0
        delta_high_open = adjustment_info.get('delta_high_open', 20) / 100.0
        delta_readd_half = adjustment_info.get('delta_readd_half', 19) / 100.0
        delta_readd_full = adjustment_info.get('delta_readd_full', 16) / 100.0
        # Off switch for the transaction-heavy partial-reduce path. Absent
        # key means enabled, matching the behaviour before the flag existed.
        partial_close_enabled = bool(adjustment_info.get('partial_close_enabled', True))
        min_distance_pct = float(adjustment_info.get('minimum_distance_pct', 1.7))
        adjust_hedges = bool(group_cfg.get('adjust_hedges'))
        legs = group.get_legs()

        # min_distance is now enforced PER SIDE inside the inward roll
        # walk and partial-reduce hedge placement — not as a global gate.
        # Outward rolls move legs AWAY from spot, so the rule is silent
        # there. Fetch spot once at the top so both branches share it,
        # then derive the absolute boundary as a percent of spot so it
        # scales with NIFTY's level.
        spot_price = self._spot_price(group.underlying) if min_distance_pct > 0 else None
        if min_distance_pct > 0 and spot_price is None:
            self.logger.warning(
                f"[{group.expiry.date()}] Adjustments: spot fetch failed; "
                f"min_distance checks will be skipped this cycle"
            )
            min_distance = 0
        elif spot_price is not None:
            min_distance = int(round(spot_price * min_distance_pct / 100))
            self.logger.info(
                f"[{group.expiry.date()}] Adjustments: min_distance = "
                f"{min_distance} pts ({min_distance_pct}% of spot {spot_price})"
            )
        else:
            min_distance = 0

        for side, short_key, hedge_key in (('PE', 'short_put',  'long_put'),
                                           ('CE', 'short_call', 'long_call')):
            short_leg = legs[short_key]
            hedge_leg = legs[hedge_key]

            if not short_leg['symbol']:
                continue
            if short_leg.get('delta') is None:
                self.logger.info(
                    f"Skipping {side} adjustment: delta unknown for "
                    f"{short_leg['symbol']} (strike outside recorder band)"
                )
                continue

            abs_delta = abs(short_leg['delta'])

            if abs_delta <= delta_low_close:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"<= {delta_low_close:.2f} — rolling INWARD to |delta| >= {delta_low_open:.2f}"
                )
                try:
                    self._roll_short_leg(side, short_leg, hedge_leg,
                                         delta_low_open, 'inward',
                                         group.strategy, adjust_hedges,
                                         spot_price=spot_price,
                                         min_distance=min_distance,
                                         group=group)
                except Exception as e:
                    self.logger.exception(f"Failed to roll {side} short: {e}")
                    self._alert(
                        "AdjustmentFailed",
                        f"Roll {side} failed for {short_leg['symbol']} "
                        f"({group.expiry.date()}): {e}. Position may be "
                        f"partially adjusted — verify manually."
                    )
            elif abs_delta >= delta_high_close:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f">= {delta_high_close:.2f} — closing short + hedge, then "
                    f"re-opening at |delta| <= {delta_high_open:.2f} if non-directional"
                )
                # Step 1 — close the losing side. NOT trend-gated: a short
                # this deep is cut whatever the market is doing. An earlier
                # version gated the whole roll, so in a trend the short sat
                # above delta_high_close with no ceiling at all.
                try:
                    closed = self._close_short_and_hedge(
                        side, short_leg, hedge_leg, group,
                        spot_price=spot_price)
                except Exception as e:
                    self.logger.exception(f"Failed to close {side} short: {e}")
                    self._alert(
                        "AdjustmentFailed",
                        f"Close {side} failed for {short_leg['symbol']} "
                        f"({group.expiry.date()}): {e}. Position may be "
                        f"partially closed — verify manually."
                    )
                    continue
                if not closed:
                    continue
                # Step 2 — re-sell further out, trend-gated. Re-selling is a
                # bet that spot stalls, and a second loss on the same side if
                # the move continues, so it runs only on a non-directional
                # reading. Refusing leaves the side flat; the book then
                # classifies as DIRECTIONAL and _directional_adjustments
                # takes it from the next cycle.
                if not self._is_non_directional(
                        group.underlying,
                        f"re-opening the {side} side for {group.expiry.date()}"):
                    self.logger.info(
                        f"[{group.expiry.date()}] {side} side closed and "
                        f"left flat — not re-opened in a directional market."
                    )
                    continue
                try:
                    self._roll_short_leg(side, short_leg, hedge_leg,
                                         delta_high_open, 'outward',
                                         group.strategy, adjust_hedges,
                                         group=group, legs_closed=True)
                except Exception as e:
                    self.logger.exception(f"Failed to re-open {side} side: {e}")
                    self._alert(
                        "AdjustmentFailed",
                        f"Re-open {side} failed after closing "
                        f"{short_leg['symbol']} ({group.expiry.date()}): {e}. "
                        f"Side may be flat or part-opened — verify manually."
                    )
            elif abs_delta >= delta_partial_close:
                if not partial_close_enabled:
                    # Stay in this band doing nothing: the close at
                    # delta_high_close still backstops the tail.
                    self.logger.info(
                        f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                        f"in [{delta_partial_close:.2f}, {delta_high_close:.2f}) — partial-reduce "
                        f"DISABLED (adjustment_info.partial_close_enabled=false) — no action"
                    )
                    continue
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"in [{delta_partial_close:.2f}, {delta_high_close:.2f}) — "
                    f"attempting partial-reduce (funding-conditional)"
                )
                try:
                    self._partial_reduce_losing_side(side, short_leg, hedge_leg, group)
                except Exception as e:
                    self.logger.exception(
                        f"Partial-reduce failed for {side}: {e}"
                    )
                    self._alert(
                        "AdjustmentFailed",
                        f"{side} partial-reduce failed for {group.expiry.date()}: {e}. "
                        f"Position may be partially adjusted — verify manually."
                    )
            elif abs_delta < delta_readd_full or abs_delta < delta_readd_half:
                # Recovery band: the short has drifted back OTM into a
                # safer |delta|. If this side was previously trimmed by
                # partial-reduce, scale it back up (short + matching
                # hedge). ``full`` (deepest, |delta| < delta_readd_full)
                # restores symmetry outright; ``half`` (|delta| <
                # delta_readd_half) adds half the current shortfall.
                # Order matters: test the deeper threshold first.
                fraction = 'full' if abs_delta < delta_readd_full else 'half'
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"< {(delta_readd_full if fraction == 'full' else delta_readd_half):.2f} — "
                    f"attempting {fraction} re-add (only if previously reduced)"
                )
                try:
                    self._readd_reduced_side(side, short_leg, hedge_leg,
                                             group, fraction)
                except Exception as e:
                    self.logger.exception(
                        f"Re-add ({fraction}) failed for {side}: {e}"
                    )
                    self._alert(
                        "AdjustmentFailed",
                        f"{side} {fraction} re-add failed for {group.expiry.date()}: {e}. "
                        f"Position may be partially adjusted — verify manually."
                    )
            else:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"within band [{delta_readd_half:.2f}, {delta_partial_close:.2f}) — no roll"
                )
