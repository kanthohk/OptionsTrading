from datetime import datetime, timedelta, time as dt_time
from dateutil.relativedelta import relativedelta
import re, logging, os, yaml, time, requests
import pytz
from filelock import FileLock, Timeout

from helpers.market_data import proxy_auth_headers

IST = pytz.timezone('Asia/Kolkata')

CONFIG_LOCK_TIMEOUT = 10

# The monitored trading session, in IST. Lives here rather than in
# autotrade.py because two processes need the SAME answer: autotrade decides
# when to start/stop its monitor thread, and main.py's /status derives the
# dashboard's "Monitoring" pill from it. Two copies of these constants would
# drift the moment one is edited.
#
# 15:31 (not 15:30) so the final 15:30 minute is still monitored — the market
# closes at 15:30 and the last cycle needs to see it.
MARKET_START = dt_time(9, 20)
MARKET_STOP = dt_time(15, 31)


def is_market_hours(now=None):
    """True when ``now`` falls inside the monitored session: a weekday with
    ``MARKET_START <= time < MARKET_STOP``.

    ``now`` defaults to the current IST time; pass an aware or naive datetime
    to test boundaries. Note this deliberately does NOT consult
    ``monitor_info.trading_holidays`` — the previous inline check in
    autotrade.py didn't either, so behaviour is unchanged. Holiday awareness
    would be a new feature, and adding it here silently would change when the
    monitor runs.
    """
    if now is None:
        now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return MARKET_START <= now.time() < MARKET_STOP

class ApiError(Exception):
    """Raised when a broker API call fails after exhausting retries or returns
    an unexpected payload shape."""

API_DEFAULT_TIMEOUT = 10
API_DEFAULT_RETRIES = 2
API_DEFAULT_BACKOFF = 1.0

# ntfy priority scale, as AlertMobile.send passes it through:
#   5 max — rings the phone. For things needing a human NOW.
#   3 default — notifies without ringing. For "you should know, not act".
# Everything used to send at 5, which meant a routine hiccup woke you the
# same as a failed order.
ALERT_URGENT = 5
ALERT_INFO = 3


def alert(user, heading, message, logger=None, priority=ALERT_URGENT):
    """Send a push alert, prefixing the heading with ``{user}_``.

    ``priority`` defaults to urgent so existing callers are unchanged; pass
    ``ALERT_INFO`` for conditions the operator should see but need not act on.

    Best-effort: a failing alert channel must never break the caller. If
    AlertMobile raises, we log via ``logger`` (if provided) or fall back to
    print so the operator still sees the failure.
    """
    try:
        from helpers.alert import AlertMobile
        AlertMobile().send(heading=f"{user}_{heading}", message=message,
                           priority=priority)
    except Exception as e:
        msg = f"AlertMobile failed for {heading}: {e} -- original message: {message}"
        if logger is not None:
            logger.error(msg)
        else:
            print(msg)


ORDER_REJECT_RETRIES = 5
ORDER_REJECT_RETRY_SLEEP_S = 30


def place_order(api_url, user, symbol, quantity, transaction_type, exchange,
                config, logger=None, alert_fn=None):
    """Place a broker order via the Flask proxy at ``api_url``.

    Short-circuits to a log-only mode when ``is_dry_run(config, user)`` — the
    deployment-wide ``dry_run`` OR this account's ``accounts.<user>.dry_run``,
    so one account can be watched in dry-run while the others trade live.
    Fires an ``OrderPlacing`` alert just before the broker call so every live
    order is auditable in the mobile channel.

    Retry policy — different for the two failure modes because the blast
    radius is not the same:

    - **Broker rejection** (``success=False`` from the broker — e.g.
      margin briefly low, rate limit, position freeze): retried up to
      ``config['monitor_info']['order_retries']`` times (default 5) with
      ``config['monitor_info']['order_retry_gap_seconds']`` seconds
      between attempts (default 30). Safe because the broker definitively
      rejected — no order is sitting in the book.
    - **Transport failure** (``ApiError``: timeout, network drop, malformed
      response): NOT retried. The original request may have reached the
      broker and been accepted before the response was lost; retrying
      would risk a duplicate fill. The caller is alerted with
      ``State UNKNOWN`` and is expected to reconcile manually.

    Args:
        api_url: Flask proxy base URL.
        user: account identifier.
        symbol/quantity/transaction_type/exchange: order parameters.
        config: live config dict (read at call time so dry_run / retry
            settings take effect immediately).
        logger: optional logger for trace + error messages.
        alert_fn: callable ``(heading, message)`` that fires push alerts. If
            None, alerting is disabled but logging continues.

    Returns the broker's ``order_id`` string on success, ``"DRY_RUN"``
    when dry-run short-circuits the path, or ``None`` on
    transport failure / final rejection. Callers that only need a
    boolean can keep using ``if not result:`` since both "DRY_RUN" and
    any order-id string are truthy; callers that need the order id
    (e.g. the adjustments audit writer) get it directly.
    """
    _alert = alert_fn or (lambda heading, message: None)
    # Per-account, not just global: with one monitor process per account a
    # single shared flag could only ever stop all of them or none.
    if is_dry_run(config, user):
        if logger is not None:
            logger.info(
                f"[DRY RUN] Would {transaction_type} {quantity} of {symbol} ({exchange}) for {user}"
            )
        # Truthy sentinel so callers' ``if not success`` checks still
        # behave correctly, and downstream code (e.g. the adjustments
        # audit row) can distinguish a dry-run "order" from a real fill.
        return "DRY_RUN"

    monitor_info = config.get('monitor_info') or {}
    max_retries = int(monitor_info.get('order_retries', ORDER_REJECT_RETRIES))
    retry_sleep = int(monitor_info.get('order_retry_gap_seconds', ORDER_REJECT_RETRY_SLEEP_S))
    total_attempts = max_retries + 1

    url = (f"{api_url}/place_order?user={user}&symbol={symbol}"
           f"&quantity={quantity}&transaction_type={transaction_type}&exchange={exchange}")

    # Per-order "OrderPlacing" alert intentionally silenced — was firing on
    # every buy/sell leg (~4 per cycle for an iron condor) and drowning the
    # push channel. Failure paths below (OrderApiError / OrderRejected)
    # still alert, so genuine problems remain visible.
    # _alert("OrderPlacing",
    #        f"Placing {transaction_type} order: {quantity} of {symbol} ({exchange})")

    last_payload = None
    for attempt in range(1, total_attempts + 1):
        if attempt > 1 and logger is not None:
            logger.info(
                f"Order retry {attempt}/{total_attempts}: "
                f"{transaction_type} {quantity} of {symbol}"
            )
        try:
            success, payload = api_get(url, retries=0, timeout=15, logger=logger)
        except ApiError as e:
            # Transport failure: NEVER retry. Original request may have
            # reached the broker; retrying could double-fill.
            if logger is not None:
                logger.error(
                    f"{transaction_type} order for {symbol} ({user}) failed: {e}. "
                    f"Order state UNKNOWN — not retrying (would risk duplicate fill). "
                    f"Verify manually."
                )
            _alert("OrderApiError",
                   f"{transaction_type} {symbol} ({user}) API error: {e}. "
                   f"State UNKNOWN — verify manually.")
            return None

        if success:
            # Broker proxy returns ``"Order is placed successfully:NXXXXX"``;
            # peel off the order id so callers (the adjustments audit
            # writer, in particular) can join back to ``kite_trades``.
            # Fall back to a truthy sentinel if parsing fails.
            order_id = None
            if isinstance(payload, str) and ':' in payload:
                order_id = payload.rsplit(':', 1)[-1].strip() or None
            result = order_id or "OK"
            if logger is not None:
                tail = f" (after {attempt - 1} retr{'y' if attempt == 2 else 'ies'})" if attempt > 1 else ""
                logger.info(
                    f"{transaction_type} transaction for {symbol} of {user} "
                    f"is successful (order_id={order_id}){tail}"
                )
            return result

        # Broker rejection — retry-eligible.
        last_payload = payload
        if logger is not None:
            logger.warning(
                f"{transaction_type} order for {symbol} of {user} rejected "
                f"(attempt {attempt}/{total_attempts}): {payload}"
            )
        if attempt < total_attempts:
            if logger is not None:
                logger.info(f"Waiting {retry_sleep}s before retrying...")
            time.sleep(retry_sleep)

    # Exhausted all attempts. One alert with the final rejection text so the
    # push channel doesn't get spammed once per retry.
    if logger is not None:
        logger.error(
            f"{transaction_type} order for {symbol} of {user} rejected "
            f"after {total_attempts} attempts: {last_payload}"
        )
    _alert(
        "OrderRejected",
        f"{transaction_type} {symbol} ({user}) rejected after {total_attempts} attempts: {last_payload}"
    )
    return None


def api_get(url, timeout=API_DEFAULT_TIMEOUT, retries=API_DEFAULT_RETRIES,
            backoff=API_DEFAULT_BACKOFF, logger=None):
    """GET a broker API URL with timeout, retry on transient failure, and
    payload-shape validation.

    The broker returns a JSON list-like payload where index 0 is the success
    flag and index 1 is the data. This helper unpacks and returns
    (success, data). It raises ApiError on network failure, non-2xx HTTP,
    malformed JSON, or an unexpected payload shape. Successful calls where
    the broker reports `success=False` are returned to the caller as
    (False, data) so the caller can decide what to do.

    Retries are exponential. Idempotent reads should keep retries>=1; order
    placement should pass retries=0 to avoid duplicate orders on timeouts.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            # Every caller of this is fetching from main.py, which rejects
            # requests that carry neither a login nor the shared secret.
            response = requests.get(url, headers=proxy_auth_headers(),
                                    timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or len(data) < 2:
                raise ApiError(f"Unexpected response shape from {url}: {data!r}")
            return data[0], data[1]
        except (requests.RequestException, ValueError, ApiError) as e:
            last_err = e
            if logger is not None:
                logger.warning(
                    f"api_get attempt {attempt + 1}/{retries + 1} failed for {url}: {e}"
                )
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    raise ApiError(f"api_get failed after {retries + 1} attempts for {url}: {last_err}")


def last_weekday_of_month(year, month, weekday):
    """Return last given weekday of a month (0=Mon ... 6=Sun)."""
    d = datetime(year, month, 1) + relativedelta(day=31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def hedge_distance_for_dte(dte, hedge_cfg=None):
    """Hedge distance (in strike points) as a function of days-to-expiry.

    Hedges sit wider from the short leg further from expiry and tighten as
    expiry approaches. The distances and DTE boundaries come from the
    ``hedge_info`` block of ``Monitor/config.yaml`` (passed in as
    ``hedge_cfg``); any missing key falls back to the built-in default:

        DTE <= near_max_dte (7)                -> near (100)
        near_max_dte < DTE <= mid_max_dte (14) -> mid  (200)
        DTE >  mid_max_dte (14)                -> far  (300)

    ``dte`` of ``None`` (expiry couldn't be resolved) falls back to ``mid``
    — the historical fixed distance — so callers never crash on a bad date.
    """
    cfg = hedge_cfg or {}
    near = cfg.get('near', 100)
    mid = cfg.get('mid', 200)
    far = cfg.get('far', 300)
    near_max_dte = cfg.get('near_max_dte', 7)
    mid_max_dte = cfg.get('mid_max_dte', 14)
    if dte is None:
        return mid
    if dte <= near_max_dte:
        return near
    if dte <= mid_max_dte:
        return mid
    return far

def snap_to_strike_grid(points, step):
    """Round ``points`` UP to a whole number of ``step``-point strikes.

    Hedge distances are configured as round numbers (100/200/300, or
    200/400/650) that don't necessarily land on the underlying's strike
    grid. An off-grid distance produces a hedge strike that was never
    listed — no error at the point of the mistake, just a quote that comes
    back empty or an order the broker rejects.

    Rounds UP so a wing is never narrower than configured; a hedge one
    strike further out is a smaller risk than one that doesn't exist.
    """
    try:
        points = int(points)
        step = int(step)
    except (TypeError, ValueError):
        return points
    if step <= 0 or points % step == 0:
        return points
    return ((points // step) + 1) * step


RSI_PERIOD = 14


def wilder_rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI of a close series, or ``None`` if there aren't enough bars.

    ``closes`` is oldest-first and must hold at least ``period + 1`` values —
    RSI is computed from the DIFFERENCES between consecutive closes, so N
    closes give only N-1 changes. Returns the single latest value (a float in
    0..100), not the whole series, because that is all the market cards show.

    This is Wilder's original smoothing (the one Kite, TradingView and every
    broker chart default to), NOT a simple moving average of gains/losses:
    the first ``period`` changes seed a plain average, and each change after
    that is folded in with ``(prev * (period - 1) + new) / period``. The
    distinction matters — an SMA version drifts several points away from what
    the user sees on their broker's chart, which would make the dashboard
    figure look broken rather than merely different.

    Because the seed is a plain average of the OLDEST ``period`` changes,
    the result depends slightly on how far back ``closes`` reaches. Wilder
    smoothing converges, so pass a generous window (~100 bars) if you want
    to agree with a chart that has years of history behind it; with the bare
    minimum of ``period + 1`` bars the answer is still a correct RSI, just
    one computed from a short memory.

    Flat series (no gains and no losses in the smoothed window) has no
    defined RS, so ``50.0`` is returned — the neutral reading — rather than
    dividing by zero or claiming an extreme.
    """
    if period is None or period < 1:
        raise ValueError(f"RSI period must be >= 1, got {period!r}")
    if not closes or len(closes) < period + 1:
        return None

    avg_gain = avg_loss = 0.0
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        if i <= period:
            # Seed: plain average over the first `period` changes. Divide as
            # we go so the loop stays single-pass.
            avg_gain += gain / period
            avg_loss += loss / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def reprice_option_symbol(symbol, old_strike, new_strike):
    """Swap the strike inside a Kite option tradingsymbol.

    Anchored at the trailing ``CE``/``PE`` rather than using a bare
    ``str.replace``: the strike's digits can also occur inside the expiry
    code, which is digit-heavy for weeklies (``BANKNIFTY26O0950900CE``).
    Rewriting the leftmost match there yields a symbol for a contract that
    was never listed — which surfaces much later as a missing quote or a
    rejected order, not as an error at the point of the mistake.

    Falls back to a plain replace when the symbol doesn't end in the
    expected ``<strike><CE|PE>`` shape, preserving the older behaviour.
    """
    old = str(old_strike)
    head, suffix = symbol[:-2], symbol[-2:]
    if suffix in ('CE', 'PE') and head.endswith(old):
        return f"{head[:-len(old)]}{new_strike}{suffix}"
    return symbol.replace(old, str(new_strike))


# Kite's weekly expiry code carries the month as a SINGLE character: the bare
# digit for Jan-Sep, then O/N/D for Oct/Nov/Dec — a two-digit 10/11/12 would
# run into the day field and make YY+MM+DD unparseable. Inverse of
# helpers.market_data._MONTH_CODE, which builds the same code when the
# recorder constructs symbols. Kept as its own map rather than imported so
# this module stays free of market_data's scipy/requests dependencies.
_WEEKLY_MONTH_CODES = {'O': 10, 'N': 11, 'D': 12}


def split_symbol(symbol):
    """Parse KiteConnect option tradingsymbol into components.

    Returns ``(underlying, expiry_datetime, strike, opt_type)``, or None
    when the symbol doesn't parse — callers treat None as "not one of my
    option legs", so a stray equity or a format change must not raise
    mid-cycle.
    """
    EXPIRY_WEEKDAY = {
        "SENSEX": 3,  # Thursday
        "NIFTY": 1,  # Tuesday
        "BANKNIFTY": 1,  # Tuesday
        "FINNIFTY": 1,  # Tuesday
    }
    match = re.match(r"([A-Z]+)(.+?)(CE|PE)?$", symbol)
    if not match:
        return None
    underlying, middle, opt_type = match.groups()
    if re.search(r"[A-Z]{3}", middle):
        yy = int(middle[:2])
        mon_str = middle[2:5]
        strike = int(middle[5:])
        year = 2000 + yy
        month = datetime.strptime(mon_str, "%b").month
        expiry_dt = last_weekday_of_month(year, month, EXPIRY_WEEKDAY.get(underlying, 1))
    else:
        # Weekly expiry: YY + month code + DD + strike, e.g.
        # NIFTY2592324900PE (23-Sep-2025) or NIFTY26O1322500PE
        # (13-Oct-2026 — see _WEEKLY_MONTH_CODES for the O/N/D months).
        date_part = middle[:-5] # assuming strike is 5 digit
        mon_code = date_part[2:-2]
        try:
            dd = int(date_part[-2:])
            mm = _WEEKLY_MONTH_CODES.get(mon_code)
            if mm is None:
                mm = int(mon_code) if len(mon_code) > 0 else 1
            yy = int(date_part[:2])
            strike = int(middle[-5:])
            expiry_dt = datetime(2000+yy, mm, dd)
        except ValueError:
            return None
    return underlying, expiry_dt, strike, opt_type

class SyncFileHandler(logging.FileHandler):
    """FileHandler that flushes and syncs every log record."""
    def emit(self, record):
        super().emit(record)  # normal logging
        self.stream.flush()  # flush Python buffer
        os.fsync(self.stream.fileno())  # flush OS buffer


# Formats a broker timestamp can arrive in. Kite's client parses these fields
# into datetime objects; Flask then serialises datetime to an RFC 1123 HTTP
# date, so that is what actually reaches the DB via /get_trades — NOT ISO.
_BROKER_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",          # ISO-ish, what we normalise TO
    "%Y-%m-%dT%H:%M:%S",
    "%a, %d %b %Y %H:%M:%S %Z",   # RFC 1123 with timezone, e.g. Flask's output
    "%a, %d %b %Y %H:%M:%S",      # RFC 1123 without
    "%Y-%m-%d",
)

BROKER_TS_CANONICAL = "%Y-%m-%d %H:%M:%S"


def parse_broker_timestamp(value):
    """Parse a broker timestamp string to ``datetime``, or None.

    Accepts both ISO and RFC 1123 ("Fri, 03 Jul 2026 09:20:21 GMT"). Returns
    None rather than raising: a single unparseable fill must not take down a
    trading cycle.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _BROKER_TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def normalise_broker_timestamp(value):
    """Return ``value`` as ``'YYYY-MM-DD HH:MM:SS'``, or unchanged if it can't
    be parsed.

    Why this matters: ``kite_trades.fill_timestamp`` is a TEXT column and
    ``read_trades`` orders by it. An RFC 1123 string sorts ALPHABETICALLY BY
    WEEKDAY NAME (Fri < Mon < Sat < Sun < Thu < Tue < Wed), so the ledger comes
    back in seven weekday blocks rather than chronologically — and FIFO
    matching, which is order-dependent by definition, then pairs the wrong
    lots. Normalising on write makes the TEXT sort chronological.

    Unparseable values pass through untouched so nothing is silently destroyed;
    they surface in tools/audit_ledger.py instead.
    """
    parsed = parse_broker_timestamp(value)
    if parsed is None:
        return value
    return parsed.strftime(BROKER_TS_CANONICAL)


# Characters allowed in an account name that is about to become part of a
# filename. Deliberately strict: ``watch_log_path`` is reached from
# ``/get_watch_log?user=`` and ``/status?user=`` on a Flask app bound to
# 0.0.0.0:80, so an unfiltered name would turn the log reader into an
# arbitrary-file reader (``?user=../../etc/passwd``). Account names are
# credential keys — alphanumerics with the odd dot, dash or underscore — so
# nothing legitimate is excluded.
_UNSAFE_USER_CHARS = re.compile(r'[^A-Za-z0-9._-]')


def watch_log_path(user=None, *, now=None):
    """Filename of ``user``'s monitor log for ``now``'s IST date.

    Single source of truth for this convention: ``setup_logger`` writes the
    file, main.py's ``/get_watch_log`` serves it, and ``/status`` reads its
    mtime as that account's heartbeat. Three hard-coded copies would drift.

    Scoped per account because the monitor now runs one process per user.
    Sharing one file across them cost more than tidiness: the three processes
    interleaved their lines, and ``/status`` read the single mtime as *the*
    heartbeat — so one account's monitor could be dead for a week while the
    pill stayed green on another's writes.

    ``user`` falsy yields the legacy unscoped ``{date}_watch.log``, so a
    deployment with no account configured still resolves to something rather
    than raising in the heartbeat path.

    ``now`` is keyword-only on purpose. It used to be the first positional
    parameter, and callers passed it that way; letting a stray
    ``watch_log_path(some_datetime)`` bind to ``user`` would have produced a
    plausible-looking filename for an account that does not exist. The
    isinstance guard below turns that mistake into an error instead.
    """
    if isinstance(user, datetime):
        raise TypeError(
            "watch_log_path(user, *, now=...): `now` is keyword-only now that "
            "the log is per-account. Call watch_log_path(now=that_datetime)."
        )
    if now is None:
        now = datetime.now(IST)
    daystr = now.strftime('%Y%m%d')
    # Strip BEFORE testing for emptiness: a whitespace-only name is truthy,
    # and treating it as an account produced "{date}__watch.log" — a real file
    # that no monitor writes and no reader would think to look for.
    name = "" if user is None else str(user).strip()
    if not name:
        return f"{daystr}_watch.log"
    if _UNSAFE_USER_CHARS.search(name):
        raise ValueError(
            f"refusing to build a log filename from account name {user!r}: "
            f"only letters, digits, dot, dash and underscore are allowed"
        )
    # Dot is allowed above (an account name may contain one), which on its own
    # would let "." and ".." through. Those can't name a real account, and
    # requiring one alphanumeric rejects them — along with "-", "__" and the
    # rest of the punctuation-only names — without banning "my.account-2".
    if not any(c.isalnum() for c in name):
        raise ValueError(
            f"refusing to build a log filename from account name {user!r}: "
            f"an account name must contain at least one letter or digit"
        )
    return f"{daystr}_{name}_watch.log"


def watch_log_last_modified(user=None, *, now=None):
    """Last write to ``user``'s watch log today as ``'YYYY-MM-DD HH:MM'`` IST,
    or None if that monitor hasn't written anything yet today.

    This replaces the old ``monitor_info.last_watched`` config key as the
    monitor's heartbeat. It's a better signal for the same cost: options.py
    logs on every cycle, so the mtime is *evidence* that account's loop ran,
    whereas the config key was a value the loop wrote about itself — and
    which went stale silently the moment the writer stopped.

    Returns None rather than raising on a rejected account name: this is the
    heartbeat path, and "no recent write" is the honest answer for a name
    that cannot own a log file.
    """
    try:
        mtime = os.path.getmtime(watch_log_path(user, now=now))
    except (OSError, ValueError):
        return None
    return datetime.fromtimestamp(mtime, IST).strftime('%Y-%m-%d %H:%M')


def setup_logger(user):
    """Build a per-user, per-day file logger.

    The logger name embeds the IST date so a long-running process gets a fresh
    file each calendar day. Existing handlers on the same name are cleared so
    repeat construction (e.g. on monitor restart) doesn't double-log.
    """
    daystr = datetime.now(IST).strftime('%Y%m%d')
    logger_name = f"{user}_{daystr}_logger"
    # Per-account file, not just a per-account logger NAME. These two lines
    # disagreeing is what put three monitors into one log.
    logfile = watch_log_path(user)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        logger.handlers.clear()

    fh = SyncFileHandler(logfile, mode='a')
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    logger.propagate = False

    return logger


def get_config(config_file):
    with open(config_file, "r") as file:
        config = yaml.safe_load(file)
    return config


# ---- Account identity -----------------------------------------------------
#
# Two different questions, previously answered by one hardcoded name in
# main.py (``SUPER_USER = "sashi"``):
#
#   trading_account   — WHOSE book is this? Scopes the per-account tables
#                       (kite_trades, adjustments) and owns the orders.
#   market_data_account — WHOSE Kite session do we pull quotes with? An LTP
#                       is the same for everybody; this is just the account
#                       that holds the market-data subscription.
#
# Both come from config so that changing ``api_info.users`` moves the book
# without touching code, while quotes keep flowing through the subscribed
# account. Neither applies any case transformation: the same string keys
# ``TradingBroker/config.yaml``'s credentials block and is written into the
# ledger, so folding case here would break one or the other.

def configured_users(config):
    """Every account listed in ``api_info.users``, in order, each stripped.

    One parser for a key that is read in five places. It used to be split
    ad hoc at each site and they did not agree: ``trading_account`` stripped,
    ``fetch_db_data`` stripped, but ``Monitor/options.py``'s membership gate
    did not — so with ``users: sashi, madhu`` (spaces after the comma, the
    natural way to write a list) the gate tested ``'madhu' in [' madhu']``,
    came back False, and that account's ``run()`` skipped its entire body,
    returned success, and kept the heartbeat green while never trading.

    Blank entries are dropped, so a trailing comma is harmless. Names are
    returned VERBATIM otherwise: the same string keys ``TradingBroker/
    config.yaml``'s credentials block and is written into the ledger, so
    folding case here would break one or the other.
    """
    api_info = (config or {}).get("api_info") or {}
    raw = str(api_info.get("users") or "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def is_dry_run(config, user=None):
    """Whether orders for ``user`` should be logged instead of placed.

    Two switches, combined with OR — never with precedence:

      * top-level ``dry_run`` — the deployment-wide kill switch.
      * ``accounts.<user>.dry_run`` — that one account's switch.

    OR, so the per-account flag can only ever make things SAFER. The
    tempting alternative (specific overrides general) would mean
    ``accounts.madhu.dry_run: false`` kept madhu trading through a global
    ``dry_run: true`` — someone pulls the deployment-wide handbrake, watches
    two accounts stop, and never learns the third ignored it. A safety switch
    that something else can veto is not a safety switch.

    The point of the per-account flag is the reverse case: bring a new account
    up alongside live ones and watch what it WOULD have done for a few
    sessions before letting it trade.

    ``user`` of None consults only the global flag, which is what a caller
    without an account in hand should get.
    """
    if config is None:
        return False
    if config.get('dry_run'):
        return True
    if not user:
        return False
    accounts = config.get('accounts')
    if not isinstance(accounts, dict):
        return False
    block = accounts.get(user)
    if not isinstance(block, dict):
        return False
    return bool(block.get('dry_run'))


def trading_account(config):
    """The account this deployment trades as — first entry of
    ``api_info.users``, verbatim.

    This is the identity written into ``kite_trades`` and ``adjustments``
    and used to scope every read of them, so the write and read paths must
    resolve it the same way. Returns ``""`` when unset, which callers should
    treat as "refuse to serve account-scoped data" rather than substituting
    a default — guessing whose book to show is worse than saying no.

    With one monitor process per account this is only a DEFAULT — the value
    the dashboard opens on and the endpoints fall back to when no ``?user=``
    is given. Each monitor is told who it is on the command line; none of
    them infers it from here.
    """
    users = configured_users(config)
    return users[0] if users else ""


def market_data_account(config):
    """The account whose Kite session serves quotes — ``api_info.
    market_data_user``, falling back to ``trading_account``.

    Separate because only one account in this deployment holds a market-data
    subscription. Quotes are not account-specific data, so borrowing that
    session is correct; what would be wrong is letting the same name also
    decide whose trades and adjustments are shown.
    """
    api_info = (config or {}).get("api_info") or {}
    explicit = str(api_info.get("market_data_user") or "").strip()
    return explicit or trading_account(config)

def _coerce_config_value(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.lower() in ('true', 'false'):
        return stripped.lower() == "true"
    try:
        return int(stripped)
    except Exception:
        return stripped

def _set_nested(cfg, dotted_key, value):
    """Set ``cfg[a][b][c] = value`` for a dotted path ``"a.b.c"``.

    Creates intermediate dicts as needed. If a path component already exists
    but is NOT a dict, it is overwritten with a fresh dict — the new value
    takes precedence over the legacy flat key.
    """
    parts = dotted_key.split(".")
    leaf = parts[-1]
    cursor = cfg
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[leaf] = value


def put_config(config_file, *args, updates=None):
    """Atomically update one or more keys in a YAML config file.

    Accepts either a single (key, value) pair as positional args (legacy
    signature) or an ``updates`` dict for batched writes. Strings of the form
    "true"/"false" coerce to bool; numeric strings coerce to int; everything
    else passes through. A cross-process file lock guards the read-modify-write
    so concurrent writers can't corrupt the file.

    Keys may be **dotted paths** (e.g. ``"monitor_info.last_watched"``) to
    target a nested leaf without replacing the whole parent block — this
    keeps concurrent writes to sibling keys from clobbering each other when
    the config is structured into per-section dicts. Plain keys without a
    dot continue to write at the top level.

    Returns ``[True, full_config]`` on success or ``[False, {"Error": ...}]``.
    """
    if updates is None:
        if len(args) != 2:
            return [False, {"Error": "put_config requires (key, value) args or updates= dict"}]
        updates = {args[0]: args[1]}
    elif args:
        return [False, {"Error": "Pass either (key, value) positional args OR updates=, not both"}]

    lock_path = f"{config_file}.lock"
    try:
        with FileLock(lock_path, timeout=CONFIG_LOCK_TIMEOUT):
            watch_config = {}
            try:
                with open(config_file, "r") as file:
                    watch_config = yaml.safe_load(file) or {}
            except FileNotFoundError:
                watch_config = {}

            for k, v in updates.items():
                coerced = _coerce_config_value(v)
                if "." in k:
                    _set_nested(watch_config, k, coerced)
                else:
                    watch_config[k] = coerced

            tmp_path = f"{config_file}.tmp"
            with open(tmp_path, "w") as file:
                yaml.dump(watch_config, file, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, config_file)
            return [True, watch_config]
    except Timeout as e:
        msg = (f"Timed out acquiring lock on {config_file} after "
               f"{CONFIG_LOCK_TIMEOUT}s: {e}")
        _alert_config_failure(msg)
        return [False, {"Error": msg}]
    except Exception as e:
        msg = f"Failed to write the config {config_file} updates={list(updates)}: {e}"
        _alert_config_failure(msg)
        return [False, {"Error": msg}]

def _alert_config_failure(msg):
    # No user context here (basic.py is generic); the heading already tells
    # the operator which subsystem failed. Caller supplies the operator-facing
    # user identity elsewhere (e.g. handle_options._alert).
    alert(user="config", heading="ConfigWriteFailed", message=msg)
