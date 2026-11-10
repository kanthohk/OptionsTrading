"""Shared market-data helpers: proxy GETs, Kite symbol formatting, and the
Black-Scholes math (price, IV, Greeks).

These primitives are used by the standalone analytics jobs (``fetch_data.py``,
``fetch_db_data.py``) that talk to the Flask proxy and need to compute IV
and Greeks locally — Kite's ``quote`` payload returns the option price and
OI but not IV or any Greeks.

Defaults assume Indian index options on a non-dividend underlying. Override
``RISK_FREE_RATE`` (or pass ``r=`` to the BS functions) if you need a
different rate.
"""
import math
import requests
import yaml

# scipy is deliberately NOT imported at module scope. This module is the HTTP
# client every component uses (proxy_get, db_get, ...), so a module-level
# import costs whatever scipy costs in EVERY process: flaskapp, db_api,
# fetch_db_data, and one autotrade per account — six interpreters on a 933MB
# host, five of which never price an option.
#
# scipy is only needed by the Black-Scholes helpers at the bottom of this
# file, and their only callers are the recorder (fetch_db_data) and the
# ad-hoc MarketAnalysis/fetch_data.py. The monitor reads deltas the recorder
# already computed into option_snapshot; it never prices anything itself.
#
# _scipy() imports on first use and caches, so the recorder behaves exactly
# as before apart from a one-off import on its first pricing call.
_SCIPY = None


def _scipy():
    """``(norm, brentq)``, imported on first use and cached.

    Propagates ImportError untouched if scipy genuinely isn't installed,
    which is the right outcome: a host that prices options needs it, and a
    host that doesn't will never call this.
    """
    global _SCIPY
    if _SCIPY is None:
        from scipy.stats import norm as _norm
        from scipy.optimize import brentq as _brentq
        _SCIPY = (_norm, _brentq)
    return _SCIPY

CONFIG_FILE = "Monitor/config.yaml"
HTTP_TIMEOUT = 15
RISK_FREE_RATE = 0.065  # India 91-day T-bill yield ~ 6.5%

_api_url_cache = None
_db_url_cache = None
_db_secret_cache = None


def _load_config():
    """Read ``Monitor/config.yaml``. Raises ``RuntimeError`` if the file is
    missing or unreadable so misconfiguration surfaces at startup rather
    than silently routing trading calls to a default URL."""
    try:
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"Config file {CONFIG_FILE} not found: {e}")
    except yaml.YAMLError as e:
        raise RuntimeError(f"Could not parse {CONFIG_FILE}: {e}")
    if not isinstance(cfg, dict):
        raise RuntimeError(f"{CONFIG_FILE} did not parse to a mapping")
    return cfg


def _require(cfg, key):
    if key not in cfg or cfg[key] in (None, ""):
        raise RuntimeError(f"{CONFIG_FILE} is missing required key {key!r}")
    return cfg[key]


def _api_info(cfg):
    section = cfg.get("api_info")
    if not isinstance(section, dict):
        raise RuntimeError(f"{CONFIG_FILE} is missing required section 'api_info'")
    return section


def api_url():
    """Broker proxy URL (Flask ``main.py``). Reads ``api_info.url`` from
    ``Monitor/config.yaml``. Raises ``RuntimeError`` if the key is absent."""
    global _api_url_cache
    if _api_url_cache is None:
        _api_url_cache = _require(_api_info(_load_config()), "url")
    return _api_url_cache


def db_url():
    """DB API server URL (Flask ``db_api.py``). Reads
    ``api_info.db_api_host`` / ``api_info.db_api_port`` from
    ``Monitor/config.yaml``. Raises ``RuntimeError`` if either key is absent."""
    global _db_url_cache
    if _db_url_cache is None:
        api_info = _api_info(_load_config())
        host = _require(api_info, "db_api_host")
        port = _require(api_info, "db_api_port")
        _db_url_cache = f"http://{host}:{port}"
    return _db_url_cache


def proxy_get(path, params=None, timeout=HTTP_TIMEOUT, logger=None):
    """GET the broker proxy and unwrap its ``[success, payload]`` envelope.

    Returns ``payload`` on success, or ``None`` for network/HTTP/JSON
    failures or when the broker reports ``success=False``.

    Pass ``logger`` to find out WHICH of those happened. Without it the four
    cases are indistinguishable, and they have nothing in common:

      * the proxy host is unreachable — wrong ``api_info.url``, or a firewall
        between the recorder's server and the trading server;
      * the request timed out;
      * the proxy answered with an HTTP error;
      * the proxy answered fine and the BROKER refused, in which case
        ``data[1]`` carries the broker's own reason — the single most useful
        string available, and the one this function used to discard.

    That last case is why the recorder could only ever report "spot
    unavailable; skipping cycle" while the actual explanation (a throttled
    or rejected Kite call) sat one HTTP hop away.
    """
    url = f"{api_url()}{path}"
    try:
        resp = requests.get(url, params=params, headers=proxy_auth_headers(),
                            timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout:
        if logger:
            logger.warning(f"proxy GET {path}: timed out after {timeout}s "
                           f"({url})")
        return None
    except requests.ConnectionError as e:
        if logger:
            logger.warning(
                f"proxy GET {path}: cannot reach the broker proxy at {url} "
                f"({type(e).__name__}). Check api_info.url on THIS host and "
                f"that the trading server is reachable from it."
            )
        return None
    except requests.HTTPError as e:
        if logger:
            logger.warning(f"proxy GET {path}: HTTP error from {url}: {e}")
        return None
    except (requests.RequestException, ValueError) as e:
        if logger:
            logger.warning(f"proxy GET {path}: bad response from {url}: "
                           f"{type(e).__name__}: {e}")
        return None
    if not isinstance(data, list) or len(data) < 2:
        if logger:
            logger.warning(f"proxy GET {path}: unexpected envelope from "
                           f"{url}: {str(data)[:200]}")
        return None
    if not data[0]:
        if logger:
            logger.warning(f"proxy GET {path}: the proxy reached the broker "
                           f"and the BROKER refused: {str(data[1])[:300]}")
        return None
    return data[1]


# Header carrying the shared secret on every DB API call. db_api rejects
# requests without it — see api_info.db_api_secret in Monitor/config.yaml.
DB_API_KEY_HEADER = "X-Api-Key"


def db_api_secret():
    """Shared secret for the DB API, from ``api_info.db_api_secret``.

    Required, like ``db_api_host``/``db_api_port``: raising here means a
    misconfigured client fails at the call site with a clear message, rather
    than silently sending no credential and getting an opaque 401 back.
    """
    global _db_secret_cache
    if _db_secret_cache is None:
        _db_secret_cache = _require(_api_info(_load_config()), "db_api_secret")
    return _db_secret_cache


def proxy_auth_headers():
    """Headers authenticating a MACHINE caller to the broker proxy.

    main.py requires a dashboard login (JWT) or this shared secret on every
    route — see helpers/web_auth. Machine callers can't log in, so they send
    the same secret db_api takes; both servers already have it configured.

    Empty when the secret isn't configured, rather than raising: the call
    then fails with main.py's explicit 401, which names the problem, instead
    of every caller needing its own handling for a config error.
    """
    try:
        return {DB_API_KEY_HEADER: db_api_secret()}
    except RuntimeError:
        return {}


def db_request(method, path, params=None, json=None, timeout=HTTP_TIMEOUT):
    """Call the DB API and unwrap its ``[success, payload]`` envelope.

    Returns ``(True, payload)`` or ``(False, message)``. Never raises for
    network / HTTP / JSON problems — the distinction matters because writes
    have to tell "the row didn't land" apart from "the row landed and the
    payload was null", which a bare ``None`` return cannot express.
    """
    # Config resolution is INSIDE the guard. db_url()/db_api_secret() raise
    # RuntimeError on a missing key, and this function's callers — db_get in
    # particular — are written to degrade on failure, not to propagate. With
    # the lookups outside, a config missing db_api_secret turned every
    # DB-first quote lookup into a 500 from main.py instead of a fallback to
    # the broker. A misconfiguration should be loud at STARTUP (see main.py)
    # and survivable per-request.
    try:
        url = f"{db_url()}{path}"
        headers = {DB_API_KEY_HEADER: db_api_secret()}
    except RuntimeError as e:
        return False, f"DB API is not configured: {e}"
    try:
        resp = requests.request(method, url, params=params, json=json,
                                headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return False, f"{method} {path} failed: {e}"
    try:
        data = resp.json()
    except ValueError:
        return False, (f"{method} {path} returned HTTP {resp.status_code} "
                       f"with a non-JSON body")
    if not isinstance(data, list) or len(data) < 2:
        return False, f"{method} {path} returned a malformed envelope: {data!r}"
    if not data[0]:
        return False, f"{method} {path} reported failure: {data[1]}"
    return True, data[1]


def proxy_request(method, path, params=None, json=None, timeout=HTTP_TIMEOUT):
    """Call the broker proxy (``main.py``) and unwrap its envelope.

    Same ``(ok, payload_or_message)`` contract as ``db_request``, authenticated
    with ``proxy_auth_headers``.

    This is the path the trading server uses for DB work. main.py is the one
    component that talks to db_api (it already proxies the dashboard's reads),
    so it is the only place that needs db_api's host and secret.
    """
    # Inside the guard for the same reason as db_request: a missing
    # api_info.url must surface as a failed call, not an escaping RuntimeError.
    try:
        url = f"{api_url()}{path}"
    except RuntimeError as e:
        return False, f"Broker proxy is not configured: {e}"
    try:
        resp = requests.request(method, url, params=params, json=json,
                                headers=proxy_auth_headers(),
                                timeout=timeout)
    except requests.RequestException as e:
        return False, f"{method} {path} failed: {e}"
    try:
        data = resp.json()
    except ValueError:
        return False, (f"{method} {path} returned HTTP {resp.status_code} "
                       f"with a non-JSON body")
    if not isinstance(data, list) or len(data) < 2:
        return False, f"{method} {path} returned a malformed envelope: {data!r}"
    if not data[0]:
        return False, f"{method} {path} reported failure: {data[1]}"
    return True, data[1]


def proxy_post(path, payload, timeout=HTTP_TIMEOUT):
    """POST a JSON body to the broker proxy. Returns ``(ok, payload_or_msg)``."""
    return proxy_request("POST", path, json=payload, timeout=timeout)


def db_get(path, params=None, timeout=HTTP_TIMEOUT):
    """GET the DB API server and unwrap its ``[success, payload]`` envelope.

    Returns ``payload`` on success, or ``None`` for any failure (network,
    HTTP error, malformed JSON, server-reported failure flag). Callers that
    need to know WHY should use ``db_request`` instead."""
    ok, payload = db_request("GET", path, params=params, timeout=timeout)
    return payload if ok else None


def db_post(path, payload, timeout=HTTP_TIMEOUT):
    """POST a JSON body to the DB API. Returns ``(ok, payload_or_message)``."""
    return db_request("POST", path, json=payload, timeout=timeout)


_MONTH_CODE = {10: 'O', 11: 'N', 12: 'D'}


def kite_expiry_code(expiry_date, monthly):
    """Build Kite's in-symbol expiry code.

    Monthly: ``YY + MON3`` uppercase, e.g. ``25SEP``.
    Weekly: ``YY + month_code + DD`` where month_code is the digit for
    months 1-9 and O/N/D for Oct/Nov/Dec — e.g. ``25923`` for 23-Sep-2025
    or ``25O09`` for 09-Oct-2025.
    """
    if monthly:
        return expiry_date.strftime('%y%b').upper()
    yy = expiry_date.strftime('%y')
    mc = _MONTH_CODE.get(expiry_date.month, str(expiry_date.month))
    dd = expiry_date.strftime('%d')
    return f"{yy}{mc}{dd}"


def _d1_d2(spot, strike, t, sigma, r):
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def bs_price(spot, strike, t_years, sigma, opt_type, r=RISK_FREE_RATE):
    """Black-Scholes premium for a European option on a non-dividend underlying.
    Returns 0 for degenerate inputs (``sigma<=0`` or ``t_years<=0``)."""
    if sigma <= 0 or t_years <= 0:
        return 0
    norm, _ = _scipy()
    d1, d2 = _d1_d2(spot, strike, t_years, sigma, r)
    disc = math.exp(-r * t_years)
    if opt_type == 'CE':
        return spot * norm.cdf(d1) - strike * disc * norm.cdf(d2)
    return strike * disc * norm.cdf(-d2) - spot * norm.cdf(-d1)


def implied_vol(spot, strike, t_years, price, opt_type, r=RISK_FREE_RATE):
    """Implied volatility as a decimal (e.g. ``0.155`` for 15.5%) via Brent's
    method on the price-vs-sigma curve. Returns 0 when inputs are degenerate
    or when no solution exists in the [0.01%, 500%] sigma bracket."""
    if price <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return 0
    _, brentq = _scipy()
    try:
        return brentq(lambda s: bs_price(spot, strike, t_years, s, opt_type, r) - price,
                      1e-4, 5.0, xtol=1e-4, maxiter=50)
    except (ValueError, RuntimeError):
        return 0


def greeks(spot, strike, t_years, sigma, opt_type, r=RISK_FREE_RATE):
    """Closed-form Black-Scholes Greeks.

    Returns ``(delta, theta_per_day, gamma, vega_per_pct)``:
    - delta: ∂price/∂spot, dimensionless
    - theta: per-calendar-day decay (annual theta / 365)
    - gamma: ∂²price/∂spot²
    - vega: per 1% absolute change in sigma (e.g. 16% → 17%)

    Zero tuple when ``sigma<=0`` or ``t_years<=0``.
    """
    if sigma <= 0 or t_years <= 0:
        return 0, 0, 0, 0
    norm, _ = _scipy()
    d1, d2 = _d1_d2(spot, strike, t_years, sigma, r)
    sqrt_t = math.sqrt(t_years)
    pdf_d1 = norm.pdf(d1)
    disc = math.exp(-r * t_years)
    if opt_type == 'CE':
        delta = norm.cdf(d1)
        theta_year = (-spot * pdf_d1 * sigma / (2 * sqrt_t)
                      - r * strike * disc * norm.cdf(d2))
    else:
        delta = norm.cdf(d1) - 1
        theta_year = (-spot * pdf_d1 * sigma / (2 * sqrt_t)
                      + r * strike * disc * norm.cdf(-d2))
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100  # per 1% IV move
    theta = theta_year / 365.0
    return delta, theta, gamma, vega
