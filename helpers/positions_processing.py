"""Position-fetching, strategy-detection, and PnL computation.

These three steps were previously methods on ``handle_options`` in
``Monitor/options.py``. They are now factored out into ``PositionProcessor``
so the same logic can be reused from anywhere (backtesting, dashboards,
ad-hoc scripts) without dragging in the rest of the trading orchestrator.

The processor groups positions by ``(underlying, expiry)`` into
``StrategyGroup`` objects so the risk engine can run stop-loss /
trail-profit / adjustments on each open iron condor or strangle
independently — earlier the engine only saw the nearest-expiry book and
ignored everything else.

Legacy single-expiry attributes (``strategy``, ``underlying``, ``expiry``)
are still populated from the nearest group so dashboard / external
callers don't break.
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import pytz

from helpers.basic import api_get, ApiError, split_symbol, put_config
from MarketAnalysis.db_queries import persist_trades

IST = pytz.timezone('Asia/Kolkata')

# Config block names for the per-weekly-expiry groups. Index N corresponds
# to the N-th nearest weekly expiry observed in the cycle (chronological
# order across both open groups and closed-only expiries). A 4th or later
# expiry has no block — its closes are logged but not banked.
#
# Each block is a nested dict in ``Monitor/config.yaml`` of shape::
#
#     first_week:
#       pnl: 0
#       adjust_hedges: true
#       adjustment: true
#       close_on_stoploss: false
#       close_on_trailprofit: false
#
# The risk engine reads the flags off the block matching the group it's
# currently processing; the banker writes back into ``pnl``.
WEEK_GROUPS = ('first_week', 'second_week', 'third_week',
               'fourth_week', 'fifth_week')

# Default per-group flags applied when a block is missing in config (e.g.
# a fresh deployment that hasn't been edited yet). Same conservative
# defaults the project shipped with at the top level.
_DEFAULT_GROUP_CFG = {
    'pnl': 0,
    'adjust_hedges': True,
    'adjustment': True,
    'close_on_stoploss': False,
    'close_on_trailprofit': False,
}


_LEG_KEY_MAP = {
    ('PE', 'buy'):  'long_put',
    ('PE', 'sell'): 'short_put',
    ('CE', 'sell'): 'short_call',
    ('CE', 'buy'):  'long_call',
}


def _empty_legs():
    return {
        'long_put':   {'strike': None, 'symbol': None, 'quantity': None, 'last_price': None},
        'short_put':  {'strike': None, 'symbol': None, 'quantity': None, 'last_price': None},
        'short_call': {'strike': None, 'symbol': None, 'quantity': None, 'last_price': None},
        'long_call':  {'strike': None, 'symbol': None, 'quantity': None, 'last_price': None},
    }


@dataclass
class StrategyGroup:
    """All positions for a single ``(underlying, expiry)`` book.

    Built by ``PositionProcessor.process_positions`` and tagged with a
    detected ``strategy`` by ``analyze_positions``. The risk engine
    iterates these per cycle, so a near-weekly iron condor and a
    next-weekly iron condor each get their own stop-loss / trail-profit
    / adjustment decisions — earlier only the nearest expiry was watched.
    """
    underlying: str
    expiry: object  # datetime from split_symbol
    positions: list = field(default_factory=list)
    strategy: Optional[str] = None
    quantity: int = 0

    def get_legs(self):
        """Return the four option legs (long/short × PE/CE) for this group.

        Missing legs have ``None`` for every field. Populated legs include
        ``strike``, ``symbol``, open ``quantity`` (positive), ``last_price``,
        and ``delta`` (which may itself be ``None`` if the strike is
        outside the recorder's delta band).
        """
        legs = _empty_legs()
        for p in self.positions:
            key = _LEG_KEY_MAP.get((p.get('option_type'), p.get('transtype')))
            if not key:
                continue
            if p['transtype'] == 'buy':
                qty = p['buy_quantity'] - p['sell_quantity']
            else:
                qty = p['sell_quantity'] - p['buy_quantity']
            legs[key] = {
                'strike': p['strike'],
                'symbol': p['tradingsymbol'],
                'quantity': qty,
                'last_price': p.get('last_price'),
                # ``delta`` is populated by ``process_positions`` from the
                # DB snapshot when available. ``None`` means the strike is
                # outside the recorder's delta band (or no DB row at all);
                # downstream callers must treat None as "delta unknown".
                'delta': p.get('delta'),
            }
        return legs


class PositionProcessor:
    """Fetch and process broker positions, detect strategy, and compute PnL.

    Construction takes the user identity, broker base URL, the loaded config
    dict, a logger, the config-file path (for persisting weekly PnL), an
    optional alert callback for surface-able failures, and an optional list
    of symbols to skip.
    """

    def __init__(self, user, api_url, config, logger, config_file,
                 alert_fn=None, options_to_avoid=None):
        self.user = user
        self.api_url = api_url
        self.config = config
        self.logger = logger
        self.config_file = config_file
        self._alert = alert_fn or (lambda heading, message: None)
        self.options_to_avoid = options_to_avoid or []

        self.positions = []
        self.closed_positions = []
        self.quantity = 0
        # Legacy single-expiry attributes: populated from the nearest group
        # after analyze_positions(), so dashboard / external callers don't
        # break. Internal callers should iterate ``self.groups`` instead.
        self.strategy = None
        self.underlying = None
        self.expiry = None
        # All (underlying, expiry) books open right now, sorted by expiry
        # ascending — groups[0] is the nearest. Built by process_positions.
        self.groups = []
        # Per-expiry set of symbols whose realized P&L has already been
        # banked into the corresponding week_pnl bucket — prevents the same
        # closed leg from being double-counted on subsequent cycles while
        # the broker still reports it in closed_positions.
        self._banked_symbols_by_expiry = {}

    @property
    def nearest_positions(self):
        """Positions belonging to the nearest expiry.

        Returns the nearest group's positions when ``process_positions`` has
        populated groups; falls back to ``self.positions`` for legacy
        callers / test fixtures that build a PositionProcessor manually.
        """
        if self.groups:
            return self.groups[0].positions
        nearest = [p for p in self.positions if p.get('is_nearest_expiry')]
        return nearest if nearest else self.positions

    def get_legs(self):
        """Backward-compat: return the nearest group's four legs.

        New code that handles multiple expiries should call
        ``group.get_legs()`` directly on each ``StrategyGroup``. This
        method exists so existing dashboard / external readers continue
        to see the nearest-expiry view.
        """
        if self.groups:
            return self.groups[0].get_legs()
        # Test path: no process_positions() ran; build a transient group
        # over whatever was assigned to ``self.positions``.
        return StrategyGroup(
            underlying='', expiry=None, positions=self.positions
        ).get_legs()

    def fetch_and_persist_trades(self):
        """Pull today's executed fills via ``/get_trades`` and append
        any new ones to ``kite_trades``.

        Called once per orchestrator cycle from ``Monitor/options.py:run()``
        so the local trade ledger lags the broker by at most one
        ``monitor_info.delay`` interval (default 30s). The daily 15:35
        recorder hook still runs as a redundant safety net, but no
        longer the sole source — useful when ``record_snapshots.py``
        is down or restarting.

        Idempotent: ``persist_trades`` uses ``INSERT OR IGNORE`` keyed
        on Kite's ``trade_id``, so multiple per-cycle fetches plus the
        15:35 fetch all converge without duplicating rows.

        Failures are logged but never raise — losing one cycle's worth
        of ledger updates is preferable to crashing the trading loop.
        Returns the count of newly-inserted rows (0 on no-op / failure).
        """
        url = f"{self.api_url}/get_trades?user={self.user}"
        try:
            success, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"/get_trades fetch failed: {e}")
            return 0
        if not success:
            self.logger.warning(f"Broker reported failure for /get_trades: {payload}")
            return 0
        user_trades = (payload or {}).get(self.user) or []
        if not user_trades:
            return 0
        try:
            inserted = persist_trades(user_trades, self.user)
        except Exception as e:
            self.logger.exception(f"Failed to persist trades into kite_trades: {e}")
            return 0
        if inserted:
            self.logger.info(
                f"kite_trades: persisted {inserted} new fill(s) "
                f"(out of {len(user_trades)} returned by /get_trades)"
            )
        return inserted

    def process_positions(self):
        url = f"{self.api_url}/get_positions?user={self.user}"
        self.positions = []
        self.closed_positions = []
        self.groups = []
        try:
            success, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.error(f"Failed to fetch positions: {e}")
            self._alert("ApiError", f"Failed to fetch positions: {e}")
            return False
        if not success:
            self.logger.info(f"Broker reported failure fetching positions: {payload}")
            return False

        positions_list = payload[self.user]
        for position in positions_list:
            if position['tradingsymbol'] in self.options_to_avoid:
                continue
            position_to_save = position
            position_to_save['transtype'] = 'buy' if position['buy_quantity'] > position['sell_quantity'] else 'sell'

            # Parse the symbol into (underlying, expiry, strike, option_type).
            # Anything we can't parse (equity holdings, brand-new symbol
            # formats, malformed strings) is skipped rather than failing the
            # whole cycle — banking and risk decisions both rely on those
            # four fields, so an unparseable row has no useful place to go.
            symbol = position_to_save.get('tradingsymbol')
            if not symbol:
                self.logger.warning("Skipping position with empty tradingsymbol")
                continue
            try:
                parsed = split_symbol(symbol)
            except Exception as e:
                self.logger.warning(
                    f"Skipping {symbol}: split_symbol raised {type(e).__name__}: {e}"
                )
                continue
            if not parsed or len(parsed) != 4:
                self.logger.warning(
                    f"Skipping {symbol}: split_symbol returned {parsed!r}"
                )
                continue
            (position_to_save['underlying'], position_to_save['expiry'],
             position_to_save['strike'], position_to_save['option_type']) = parsed

            if position['quantity'] == 0:
                self.closed_positions.append(position_to_save)
            else:
                price_url = f"{self.api_url}/get_current_price/{position_to_save['tradingsymbol']}/option"
                try:
                    ok, price_payload = api_get(price_url, logger=self.logger)
                except ApiError as e:
                    self.logger.error(f"Failed to get price for {position_to_save['tradingsymbol']}: {e}")
                    self._alert("ApiError",
                                f"Failed to get price for {position_to_save['tradingsymbol']}: {e}")
                    return False
                if ok:
                    quote = price_payload[f"NFO:{position_to_save['tradingsymbol']}"]
                    position_to_save['last_price'] = quote['last_price']
                    # ``delta`` is populated when the quote came from the DB
                    # snapshot recorder (option_snapshot.delta). It will be
                    # None for KITE-sourced quotes or for strikes outside the
                    # recorded delta band — callers should treat None as
                    # "delta unknown for this strike right now".
                    position_to_save['delta'] = quote.get('delta')
                else:
                    self.logger.error(f"Broker reported failure for {position_to_save['tradingsymbol']}: {price_payload}")
                    self._alert("PriceFetchFailed",
                                f"Broker returned failure for {position_to_save['tradingsymbol']}; aborting cycle")
                    return False

                self.quantity = max(self.quantity, abs(position_to_save['quantity'])) if abs(position_to_save['quantity']) > 0 else self.quantity

                self.positions.append(position_to_save)

        if len(self.positions) > 0:
            # Tag each position with whether it belongs to the nearest expiry —
            # kept for legacy readers and the ``nearest_positions`` fallback.
            nearest_expiry = min(p['expiry'] for p in self.positions)
            for p in self.positions:
                p['is_nearest_expiry'] = (p['expiry'] == nearest_expiry)

            # Group by (underlying, expiry) and sort by expiry ascending so
            # ``groups[0]`` is the nearest book — preserves the historical
            # "nearest expiry" semantics where it's still meaningful.
            grouped = {}
            for p in self.positions:
                grouped.setdefault((p['underlying'], p['expiry']), []).append(p)
            self.groups = []
            for (underlying, expiry), group_positions in sorted(
                    grouped.items(), key=lambda kv: kv[0][1]):
                qty = max((abs(p['quantity']) for p in group_positions), default=0)
                self.groups.append(StrategyGroup(
                    underlying=underlying,
                    expiry=expiry,
                    positions=group_positions,
                    quantity=qty,
                ))

            df = pd.DataFrame(self.positions)
            #df.to_csv(f"positions_{datetime.now(IST).date()}.csv", header=True, mode='w',
            #          quoting=csv.QUOTE_NONNUMERIC, index=False)
            self.logger.info(f"\n{df[['tradingsymbol', 'quantity', 'average_price', 'last_price', 'delta']]}")
            return True
        else:
            self.logger.info("No position found.")
            return False

    def analyze_positions(self):
        """Detect the strategy of every open (underlying, expiry) book.

        Updates ``group.strategy`` for each entry in ``self.groups`` and
        mirrors the nearest group's strategy / underlying / expiry into the
        legacy single-expiry attributes so the dashboard and other readers
        keep working.
        """
        for group in self.groups:
            self._analyze_group(group)

        # Pick the first tradable group (or the nearest one if none classified)
        # as the "primary" for the legacy single-expiry attributes.
        primary = next(
            (g for g in self.groups if g.strategy in ('IRON_CONDOR', 'SHORT_STRANGLE')),
            self.groups[0] if self.groups else None,
        )
        if primary is not None:
            self.strategy = primary.strategy
            self.underlying = primary.underlying
            self.expiry = primary.expiry
        else:
            self.strategy = self.underlying = self.expiry = None

        if self.groups:
            self.logger.info(
                "Strategies identified: " +
                ", ".join(f"{g.expiry.date() if hasattr(g.expiry, 'date') else g.expiry}={g.strategy}"
                          for g in self.groups)
            )
        else:
            self.logger.info("Strategy identified None.")

    def _analyze_group(self, group):
        """Detect IRON_CONDOR / SHORT_STRANGLE / LONG_STRANGLE within one
        (underlying, expiry) book. All positions in the group share an
        underlying and expiry, so the strike-ordering check is enough.
        """
        call_long_strike = call_short_strike = put_short_strike = put_long_strike = 0
        long_strangle_found = short_strangle_found = False
        for position in group.positions:
            if position['transtype'] == "buy" and position['option_type'] == "PE":
                put_long_strike = position['strike']
                if call_long_strike > 0 and put_long_strike < call_long_strike:
                    long_strangle_found = True
            elif position['transtype'] == "sell" and position['option_type'] == "PE":
                put_short_strike = position['strike']
                if call_short_strike > 0 and put_short_strike < call_short_strike:
                    short_strangle_found = True
            elif position['transtype'] == "sell" and position['option_type'] == "CE":
                call_short_strike = position['strike']
                if 0 < put_short_strike < call_short_strike:
                    short_strangle_found = True
            elif position['transtype'] == "buy" and position['option_type'] == "CE":
                call_long_strike = position['strike']
                if 0 < put_long_strike < call_long_strike:
                    long_strangle_found = True

        if long_strangle_found and short_strangle_found:
            group.strategy = "IRON_CONDOR"
        elif long_strangle_found:
            group.strategy = "LONG_STRANGLE"
        elif short_strangle_found:
            group.strategy = "SHORT_STRANGLE"

    def _ordered_expiry_keys(self):
        """All ``(underlying, expiry)`` pairs seen this cycle — across both
        open groups and closed-only books — sorted by expiry ascending.

        Including closed-only expiries matters when stop-loss / trail-profit
        / manual close has just closed every leg of a book: the next cycle
        has no open group for that expiry, but its closed legs are still
        in ``self.closed_positions`` waiting to be banked.
        """
        seen = set()
        for g in self.groups:
            seen.add((g.underlying, g.expiry))
        for p in self.closed_positions:
            if p.get('underlying') and p.get('expiry'):
                seen.add((p['underlying'], p['expiry']))
        return sorted(seen, key=lambda k: k[1])

    def _group_block_name(self, group):
        """Config block name assigned to ``group`` based on its chronological
        position among observed expiries. One of ``WEEK_GROUPS`` (e.g.
        ``'first_week'``), or ``None`` for groups past the last entry —
        those expiries don't get a block.
        """
        ordered = self._ordered_expiry_keys()
        for idx, (underlying, expiry) in enumerate(ordered):
            if underlying == group.underlying and expiry == group.expiry:
                return WEEK_GROUPS[idx] if idx < len(WEEK_GROUPS) else None
        return None

    def get_group_config(self, group):
        """Return the per-group config block for ``group`` — a dict with
        ``pnl``, ``adjust_hedges``, ``adjustment``, ``close_on_stoploss``,
        ``close_on_trailprofit``. Falls back to the conservative defaults
        in ``_DEFAULT_GROUP_CFG`` when the block is missing (so a fresh
        deployment doesn't crash before the user has edited config).

        Risk decisions read flags off this dict instead of off the top-level
        config — every (underlying, expiry) book has its own per-group
        toggles.
        """
        block_name = self._group_block_name(group)
        if not block_name:
            return dict(_DEFAULT_GROUP_CFG)
        block = self.config.get(block_name) or {}
        return {**_DEFAULT_GROUP_CFG, **block}

    def bank_closed_positions(self):
        """Add the P&L of newly-closed positions to their week group's
        ``pnl`` bucket and persist the blocks to ``config.yaml``.

        Banking uses an in-memory set of already-banked symbols per expiry
        to avoid double-counting when the broker keeps reporting today's
        closes in subsequent cycles. Expiries past ``WEEK_GROUPS[-1]`` are
        logged but not banked — no block exists for them.
        """
        if not self.closed_positions:
            return

        ordered = self._ordered_expiry_keys()
        updates = {}
        for idx, (underlying, expiry) in enumerate(ordered):
            if idx >= len(WEEK_GROUPS):
                # Log once per expiry-out-of-band so it's visible without
                # noisy spam every cycle.
                stale = [p['tradingsymbol'] for p in self.closed_positions
                         if p.get('underlying') == underlying
                         and p.get('expiry') == expiry
                         and p['tradingsymbol'] not in self._banked_symbols_by_expiry.get(expiry, set())]
                if stale:
                    self.logger.warning(
                        f"Closed positions for expiry {expiry} have no week block "
                        f"(past {WEEK_GROUPS[-1]}); not banked: {stale}"
                    )
                    self._banked_symbols_by_expiry.setdefault(expiry, set()).update(stale)
                continue

            block_name = WEEK_GROUPS[idx]
            banked = self._banked_symbols_by_expiry.setdefault(expiry, set())
            block = dict(_DEFAULT_GROUP_CFG)
            block.update(self.config.get(block_name) or {})
            bucket_value = float(block.get('pnl', 0) or 0)
            delta = 0.0
            for closed in self.closed_positions:
                if closed.get('underlying') != underlying or closed.get('expiry') != expiry:
                    continue
                sym = closed['tradingsymbol']
                if sym in banked:
                    continue
                pnl = float(closed.get('pnl', 0) or 0)
                delta += pnl
                banked.add(sym)
                self.logger.info(
                    f"Banking closed {sym} P&L={pnl} -> {block_name}.pnl"
                )
            if delta:
                bucket_value += delta
                block['pnl'] = bucket_value
                # Write back the whole block (the other flags stay
                # unchanged from what was read above).
                self.config[block_name] = block
                updates[block_name] = block
                self.logger.info(f"{block_name}.pnl is now {bucket_value}")

        if updates:
            ok, info = put_config(self.config_file, updates=updates)
            if not ok:
                self.logger.error(f"Failed to persist week blocks: {info}")
                self._alert(
                    "ConfigWriteFailed",
                    f"Failed to persist week blocks: {info}"
                )

    def _group_pnl_components(self, group):
        """Return ``(partial_realized, unrealized)`` for ONE group from its
        OPEN positions only.

        Closed positions are no longer summed here — their realized P&L is
        banked into a week_pnl bucket by ``bank_closed_positions`` and
        carried back in by ``compute_group_pnl``. Partial closes on
        still-open legs (where ``buy_quantity`` != ``sell_quantity``) are
        kept here because those legs are open and recomputed every cycle.
        """
        realized = 0.0
        unrealized = 0.0
        for position in group.positions:
            net_qty = position['buy_quantity'] - position['sell_quantity']
            avg_buy_price = (position['buy_value'] / position['buy_quantity']
                             if position['buy_quantity'] != 0 else 0)
            avg_sell_price = (position['sell_value'] / position['sell_quantity']
                              if position['sell_quantity'] != 0 else 0)
            closed_qty = min(position['buy_quantity'], position['sell_quantity'])
            realized += (avg_sell_price - avg_buy_price) * closed_qty * position['multiplier']
            if net_qty > 0:
                unrealized += (position['last_price'] - avg_buy_price) * net_qty * position['multiplier']
            elif net_qty < 0:
                unrealized += (avg_sell_price - position['last_price']) * abs(net_qty) * position['multiplier']

        return realized, unrealized

    def compute_group_pnl(self, group):
        """P&L for ONE group including its week block's ``pnl`` carry-forward.

        Returns ``pnl + open-leg partial realized + open-leg unrealized``.
        The ``pnl`` bucket already contains all previously-banked closed-leg
        P&L for this expiry slot, so risk decisions on this group see its
        complete running history — no double-counting because closed
        positions are excluded from ``_group_pnl_components`` and tracked
        via ``_banked_symbols_by_expiry``.
        """
        group_cfg = self.get_group_config(group)
        bucket = float(group_cfg.get('pnl', 0) or 0)
        partial_realized, unrealized = self._group_pnl_components(group)
        return int(bucket + partial_realized + unrealized)

    def get_pnl(self, persist=False):
        """Session-level total P&L: sum of ``compute_group_pnl`` across all
        open groups (each already includes its bucket carry-forward).

        ``persist=True`` triggers ``bank_closed_positions`` so any new
        closes get written to ``config.yaml`` — kept on the same signature
        for backward compatibility with the orchestrator's per-cycle call.
        Dashboard / external readers can call with ``persist=False``.
        """
        if persist:
            self.bank_closed_positions()
        return sum(self.compute_group_pnl(g) for g in self.groups)
