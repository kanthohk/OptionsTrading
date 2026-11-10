"""Realised P&L per (underlying, expiry), derived by FIFO-matching the
persisted ``kite_trades`` fill ledger.

This replaces the ``<week_block>.pnl`` buckets in ``Monitor/config.yaml`` as
the source of realised P&L for risk decisions. The buckets were keyed by a
book's *rank* among currently-held expiries, so an expiry rolling off — or
opening a nearer one — silently re-pointed each book at another book's
accumulated total. Keying on the actual expiry removes that class of bug, and
reading rather than writing removes the config as shared mutable state.

The FIFO functions were originally written in brain.py, which has never run in
production. They are promoted here unchanged in behaviour but restructured:

  * ``fifo_realized_pnl`` / ``aggregate_realized_by_expiry`` /
    ``net_realized_by_expiry`` are **pure** — they take a list of trade dicts
    and are exhaustively unit-tested without a database.
  * ``realized_pnl_by_expiry`` is the thin I/O shell that reads the ledger.

Not accounted for: Kite's trades endpoint carries no charges, so brokerage /
STT / GST are approximated by a flat ``CHARGE_PER_TRADE`` per fill.

TRADING POLICY THIS RELIES ON: every position is closed before expiry day.
That single fact makes the ledger self-checking — a residual lot on an expired
book cannot be a held position, so it is proof that fills were missed. Expiry
settlement (``expiry_settlement_fills``) exists for the unintended-expiry case
but defaults OFF, because enabling it would convert that proof into a
plausible-looking number. If the policy ever changes, revisit both.
"""

from collections import defaultdict, deque
from datetime import date, datetime

from helpers.basic import IST, parse_broker_timestamp, split_symbol
from MarketAnalysis.db_queries import read_trades, settlement_spot

# Flat-rate approximation of broker fees per executed fill (INR): Zerodha
# brokerage + STT + exchange transaction + GST + stamp duty + SEBI fees.
# Charged per fill, not per matched pair — an unmatched open still cost money.
CHARGE_PER_TRADE = 40

# Only F&O fills participate. An equity leg in the same account would
# otherwise be FIFO-matched into an option book's P&L.
LEDGER_EXCHANGE = "NFO"


def expiry_key(expiry):
    """Normalise an expiry to a ``date`` for use as a dict key.

    Accepts ``datetime``, ``date``, or ``'YYYY-MM-DD'``. Returns the value
    unchanged if it is none of those, so an unexpected type surfaces as a key
    miss rather than an exception.

    This exists because the producer and the consumer reach the expiry by
    different routes: ``split_symbol`` yields a ``datetime`` here, while
    callers hold ``group.expiry``. Keying one as ``datetime`` and looking up
    the other as ``date`` would silently miss, and a miss reads as "no
    realised P&L" — which understates a loss and hides a losing side from
    the partial-reduce gate. Both sides must go through this function.
    """
    if hasattr(expiry, "date") and callable(getattr(expiry, "date")):
        return expiry.date()
    if isinstance(expiry, str):
        try:
            y, m, d = (int(p) for p in expiry.split("-"))
        except (ValueError, TypeError):
            return expiry
        import datetime as _dt
        return _dt.date(y, m, d)
    return expiry


def sort_fills(trades):
    """Return ``trades`` in true chronological order, oldest first.

    FIFO is order-dependent, and relying on ``read_trades``' SQL
    ``ORDER BY fill_timestamp`` is not safe: that column is TEXT, and broker
    timestamps have historically been stored as RFC 1123, which sorts
    alphabetically by weekday name. Sorting here in Python — on a *parsed*
    timestamp — makes the result independent of both the stored format and the
    query's ORDER BY, so a future regression upstream cannot silently corrupt
    P&L.

    Rows with a missing or unparseable timestamp sort first and keep their
    relative order (Python's sort is stable), rather than being dropped.
    ``trade_id`` breaks ties so same-second fills are deterministic.
    """
    return sorted(
        trades,
        key=lambda t: (parse_broker_timestamp(t.get("fill_timestamp")) or datetime.min,
                       str(t.get("trade_id") or "")),
    )


def fifo_realized_pnl(trades):
    """Per-symbol realised P&L from a chronological list of fills, matched
    First-In-First-Out. Returns ``{tradingsymbol: realised}``.

    Each trade dict needs ``tradingsymbol``, ``transaction_type``
    (BUY/SELL), ``quantity`` and ``average_price``. No multiplier is applied:
    Kite reports quantity in shares (lot_size * lots), so a price difference
    times matched quantity is already rupees.

    Maintains a per-symbol queue of open lots. Each fill either matches
    against the oldest opposite-direction lot or, if nothing is left to match,
    becomes a new open lot. The formula is direction-agnostic::

        realised = (sell_price - buy_price) * matched_qty

    ``trades`` MUST be in fill order — ``read_trades`` sorts by
    ``fill_timestamp, trade_id`` for exactly this reason. Out-of-order input
    produces wrong pairings, not an error.

    Gross: charges are not deducted here (see ``net_realized_by_expiry``).
    """
    return fifo_match(trades)[0]


def fifo_open_lots(trades):
    """``{tradingsymbol: [(qty, price, side), ...]}`` left unmatched after FIFO,
    oldest lot first. Empty entries are omitted.

    These are the positions the ledger believes are still open. Two things read
    them: expiry settlement (an expired residual lot has to be valued at
    intrinsic, since expiry produces no fill) and orphan detection (a residual
    lot the broker does not corroborate came from an opening fill that predates
    the ledger).
    """
    return fifo_match(trades)[1]


def realised_today_by_symbol(trades, today):
    """``{tradingsymbol: realised}`` from closing fills made on ``today``.

    FIFO over every fill, minus FIFO over the fills from before ``today``.
    ``trades`` must be ``sort_fills`` order, which makes the pre-today fills a
    prefix of the list — their matches are identical in both runs, so the
    difference is exactly what today's fills matched. That includes a partial
    close of a leg that is still open, and the closed tranche of a symbol
    closed and reopened today, neither of which the broker's net position row
    can separate out.

    Fill timestamps are the broker's IST wall-clock (naive), so ``today`` must
    be the IST date. An unparseable timestamp counts as before today — it
    sorts first in ``sort_fills`` too, which keeps the prefix property.

    Gross: charges are not deducted, matching the MTM it is shown beside.
    """
    def _is_today(t):
        ts = parse_broker_timestamp(t.get("fill_timestamp"))
        return ts is not None and ts.date() >= today

    prior = [t for t in trades if not _is_today(t)]
    realised_all = fifo_realized_pnl(trades)
    realised_prior = fifo_realized_pnl(prior)
    return {sym: pnl - realised_prior.get(sym, 0.0)
            for sym, pnl in realised_all.items()}


def open_lot_basis(lots):
    """``(signed_qty, avg_price)`` for one symbol's ``fifo_open_lots`` entry.

    ``signed_qty`` is positive for a net long (unmatched BUY lots), negative
    for a net short. ``avg_price`` is the quantity-weighted entry price of
    those lots — the cost basis of what is *still open*, which is the figure
    the broker's ``buy_value / buy_quantity`` does NOT give you.

    Why this matters: the broker's net position row carries DAY-CUMULATIVE
    buy/sell totals per tradingsymbol. Close a leg and reopen it at the same
    strike — which ``_partial_reduce_losing_side`` does on every trim — and
    the reopened lot's average is blended with the closed lot's, because
    both are buys in the same symbol on the same day. The blended average
    sits below the true basis of the open lot (the leg appreciated, that is
    why it was trimmed), so the open leg looks more profitable than it is
    and the already-realised, already-spent gain gets counted a second time.
    FIFO has the matched lots removed, so what is left is the real basis.

    Returns ``(0.0, None)`` when nothing is open — the caller must treat a
    ``None`` price as "no corroborated basis" and not substitute a fallback.

    ``lots`` is one value from ``fifo_open_lots`` (``[(qty, price, side)]``);
    pass ``None`` or ``[]`` for a symbol the ledger has never seen. FIFO
    leaves a symbol's lots single-sided — a fill in the opposite direction
    is absorbed before it can open a lot — so mixing cannot occur, but the
    sum below is direction-aware regardless rather than trusting that.
    """
    if not lots:
        return (0.0, None)
    signed = 0.0
    total = 0.0
    value = 0.0
    for qty, price, side in lots:
        signed += qty if side == "BUY" else -qty
        total += qty
        value += qty * price
    if total <= 0:
        return (0.0, None)
    return (signed, value / total)


def fifo_match(trades):
    """Run the FIFO match once, returning ``(realized, open_lots)``.

    Single implementation behind ``fifo_realized_pnl`` and ``fifo_open_lots``
    so the two can never disagree about what matched.
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
        # Absorb against opposite-direction lots, oldest first.
        while qty > 0 and q and q[0][2] != side:
            lot_qty, lot_price, lot_side = q[0]
            matched = min(qty, lot_qty)
            if side == "SELL":
                # Closing a long: sold at price, bought at lot_price.
                realized[sym] += (price - lot_price) * matched
            else:
                # Closing a short: bought at price, sold at lot_price.
                realized[sym] += (lot_price - price) * matched
            qty -= matched
            if matched < lot_qty:
                q[0] = (lot_qty - matched, lot_price, lot_side)
            else:
                q.popleft()

        # Whatever is left opens a new lot in this fill's direction.
        if qty > 0:
            q.append((qty, price, side))

    return (dict(realized),
            {sym: list(lots) for sym, lots in open_lots.items() if lots})


def _symbol_expiry_key(symbol):
    """``(underlying, expiry_date)`` for ``symbol``, or None if unparseable."""
    try:
        parsed = split_symbol(symbol)
    except Exception:
        return None
    if not parsed or len(parsed) != 4:
        return None
    underlying, expiry, _, _ = parsed
    return (underlying, expiry_key(expiry))


def aggregate_realized_by_expiry(per_symbol_pnl, logger=None):
    """Group ``{tradingsymbol: realised}`` into ``{(underlying, expiry): sum}``.

    Unparseable symbols are dropped, but — unlike the original in brain.py —
    they are counted and warned about. Silence here would be dangerous now
    that this figure drives close decisions: a change to NSE's symbol format
    would zero out realised P&L across the board and look like a flat book.
    """
    by_expiry = defaultdict(float)
    dropped = []
    for sym, pnl in per_symbol_pnl.items():
        key = _symbol_expiry_key(sym)
        if key is None:
            dropped.append(sym)
            continue
        by_expiry[key] += pnl
    if dropped and logger:
        logger.warning(
            f"FIFO ledger: {len(dropped)} symbol(s) dropped as unparseable, "
            f"their realised P&L is NOT counted: {sorted(dropped)}"
        )
    return dict(by_expiry)


def intrinsic_value(option_type, strike, spot):
    """Cash-settled value of an option at expiry. Zero if out of the money."""
    if option_type == "CE":
        return max(0.0, float(spot) - float(strike))
    if option_type == "PE":
        return max(0.0, float(strike) - float(spot))
    return None


def expiry_settlement_fills(trades, spot_fn, today=None, logger=None):
    """Synthetic closing fills that value expired residual positions at
    intrinsic, so FIFO can realise P&L that no trade ever produced.

    OPT-IN, and OFF by default — see the warning below.

    An option that expires is *settled*, not traded: the contract ceases to
    exist, so no closing fill is ever generated. A position held to expiry
    therefore leaves FIFO with an unmatched opening lot reporting realised = 0.
    This closes those lots at intrinsic value.

    WARNING — do not enable this without knowing the trading policy. This
    deployment closes every position BEFORE expiry day, which means a residual
    lot on an expired book can only be a DATA GAP (an opening fill that
    predates the ledger), never a genuinely held position. Settling a phantom
    lot books a fabricated P&L: a phantom long valued at intrinsic 0 produces
    an invented loss of ``buy_price * qty``. Nothing in the fills can tell the
    two apart, and the broker cannot corroborate an expired contract — so the
    default is to leave such lots alone and let ``orphan_lots`` flag them.

    Enable this only for an unintended expiry (monitor down on expiry day, a
    close that failed) where you have separately confirmed the position was
    genuinely held.

    ``spot_fn(underlying, 'YYYY-MM-DD')`` must return ``{'spot': float, ...}``
    or None. When it returns None nothing is synthesised for that book and a
    warning is logged: an invented settlement price would silently book a wrong
    realised figure into a live risk decision, which is worse than a known gap.

    Returns a list of fill dicts marked ``synthetic=True``. These are derived
    on every read and NEVER persisted to ``kite_trades`` — writing them would
    break the ledger's reconciliation against the broker, which is the one
    check that can detect missing data.
    """
    if today is None:
        today = datetime.now(IST).date()
    out = []
    spot_cache = {}
    for symbol, lots in sorted(fifo_open_lots(trades).items()):
        parsed = _symbol_expiry_key(symbol)
        if parsed is None:
            continue
        underlying, expiry = parsed
        if not isinstance(expiry, date) or expiry >= today:
            continue          # still live: a real close is still expected

        try:
            _, _, strike, option_type = split_symbol(symbol)
        except Exception:                    # noqa: BLE001
            continue

        cache_key = (underlying, expiry)
        if cache_key not in spot_cache:
            spot_cache[cache_key] = spot_fn(underlying, expiry.isoformat())
        settlement = spot_cache[cache_key]
        if not settlement or settlement.get('spot') is None:
            if logger:
                logger.warning(
                    f"Expiry settlement: no spot recorded for {underlying} on "
                    f"{expiry}; {symbol} keeps {sum(l[0] for l in lots)} "
                    f"unsettled and its realised P&L stays understated"
                )
            continue

        value = intrinsic_value(option_type, strike, settlement['spot'])
        if value is None:
            continue
        for idx, (qty, _price, side) in enumerate(lots):
            closing_side = "BUY" if side == "SELL" else "SELL"
            out.append({
                "tradingsymbol": symbol,
                "transaction_type": closing_side,
                "quantity": qty,
                "average_price": value,
                "exchange": LEDGER_EXCHANGE,
                # Sorts after every real fill for that expiry day.
                "fill_timestamp": f"{expiry.isoformat()} 23:59:59",
                "trade_id": f"SETTLE-{symbol}-{idx}",
                "synthetic": True,
            })
        if logger:
            logger.info(
                f"Expiry settlement: {symbol} settled at intrinsic {value:.2f} "
                f"(spot {settlement['spot']} @ {settlement.get('snapshot_time')})"
            )
    return out


def orphan_lots(trades, broker_net):
    """``{(underlying, expiry): [symbols]}`` for residual lots the broker does
    not corroborate.

    A residual lot on a LIVE expiry that the broker does not hold means the
    matching opening fill predates the ledger: FIFO filed the closing fill as a
    fresh open lot and reported realised = 0, so that book's realised P&L is
    understated by an amount no available source can reconstruct (the price
    exists only in Kite Console's trade history, not the API).

    ``broker_net`` is ``{tradingsymbol: net_qty}`` from the live position book.
    Callers should surface these books as INCOMPLETE rather than trusting their
    figure.

    EXPIRED symbols are included, deliberately. They are absent from the broker
    (so ``broker_net`` reports 0) and this deployment closes every position
    before expiry day — so a residual lot on an expired book is by definition a
    data gap, not a held position. Flagging it is the whole point: it is the
    only signal that fills were missed, since the broker can no longer
    corroborate a contract that has ceased to exist.

    (If ``expiry_settlement_fills`` has been applied first, it will have closed
    those lots and they will not appear here. That is why settlement defaults
    off — enabling it hides exactly the gaps this detects.)
    """
    if broker_net is None:
        return {}
    out = defaultdict(list)
    for symbol, lots in fifo_open_lots(trades).items():
        parsed = _symbol_expiry_key(symbol)
        if parsed is None:
            continue
        ledger_qty = sum(q if s == "BUY" else -q for q, _p, s in lots)
        if ledger_qty == broker_net.get(symbol, 0):
            continue
        out[parsed].append(symbol)
    return {k: sorted(v) for k, v in out.items()}


def net_realized_by_expiry(trades, charge_per_trade=CHARGE_PER_TRADE,
                           logger=None, sort=True):
    """``{(underlying, expiry): net_realised}`` for ``trades``.

    Net of an approximated ``charge_per_trade`` per fill. Charges are counted
    over EVERY fill, including ones that produced no FIFO match — an expiry
    with only opens still shows its negative charge footprint, which is why
    the two keysets are unioned rather than intersected.

    ``sort`` re-orders the fills chronologically first (see ``sort_fills``) and
    defaults to True so callers get a correct answer without having to know
    that FIFO is order-sensitive. Pass ``sort=False`` only to measure the
    effect of a given input order.

    Pure: no database, no config. ``trades`` must already be filtered to the
    relevant user and exchange (see ``realized_pnl_by_expiry``).
    """
    if sort:
        trades = sort_fills(trades)
    gross_by_expiry = aggregate_realized_by_expiry(
        fifo_realized_pnl(trades), logger=logger
    )

    fills_by_expiry = defaultdict(int)
    for t in trades:
        # Synthetic expiry-settlement fills are derived, not executed, so they
        # incur no brokerage. Charging them would invent a cost that the broker
        # never billed — and settling a book would then look like a small loss.
        if t.get("synthetic"):
            continue
        key = _symbol_expiry_key(t.get("tradingsymbol") or "")
        if key is not None:
            fills_by_expiry[key] += 1

    net_by_expiry = {}
    for key in set(gross_by_expiry) | set(fills_by_expiry):
        gross = gross_by_expiry.get(key, 0.0)
        fills = fills_by_expiry.get(key, 0)
        net_by_expiry[key] = gross - fills * charge_per_trade
    return net_by_expiry


def realised_summary(user, trades=None, charge_per_trade=CHARGE_PER_TRADE,
                     today=None):
    """JSON-ready per-book realised P&L breakdown, for the dashboard.

    Same FIFO figures the risk engine uses, but split into ``gross`` /
    ``charges`` / ``realised`` and annotated per book, because a single net
    number hides whether a book is down on the trade or just on brokerage.

    Deliberately broker-free so this can be served from ``db_api.py`` without
    a Kite call on every dashboard poll. That costs one thing: for a LIVE book,
    residual lots are simply the open position and say nothing. For an EXPIRED
    book they are unambiguous — this deployment closes everything before expiry
    day, so residue means fills are missing — and that case is flagged via
    ``expired`` + ``unmatched_symbols`` with no broker needed.

    Returns::

        {'user', 'charge_per_trade', 'total_realised',
         'books': [{'underlying', 'expiry', 'gross', 'charges', 'fills',
                    'realised', 'expired', 'unmatched_symbols'}, ...]}

    ``expiry`` is an ISO date string so the payload is JSON-serialisable.
    Books are newest expiry last.
    """
    if today is None:
        today = datetime.now(IST).date()
    if trades is None:
        trades = read_trades(user=user)
    ledger = sort_fills([t for t in trades
                         if t.get("exchange") == LEDGER_EXCHANGE])

    gross_by_book = aggregate_realized_by_expiry(fifo_realized_pnl(ledger))
    fills_by_book = defaultdict(int)
    for t in ledger:
        key = _symbol_expiry_key(t.get("tradingsymbol") or "")
        if key is not None:
            fills_by_book[key] += 1

    residual_by_book = defaultdict(list)
    for symbol in fifo_open_lots(ledger):
        key = _symbol_expiry_key(symbol)
        if key is not None:
            residual_by_book[key].append(symbol)

    books = []
    for key in sorted(set(gross_by_book) | set(fills_by_book),
                      key=lambda k: (str(k[1]), k[0])):
        underlying, expiry = key
        gross = round(gross_by_book.get(key, 0.0), 2)
        fills = fills_by_book.get(key, 0)
        charges = round(fills * charge_per_trade, 2)
        books.append({
            "underlying": underlying,
            "expiry": expiry.isoformat() if isinstance(expiry, date) else str(expiry),
            "gross": gross,
            "charges": charges,
            "fills": fills,
            "realised": round(gross - charges, 2),
            "expired": isinstance(expiry, date) and expiry < today,
            "unmatched_symbols": sorted(residual_by_book.get(key, [])),
        })

    return {
        "user": user,
        "charge_per_trade": charge_per_trade,
        "total_realised": round(sum(b["realised"] for b in books), 2),
        "books": books,
    }


def realized_pnl_by_expiry(user, logger=None,
                           charge_per_trade=CHARGE_PER_TRADE, trades=None,
                           settle_expired=False, spot_fn=None, today=None,
                           broker_net=None):
    """Realised P&L per ``(underlying, expiry_date)`` for ``user``, read from
    the local ``kite_trades`` ledger.

    ``trades`` may be supplied to bypass the read (tests, or a caller that
    already holds the ledger). Otherwise every persisted fill for ``user`` is
    read via ``read_trades`` and filtered to ``LEDGER_EXCHANGE``.

    ``settle_expired`` is OFF by default. This deployment closes every position
    before expiry day, so a residual lot on an expired book is a data gap, not
    a held position — settling it would book a fabricated P&L and would also
    suppress the orphan warning that is the only way to notice the gap. Turn it
    on only for a confirmed unintended expiry. ``spot_fn`` overrides the
    ``db_queries.settlement_spot`` lookup and ``today`` the expiry cutoff; both
    exist for testing.

    ``broker_net`` (``{symbol: net_qty}``) enables orphan detection: books
    holding residual lots the broker doesn't confirm are logged as INCOMPLETE,
    because their opening fills predate the ledger and their realised figure is
    understated by an amount no available source can reconstruct.

    ``logger`` is optional and defaults to silent: this is called once per
    monitor cycle (every ~30s), so per-expiry logging belongs to callers that
    actually want it — brain.py's daily pass, or a reconciliation run. Note
    that INCOMPLETE warnings are only emitted when a logger is supplied.

    Returns ``{}`` when the ledger is empty or absent. That is a legitimate
    fresh-deployment state, but note it is INDISTINGUISHABLE from "no fills
    yet" — callers must not treat an empty result as "this book has no
    realised loss" without first confirming the ledger covers the book.
    """
    if trades is None:
        trades = read_trades(user=user)
    ledger = [t for t in trades if t.get("exchange") == LEDGER_EXCHANGE]
    if not ledger:
        if logger:
            logger.info(
                f"FIFO ledger: no {LEDGER_EXCHANGE} fills persisted for {user}"
            )
        return {}

    # Value anything that expired without a closing trade, then note any book
    # whose residual lots the broker doesn't corroborate. Order matters:
    # settlement consumes expired lots, so orphan detection afterwards sees
    # only genuinely unexplained ones.
    settlements = []
    if settle_expired:
        settlements = expiry_settlement_fills(
            ledger, spot_fn or settlement_spot, today=today, logger=logger)
    combined = ledger + settlements

    result = net_realized_by_expiry(
        combined, charge_per_trade=charge_per_trade, logger=logger
    )
    incomplete = orphan_lots(combined, broker_net)

    if logger:
        logger.info(
            f"FIFO ledger: {len(ledger)} {LEDGER_EXCHANGE} fill(s) for {user}"
            + (f" + {len(settlements)} expiry settlement(s)" if settlements else "")
            + f" across {len(result)} book(s)"
        )
        for (underlying, expiry), net in sorted(result.items(),
                                                key=lambda kv: str(kv[0])):
            flag = "  INCOMPLETE" if (underlying, expiry) in incomplete else ""
            logger.info(f"  {underlying}@{expiry}: net={net:+.2f}{flag}")
        for (underlying, expiry), symbols in sorted(incomplete.items(),
                                                    key=lambda kv: str(kv[0])):
            logger.warning(
                f"INCOMPLETE realised P&L for {underlying}@{expiry}: the ledger "
                f"holds residual lots the broker does not confirm for "
                f"{symbols}. Their opening fills predate the ledger, so this "
                f"book's realised figure is understated by an unknown amount "
                f"and must not be treated as authoritative."
            )
    return result
