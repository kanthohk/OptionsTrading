"""Per-underlying facts, in one place.

Before this module the project was NIFTY-only and the constants that
differ between indices were scattered as literals: ``STRIKE_STEP = 50``
in the recorder, ``_STRIKE_STEP = 50`` on ``RiskManager``, if/elif chains
mapping ``'NIFTY' -> 'NIFTY 50'`` in ``risk_management._spot_price``, and
an ``EXPIRY_WEEKDAY`` dict inside ``helpers.basic.split_symbol``. Adding a
second index meant finding every one of them.

Everything that varies per underlying now lives in ``UNDERLYINGS`` below,
so supporting a third index (FINNIFTY, SENSEX) is a matter of adding one
entry rather than hunting literals.

Field notes:

``nse_symbol``
    The spot quote symbol Kite expects on the NSE exchange. Note it does
    NOT match the derivatives prefix — BANKNIFTY options are prefixed
    ``BANKNIFTY`` but the index quote is ``NSE:NIFTY BANK``. That mismatch
    is exactly the kind of thing this table exists to record.

``strike_step``
    Strike interval in points. Governs ATM rounding, the strike ladder the
    recorder queries, and the step size when the risk engine walks strikes
    looking for a delta target. Getting this wrong doesn't error — it
    silently generates symbols for strikes that don't exist, which come
    back as missing quotes.

``lot_size``
    Exchange lot size, used for position sizing. These change when the
    exchange revises contract specs, so they're overridable from
    ``Monitor/config.yaml`` (see ``load_underlying_overrides``) rather than
    being edited here on every revision.

``expiry_weekday``
    Weekday of weekly/monthly expiry, Monday=0. Mirrors the table that was
    inline in ``split_symbol``.

``expiry_cadence``
    ``'weekly'`` or ``'monthly'`` — whether this underlying lists weekly
    expiries at all, or only the monthly one. NSE's 2024 rationalisation
    left weeklies on NIFTY alone; BANKNIFTY and FINNIFTY are monthly-only.
    Getting this wrong is not a visible error: the recorder happily builds
    symbols for weekly contracts that were never listed, Kite returns no
    quote for them, and the underlying silently records little or nothing.
    Because the exchange has changed this rule before, it is overridable
    from config so reacting doesn't require a code edit.

``ladder_points``
    How far either side of ATM the recorder quotes, in index POINTS (not
    strikes) — points are grid-independent, so the same number means the
    same market coverage on a 50-pt and a 100-pt grid.

    This has to reach past the lowest |delta| the adjustments engine acts
    on, or an inward-roll target simply won't be in the database. At a
    54,000 BANKNIFTY on a ~25-DTE monthly, |delta| 5% (``delta_low_close``)
    sits ~3,100 points OTM, so the old ±3,000 fell just short.

``has_futures``
    Whether the recorder should try to quote a futures contract for this
    underlying.
"""
from collections import OrderedDict

# Canonical registry. Keys are the derivatives prefix used in Kite
# tradingsymbols (``NIFTY26JUN24500PE``), which is also what
# ``helpers.basic.split_symbol`` returns as the underlying.
UNDERLYINGS = OrderedDict([
    ('NIFTY', {
        'nse_symbol': 'NIFTY 50',
        'strike_step': 50,
        'ladder_points': 1500,
        'lot_size': 65,
        'expiry_weekday': 1,      # Tuesday
        'expiry_cadence': 'weekly',
        'has_futures': True,
    }),
    ('BANKNIFTY', {
        'nse_symbol': 'NIFTY BANK',
        'strike_step': 100,
        'ladder_points': 5000,
        # 30 as of Aug 2026. NSE revises contract specs periodically, and a
        # stale value here is not a visible error: it silently mis-sizes the
        # stop-loss threshold and makes partial-reduce/re-add compute order
        # quantities that aren't a whole number of lots.
        'lot_size': 30,
        'expiry_weekday': 1,      # Tuesday
        'expiry_cadence': 'monthly',   # weeklies discontinued
        'has_futures': True,
    }),
    ('FINNIFTY', {
        'nse_symbol': 'NIFTY FIN SERVICE',
        'strike_step': 50,
        'ladder_points': 1500,
        'lot_size': 65,
        'expiry_weekday': 1,      # Tuesday
        'expiry_cadence': 'monthly',   # unverified — not currently recorded
        'has_futures': True,
    }),
    ('SENSEX', {
        'nse_symbol': 'SENSEX',
        'strike_step': 100,
        'ladder_points': 3000,
        'lot_size': 20,
        'expiry_weekday': 3,      # Thursday
        'expiry_cadence': 'weekly',    # unverified — not currently recorded
        'has_futures': False,
    }),
])

WEEKLY = 'weekly'
MONTHLY = 'monthly'

# The underlying assumed when a caller doesn't name one. Every read-side
# query and API endpoint defaults to this, so existing NIFTY-only callers
# (and the historical rows backfilled by ``migrate_db.py``) keep working
# unchanged.
DEFAULT_UNDERLYING = 'NIFTY'

# Fallbacks for an underlying absent from the registry. Deliberately
# NIFTY-ish rather than zeros — a wrong-but-plausible step still produces
# valid-looking symbols that fail visibly at quote time, whereas a 0 step
# would raise deep inside ATM rounding.
_FALLBACK = {
    'nse_symbol': None,
    'strike_step': 50,
    'ladder_points': 1500,
    'lot_size': 0,
    'expiry_weekday': 1,
    # Monthly is the conservative default for an unknown underlying: asking
    # for monthlies that exist costs one wasted expiry at worst, whereas
    # assuming weeklies generates a whole ladder of phantom symbols.
    'expiry_cadence': MONTHLY,
    'has_futures': False,
}


def normalize(underlying):
    """Upper-case and strip ``underlying``, falling back to the default.

    Accepts ``None``/blank so callers can pass a raw query param straight
    through without their own guard.
    """
    if underlying is None:
        return DEFAULT_UNDERLYING
    cleaned = str(underlying).strip().upper()
    return cleaned or DEFAULT_UNDERLYING


def is_known(underlying):
    """True iff ``underlying`` has a registry entry."""
    return normalize(underlying) in UNDERLYINGS


def spec(underlying):
    """Full spec dict for ``underlying``.

    Unknown underlyings get ``_FALLBACK`` with ``nse_symbol`` defaulted to
    the name itself — enough for an equity or an index this table hasn't
    been taught about yet, without raising.
    """
    name = normalize(underlying)
    entry = UNDERLYINGS.get(name)
    if entry is None:
        fallback = dict(_FALLBACK)
        fallback['nse_symbol'] = name
        return fallback
    return dict(entry)


def nse_symbol(underlying):
    """Kite NSE spot-quote symbol, e.g. ``'NIFTY BANK'`` for BANKNIFTY."""
    return spec(underlying)['nse_symbol']


def strike_step(underlying):
    """Strike interval in points."""
    return int(spec(underlying)['strike_step'])


def lot_size(underlying):
    """Exchange lot size (0 when unknown — callers must treat that as
    'cannot size' rather than as a valid quantity)."""
    return int(spec(underlying)['lot_size'])


def expiry_weekday(underlying):
    """Weekday of expiry, Monday=0."""
    return int(spec(underlying)['expiry_weekday'])


def expiry_cadence(underlying):
    """``'weekly'`` or ``'monthly'``. Anything unrecognised is treated as
    monthly — the conservative choice, since over-requesting weeklies
    produces phantom symbols rather than a visible error."""
    value = str(spec(underlying).get('expiry_cadence') or MONTHLY).strip().lower()
    return WEEKLY if value == WEEKLY else MONTHLY


def has_weekly_expiries(underlying):
    """True iff this underlying lists weekly option expiries."""
    return expiry_cadence(underlying) == WEEKLY


def has_futures(underlying):
    """Whether a futures contract should be quoted for this underlying."""
    return bool(spec(underlying)['has_futures'])


def ladder_points(underlying):
    """Half-width of the recorder's strike ladder, in index points."""
    return int(spec(underlying)['ladder_points'])


def strikes_each_side(underlying, hard_cap=None):
    """How many strikes either side of ATM to quote.

    Derived from ``ladder_points / strike_step``, so the ladder is
    configured in market terms and the grid conversion happens here.
    ``hard_cap`` bounds the result — a safety rail so a mistyped
    ``ladder_points`` can't expand the quote batch without limit.
    """
    step = strike_step(underlying)
    if step <= 0:
        return 0
    count = ladder_points(underlying) // step
    if hard_cap is not None:
        count = min(count, int(hard_cap))
    return max(count, 0)


def atm_strike(underlying, spot):
    """Round ``spot`` to the nearest tradable strike for ``underlying``."""
    step = strike_step(underlying)
    return int(round(float(spot) / step) * step)


def strike_ladder(underlying, spot, steps_each_side):
    """Ascending list of strikes spanning ``steps_each_side`` either side
    of ATM. Used by the recorder to build its quote batch."""
    step = strike_step(underlying)
    atm = atm_strike(underlying, spot)
    return [atm + i * step
            for i in range(-steps_each_side, steps_each_side + 1)]


def load_underlying_overrides(config):
    """Apply per-underlying overrides from a parsed ``Monitor/config.yaml``.

    Reads the optional top-level ``underlyings`` block::

        underlyings:
          record: [NIFTY, BANKNIFTY]
          NIFTY:
            lot_size: 75
          BANKNIFTY:
            strike_step: 100
            lot_size: 30
            expiry_cadence: monthly

    Overridable: ``strike_step``, ``lot_size``, ``ladder_points``,
    ``nse_symbol`` and ``expiry_cadence``. The cadence is included because
    the exchange has revised which indices carry weeklies more than once,
    and reacting to that should not require a code change and redeploy.

    Not overridable: ``expiry_weekday`` and ``has_futures`` — contract facts
    that don't belong in an operator-edited file. Mutates ``UNDERLYINGS`` in
    place and returns it, so callers holding a reference see the update.

    Unknown keys and unparseable values are ignored rather than raising:
    this runs at recorder startup and a typo in config should not take the
    data pipeline down.
    """
    block = (config or {}).get('underlyings')
    if not isinstance(block, dict):
        return UNDERLYINGS
    for name, overrides in block.items():
        if name == 'record' or not isinstance(overrides, dict):
            continue
        key = normalize(name)
        target = UNDERLYINGS.setdefault(key, dict(_FALLBACK))
        for field in ('strike_step', 'lot_size', 'ladder_points'):
            if field in overrides:
                try:
                    target[field] = int(overrides[field])
                except (TypeError, ValueError):
                    continue
        if overrides.get('nse_symbol'):
            target['nse_symbol'] = str(overrides['nse_symbol'])
        cadence = overrides.get('expiry_cadence')
        if cadence:
            cadence = str(cadence).strip().lower()
            # Only accept the two known values; a typo must not silently
            # flip an underlying to weekly and start generating phantom
            # symbols for contracts that were never listed.
            if cadence in (WEEKLY, MONTHLY):
                target['expiry_cadence'] = cadence
    return UNDERLYINGS


def configured_underlyings(config):
    """Underlyings the recorder should capture, from ``underlyings.record``
    in config. Falls back to ``[DEFAULT_UNDERLYING]`` when the block is
    absent, so an un-migrated config keeps its NIFTY-only behaviour.

    Entries are normalized and de-duplicated with order preserved.
    """
    block = (config or {}).get('underlyings')
    raw = block.get('record') if isinstance(block, dict) else None
    if isinstance(raw, str):
        raw = [p for p in raw.split(',')]
    if not isinstance(raw, (list, tuple)) or not raw:
        return [DEFAULT_UNDERLYING]
    out = []
    for item in raw:
        name = normalize(item)
        if name and name not in out:
            out.append(name)
    return out or [DEFAULT_UNDERLYING]
