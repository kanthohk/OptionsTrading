from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import re, logging, os, yaml, time, requests
import pytz
from filelock import FileLock, Timeout

IST = pytz.timezone('Asia/Kolkata')

CONFIG_LOCK_TIMEOUT = 10

class ApiError(Exception):
    """Raised when a broker API call fails after exhausting retries or returns
    an unexpected payload shape."""

API_DEFAULT_TIMEOUT = 10
API_DEFAULT_RETRIES = 2
API_DEFAULT_BACKOFF = 1.0

def alert(user, heading, message, logger=None):
    """Send a push alert, prefixing the heading with ``{user}_``.

    Best-effort: a failing alert channel must never break the caller. If
    AlertMobile raises, we log via ``logger`` (if provided) or fall back to
    print so the operator still sees the failure.
    """
    try:
        from helpers.alert import AlertMobile
        AlertMobile().send(heading=f"{user}_{heading}", message=message)
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

    Honors ``config['dry_run']`` to short-circuit to a log-only mode. Fires an
    ``OrderPlacing`` alert just before the broker call so every live order is
    auditable in the mobile channel.

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
    when ``config['dry_run']`` short-circuits the path, or ``None`` on
    transport failure / final rejection. Callers that only need a
    boolean can keep using ``if not result:`` since both "DRY_RUN" and
    any order-id string are truthy; callers that need the order id
    (e.g. the adjustments audit writer) get it directly.
    """
    _alert = alert_fn or (lambda heading, message: None)
    if config.get('dry_run'):
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
            response = requests.get(url, timeout=timeout)
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

def split_symbol(symbol):
    """Parse KiteConnect option tradingsymbol into components."""
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
        # Weekly expiry e.g. NIFTY2592324900PE
        date_part = middle[:-5] # assuming strike is 5 digit
        dd = int(date_part[-2:])
        mm = int(date_part[2:-2]) if len(date_part[2:-2]) > 0 else 1
        yy = int(date_part[:2])
        strike = int(middle[-5:])
        expiry_dt = datetime(2000+yy, mm, dd)
    return underlying, expiry_dt, strike, opt_type

class SyncFileHandler(logging.FileHandler):
    """FileHandler that flushes and syncs every log record."""
    def emit(self, record):
        super().emit(record)  # normal logging
        self.stream.flush()  # flush Python buffer
        os.fsync(self.stream.fileno())  # flush OS buffer


def setup_logger(user):
    """Build a per-user, per-day file logger.

    The logger name embeds the IST date so a long-running process gets a fresh
    file each calendar day. Existing handlers on the same name are cleared so
    repeat construction (e.g. on monitor restart) doesn't double-log.
    """
    daystr = datetime.now(IST).strftime('%Y%m%d')
    logger_name = f"{user}_{daystr}_logger"
    logfile = f"{daystr}_watch.log"

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
