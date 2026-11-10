"""Position-fetching, strategy-detection, and PnL computation.

These three steps were previously methods on ``handle_options`` in
``Monitor/options.py``. They are now factored out into ``PositionProcessor``
so the same logic can be reused from anywhere (backtesting, dashboards,
ad-hoc scripts) without dragging in the rest of the trading orchestrator.

The processor groups positions by ``(underlying, expiry)`` into
``StrategyGroup`` objects so the risk engine can run adjustments on each
open iron condor or strangle independently — earlier the engine only saw
the nearest-expiry book and ignored everything else.

Legacy single-expiry attributes (``strategy``, ``underlying``, ``expiry``)
are still populated from the nearest group so dashboard / external
callers don't break.
"""
from dataclasses import dataclass, field
from typing import Optional
import pytz

from helpers.basic import api_get, ApiError, split_symbol
from helpers.trade_ledger import (LEDGER_EXCHANGE, expiry_key, fifo_open_lots,
                                  open_lot_basis, orphan_lots,
                                  realized_pnl_by_expiry, sort_fills)
# Remote: this module runs on the trading server, which no longer shares a
# filesystem with trading.db. ``settlement_spot`` is injected into
# trade_ledger below for the same reason — trade_ledger itself must keep the
# local defaults, since db_api imports it and would otherwise call itself.
from helpers.db_client import (persist_trades, read_trades, settlement_spot,
                               DbApiError)

IST = pytz.timezone('Asia/Kolkata')

# Config block names for the per-expiry groups. Index N corresponds to the
# N-th nearest expiry FOR THAT UNDERLYING (chronological order across both
# open groups and closed-only expiries). A 6th or later expiry has no block
# — its closes are logged but not banked.
#
# Each block is a nested dict in ``Monitor/config.yaml`` of shape::
#
#     first_week:
#       adjust_hedges: true
#       adjustment: true
#
# These blocks are now OPERATOR FLAGS ONLY — read, never written. The ``pnl``
# key they used to carry is gone: realised P&L is FIFO-matched from the
# kite_trades ledger and keyed on the book's real (underlying, expiry), not on
# its rank among held expiries. Ranking meant a rolled-off expiry re-pointed
# every bucket at another book's accumulated total.
#
# TWO LOCATIONS, in precedence order:
#
#   1. ``underlyings.<NAME>.<block>`` — per-underlying. Used when that
#      underlying's section defines ANY week block.
#   2. ``<block>`` at top level — the original layout, still the home for
#      any underlying with no per-underlying blocks of its own.
#
# The fallback is what keeps existing single-underlying deployments intact:
# NIFTY carries on reading and banking into the top-level blocks, so the
# ``pnl`` accumulated there is neither orphaned nor double-counted.
#
# Indexing is PER UNDERLYING. Globally-indexed slots broke as soon as a
# second underlying appeared: BANKNIFTY's monthly expiries fall on the last
# Tuesday, which is also a NIFTY weekly, so the two books shared a date, the
# sort tie broke alphabetically, and books past the fifth slot silently got
# no config block and no P&L banking at all.
#
# Naming note: "week" is historical. For a monthly-only underlying like
# BANKNIFTY these mean 1st/2nd/3rd nearest MONTHLY expiry. The names are
# kept so the top-level fallback keeps working and no banked P&L has to be
# migrated to a renamed key.
WEEK_GROUPS = ('first_week', 'second_week', 'third_week',
               'fourth_week', 'fifth_week')

# Default per-group flags applied when a block is missing in config (e.g.
# a fresh deployment that hasn't been edited yet). Same conservative
# defaults the project shipped with at the top level.
_DEFAULT_GROUP_CFG = {
    # No 'pnl' key: realised P&L is derived from the kite_trades ledger, not
    # stored here. A default of 0 would have masked a missing ledger read as a
    # legitimately flat book.
    #
    # No 'close_on_stoploss' / 'close_on_trailprofit' either — the two paths
    # that read them are gone. Leaving them defaulted here would be worse than
    # useless: an operator would find the keys in a config block, set one true,
    # and get no behaviour change and no error.
    'adjust_hedges': True,
    'adjustment': True,
}


# Pandas used to be imported by this module for exactly one thing: building a
# DataFrame so the positions table printed nicely in the log. That is a real
# convenience, but not a ~50MB-per-interpreter one — and this module is
# imported by flaskapp, db_api, fetch_db_data and every autotrade process, so
# the host paid for it six times over to format one log line.
_TABLE_COLUMNS = ("tradingsymbol", "quantity", "average_price",
                  "last_price", "delta")


def _format_positions_table(positions, columns=_TABLE_COLUMNS):
    """Fixed-width table of ``positions``, in place of DataFrame's repr.

    Columns are sized to their widest cell so the output stays aligned, and a
    missing key renders as an empty cell rather than raising — a position
    whose delta was never recorded must still appear in the log.
    """
    if not positions:
        return "(no positions)"

    def cell(row, key):
        value = row.get(key)
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    rows = [[cell(p, c) for c in columns] for p in positions]
    widths = [max(len(head), *(len(r[i]) for r in rows))
              for i, head in enumerate(columns)]
    lines = ["  ".join(h.rjust(w) for h, w in zip(columns, widths)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.rjust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(lines)


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


# ---- Strategy names -------------------------------------------------------
#
# Named constants rather than bare literals because two DIFFERENT questions
# used to be asked with the same hardcoded tuple, in two files, and they now
# have genuinely different answers (see the two sets below).

IRON_CONDOR = 'IRON_CONDOR'
SHORT_STRANGLE = 'SHORT_STRANGLE'
LONG_STRANGLE = 'LONG_STRANGLE'
# One short leg and one long leg of the SAME option type and expiry — a short
# CE hedged by a long CE, or a short PE hedged by a long PE. A single-sided
# vertical. Directional because, unlike a condor or a strangle, it has a view:
# it is exposed to one side of the market only.
DIRECTIONAL = 'DIRECTIONAL'

# Books the risk engine has adjustment logic for. Membership here means
# ``RiskManager.adjustments`` will be called on the group and may place
# orders — so adding a strategy to this tuple is a decision about real money,
# not a labelling change.
ADJUSTABLE_STRATEGIES = (IRON_CONDOR, SHORT_STRANGLE)

# Books the monitor walks each cycle. A superset: DIRECTIONAL is monitored
# (classified, logged, P&L computed) but deliberately NOT adjusted, because
# no adjustment logic exists for it yet. Keeping the two sets separate is
# what lets a strategy be visible in the log and the dashboard while still
# being hands-off — before this, "not adjustable" and "not detected" were
# the same state, so an unrecognised book was silently invisible.
#
# When directional adjustments arrive, the change is moving DIRECTIONAL from
# here into ADJUSTABLE_STRATEGIES.
MONITORED_STRATEGIES = ADJUSTABLE_STRATEGIES + (DIRECTIONAL,)


def is_directional(positions):
    """True for a single-sided vertical: ONE short leg and ONE long leg of
    the same option type — a short CE with a long CE, or a short PE with a
    long PE.

    Same expiry is the caller's responsibility. Every caller here passes one
    book's positions, and books are keyed on ``(underlying, expiry)``, so it
    cannot fail in practice.

    Two conditions, each of which excludes something real:

    * **Exactly two positions, one of each side.** A third leg makes this
      some other structure; classifying it as directional would hand the
      adjustment engine a book it doesn't understand.
    * **Same option type.** A short CE paired with a long PE is a synthetic,
      not a hedge — the long leg does nothing to cap the short's loss, which
      stays unbounded.

    Relative strike is deliberately NOT examined. Both orderings count: the
    long leg above a short CE (a credit spread) and below it (a debit spread)
    are both directional books. They have opposite risk profiles, which
    matters to the adjustment logic — that logic reads the strikes itself
    rather than assuming the classifier already filtered for it.
    """
    if len(positions) != 2:
        return False
    shorts = [p for p in positions if p.get('transtype') == 'sell']
    longs = [p for p in positions if p.get('transtype') == 'buy']
    if len(shorts) != 1 or len(longs) != 1:
        return False
    opt_type = shorts[0].get('option_type')
    return (opt_type in ('CE', 'PE')
            and longs[0].get('option_type') == opt_type)


def detect_strategy(positions):
    """Strategy name for one book's ``positions``, or None if unrecognised.

    Each position needs ``transtype`` ('buy'/'sell'), ``option_type``
    ('CE'/'PE') and ``strike``. All positions must belong to one
    ``(underlying, expiry)`` book — the strike-ordering tests below compare
    a PE strike against a CE strike, which only means anything within one
    expiry.

    Module-level rather than a ``PositionProcessor`` method because the
    dashboard endpoint needs the same verdict without building a processor
    (which would fetch positions all over again). One classifier, so the
    badge the operator reads and the routing the engine does cannot disagree.
    """
    call_long_strike = call_short_strike = put_short_strike = put_long_strike = 0
    long_strangle_found = short_strangle_found = False
    for position in positions:
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
        return IRON_CONDOR
    if long_strangle_found:
        return LONG_STRANGLE
    if short_strangle_found:
        return SHORT_STRANGLE
    # Last, and reached only when none of the above matched. The three checks
    # above all need a PE and a CE on the same side, which a single-sided
    # vertical cannot supply — so this ordering does not reclassify anything
    # that was already being detected.
    if is_directional(positions):
        return DIRECTIONAL
    return None


@dataclass
class StrategyGroup:
    """All positions for a single ``(underlying, expiry)`` book.

    Built by ``PositionProcessor.process_positions`` and tagged with a
    detected ``strategy`` by ``analyze_positions``. The risk engine
    iterates these per cycle, so a near-weekly iron condor and a
    next-weekly iron condor each get their own adjustment decisions —
    earlier only the nearest expiry was watched.
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
        self._alert = alert_fn or (lambda heading, message, priority=5: None)
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
        # Realised P&L per (underlying, expiry_date), FIFO-matched from the
        # kite_trades ledger. Computed at most ONCE per cycle and cached here:
        # compute_group_pnl is called per group, and re-reading the whole
        # ledger five times every 30s would be wasteful.
        #
        # Replaces the old ``_banked_symbols_by_expiry`` + ``<week>.pnl``
        # bucket scheme. Those buckets were keyed by a book's RANK among held
        # expiries, so an expiry rolling off silently re-pointed every book at
        # another book's accumulated total.
        self._realised_cache = None
        # {tradingsymbol: [(qty, price, side), ...]} — the lots FIFO could not
        # match, i.e. what the ledger believes is still OPEN, with the price
        # each open lot actually went on at. Filled by the same ledger read
        # that produces _realised_cache, so it costs no extra I/O.
        #
        # This is the cost basis the entry-price consumers need. The broker's
        # buy_value/buy_quantity are day-cumulative per symbol, so a leg that
        # was closed and reopened at the same strike (every partial-reduce
        # does exactly that) reports an average blended across both tranches.
        # None means the read FAILED — distinct from {} (read fine, ledger
        # empty), because callers must refuse to size a trade off a basis
        # they could not corroborate.
        self._ledger_open_lots_cache = None
        # {tradingsymbol: net_qty} from the last broker read, used to detect
        # residual lots the broker doesn't corroborate (missing fills).
        self._broker_net = {}
        # False when the ledger could not be READ this cycle, so realised P&L
        # is {} and every book would read realised=0 — not "possibly missing
        # the last fill" but "no realised data at all". The orchestrator skips
        # risk decisions while it is False, because a book with banked losses
        # would otherwise look flat.
        #
        # A failed trade INGEST (fetch or persist) deliberately does NOT clear
        # this: that leaves the ledger at most one cycle behind, which is a
        # bounded understatement, and skipping cycles for it proved more
        # disruptive than the risk it avoided. That path alerts at low
        # priority and proceeds — see fetch_and_persist_trades.
        self.realised_trustworthy = True
        # Why the last ingest failed, for the caller's alert text.
        self._last_ingest_error = None
        # Trade-ledger ingest pacing. Cycles since the last attempt (None =
        # never attempted), and the broker net-quantity map as of the last
        # attempt — see maybe_fetch_and_persist_trades for why both matter.
        self._cycles_since_ingest = None
        self._net_at_last_ingest = None

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

    def _trade_ingest_cycles(self):
        """Cycles between routine trade-ledger ingests.
        ``monitor_info.trade_ingest_cycles``, default 20 (~10 min at the
        default 30s delay)."""
        monitor_info = self.config.get('monitor_info') or {}
        try:
            return max(1, int(monitor_info.get('trade_ingest_cycles', 20)))
        except (TypeError, ValueError):
            self.logger.warning(
                "monitor_info.trade_ingest_cycles is not an integer; using 20")
            return 20

    def maybe_fetch_and_persist_trades(self):
        """Ingest the trade ledger, but not on every cycle.

        Polling ``/get_trades`` every 30s is mostly waste: the trade book
        changes a handful of times a day, and each poll is a broker round
        trip plus a DB API write. This runs it every
        ``monitor_info.trade_ingest_cycles`` cycles instead.

        It ALSO runs immediately whenever the broker's net position map has
        changed since the last ingest. That trigger is what makes the slower
        cadence safe: realised P&L is FIFO-matched from ``kite_trades``, so
        the moment that matters is right after a fill — and a changed net
        quantity IS a fill, whether the bot placed it (a roll or a
        partial-reduce) or you did by hand. Without this, closing part of a
        short would leave its realised loss invisible to the adjustments
        engine for the rest of the interval; with it, the ledger is current
        on the very next cycle.

        Relies on ``process_positions`` having already run this cycle, which
        the orchestrator guarantees — ``analyze_positions`` precedes this
        call, and it is what populates ``_broker_net``.

        Returns what ``fetch_and_persist_trades`` returns (rows inserted, or
        ``None`` on failure), and ``0`` when it deliberately skipped — a skip
        persisted nothing but is not a failure, so the caller must not alert
        on it.
        """
        interval = self._trade_ingest_cycles()
        never_run = self._cycles_since_ingest is None
        # Compared against the last ATTEMPT, not the last success: while an
        # ingest keeps failing this stays true, so a cycle with unaccounted
        # fills keeps retrying rather than waiting out the interval.
        positions_changed = (not never_run
                             and self._broker_net != self._net_at_last_ingest)
        due = not never_run and self._cycles_since_ingest >= interval

        if not (never_run or due or positions_changed):
            self._cycles_since_ingest += 1
            self.logger.info(
                f"kite_trades ingest skipped ({self._cycles_since_ingest}/"
                f"{interval} cycles, positions unchanged)"
            )
            return 0

        if positions_changed and not due and not never_run:
            self.logger.info(
                "Broker net position changed since the last ingest — pulling "
                "the trade ledger now rather than waiting for the interval"
            )
        self._cycles_since_ingest = 0
        result = self.fetch_and_persist_trades()
        if result is not None:
            # Only a SUCCESSFUL ingest accounts for the current positions.
            # Advancing this on failure would clear positions_changed and let
            # a missed fill sit unrecorded for a whole interval.
            self._net_at_last_ingest = dict(self._broker_net)
        return result

    def fetch_and_persist_trades(self):
        """Pull today's executed fills via ``/get_trades`` and append
        any new ones to ``kite_trades``.

        Called once per orchestrator cycle from ``Monitor/options.py:run()``
        so the local trade ledger lags the broker by at most one
        ``monitor_info.delay`` interval (default 30s). The daily 15:35
        recorder hook still runs as a redundant safety net, but no
        longer the sole source — useful when ``fetch_db_data.py``
        is down or restarting.

        Idempotent: ``persist_trades`` uses ``INSERT OR IGNORE`` keyed
        on Kite's ``trade_id``, so multiple per-cycle fetches plus the
        15:35 fetch all converge without duplicating rows.

        Never raises. Returns the count of newly-inserted rows, or ``None``
        when the ledger could not be brought up to date. ``0`` is a
        legitimate success — nothing new to record — which is why failure
        needs its own value rather than reusing 0.

        ``None`` does NOT stop the risk pipeline. It means the ledger may be
        one cycle behind the broker, so realised P&L could be understated by
        whatever just filled; the caller alerts at low priority and carries
        on (operator decision). It deliberately does not set
        ``realised_trustworthy``, which is reserved for the worse case where
        the ledger cannot be READ at all and every book would read
        realised=0.

        ``self._last_ingest_error`` carries the reason, so the alert can name
        which of the three failures happened instead of listing all of them.
        """
        url = f"{self.api_url}/get_trades?user={self.user}"
        self._last_ingest_error = None
        try:
            success, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"/get_trades fetch failed: {e}")
            self._last_ingest_error = f"broker /get_trades unreachable: {e}"
            return None
        if not success:
            self.logger.warning(f"Broker reported failure for /get_trades: {payload}")
            self._last_ingest_error = f"broker refused /get_trades: {payload}"
            return None
        user_trades = (payload or {}).get(self.user) or []
        if not user_trades:
            return 0
        try:
            inserted = persist_trades(user_trades, self.user)
        except DbApiError as e:
            # The DB API is on another host now, so this is a network failure
            # rather than a disk one — likelier, and the reason it must be
            # reported rather than logged and forgotten.
            self.logger.error(f"Could not persist trades to the DB API: {e}")
            self._last_ingest_error = f"DB API write failed: {e}"
            return None
        except Exception as e:
            self.logger.exception(f"Failed to persist trades into kite_trades: {e}")
            self._last_ingest_error = f"ledger write failed: {e}"
            return None
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
        # Start of a cycle: drop the cached realised figures so this cycle
        # re-reads the ledger (the monitor may have persisted new fills since).
        # The open-lot map comes off that same read and must expire with it —
        # a stale one would price a leg off lots that have since been closed.
        self._realised_cache = None
        self._ledger_open_lots_cache = None
        self._broker_net = {}
        self.realised_trustworthy = True
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
        # Broker net per symbol, captured BEFORE options_to_avoid filtering and
        # symbol parsing: orphan detection compares the ledger's implied
        # position against what the broker actually holds, so it needs the
        # unfiltered truth. A leg we skip for trading still has to reconcile.
        for position in positions_list:
            sym = position.get('tradingsymbol')
            if sym:
                self._broker_net[sym] = int(position.get('quantity') or 0)

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

            self.logger.info("\n" + _format_positions_table(self.positions))
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
        #
        # Deliberately ADJUSTABLE and not MONITORED: this picks what the
        # dashboard's single-strategy fields describe, and a book the engine
        # actively manages is the more useful answer than a nearer one it only
        # watches. A directional book still becomes primary when it is the
        # only thing open — the fallback below reaches it.
        primary = next(
            (g for g in self.groups if g.strategy in ADJUSTABLE_STRATEGIES),
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
        """Tag ``group.strategy`` from its positions. See ``detect_strategy``."""
        group.strategy = detect_strategy(group.positions)

    def _ordered_expiry_keys(self):
        """All ``(underlying, expiry)`` pairs seen this cycle — across both
        open groups and closed-only books — sorted by expiry ascending.

        Including closed-only expiries matters when an adjustment or a
        manual close has just closed every leg of a book: the next cycle
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

    def _expiries_for_underlying(self, underlying):
        """This underlying's observed expiries, ascending.

        Scoped per underlying so each index gets its own first/second/third
        slots. A global ordering let one underlying's expiries consume the
        other's slots.
        """
        return [exp for (u, exp) in self._ordered_expiry_keys()
                if u == underlying]

    def _has_own_week_blocks(self, underlying):
        """True iff ``underlyings.<NAME>`` defines at least one week block.

        All-or-nothing on purpose: a partial per-underlying section would
        otherwise fall back to the top-level blocks for the gaps, silently
        commingling two indices' P&L in one bucket.
        """
        section = (self.config.get('underlyings') or {}).get(underlying)
        if not isinstance(section, dict):
            return False
        return any(isinstance(section.get(w), dict) for w in WEEK_GROUPS)

    def _block_path(self, underlying, idx):
        """Dotted config path for ``underlying``'s ``idx``-th expiry block,
        or ``None`` when past the last slot.

        Read-only now: this locates the block whose operator FLAGS apply to a
        book. It no longer addresses a write target — realised P&L moved to the
        trade ledger, and rank-based addressing is exactly why (the Nth-nearest
        expiry changes identity as expiries roll off). brain.py used to write
        ``close_on_trailprofit`` through this path — though brain has never run
        in production, so no deployment ever saw that write. With the flag
        gone, nothing writes here at all.
        """
        if idx is None or idx >= len(WEEK_GROUPS):
            return None
        week = WEEK_GROUPS[idx]
        if self._has_own_week_blocks(underlying):
            return f"underlyings.{underlying}.{week}"
        return week

    def _read_block(self, path):
        """Read a dotted config path, returning ``{}`` when any level is
        missing or isn't a dict."""
        node = self.config
        for key in path.split('.'):
            if not isinstance(node, dict):
                return {}
            node = node.get(key)
        return node if isinstance(node, dict) else {}

    def _group_block_name(self, group):
        """Dotted config path for ``group``'s block, based on its position
        among that underlying's own expiries. ``None`` for groups past the
        last slot — those expiries don't get a block.
        """
        expiries = self._expiries_for_underlying(group.underlying)
        try:
            idx = expiries.index(group.expiry)
        except ValueError:
            return None
        return self._block_path(group.underlying, idx)

    def get_group_config(self, group):
        """Return the per-group config block for ``group`` — a dict with
        ``adjust_hedges`` and ``adjustment``. Falls back to the conservative
        defaults in ``_DEFAULT_GROUP_CFG`` when the block is missing (so a
        fresh deployment doesn't crash before the user has edited config).

        Keys present in the config file but not in the defaults are still
        passed through, so a leftover ``close_on_stoploss`` in someone's
        unmigrated config is inert rather than fatal.

        Risk decisions read flags off this dict instead of off the top-level
        config — every (underlying, expiry) book has its own per-group
        toggles.
        """
        block_name = self._group_block_name(group)
        if not block_name:
            return dict(_DEFAULT_GROUP_CFG)
        return {**_DEFAULT_GROUP_CFG, **self._read_block(block_name)}

    # bank_closed_positions() was removed. It read each week block's `pnl`,
    # added newly-closed legs' P&L, and wrote the block back to
    # Monitor/config.yaml every cycle. Three problems went with it:
    #   * the block was chosen by the book's RANK among held expiries, so a
    #     rolled-off expiry re-pointed every bucket at another book's total;
    #   * it was a read-modify-write of a shared file, so two processes
    #     (per-user monitors) would lose each other's updates;
    #   * realised P&L was stored rather than derived, so it could not be
    #     recomputed or audited.
    # Realised P&L now comes from the kite_trades ledger on every read.

    def _group_unrealized(self, group):
        """Mark-to-market on ONE group's still-open quantity.

        Realised P&L is NOT computed here. It comes from the FIFO ledger via
        ``realised_by_expiry``, which matches every fill including the partial
        closes on still-open legs that this method used to add up separately.
        Keeping both would double-count them: the ledger sees the same fills
        the broker's buy/sell_value totals are built from.

        The entry price is the FIFO basis of the OPEN lots, not the broker's
        day-cumulative average. Those differ the moment a leg is closed and
        reopened in the same symbol on the same day, and then the two halves
        of ``compute_group_pnl`` stop composing: FIFO banks the closed lot's
        gain in realised, while a blended average leaves part of that same
        gain sitting in unrealised as well. On a reopened hedge that
        double-counts thousands of rupees, which is why this reads the
        ledger rather than the cheaper broker field.

        Falls back to the broker average when the ledger cannot vouch for a
        leg (``open_lot_price`` logs why). This is a reported figure, not a
        sizing input, so a degraded number beats a blank one — the trading
        paths in ``risk_management`` make the opposite choice and skip.
        """
        unrealized = 0.0
        for position in group.positions:
            symbol = position.get('tradingsymbol')
            net_qty = position['buy_quantity'] - position['sell_quantity']
            if net_qty == 0:
                continue

            basis = self.open_lot_price(symbol, broker_qty=net_qty, quiet=True)
            if basis is None:
                # Same arithmetic this method always used. Correct until a
                # symbol carries more than one tranche; understood to be
                # approximate after that.
                self.logger.info(
                    f"  {symbol}: unrealised uses the broker's blended "
                    f"average, which is approximate once a leg has been "
                    f"reopened in the same symbol."
                )
                if net_qty > 0:
                    basis = (position['buy_value'] / position['buy_quantity']
                             if position['buy_quantity'] != 0 else 0)
                else:
                    basis = (position['sell_value'] / position['sell_quantity']
                             if position['sell_quantity'] != 0 else 0)

            if net_qty > 0:
                unrealized += (position['last_price'] - basis) * net_qty * position['multiplier']
            else:
                unrealized += (basis - position['last_price']) * abs(net_qty) * position['multiplier']

        return unrealized

    def realised_by_expiry(self):
        """``{(underlying, expiry_date): realised}`` from the FIFO ledger,
        computed once per cycle and cached.

        Logs INCOMPLETE warnings for books whose residual lots the broker does
        not corroborate — those figures are understated by an amount no
        available source can reconstruct, so they must not be trusted. The
        per-book figures themselves are NOT logged here; ``Monitor/options.py``
        already logs each group's P&L once per cycle, and 16 more lines every
        30s would bury it.
        """
        if self._realised_cache is not None:
            return self._realised_cache

        try:
            # One ledger read, shared by the P&L computation and the orphan
            # check, so a cycle touches SQLite once rather than twice.
            ledger = [t for t in read_trades(user=self.user)
                      if t.get('exchange') == LEDGER_EXCHANGE]
            realised = realized_pnl_by_expiry(
                self.user, trades=ledger, broker_net=self._broker_net,
                # Inject the remote lookup: trade_ledger's default is the
                # local one, which doesn't exist on this host.
                spot_fn=settlement_spot)
            orphans = orphan_lots(ledger, self._broker_net)
            # Open-lot cost basis, off the same read. Sorted explicitly:
            # FIFO is order-sensitive and read_trades' ORDER BY is a TEXT
            # sort on fill_timestamp, which has historically not been
            # chronological (see sort_fills). Getting the order wrong here
            # mis-prices a leg rather than erroring.
            self._ledger_open_lots_cache = fifo_open_lots(sort_fills(ledger))
        except Exception as e:
            # Never take down a cycle over the ledger, but do NOT let a
            # failure pass as a flat book: {} would zero every book's
            # realised P&L, and the partial-reduce path gates on whether a
            # side is net-losing. The flag tells the orchestrator to skip
            # risk decisions this cycle.
            self.realised_trustworthy = False
            self.logger.exception(f"FIFO ledger read failed: {e}")
            self._alert("LedgerReadFailed",
                        f"Could not compute realised P&L from kite_trades: {e}. "
                        f"Risk decisions are being skipped this cycle.")
            self._realised_cache = {}
            return self._realised_cache
        orphans = orphans or {}

        # An empty ledger is indistinguishable from "no fills yet", and it
        # would make every book look flat. If we hold positions but the ledger
        # says nothing, that is a data problem, not a flat book.
        if not realised and self.groups:
            self.logger.error(
                f"FIFO ledger returned NO realised P&L for {self.user} while "
                f"{len(self.groups)} book(s) are open. Every book will read "
                f"realised=0, which understates any loss. Check that "
                f"kite_trades is being written."
            )
            self._alert("LedgerEmpty",
                        f"kite_trades has no fills for {self.user} but "
                        f"{len(self.groups)} book(s) are open.")

        for key, symbols in sorted(orphans.items(), key=lambda kv: str(kv[0])):
            self.logger.warning(
                f"INCOMPLETE realised P&L for {key[0]}@{key[1]}: ledger holds "
                f"residual lots the broker does not confirm for {symbols}. "
                f"Their opening fills predate the ledger, so this book's "
                f"realised figure is understated by an unknown amount."
            )

        self._realised_cache = realised
        return realised

    def open_lot_price(self, symbol, broker_qty=None, quiet=False):
        """FIFO cost basis of the lots of ``symbol`` that are still open.

        Returns the quantity-weighted entry price of the unmatched lots, or
        ``None`` when there is no basis this method is willing to vouch for.
        ``None`` means exactly one thing to callers: **do not substitute the
        broker's average.** That average is day-cumulative per symbol, which
        is the blend this method exists to avoid.

        ``broker_qty`` is the leg's SIGNED net quantity from the position
        book (negative for a short). Pass it and the ledger's implied open
        quantity must agree, or you get ``None``. That check is the whole
        safety property: the ledger lags the broker by up to one ingest, so
        right after a trim the reopened lot may not be persisted yet — and
        the lots still on file would then price a leg that no longer exists
        at that size. Disagreement is also how a pre-ledger opening fill
        surfaces (the same condition ``orphan_lots`` reports as INCOMPLETE).

        Reads through the per-cycle cache, so the first call in a cycle
        triggers the ledger read and later ones are free.

        ``quiet`` drops the explanatory warning to debug. Reporting callers
        pass it because they run on every leg of every book every cycle and
        log their own one-line fallback note; a leg whose opening fill
        predates the ledger would otherwise warn twice a minute forever.
        Callers that SKIP A TRADE on ``None`` must leave it off — there the
        reason is the record of why an adjustment did not happen.
        """
        warn = self.logger.debug if quiet else self.logger.warning
        self.realised_by_expiry()  # populates the cache, or leaves it None
        lots = self._ledger_open_lots_cache
        if lots is None:
            warn(f"No FIFO open-lot basis for {symbol}: the ledger could not "
                 f"be read this cycle.")
            return None

        signed_qty, price = open_lot_basis(lots.get(symbol))
        if price is None:
            warn(f"No FIFO open-lot basis for {symbol}: the ledger holds no "
                 f"open lots for it. Its opening fill predates the ledger.")
            return None
        if broker_qty is not None and round(signed_qty) != int(broker_qty):
            warn(f"FIFO open-lot basis for {symbol} REJECTED: ledger says "
                 f"{signed_qty:+.0f} open, broker says {int(broker_qty):+d}. "
                 f"Either a fill is not yet persisted or one is missing; "
                 f"pricing this leg off the ledger would be wrong.")
            return None
        return price

    def group_realised(self, group):
        """Realised P&L for ONE group, keyed on its ACTUAL expiry.

        ``expiry_key`` normalises both sides: the ledger builds its keys from
        ``split_symbol`` (a datetime) while ``group.expiry`` arrives by another
        route. A type mismatch would silently miss and read as "no realised
        P&L" — understating a loss rather than erroring.
        """
        key = (group.underlying, expiry_key(group.expiry))
        return float(self.realised_by_expiry().get(key, 0.0))

    def compute_group_pnl(self, group):
        """Total P&L for ONE group: realised (from the ledger) + unrealised.

        Realised comes from FIFO-matching ``kite_trades`` for this book's
        ACTUAL ``(underlying, expiry)``. It previously came from the
        ``<week>.pnl`` config bucket, keyed by the book's RANK among held
        expiries — so when an expiry rolled off, every book below it shifted up
        a slot and inherited the previous occupant's accumulated total. A
        freshly opened book could read tens of thousands of rupees of a dead
        book's P&L.

        Unrealised is mark-to-market on the open quantity only. The old
        "partial realized" term is gone: FIFO already matches those partial
        closes, so keeping both double-counted them.
        """
        return int(self.group_realised(group) + self._group_unrealized(group))

    def get_pnl(self, persist=False):
        """Session-level total P&L: sum of ``compute_group_pnl`` across all
        open groups.

        ``persist`` is accepted and ignored. It used to trigger
        ``bank_closed_positions``, which wrote realised P&L back into
        ``Monitor/config.yaml``; realised P&L is now derived from the trade
        ledger on read, so there is nothing to persist. Kept on the signature
        so existing callers (``Monitor/options.py``'s wrapper) don't break.
        """
        return sum(self.compute_group_pnl(g) for g in self.groups)
