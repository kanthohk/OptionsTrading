"""Shared market-data helpers: proxy GETs, Kite symbol formatting, and the
Black-Scholes math (price, IV, Greeks).

These primitives are used by the standalone analytics jobs (``fetch_data.py``,
``record_snapshots.py``) that talk to the Flask proxy and need to compute IV
and Greeks locally — Kite's ``quote`` payload returns the option price and
OI but not IV or any Greeks.

Defaults assume Indian index options on a non-dividend underlying. Override
``RISK_FREE_RATE`` (or pass ``r=`` to the BS functions) if you need a
different rate.
"""
import math
import requests
import yaml
from scipy.stats import norm
from scipy.optimize import brentq

CONFIG_FILE = "Monitor/config.yaml"
HTTP_TIMEOUT = 15
RISK_FREE_RATE = 0.065  # India 91-day T-bill yield ~ 6.5%

_api_url_cache = None
_db_url_cache = None


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


def proxy_get(path, params=None, timeout=HTTP_TIMEOUT):
    """GET the broker proxy and unwrap its ``[success, payload]`` envelope.

    Returns ``payload`` on success, or ``None`` for network/HTTP/JSON
    failures or when the broker reports ``success=False``.
    """
    try:
        resp = requests.get(f"{api_url()}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, list) or len(data) < 2 or not data[0]:
        return None
    return data[1]


def db_get(path, params=None, timeout=HTTP_TIMEOUT):
    """GET the DB API server and unwrap its ``[success, payload]`` envelope.

    Returns ``payload`` on success, or ``None`` for any failure (network,
    HTTP error, malformed JSON, broker-reported failure flag)."""
    try:
        resp = requests.get(f"{db_url()}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, list) or len(data) < 2 or not data[0]:
        return None
    return data[1]


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
