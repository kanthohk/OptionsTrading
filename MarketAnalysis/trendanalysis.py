"""Market-trend classification from RSI.

One pure function, ``current_trend``, which turns an RSI reading into one of
three verdicts. It deliberately owns no data access: the caller supplies the
RSI, so the same classification is available to the risk engine (which reads
RSI over HTTP from ``/api/index_summary``), to the dashboard, and to tests —
without any of them needing a database handle.

The RSI itself is Wilder's, computed by ``helpers.basic.wilder_rsi`` over
daily closes and surfaced per index by
``MarketAnalysis.db_queries.get_index_summary``.

Why the risk engine cares: two of its actions only make sense in a
range-bound market. Rolling a threatened short back OUT to
``delta_high_open`` re-establishes a short at a strike the market is walking
toward — profitable if spot stops, expensive if it keeps going. Completing a
one-sided book into an iron condor sells the other wing, which is a bet on
both wings expiring worthless. Both are non-directional trades, so both are
gated on a non-directional reading.
"""

# Verdicts. Strings rather than an enum because they are logged verbatim and
# written to the adjustments audit table.
NON_DIRECTIONAL = 'non_directional'
UPWARDS = 'upwards'
DOWNWARDS = 'downwards'

# DEFAULT inclusive band treated as range-bound: 35 <= RSI <= 65. These are
# fallbacks, not the operating values — the risk engine reads
# ``adjustment_info.rsi_non_directional_low`` / ``_high`` from
# Monitor/config.yaml each cycle and passes them in. They still matter: they
# are what applies when the config keys are absent (an un-updated
# deployment) or unreadable, and they keep this module usable standalone.
RSI_NON_DIRECTIONAL_LOW = 35
RSI_NON_DIRECTIONAL_HIGH = 65


def rsi_band(adjustment_info, on_warning=None):
    """``(low, high)`` from an ``adjustment_info`` mapping.

    Reads ``rsi_non_directional_low`` / ``_high``, falling back to the module
    defaults for absent or non-numeric values so a config predating these
    keys keeps the behaviour it had.

    ``on_warning`` is called with a message for each problem found; pass the
    logger's ``warning`` from a long-running process, or omit it where there
    is nothing useful to log to.

    ``low > high`` is reported but NOT corrected. An empty band means no
    reading can be non-directional, which stands the gated actions down —
    a documented way to switch them off. Silently widening it back to the
    defaults would re-enable trading that had deliberately been stopped.

    Lives here, beside ``current_trend``, because two processes read this
    band: the risk engine that acts on it and the dashboard that colours the
    RSI by it. The page carried its own hardcoded copy once, and it was a
    different band from the engine's.
    """
    adjustment_info = adjustment_info or {}
    low, high = RSI_NON_DIRECTIONAL_LOW, RSI_NON_DIRECTIONAL_HIGH
    for key, default in (('rsi_non_directional_low', low),
                         ('rsi_non_directional_high', high)):
        raw = adjustment_info.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            if on_warning:
                on_warning(f"adjustment_info.{key}={raw!r} is not a number; "
                           f"using the default {default}.")
            value = float(default)
        if key.endswith('_low'):
            low = value
        else:
            high = value
    if low > high and on_warning:
        on_warning(
            f"adjustment_info RSI band is empty (low={low} > high={high}), so "
            f"no reading can be non-directional: the outward roll and condor "
            f"completion are both switched off. Intended? If not, the two "
            f"keys are the wrong way round."
        )
    return low, high


def current_trend(indexname, RSI, low=RSI_NON_DIRECTIONAL_LOW,
                  high=RSI_NON_DIRECTIONAL_HIGH):
    """Classify ``indexname``'s trend from its ``RSI``.

    ``low`` / ``high`` are the INCLUSIVE bounds of the non-directional band.
    They default to the module constants so a two-argument call — the
    original signature — keeps working; the risk engine passes the
    configured pair.

    A ``low`` greater than ``high`` makes the band empty, so every reading
    is directional and both gated actions stop firing. That is deliberate
    and documented as the way to switch them off; it is validated at the
    config boundary (``RiskManager._rsi_band``) rather than here, so this
    stays a total function that cannot raise into the trading loop.

    Returns:
        ``NON_DIRECTIONAL`` when ``low <= RSI <= high``, ``DOWNWARDS``
        below that band, ``UPWARDS`` above it — and ``None`` when the
        trend is UNKNOWN.

    ``None`` is a fourth outcome the three-way spec doesn't mention, and it
    exists because the input genuinely can be absent: ``get_index_summary``
    returns ``rsi=None`` until the database holds ``rsi_period + 1`` days of
    closes for that index, which is the normal state for a newly recorded
    underlying (BANKNIFTY's series starts when ``banknifty_spot`` did, not
    when the table was created). Callers must not read ``None`` as
    "non-directional" — an unknown trend is not a range-bound one, and every
    caller here refuses to act on it. Returning 50.0 or defaulting to
    non-directional would have converted missing data into a live trade.

    ``indexname`` does not affect the result today: the band is the same for
    every index. It is on the signature because the caller always knows which
    index it is asking about, and threading it through now means adding
    per-index bands later is a change to this function alone rather than to
    every call site. It is also what makes the log line at each call site
    self-describing.
    """
    if RSI is None:
        return None
    try:
        rsi = float(RSI)
    except (TypeError, ValueError):
        # A non-numeric RSI is a bug upstream, not a trend. Same treatment as
        # absent: unknown, and therefore no trade.
        return None
    if rsi < low:
        return DOWNWARDS
    if rsi > high:
        return UPWARDS
    return NON_DIRECTIONAL
