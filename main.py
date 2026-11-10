import threading

from flask import Flask, request, render_template, redirect, g, jsonify, send_file
import yaml
from datetime import datetime
from urllib.parse import urlparse, quote
from pathlib import Path
import uuid
import pytz
ist = pytz.timezone('Asia/Kolkata')

from kiteconnect.exceptions import TokenException
from TradingBroker.kite_api_bot import KITE_CONNECT

from Monitor.options import *
from helpers.alert import AlertMobile
from helpers.basic import (get_config, split_symbol, is_market_hours,
                           watch_log_path, watch_log_last_modified,
                           trading_account, market_data_account,
                           configured_users,
                           hedge_distance_for_dte, snap_to_strike_grid, IST)
from helpers import underlyings as ul
from helpers.positions_processing import (detect_strategy,
                                          ADJUSTABLE_STRATEGIES,
                                          MONITORED_STRATEGIES)
from MarketAnalysis.trendanalysis import rsi_band
from TrendAnalysis.analyze_levels import MarketAnalyzer
from TrendAnalysis.plot_market_speed import load_rows, plot_market_speed
# Pure FIFO helpers — no DB access of their own. The ledger rows they match
# arrive via _proxy_db, so this stays consistent with every other DB read on
# the trading server going through db_api.
from helpers.trade_ledger import (LEDGER_EXCHANGE, fifo_open_lots,
                                  open_lot_basis, realised_today_by_symbol,
                                  sort_fills)
from helpers.market_data import (db_get, db_request, db_post, db_url,
                                 db_api_secret)
from helpers.system_health import (monitor_unit, monitored_services,
                                   system_status, systemd_states)
from helpers import web_auth
from TrendAnalysis.get_market_speed import monitor_market_speed

# Maximum age of a DB snapshot we'll trust for live quote serving. The
# recorder samples every 30s; anything older than 2 minutes is treated as
# stale and we fall through to KITE.
DB_FRESHNESS_SECONDS = 120

WATCH_CONFIG = "Monitor/config.yaml"
BROKER_CONFIG = "TradingBroker/config.yaml"

# Create Flask app
app = Flask(__name__)
broker_connections = {}

# Serialises broker logins across request threads.
#
# app.run() is threaded, so three accounts polling concurrently can each miss
# the cache and call get_broker at the same moment — three headless Chromes
# at once, each wanting 300-500MB. On a 933MB host the kernel kills one, and
# the symptom is a 120s read timeout in driver.get() for an account whose
# credentials are perfectly fine.
#
# One at a time is no slower in the normal case: a login happens once a day
# per account (Kite tokens expire ~06:00 IST), and the double-check inside
# the lock means a thread that waited usually finds the session already
# there and never launches a browser at all.
_broker_login_lock = threading.Lock()

# ---- Account identity ----------------------------------------------------
#
# Replaces the former ``SUPER_USER = "sashi"``, which answered two unrelated
# questions with one hardcoded name (see helpers/basic for the split):
#
#   TRADING_ACCOUNT     — whose book this is. Scopes kite_trades and
#                         adjustments reads; owns the orders.
#   MARKET_DATA_ACCOUNT — whose Kite session serves quotes. Only one account
#                         here holds a market-data subscription, and an LTP is
#                         the same for everyone, so borrowing that session is
#                         correct. What would be wrong is letting it also
#                         decide whose trades get shown.
#
# Resolved once at import: changing api_info.users needs a flaskapp restart,
# which it needed anyway (broker sessions are cached per account, and
# autotrade reads the same value once at thread start). A missing or
# malformed config leaves these empty rather than aborting startup — the
# market-data endpoints still work, and the account-scoped ones refuse
# explicitly instead of guessing whose data to serve.
try:
    _WATCH_CFG_AT_BOOT = get_config(WATCH_CONFIG) or {}
except Exception as _cfg_err:      # noqa: BLE001 - startup diagnostics only
    print(f"WARNING: could not read {WATCH_CONFIG} at startup: {_cfg_err}. "
          f"Account-scoped endpoints will refuse until it is readable.")
    _WATCH_CFG_AT_BOOT = {}
TRADING_ACCOUNT = trading_account(_WATCH_CFG_AT_BOOT)
MARKET_DATA_ACCOUNT = market_data_account(_WATCH_CFG_AT_BOOT)
# Every account this deployment serves, resolved at import for the same reason
# and on the same terms as TRADING_ACCOUNT: adding one needs a flaskapp
# restart anyway (broker sessions are cached per account). Resolving it once
# also keeps /status independent of whether the config is readable AT REQUEST
# TIME — a property tests/test_status.py pins, and a good one: the monitoring
# pill should not go blank because the config file is briefly unreadable.
CONFIGURED_ACCOUNTS = tuple(configured_users(_WATCH_CFG_AT_BOOT))


def _hedge_cfg_for(underlying):
    """``hedge_info`` (near/mid/far distances + DTE boundaries) for
    ``underlying``, falling back to the global block.

    Mirrors ``RiskManager._hedge_cfg_for`` and ``db_api._hedge_cfg`` so the
    wing width the dashboard CHECKS against is the same one the adjustments
    engine PLACES. Duplicating the bands in the template instead would let
    the two drift apart silently on the next config change, and the
    dashboard would then flag correctly-placed hedges as deviations.

    Read fresh per call rather than from ``_WATCH_CFG_AT_BOOT``: editing
    hedge_info should take effect on the next refresh, not the next restart.
    Best-effort — any failure returns ``{}`` so ``hedge_distance_for_dte``
    falls back to its built-in 100/200/300 @ 7/14 DTE defaults.
    """
    try:
        cfg = get_config(WATCH_CONFIG) or {}
        if underlying:
            section = (cfg.get("underlyings") or {}).get(str(underlying).upper())
            own = section.get("hedge_info") if isinstance(section, dict) else None
            if isinstance(own, dict) and own:
                return own
        hedge_info = cfg.get("hedge_info")
        return hedge_info if isinstance(hedge_info, dict) else {}
    except Exception:      # noqa: BLE001 - display aid, never fatal
        return {}


def _days_to_expiry(expiry_str):
    """Calendar days from today (IST) to ``expiry_str`` ('YYYY-MM-DD').

    IST rather than the host's local date because expiry is an exchange
    calendar fact: a server in UTC is still on the previous date at 09:15
    IST and would report one day too many for the whole morning session.
    Returns None for an unparseable date.
    """
    try:
        exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (exp - datetime.now(IST).date()).days


def _expected_hedge_distance(underlying, dte):
    """Configured hedge width in points for this book, snapped to the grid.

    Snapping matches ``RiskManager._hedge_distance_for``: an off-grid
    configured value (650 on BANKNIFTY's 100-pt grid) names a strike that
    was never listed, so the engine places the snapped figure — and that
    snapped figure is what the dashboard must compare against.
    """
    raw = hedge_distance_for_dte(dte, _hedge_cfg_for(underlying))
    try:
        return snap_to_strike_grid(raw, ul.strike_step(underlying))
    except Exception:      # noqa: BLE001 - unknown underlying, display aid
        return raw


def _account_for(explicit):
    """Resolve the account a per-account request applies to.

    An explicit ``?user=`` always wins; otherwise the configured trading
    account. Returns ``""`` when neither is available, which callers must
    surface as an error — defaulting to some other account would show one
    book's data under another's name.
    """
    return (explicit or "").strip() or TRADING_ACCOUNT


def _known_account(explicit):
    """Resolve and VALIDATE the account for a per-account request.

    Like ``_account_for``, but the result is checked against
    ``CONFIGURED_ACCOUNTS`` and ``""`` is returned for anything else. Use
    this — not ``_account_for`` — anywhere the account reaches the filesystem
    or a systemd unit name.

    Why an allowlist rather than a character filter: ``/get_watch_log?user=``
    and ``/status?user=`` turn the account into a log FILENAME, on a Flask app
    bound to 0.0.0.0:80. A filter is a guess about what is dangerous; a list
    of names the operator actually configured is a statement about what is
    permitted. ``watch_log_path`` keeps its own character guard as a backstop
    for callers that never come through here.

    Checked against the boot snapshot rather than a fresh read, so this stays
    a pure function of the request and cannot fail because the config file is
    momentarily unreadable. Adding an account needs a flaskapp restart, which
    it needed regardless.
    """
    account = _account_for(explicit)
    return account if account and account in CONFIGURED_ACCOUNTS else ""


def _safe_alert(heading, message, priority=5):
    """Best-effort alert that never raises."""
    try:
        AlertMobile().send(heading=heading, message=message, priority=priority)
    except Exception as e:
        print(f"AlertMobile failed for {heading}: {e}")


def get_credentials():
    with open(BROKER_CONFIG, "r") as file:
        config = yaml.safe_load(file)
    return config["credentials"]


def get_broker(user):
    """Authenticate a user with the broker and cache the connection.

    Returns ``[True, msg]`` on success or ``[False, msg]`` on failure. The
    cached connection is keyed by ``user``; if a stale entry exists it is
    overwritten on success.
    """
    global broker_connections
    credentials = get_credentials()
    if not user or user not in credentials:
        return [False, "Either user or his credentials are missing"]
    with _broker_login_lock:
        # Re-check inside the lock. Threads that queued behind another
        # account's login are usually looking for a session that now exists,
        # and launching a second browser to rediscover that wastes the one
        # resource this host has least of.
        existing = broker_connections.get(user)
        if existing is not None and getattr(existing, "access_token", None):
            return [True, "Got Connection successfully (already connected)"]
        kc = KITE_CONNECT(user, credentials.get(user))
        if kc.access_token:
            broker_connections[user] = kc
            return [True, "Got Connection successfully"]
        return [False, "Failed to get connection"]

def _ensure_broker(user):
    """Return the cached broker connection for ``user``, authenticating if
    missing. Raises ``RuntimeError`` if authentication fails."""
    if user not in broker_connections:
        ok, msg = get_broker(user)
        if not ok:
            _safe_alert("BrokerAuthFailed", f"{user}: {msg}")
            raise RuntimeError(f"KITE connection failed for {user}: {msg}")
    return broker_connections[user]


def _call_with_reauth(user, method_name, *args, **kwargs):
    """Invoke a method on the user's broker connection, transparently
    re-authenticating once if the session has expired.

    Why: KiteConnect access tokens expire daily (~06:00 IST) and after long
    idle periods, so an app left running overnight starts the next session
    holding a dead token. Limited to one reauth attempt per call so a
    persistent auth failure surfaces quickly rather than retrying forever.

    This only works because the broker methods now RE-RAISE TokenException
    instead of catching it and returning None (see
    TradingBroker/kite_api_bot.py). While they swallowed it, this handler was
    unreachable and the daily expiry presented as "no open positions" until
    someone restarted the service.
    """
    broker = _ensure_broker(user)
    try:
        return getattr(broker, method_name)(*args, **kwargs)
    except TokenException as e:
        print(f"Session expired for {user} during {method_name}: {e}. Re-authenticating.")
        # Routine (tokens expire daily) — low priority. ReauthFailed below
        # stays urgent.
        _safe_alert("SessionExpired", f"{user}: re-authing during {method_name}",
                    priority=3)
        broker_connections.pop(user, None)
        ok, msg = get_broker(user)
        if not ok:
            _safe_alert("ReauthFailed", f"{user}: {msg}")
            raise RuntimeError(f"Re-auth failed for {user}: {msg}")
        try:
            return getattr(broker_connections[user], method_name)(*args, **kwargs)
        except TokenException as retry_err:
            # A fresh session that is immediately rejected is not a transient
            # expiry — usually revoked API credentials. Convert it to
            # RuntimeError, which every endpoint already handles, so the
            # caller gets [False, reason] instead of an unhandled 500.
            _safe_alert("ReauthFailed",
                        f"{user}: session rejected immediately after re-auth "
                        f"during {method_name}")
            raise RuntimeError(
                f"Re-authenticated {user} but {method_name} was still "
                f"rejected: {retry_err}. Check the API credentials in "
                f"TradingBroker/config.yaml."
            )


# ---- Authentication --------------------------------------------------------
#
# Every route needs a logged-in dashboard user (JWT cookie) or a machine
# caller carrying the shared secret — see helpers/web_auth for why there are
# two. Enforced here rather than per route so a new endpoint is protected by
# default; forgetting a decorator on /place_order is not a mistake this app
# can afford.

# Reachable without credentials. Logout is here so an expired session can
# still clear its cookie.
_PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/logout",
                 "/favicon.ico"}
# Browser pages: an anonymous visit is redirected to the login form rather
# than shown a JSON 401.
_PAGE_PATHS = {"/", "/snapshots"}


def _safe_next(target):
    """``target`` if it's a path on THIS site, else ``/``.

    The login form redirects to ``?next=`` after success; accepting any URL
    there would make the login page an open redirect, e.g.
    ``/login?next=//evil.example`` bouncing a freshly logged-in user to a
    lookalike page.
    """
    target = str(target or "")
    if not target.startswith("/") or target.startswith("//") \
            or target.startswith("/\\"):
        return "/"
    return target


@app.before_request
def _require_login():
    if not web_auth.ENFORCE or request.path in _PUBLIC_PATHS:
        return None
    identity = web_auth.request_identity(request)
    if identity:
        g.auth_user = identity
        return None
    if request.method == "GET" and request.path in _PAGE_PATHS:
        return redirect("/login?next=" + quote(_safe_next(request.full_path
                                                          .rstrip("?"))))
    return jsonify([False, "unauthorised: log in at /login"]), 401


@app.route("/login", methods=["GET"])
def login_page():
    next_url = _safe_next(request.args.get("next"))
    # Already signed in: skip the form.
    if web_auth.request_identity(request):
        return redirect(next_url)
    return render_template("login.html", next_url=next_url)


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """Body: ``{username, password}``. Sets the auth cookie on success.

    JSON only: a cross-site HTML form can't send ``application/json``, so
    this can't be driven from another site to log a browser into an account
    of the attacker's choosing.
    """
    if not request.is_json:
        return jsonify([False, "expected a JSON body"]), 400
    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username or not password:
        return jsonify([False, "Enter a username and password."]), 400

    # The users table lives on the DB server; db_api checks the hash there
    # and only tells us yes or no.
    ok, result = db_post("/api/auth/verify",
                         {"username": username, "password": password})
    if not ok:
        # Usually db_api down or out of date (404 = a db_api.py without
        # /api/auth/verify). Logged in full here; the browser gets a message
        # that doesn't blame the password.
        print(f"Login for {username!r} could not be checked: {result}")
        return jsonify([False, "Login service is unavailable. Try again "
                               "shortly."]), 503
    if not isinstance(result, dict) or not result.get("ok"):
        reason = result.get("reason") if isinstance(result, dict) else result
        print(f"Login refused for {username!r}: {reason}")
        message = ("Too many failed attempts. Try again later."
                   if reason == "locked" else "Invalid username or password.")
        return jsonify([False, message]), 401

    canonical = result.get("username") or username
    token, expires = web_auth.issue_token(canonical)
    resp = jsonify([True, {"username": canonical,
                           "expires": expires.isoformat()}])
    web_auth.set_auth_cookie(resp, token, expires)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    resp = jsonify([True, ""])
    web_auth.clear_auth_cookie(resp)
    return resp


@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    return [True, {"username": g.get("auth_user")}]


@app.route("/")
def home():
    # Pull host / port out of ``api_info`` so the dashboard template doesn't
    # carry hard-coded copies of values that already live in
    # Monitor/config.yaml. url is parsed safely so a missing or malformed
    # value falls back to localhost:80 instead of crashing the page.
    #
    # The account comes from TRADING_ACCOUNT — the same value the endpoints
    # default to — rather than being re-derived here. It used to be
    # re-parsed and lowercased, which made the page's idea of the account a
    # second, subtly different source: the ledger stores whatever casing
    # config carries, so a capitalised name produced an empty audit panel
    # instead of an error.
    watch_cfg = get_config(WATCH_CONFIG) or {}
    api_info = watch_cfg.get("api_info") or {}
    # The RSI band the RISK ENGINE gates on, handed to the page so the two
    # cannot disagree. The dashboard used to carry its own hardcoded 40-60,
    # which was a different band from the engine's — the rail could show a
    # reading in green while the engine was refusing to trade on it. Read at
    # request time, so a retune shows up on the next page load.
    rsi_low, rsi_high = rsi_band(watch_cfg.get("adjustment_info"))
    # ``?user=`` opens the page on that account, so a per-account view can be
    # bookmarked or linked. Every OTHER endpoint already honoured ?user=, and
    # this one silently didn't — which made "/?user=madhu" render as the
    # default account, the single most confusing possible outcome: the URL
    # says madhu and the page shows sashi's book under sashi's name.
    #
    # Allowlisted via _known_account, same as the log and status endpoints:
    # an unknown account falls back to the default rather than rendering a
    # page whose every panel will then refuse.
    account = _known_account(request.args.get("user")) or TRADING_ACCOUNT
    display_user = (account[:1].upper() + account[1:]) if account else "Trader"
    parsed = urlparse(str(api_info.get("url") or ""))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return render_template(
        "dashboard.html",
        dashboard_username=display_user,
        dashboard_account=account,
        # Who is LOGGED IN, as distinct from whose book is shown above.
        login_user=g.get("auth_user"),
        # Every account this deployment serves, so the page can offer a
        # switcher. One process per account trades, but ONE dashboard watches
        # them all — without this list the page would show the first account's
        # book while three were open, which is worse than showing none.
        dashboard_accounts=list(CONFIGURED_ACCOUNTS),
        dashboard_host=host,
        dashboard_port=port,
        rsi_band_low=rsi_low,
        rsi_band_high=rsi_high,
    )


@app.route("/snapshots")
def snapshots_page():
    """Render the market-snapshot visualization page."""
    return render_template("snapshots.html")


def _proxy_db(path, params=None):
    """Forward a GET to the DB API server, preserving its ``[success,
    payload]`` envelope — including the reason on failure.

    This used to flatten every failure to "DB API unavailable", which was
    wrong twice over: it discarded db_api's actual message (a 401 from a
    mismatched shared secret looked identical to the host being down), and it
    turned a legitimate ``null`` payload into an error. autotrade now relies
    on these messages to explain a skipped risk cycle.
    """
    ok, result = db_request("GET", path, params=params)
    return [True, result] if ok else [False, result]


def _proxy_db_write(path, payload):
    """Forward a POST to the DB API server, preserving its envelope.

    Kept separate from ``_proxy_db`` so the write paths are greppable: these
    are the only endpoints on this app that mutate the trade ledger.
    """
    ok, result = db_post(path, payload)
    return [True, result] if ok else [False, result]


@app.route("/api/snapshot_dates", methods=["GET"])
def api_snapshot_dates():
    """Proxy to DB API server: list of YYYY-MM-DD dates with snapshot rows."""
    return _proxy_db("/api/snapshot_dates")


@app.route("/api/index_summary", methods=["GET"])
def api_index_summary():
    """Proxy to DB API server: open/close/change/horizon-% for NIFTY 50 / INDIA VIX."""
    return _proxy_db("/api/index_summary")


@app.route("/api/realised_pnl", methods=["GET"])
def api_realised_pnl():
    """Proxy to DB API server: realised P&L per (underlying, expiry), FIFO-
    matched from the kite_trades ledger. Defaults ``user`` to the configured
    trading account so the dashboard doesn't have to know the name.

    Same figures the risk engine's realised component uses — the dashboard and
    the risk engine read one source, so they cannot drift apart.
    """
    user = _account_for(request.args.get("user"))
    if not user:
        return [False, "No account configured (api_info.users) and no ?user= given"]
    params = {"user": user}
    charge = request.args.get("charge")
    if charge is not None:
        params["charge"] = charge
    return _proxy_db("/api/realised_pnl", params=params)


@app.route("/api/system_status", methods=["GET"])
def api_system_status():
    """systemd state of the sibling jobs plus disk usage of the host they run
    on. Drives the Services block of the dashboard's index rail.

    One ``autotrade@<account>`` row per entry in ``api_info.users``, then
    whichever shared units ``monitor_info.local_services`` says live on this
    host (``db_api`` / ``fetch_db_data`` when they do; on a split deployment
    they sit on the DB server and are listed by neither). Per-account because
    the monitor is one process per account: a single ``autotrade`` row would
    have gone green on whichever instance happened to be up and hidden the
    rest.

    Local to this host — no DB API proxy involved, which is the point: this
    endpoint has to stay answerable when the DB API job is the thing that's
    down. ``flaskapp`` isn't reported because it's this process.

    Failed probes come back as degraded rows inside a successful envelope
    rather than as an error, so one unreadable unit can't blank the card.
    """
    try:
        # Config read per request, not from the boot snapshot: moving a
        # service between hosts should be reflected on the next dashboard
        # poll, not the next restart. Cheap, and a failure just falls back
        # to the built-in default.
        try:
            cfg = get_config(WATCH_CONFIG) or {}
        except Exception:      # noqa: BLE001 - fall back to the defaults
            cfg = None
        units = monitored_services(CONFIGURED_ACCOUNTS, config=cfg)
        return [True, system_status(units=units)]
    except Exception as e:
        return [False, f"Failed to read system status: {e}"]


@app.route("/api/strikes_by_delta", methods=["GET"])
def api_strikes_by_delta():
    """Proxy to DB API server: strikes whose |delta| sits in ``?min=&max=``
    (percent) for the latest snapshot of ``?underlying=`` (NIFTY by
    default), grouped by expiry and option_type."""
    params = {}
    for k in ("min", "max", "underlying"):
        v = request.args.get(k)
        if v is not None:
            params[k] = v
    return _proxy_db("/api/strikes_by_delta", params=params or None)


@app.route("/api/underlyings", methods=["GET"])
def api_underlyings():
    """Proxy to DB API server: underlyings with recorded option snapshots.
    Drives the dashboard's underlying selector."""
    return _proxy_db("/api/underlyings")


@app.route("/api/iron_condor_preset", methods=["GET"])
def api_iron_condor_preset():
    """Proxy to DB API server: iron-condor leg set for chosen underlying
    and expiry (default NIFTY / next-weekly)."""
    params = {}
    for k in ("delta_target_pct", "hedge_points", "expiry", "underlying"):
        v = request.args.get(k)
        if v is not None:
            params[k] = v
    return _proxy_db("/api/iron_condor_preset", params=params or None)


# ---- Per-account ledger: autotrade's DB path -----------------------------
#
# autotrade reaches the database through here rather than calling db_api
# directly, so this app stays the single point of contact between the trading
# server and everything else — and the only component holding db_api's host
# and shared secret. It already proxies nine DB reads for the dashboard;
# these are the same idea for the trading loop.
#
# Note these are UNAUTHENTICATED, like every other route on this app. The
# shared secret protects db_api from the network, not from this app — so this
# app must not be publicly reachable. /place_order has always meant that.


@app.route("/api/trades", methods=["GET"])
def api_read_trades():
    """Raw ledger fills for ``?user=`` (or the configured trading account).
    Proxies db_api; autotrade FIFO-matches these against its own broker
    positions, which is why they come back raw rather than aggregated."""
    user = _account_for(request.args.get("user"))
    if not user:
        return [False, "No account configured (api_info.users) and no ?user= given"]
    return _proxy_db("/api/trades", params={"user": user})


@app.route("/api/trades", methods=["POST"])
def api_persist_trades():
    """Append fills to the ledger. Body: ``{user, trades, fetched_at?}``.

    Idempotent end to end — db_api uses INSERT OR IGNORE on Kite's
    ``trade_id`` — so autotrade may retry freely.
    """
    body = request.get_json(silent=True) or {}
    body.setdefault("user", TRADING_ACCOUNT)
    if not body.get("user"):
        return [False, "No account configured (api_info.users) and none in the body"]
    return _proxy_db_write("/api/trades", body)


@app.route("/api/adjustments", methods=["POST"])
def api_record_adjustment():
    """Write one adjustment audit row. Body is the row itself.

    Pass ``client_key`` for idempotency: a repeat with the same key returns
    the original id and ``duplicate: true`` rather than inserting again.
    Without it a response lost in transit becomes a second audit row, which
    double-counts in the dashboard's P&L chart.
    """
    body = request.get_json(silent=True) or {}
    body.setdefault("user", TRADING_ACCOUNT)
    if not body.get("user"):
        return [False, "No account configured (api_info.users) and none in the body"]
    return _proxy_db_write("/api/adjustments", body)


@app.route("/api/settlement_spot", methods=["GET"])
def api_settlement_spot():
    """Last recorded spot for ``?underlying=`` on ``?date=``, used to value
    options that expired instead of being closed. A ``null`` payload is a
    real answer — nothing recorded — not a failure."""
    underlying = request.args.get("underlying")
    date_str = request.args.get("date")
    if not underlying or not date_str:
        return [False, "underlying and date query params are required"]
    return _proxy_db("/api/settlement_spot",
                     params={"underlying": underlying, "date": date_str})


@app.route("/api/adjustments", methods=["GET"])
def api_adjustments():
    """Proxy to DB API server: audit rows from the ``adjustments`` table
    (one row per successful inward / outward / partial-reduce). Drives
    the adjustments analytics panel on the dashboard.

    Scoped to one account. Defaults ``user`` to the configured trading
    account so the dashboard doesn't have to know the name — the same
    convention ``/api/realised_pnl`` uses, and the reason both endpoints can
    be called from the page with no arguments.
    """
    user = _account_for(request.args.get("user"))
    if not user:
        return [False, "No account configured (api_info.users) and no ?user= given"]
    params = {"user": user}
    for k in ("limit", "expiry"):
        v = request.args.get(k)
        if v is not None:
            params[k] = v
    return _proxy_db("/api/adjustments", params=params)


@app.route("/api/open_positions", methods=["GET"])
def api_open_positions():
    """Return ALL NFO option legs for ``?user=`` (or the configured
    trading account) —
    open and closed-today, across every expiry — enriched with the last
    available delta from ``option_snapshot``.

    Formerly this only surfaced legs when they formed a recognised iron
    condor / short strangle on the nearest expiry; it now returns every
    leg so the dashboard can show all open books plus what was closed
    today. Each leg carries ``status`` ('open' | 'closed'):

      * **open**   — net quantity != 0 (still in the market). ``pnl`` is
        mark-to-market: ``(last - avg) * net_qty * multiplier``.
      * **closed** — traded today then fully squared off (net qty 0 with
        non-zero buy/sell qty). ``pnl`` is realised: ``sell_value -
        buy_value``. Legs never traded today are skipped.

    Delta is fetched with ``max_age_seconds=0`` (no freshness cutoff) so
    the column keeps the LAST recorded value after the recorder stops
    writing at 15:30, instead of going blank.

    Returns ``[True, {'legs': [...], 'available_expiries': [...],
                       'message': 'OK'}]`` where legs are sorted by
    (expiry, strike, option_type).

    ``available_books`` carries one entry per (underlying, expiry) with
    ``days_to_expiry`` and ``expected_hedge_distance`` — the wing width
    ``hedge_info`` prescribes for that DTE. The dashboard renders the first
    beside each expiry date and uses the second to flag hedges placed at
    the wrong distance from their short.
    """
    user = _account_for(request.args.get("user"))
    if not user:
        return [False, "No account configured (api_info.users) and no ?user= given"]
    try:
        raw = _call_with_reauth(user, "fetch_optoin_positions")
    except RuntimeError as e:
        return [False, f"Failed to fetch positions: {e}"]
    if raw is None:
        return [False, "Broker returned no data"]

    # Cost basis for the OPEN legs comes from the FIFO ledger, not from the
    # broker's buy_value/buy_quantity. Those are day-cumulative per
    # tradingsymbol, so a leg the adjustment engine closed and reopened at
    # the same strike (every partial-reduce does) averages the two tranches
    # together and prices the surviving lot below what it actually cost —
    # showing the operator a gain that was already realised and spent. One
    # ledger read for the whole table, not one per leg.
    #
    # Best-effort: a failed read leaves this None and each leg falls back to
    # the broker average, which is exact until a symbol carries a second
    # tranche. A dashboard that degrades beats one that goes blank; the
    # trading paths in risk_management make the opposite call and skip.
    #
    # The same read supplies ``realised_today`` for OPEN legs: what today's
    # closing fills banked on a leg that is still held (a partial close, or
    # the closed tranche of a close-and-reopen at one strike). The broker row
    # cannot separate that out, so without the ledger it is None, not 0.
    open_lots = None
    realised_today = None
    ok_ledger, ledger = _proxy_db("/api/trades", params={"user": user})
    if not ok_ledger:
        app.logger.warning(
            f"Could not read the trade ledger for {user}; leg entry prices "
            f"fall back to the broker's blended averages: {ledger}"
        )
    else:
        # An empty ledger is a successful read — every leg simply has no
        # corroborating lot and falls back on its own, no warning needed.
        try:
            fills = sort_fills(
                [t for t in (ledger or [])
                 if t.get("exchange") == LEDGER_EXCHANGE])
            open_lots = fifo_open_lots(fills)
            realised_today = realised_today_by_symbol(
                fills, datetime.now(IST).date())
        except Exception as e:
            open_lots = realised_today = None
            app.logger.warning(
                f"FIFO open-lot basis unavailable for {user}: {e}")

    def _ledger_basis(symbol, net_qty):
        """FIFO basis of ``symbol``'s open lots, or None when the ledger
        cannot corroborate them. The quantity must agree: the ledger lags
        the broker by up to one ingest, and stale lots would price a leg at
        a size it no longer holds."""
        if open_lots is None:
            return None
        signed, price = open_lot_basis(open_lots.get(symbol))
        if price is None or round(signed) != net_qty:
            return None
        return price

    all_legs = []
    for p in raw:
        sym = p.get("tradingsymbol")
        if not sym or p.get("exchange") != "NFO":
            continue
        buy_q = p.get("buy_quantity", 0) or 0
        sell_q = p.get("sell_quantity", 0) or 0
        net_qty = buy_q - sell_q
        traded_qty = max(buy_q, sell_q)
        # Skip only rows with no activity at all. net_qty == 0 with a
        # non-zero traded_qty is a position that was opened and closed
        # today — we now surface those as 'closed' rather than dropping them.
        if net_qty == 0 and traded_qty == 0:
            continue
        parsed = split_symbol(sym)
        if not parsed:
            continue
        underlying, expiry, strike, opt_type = parsed
        if opt_type not in ("CE", "PE"):
            continue

        buy_val = p.get("buy_value", 0) or 0
        sell_val = p.get("sell_value", 0) or 0
        last_price = p.get("last_price")
        multiplier = p.get("multiplier", 1) or 1

        # Last available delta — no freshness cutoff (see task 3 note in
        # _fetch_latest_option_quote) so it survives after 15:30.
        delta = None
        snap = _fetch_latest_option_quote(sym, max_age_seconds=0)
        if snap and snap.get("delta") is not None:
            delta = snap["delta"]

        if net_qty != 0:
            status = "open"
            side = "long" if net_qty > 0 else "short"
            display_qty = net_qty
            avg_price = _ledger_basis(sym, net_qty)
            if avg_price is None:
                if net_qty > 0 and buy_q:
                    avg_price = buy_val / buy_q
                elif net_qty < 0 and sell_q:
                    avg_price = sell_val / sell_q
            # Mark-to-market. (last - avg) * net_qty flips sign for shorts.
            if avg_price is not None and last_price is not None:
                pnl = (last_price - avg_price) * net_qty * multiplier
            else:
                pnl = None
            # Banked today on this still-open symbol. Trusted only when the
            # ledger corroborates the open quantity: a mismatch means the
            # ledger is behind the broker (up to one ingest), so its realised
            # figure is missing the latest closes too. A leg with no fill
            # against its direction today (no buy on a short, no sell on a
            # long) closed nothing, so it is 0 without asking the ledger.
            closing_q = buy_q if net_qty < 0 else sell_q
            if not closing_q:
                leg_realised_today = 0.0
            elif (realised_today is not None
                    and _ledger_basis(sym, net_qty) is not None):
                leg_realised_today = realised_today.get(sym, 0.0)
            else:
                leg_realised_today = None
        else:
            # Squared off today: realised P&L is sell proceeds minus buy
            # cost (both broker totals, already lot-size / multiplier
            # scaled). Side is indeterminate once flat, so mark it 'closed'.
            status = "closed"
            side = "closed"
            display_qty = 0
            avg_price = None
            pnl = sell_val - buy_val
            # Already all in ``pnl`` — a closed leg has nothing held to mark.
            leg_realised_today = 0.0

        # Today's realised P&L, FIFO over the ledger's fills — the closing
        # fills made today, matched against the oldest open lots at their
        # actual entry prices. A closed leg's ``pnl`` can't stand in for it:
        # the broker's sell_value - buy_value spans the leg's whole life, so
        # a leg carried in from an earlier day and closed today showed its
        # lifetime P&L as today's. Trusted only when the ledger agrees with
        # the broker's net quantity; otherwise it is missing a fill.
        if status == "open":
            leg_day_pnl = leg_realised_today
        elif (realised_today is not None and open_lots is not None
                and round(open_lot_basis(open_lots.get(sym))[0]) == 0):
            leg_day_pnl = realised_today.get(sym, 0.0)
        else:
            leg_day_pnl = None

        all_legs.append({
            "symbol": sym,
            # Which index this leg belongs to. Essential rather than
            # decorative: BANKNIFTY's monthly expiry always falls on the
            # last Tuesday, which is always also a NIFTY weekly, so expiry
            # alone does NOT identify a book. Close All keys off
            # (underlying, expiry) so a shared date can't sweep both.
            "underlying": underlying,
            "strike": strike,
            "option_type": opt_type,
            "expiry": expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry),
            "status": status,
            "side": side,
            # Signed net qty — 0 for closed. The dashboard uses this for
            # Close All (direction + size); closed legs are filtered there.
            "quantity": net_qty,
            # Absolute size to display (traded size for closed rows).
            "display_quantity": traded_qty if status == "closed" else abs(display_qty),
            "avg_price": avg_price,
            "last_price": last_price,
            "delta": delta,
            "multiplier": multiplier,
            "pnl": pnl,
            # Open legs: P&L banked today by partial closes on this symbol,
            # NOT included in ``pnl`` (which marks only the lots still
            # held). None when the ledger can't vouch for it. Closed legs: 0.
            "realised_today": leg_realised_today,
            # The dashboard's "today": FIFO-realised P&L of today's closing
            # fills on this symbol, open or closed leg alike. None when the
            # ledger can't vouch for it (see where it is set).
            "day_pnl": leg_day_pnl,
        })

    # Sort by underlying first so each index's legs stay contiguous — the
    # dashboard groups on (underlying, expiry) and would otherwise interleave
    # two indices' strikes under one header on a shared expiry date.
    all_legs.sort(key=lambda l: (l["underlying"], l["expiry"], l["strike"],
                                 l["option_type"]))
    available_expiries = sorted({leg["expiry"] for leg in all_legs})
    available_underlyings = sorted({leg["underlying"] for leg in all_legs})
    # One entry per BOOK, not per date. This is what the expiry picker binds
    # to; ``available_expiries`` is kept alongside it for older callers.
    # One entry per book, carrying the two derived figures the dashboard
    # shows beside each expiry: days-to-expiry (computed in IST here, not in
    # the browser, so an operator in another timezone sees the exchange's
    # day count) and the hedge width that DTE entitles the book to under
    # hedge_info. Resolved server-side because both the config and the
    # strike grid live here — see _expected_hedge_distance.
    available_books = []
    for (u, e) in sorted({(l["underlying"], l["expiry"]) for l in all_legs}):
        dte = _days_to_expiry(e)
        # Strategy is classified from the book's OPEN legs only. A closed leg
        # is no longer risk, and counting one would misread a book that has
        # been partly squared off today — a condor down to two open legs is a
        # directional book now, which is exactly what the operator needs to
        # see. ``monitored`` says whether the per-account monitor will walk
        # this book at all; anything else (a long strangle, an unrecognised
        # shape) is held without supervision, and that is worth showing
        # rather than leaving to be inferred from an absent log line.
        open_legs = [l for l in all_legs
                     if l["underlying"] == u and l["expiry"] == e
                     and l["status"] == "open"]
        strategy = detect_strategy([
            {"transtype": "buy" if l["side"] == "long" else "sell",
             "option_type": l["option_type"],
             "strike": l["strike"]}
            for l in open_legs
        ])
        available_books.append({
            "underlying": u,
            "expiry": e,
            "days_to_expiry": dte,
            "expected_hedge_distance": _expected_hedge_distance(u, dte),
            "strategy": strategy,
            "monitored": strategy in MONITORED_STRATEGIES,
            "adjustable": strategy in ADJUSTABLE_STRATEGIES,
        })

    if not all_legs:
        return [True, {"legs": [], "available_expiries": [],
                       "available_underlyings": [], "available_books": [],
                       "message": "No NFO option positions today"}]

    return [True, {"legs": all_legs,
                   "available_expiries": available_expiries,
                   "available_underlyings": available_underlyings,
                   "available_books": available_books,
                   "message": "OK"}]


@app.route("/api/market_snapshots", methods=["GET"])
def api_market_snapshots():
    """Proxy to DB API server: market_snapshot rows for ``?date=YYYY-MM-DD``."""
    return _proxy_db("/api/market_snapshots",
                     params={"date": request.args.get("date")} if request.args.get("date") else None)


@app.route("/api/option_snapshot", methods=["GET"])
def api_option_snapshot():
    """Proxy to DB API server: latest option_snapshot for ``?date=YYYY-MM-DD``
    and ``?underlying=`` (NIFTY by default)."""
    params = {}
    for k in ("date", "underlying"):
        v = request.args.get(k)
        if v is not None:
            params[k] = v
    return _proxy_db("/api/option_snapshot", params=params or None)


@app.route("/status")
def monitor_status():
    """Whether ``?user=``'s monitor is running, derived rather than read.

    Previously this reported ``monitor_info.autotrade`` — a flag autotrade.py
    wrote about itself. That could only ever be as truthful as its writer: a
    crashed monitor left the flag at ``true`` and this endpoint kept reporting
    "Monitoring" through a dead session. It now combines two independent
    facts, neither of which the monitor gets to self-report:

      * the session window (``is_market_hours``, shared with autotrade.py so
        the two can't disagree), and
      * whether that account's ``autotrade@<user>`` unit is active, per
        systemd.

    BOTH facts are per-account now that the monitor is one process per user.
    Reporting the shared ``autotrade`` unit and the shared log mtime made the
    pill true if ANY monitor was alive — the same class of lie as the old
    self-reported flag, one level up. An unknown or unconfigured account
    reports DOWN rather than falling back to another account's monitor.

    Response shape is unchanged — ``[ok, status_text, last_run]`` — so the
    dashboard's get_status() needs no edit.
    """
    account = _known_account(request.args.get("user"))
    if not account:
        # Deliberately not "Unknown": an account that isn't configured has no
        # monitor, and saying so is more useful than implying a probe failure.
        return [False, "No such account configured", None]
    last_run = watch_log_last_modified(account)
    state = (systemd_states([monitor_unit(account)]) or [{}])[0]

    if not is_market_hours():
        # Outside the window the unit may legitimately be up (supervisor loop
        # idling) or down (stopped by timer) — neither is a problem, so don't
        # raise an alarm either way.
        return [False, "Outside market hours", last_run]
    if state.get("active"):
        return [True, "Monitoring", last_run]
    if state.get("state") == "unavailable":
        # Couldn't ask systemd. Distinguished from a confirmed-down unit so a
        # transient probe failure doesn't read as "the monitor has crashed".
        return [False, "Unknown — systemd probe failed", last_run]
    return [False,
            f"DOWN — {monitor_unit(account)} {state.get('state') or 'unknown'}",
            last_run]
@app.route("/get_orders", methods=["GET"])
def get_orders():
    user = request.args.get("user")
    orders = {}
    try:
        if user:
            orders[user] = _call_with_reauth(user, "fetch_orders")
        else:
            for u in list(broker_connections.keys()):
                orders[u] = _call_with_reauth(u, "fetch_orders")
        return [True, orders]
    except Exception as e:
        print(f"Failed to get orders with error: {e}")
        return [False, f"Failed to get orders with error: {e}"]


@app.route("/get_positions", methods=["GET"])
def get_positions():
    positions = {}
    try:
        user = request.args.get("user")
        print(f"Trying to get positions for user {user}")
        if user:
            positions[user] = _call_with_reauth(user, "fetch_optoin_positions")
        else:
            for u in list(broker_connections.keys()):
                positions[u] = _call_with_reauth(u, "fetch_optoin_positions")
        return [True, positions]
    except Exception as e:
        print(f"Failed to get positions with error: {e}")
        return [False, f"Failed to get positions with error: {e}"]


@app.route("/get_trades", methods=["GET"])
def get_trades():
    """Per-user executed trades from Kite (``kite.trades()``).

    Used by brain.py to build a local FIFO ledger for realised-P&L
    accounting. The response shape is::

        [True, {user: [trade_dict, ...]}]

    where each ``trade_dict`` carries ``tradingsymbol``, ``exchange``,
    ``transaction_type`` (BUY/SELL), ``quantity`` (already lot-size
    multiplied), ``average_price``, plus ``trade_id`` / ``order_id`` and
    timestamps (``fill_timestamp`` / ``exchange_timestamp``).

    Kite only serves the CURRENT trading day's trades — historical
    coverage requires persisting these locally or scraping the Console
    CSV. No batching needed because the trade book per day is small.
    """
    trades = {}
    user = request.args.get("user")
    try:
        if user:
            trades[user] = _call_with_reauth(user, "fetch_trades")
        else:
            for u in list(broker_connections.keys()):
                trades[u] = _call_with_reauth(u, "fetch_trades")
    except Exception as e:
        # Report the exception TYPE alongside its text. Kite's messages are
        # often terse ("Incorrect `api_key` or `access_token`") and the class
        # is what distinguishes a token problem from a network one — this
        # string is what lands in the operator's LedgerBehind alert, so it is
        # the only place the cause is visible without shelling into the host.
        detail = f"{type(e).__name__}: {e}"
        print(f"Failed to get trades for {user or 'all users'}: {detail}")
        return [False, f"Failed to get trades for {user or 'all users'}: "
                       f"{detail}"]
    # A None from the broker layer must never be dressed up as success: the
    # caller would read it as "no fills today" and quietly stop advancing the
    # ledger. fetch_trades now raises rather than returning None, so this is a
    # guard against any other path acquiring that habit.
    missing = [u for u, v in trades.items() if v is None]
    if missing:
        return [False, f"Broker returned no trade list for "
                       f"{', '.join(missing)} (no exception raised) — treating "
                       f"as a failure, not as an empty trade book"]
    return [True, trades]


@app.route("/get_pnl", methods=["GET"])
def get_pnl():
    """Per-user P&L straight from Kite (``kite.positions()['net']``).

    An independent second opinion on realised P&L, useful for reconciling
    against ``helpers.trade_ledger``'s FIFO figure (which is what the risk
    engine actually uses). Note Kite's realised field is day-scoped, so it is
    not a substitute for the ledger across a date boundary. Each user's payload
    matches the ``fetch_pnl`` shape in ``TradingBroker/kite_api_bot.py``::

        { "positions": [ {...per-position fields..., pnl, realised,
                          unrealised, m2m}, ... ],
          "totals":    {"pnl": ..., "realised": ..., "unrealised": ...,
                        "m2m": ...} }

    Query params:
        user  optional. When set, fetch only that user. When omitted,
              fetch for every currently-authenticated broker connection.

    Returns ``[True, {user: payload}]`` on success or
    ``[False, message]`` on failure. A user whose Kite call errors gets
    ``None`` as their payload — callers should treat that as a hard miss
    rather than silently working with stale config-bucket numbers.
    """
    pnl = {}
    try:
        user = request.args.get("user")
        if user:
            pnl[user] = _call_with_reauth(user, "fetch_pnl")
        else:
            for u in list(broker_connections.keys()):
                pnl[u] = _call_with_reauth(u, "fetch_pnl")
        return [True, pnl]
    except Exception as e:
        print(f"Failed to get pnl with error: {e}")
        return [False, f"Failed to get pnl with error: {e}"]


def _fetch_latest_market_snapshot():
    """Pull the latest market_snapshot from the DB API server (or ``None``
    when the server is unreachable / has no fresh row)."""
    return db_get("/api/latest_market_snapshot",
                  params={"max_age_seconds": DB_FRESHNESS_SECONDS})


def _fetch_latest_option_quote(symbol, max_age_seconds=DB_FRESHNESS_SECONDS):
    """Pull the latest option_snapshot row for ``symbol`` from the DB API
    server (or ``None``).

    ``max_age_seconds`` bounds how stale a row may be. The default
    (``DB_FRESHNESS_SECONDS``) is right for the live-quote path, which must
    fall through to KITE when the recorder is down. Pass ``0`` to disable
    the cutoff and get the LAST available row regardless of age — used for
    open-position delta enrichment so the delta column keeps showing the
    last recorded value after the recorder stops writing at 15:30.
    """
    return db_get(f"/api/latest_option_quote/{symbol}",
                  params={"max_age_seconds": max_age_seconds})


def _db_quote_for_instrument(inst, market_snap):
    """Return a Kite-shaped quote dict from the DB API server for ``inst``
    (``EXCHANGE:SYMBOL``), or ``None`` when the instrument can't be served
    from the recorder tables.

    ``market_snap`` is a single ``_fetch_latest_market_snapshot()`` result
    reused across a batch so we don't hit the DB API for every spot / fut /
    VIX instrument. Option (NFO) instruments fall through to their own
    per-symbol HTTP lookup.
    """
    if inst.startswith("NFO:"):
        symbol = inst[4:]
        if symbol.endswith("FUT"):
            if market_snap and market_snap.get("fut_price") is not None:
                return {"last_price": market_snap["fut_price"]}
            return None
        opt = _fetch_latest_option_quote(symbol)
        if opt and opt.get("ltp") is not None:
            # ``delta`` is BS-computed in the recorder; only present for
            # strikes the recorder chose to persist (|delta| > MIN_ABS_DELTA).
            # Absent for KITE-sourced quotes and for deep-OTM strikes outside
            # the recorder band — callers should treat None as "unknown".
            # oi/volume pass through as recorded, None included. Defaulting
            # oi to 0 here used to make "the recorder captured no OI" look
            # identical to "nothing is open", and the adjustment engine's
            # liquidity gate reads that as a dead strike. None means unknown.
            return {
                "last_price": opt["ltp"],
                "oi": opt.get("oi"),
                "volume": opt.get("volume"),
                "delta": opt.get("delta"),
            }
        return None
    if inst == "NSE:NIFTY 50" and market_snap and market_snap.get("spot_price") is not None:
        return {"last_price": market_snap["spot_price"]}
    if inst == "NSE:NIFTY BANK" and market_snap and market_snap.get("banknifty_spot") is not None:
        return {"last_price": market_snap["banknifty_spot"]}
    if inst == "NSE:INDIA VIX" and market_snap and market_snap.get("india_vix") is not None:
        return {"last_price": market_snap["india_vix"]}
    return None


def _wants_live():
    """True when the caller asked us to skip the DB cache and hit KITE
    directly (``?live=true``). Used by clients that populate the DB itself
    (e.g. fetch_db_data.py) so they can't end up reading their own
    stale rows back."""
    return request.args.get("live", "").lower() == "true"


def _market_speed_image_dir():
    report_dir = Path(__file__).resolve().parent / "TrendAnalysis" / "data"
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir


def _purge_market_speed_images(report_dir: Path | None = None):
    target_dir = report_dir or _market_speed_image_dir()
    for image_path in sorted(target_dir.glob("*.png")):
        try:
            image_path.unlink()
        except OSError:
            pass


def _market_speed_analysis(csv_path):
    csv_file = Path(csv_path)
    if not csv_file.exists():
        return "No CSV available yet. Trigger a live market-speed read first."

    analyzer = MarketAnalyzer(str(csv_file))
    try:
        analyzer.load_data()
    except Exception as exc:
        return f"Analysis unavailable: {exc}"

    lines = [
        f"Records: {len(analyzer.df)}",
        f"Latest time: {analyzer.df['time'].max().strftime('%Y-%m-%d %H:%M:%S') if not analyzer.df.empty else 'n/a'}",
    ]

    try:
        analyzer.calculate_daily_open_prices()
        analyzer.analyze_hourly_trends()
        analyzer.detect_support_resistance_levels(lookback=10, atr_multiplier=0.75, max_levels=5)
    except Exception as exc:
        return "\n".join(lines) + f"\nWarning: partial analysis failed: {exc}"

    if analyzer.hourly_trends:
        recent = analyzer.hourly_trends[-3:]
        lines.append("\nHourly trend summary:")
        for row in recent:
            lines.append(
                f"- {row['hour']}: {row['trend']} {row['strength']} | "
                f"O={row['open']:.2f} H={row['high']:.2f} L={row['low']:.2f} C={row['close']:.2f}"
            )

    support = analyzer.support_levels or {}
    resistance = analyzer.resistance_levels or {}
    if support:
        lines.append("\nSupport levels:")
        for level, info in sorted(support.items(), key=lambda kv: kv[1]['strength'], reverse=True):
            lines.append(f"- {level:.2f} (strength {info['strength']:.2f}, {info['timestamp']})")
    if resistance:
        lines.append("\nResistance levels:")
        for level, info in sorted(resistance.items(), key=lambda kv: kv[1]['strength'], reverse=True):
            lines.append(f"- {level:.2f} (strength {info['strength']:.2f}, {info['timestamp']})")

    return "\n".join(lines)


@app.route("/api/market_speed_image", methods=["GET"])
def api_market_speed_image():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify([False, "Missing market speed image filename"]), 400

    raw_name = filename.split("?", 1)[0].strip()
    safe_name = Path(raw_name).name
    if not safe_name or safe_name in {".", ".."}:
        return jsonify([False, "Invalid market speed image filename"]), 400

    file_path = _market_speed_image_dir() / safe_name
    if not file_path.exists() or file_path.suffix.lower() != ".png":
        return jsonify([False, f"Image not found: {safe_name}"]), 404

    return send_file(file_path, mimetype="image/png")


@app.route("/api/market_speed", methods=["GET"])
def api_market_speed():
    """One-shot market-speed check using the shared helper module."""
    user = _account_for(request.args.get("user")) or MARKET_DATA_ACCOUNT
    symbols = request.args.get("symbols", "NSE:NIFTY 50")
    interval = int(request.args.get("interval", "60") or 60)
    date_stamp = datetime.now(ist).strftime("%Y%m%d")
    csv_path = request.args.get("csv") or str((Path(__file__).resolve().parent / "TrendAnalysis" / f"market_speed_{date_stamp}.csv"))
    csv_file = Path(csv_path)
    if csv_file.name.lower() == "market_speed.csv":
        csv_file = csv_file.with_name(f"market_speed_{date_stamp}.csv")
    indices_csv_path = csv_file.with_name(f"market_indices_{date_stamp}.csv")
    plot_path = request.args.get("plot")

    try:
        kite = _ensure_broker(user).get_connection()
        result = monitor_market_speed(
            kite,
            symbols=symbols,
            interval=interval,
            output_csv=str(csv_file),
            plot_path=plot_path,
            max_iterations=1,
        )
        csv_file = Path(result.get("csv_path", str(csv_file))) if isinstance(result, dict) else csv_file
        indices_csv_path = csv_file.with_name(csv_file.name.replace("market_speed_", "market_indices_"))
    except Exception as exc:
        return jsonify([False, f"Market speed check failed: {exc}"]), 500

    last_point = result["tracked_points"][-1] if result["tracked_points"] else None
    report_dir = _market_speed_image_dir()
    _purge_market_speed_images(report_dir)
    report_key = f"{(user or 'market').replace(' ', '_')}_{uuid.uuid4().hex[:8]}"
    single_chart_path = report_dir / f"{report_key}_single.png"
    multi_chart_path = report_dir / f"{report_key}_multi.png"
    analysis_text = _market_speed_analysis(str(csv_file))

    try:
        speed_rows = load_rows(csv_file)
        index_rows = load_rows(indices_csv_path)
        if speed_rows:
            plot_market_speed(speed_rows, single_chart_path, mode="single")
        if index_rows:
            plot_market_speed(index_rows, multi_chart_path, mode="multi")
        elif speed_rows:
            plot_market_speed(speed_rows, multi_chart_path, mode="multi")
    except Exception:
        single_chart_path = None
        multi_chart_path = None

    payload = {
        "symbols": result["symbols"],
        "last_point": last_point,
        "current_average_speed": result["current_average_speed"],
        "csv_path": csv_path,
        "analysis": analysis_text,
        "single_chart_url": f"/api/market_speed_image?file={quote(single_chart_path.name)}" if single_chart_path and single_chart_path.exists() else None,
        "multi_chart_url": f"/api/market_speed_image?file={quote(multi_chart_path.name)}" if multi_chart_path and multi_chart_path.exists() else None,
    }
    return jsonify([True, payload])


@app.route("/get_current_price/<symbol>/<request_type>", methods=["GET"])
def get_current_price(symbol, request_type):
    if not symbol:
        symbol = request.args.get("symbol")
    if not request_type:
        request_type = request.args.get("request_type")
    exchange = 'NFO' if request_type == 'option' else 'NSE'
    inst = f"{exchange}:{symbol}"

    # Try DB first unless the caller demanded a live broker hit. Only fetch
    # the market_snapshot row for NSE lookups — NFO option lookups have
    # their own per-symbol query inside the helper.
    if not _wants_live():
        market_snap = _fetch_latest_market_snapshot() if exchange == 'NSE' else None
        db_quote = _db_quote_for_instrument(inst, market_snap)
        if db_quote is not None:
            print(f"/get_current_price served {inst} from DB snapshot")
            return [True, {inst: db_quote}]

    print(f"Sending request fetch_last_price to broker with symbol {symbol}, exchange {exchange}")
    try:
        # Quotes come through the market-data account's session, not the
        # trading account's — an LTP is identical for every account, and only
        # this one holds the subscription.
        ltp = _call_with_reauth(MARKET_DATA_ACCOUNT, "fetch_last_price",
                                symbol=symbol, exchange=exchange)
    except RuntimeError as e:
        print(f"Price fetch failed for {symbol}: {e}")
        return [False, f"Price fetch failed for {symbol}: {e}"]
    if ltp:
        return [True, ltp]
    return [False, None]


@app.route("/get_quotes", methods=["GET"])
def get_quotes():
    """Batch quote endpoint. Serves from ``market_snapshot``/``option_snapshot``
    when the latest row is fresh (within ``DB_FRESHNESS_SECONDS``), otherwise
    falls through to ``kite.quote()`` for the misses only.

    Accepts comma-separated ``EXCHANGE:SYMBOL`` instruments via the
    ``instruments`` query param, e.g.
    ``/get_quotes?instruments=NFO:NIFTY25SEP24500PE,NSE:NIFTY 50``.

    Returns ``[True, {instrument: quote_dict, ...}]``. Quotes from the DB
    carry ``last_price`` (+ ``oi`` for options); KITE-sourced quotes carry
    the full Kite payload.
    """
    raw = request.args.get("instruments", "")
    instruments = [s.strip() for s in raw.split(",") if s.strip()]
    if not instruments:
        return [False, "instruments query param is required (comma-separated EXCHANGE:SYMBOL)"]

    live = _wants_live()
    if live:
        # Caller wants fresh KITE data for every instrument; skip DB lookup
        # entirely so the recorder (which populates the DB) can't read its
        # own stale rows back.
        result = {}
        misses = list(instruments)
    else:
        market_snap = _fetch_latest_market_snapshot()
        result = {}
        misses = []
        for inst in instruments:
            q = _db_quote_for_instrument(inst, market_snap)
            if q is not None:
                result[inst] = q
            else:
                misses.append(inst)

    if misses:
        try:
            kite_quotes = _call_with_reauth(MARKET_DATA_ACCOUNT,
                                            "fetch_quotes", misses)
        except RuntimeError as e:
            if not result:
                return [False, f"Quotes fetch failed: {e}"]
            print(f"DB hits {len(result)}/{len(instruments)}; KITE fallback failed for misses: {e}")
        else:
            if kite_quotes:
                result.update(kite_quotes)

    if not result:
        return [False, "Broker returned no data and DB had no fresh rows"]
    print(f"/get_quotes served {len(result)}/{len(instruments)} "
          f"(DB hits={len(instruments) - len(misses)}, KITE misses={len(misses)})")
    return [True, result]


# ---- Listed expiries ------------------------------------------------------
#
# The recorder used to GENERATE the expiry dates it quoted — N consecutive
# Tuesdays — which is wrong twice over. It misses contracts NSE shifted off
# their usual weekday (Dussehra on Tue 20-Oct-2026 moved that weekly to Mon
# 19-Oct, so nothing for that expiry was ever recorded), and it invents
# contracts NSE never listed (NIFTY lists 5 weeklies + 3 monthlies, not 10
# consecutive weeklies). Both failures look identical in the recorder log.
#
# This endpoint answers the question from the only authoritative source: the
# instrument dump. The FILTERING HAPPENS HERE, on the trading server, and the
# recorder receives a handful of date strings. That split is deliberate — the
# NFO dump is ~100k rows and the recorder lives on the DB box; shipping it
# across would cost far more than the problem it solves.
_expiry_cache = {"day": None, "map": None}
_expiry_cache_lock = threading.Lock()


def _build_expiry_map(dump):
    """Reduce an instrument dump to ``{(name, segment): [expiry_str, ...]}``.

    Sorted ascending and de-duplicated: one NIFTY expiry covers hundreds of
    strikes in the dump, and callers only ever want the distinct dates.
    """
    out = {}
    for row in dump:
        name = row.get("name")
        segment = row.get("segment")
        expiry = row.get("expiry")
        if not name or not segment or not expiry:
            continue
        # kiteconnect parses ``expiry`` into a datetime.date, but a dump read
        # straight from the CSV leaves it a string. Accept either rather than
        # depending on which path produced it.
        expiry_str = (expiry.isoformat() if hasattr(expiry, "isoformat")
                      else str(expiry)[:10])
        out.setdefault((name, segment), set()).add(expiry_str)
    return {key: sorted(values) for key, values in out.items()}


def _expiry_map():
    """Today's ``{(name, segment): [expiry, ...]}``, fetched at most once per
    IST day. Returns None if the broker gave us nothing.

    Cached on the DAY because that is Kite's own refresh cadence (~08:00
    IST). Under the lock for the whole fetch: the download is the expensive
    thing, and two request threads racing on the first cycle of the day would
    otherwise each pull 100k rows on a host that cannot spare the memory.
    Waiting is strictly cheaper than duplicating it.

    A FAILED fetch is deliberately not cached, so a broker hiccup at 09:15
    doesn't leave the recorder without listed expiries for the rest of the
    session.
    """
    day = datetime.now(IST).strftime("%Y-%m-%d")
    with _expiry_cache_lock:
        if _expiry_cache["day"] == day and _expiry_cache["map"] is not None:
            return _expiry_cache["map"]
        dump = _call_with_reauth(MARKET_DATA_ACCOUNT, "fetch_instruments", "NFO")
        if not dump:
            return None
        mapping = _build_expiry_map(dump)
        # Drop the 100k-row list before returning rather than waiting for it
        # to fall out of scope — on this host the difference is measurable.
        del dump
        _expiry_cache["day"] = day
        _expiry_cache["map"] = mapping
        print(f"/get_expiries refreshed the NFO instrument dump for {day}: "
              f"{len(mapping)} (name, segment) pairs")
        return mapping


@app.route("/get_expiries/<underlying>", methods=["GET"])
def get_expiries(underlying):
    """Expiry dates Kite ACTUALLY LISTS for ``underlying``, ascending.

    ``?segment=`` selects the contract type, default ``NFO-OPT``; pass
    ``NFO-FUT`` for futures. Returns
    ``[True, {"underlying", "segment", "expiries": ["YYYY-MM-DD", ...]}]``.

    An empty ``expiries`` list with ``True`` means the dump was fetched and
    this (name, segment) has nothing listed — a real answer, distinct from
    ``[False, reason]`` meaning we could not find out. The recorder needs
    that distinction to decide whether falling back to generated dates is
    the right move.
    """
    segment = request.args.get("segment", "NFO-OPT").upper()
    name = underlying.upper()
    try:
        mapping = _expiry_map()
    except RuntimeError as e:
        return [False, f"Could not fetch the instrument dump: {e}"]
    if mapping is None:
        return [False, "Broker returned no instrument dump"]
    expiries = mapping.get((name, segment), [])
    return [True, {"underlying": name, "segment": segment,
                   "expiries": expiries}]


@app.route("/place_order", methods=["GET"])
def place_order():
    user = request.args.get("user")
    symbol = request.args.get("symbol")
    quantity = request.args.get("quantity")
    transaction_type = request.args.get("transaction_type")
    exchange = request.args.get("exchange")
    if not user:
        return [False, "User cannot be null"]
    try:
        order_id = _call_with_reauth(user, "place_order",
                                     symbol=symbol, quantity=quantity,
                                     transaction_type=transaction_type,
                                     exchange=exchange)
        if order_id:
            return [True, f"Order is placed successfully:{order_id}"]
        return [False, "Failed to place order with error"]
    except Exception as e:
        print(f"Failed to place order with error: {e}")
        return [False, f"Failed to place order with error: {e}"]


# /get_watch_config and /put_watch_config were removed. The dashboard's
# Config card was their only caller (nothing in Python used them over HTTP —
# autotrade and the monitor call helpers.basic.put_config on the file
# directly). /put_watch_config in particular was an unauthenticated GET that
# wrote the live trading config: `?dry_run=true` from anywhere on the network
# would have silently stopped live trading, since dry_run is a top-level key.
# Config edits now go through the file on the host.


@app.route("/broker_identity", methods=["GET"])
def broker_identity():
    """Which Zerodha account ``?user=`` is actually logged into.

    Diagnostic for the one failure the rest of the system cannot see. Every
    account-scoped path is correctly keyed on the local name — separate
    ``KITE_CONNECT`` instance, separate ``KiteConnect``, separate access
    token, cached per user in ``broker_connections``. So if two local names
    are pointed at the SAME Zerodha login (the obvious way to add an account
    being to copy the credentials block and change its key), every view
    returns identical positions, orders and P&L. That is correct behaviour
    over a wrong configuration, and it is indistinguishable from a bug
    without asking the broker who it thinks you are.

    Call it once per account and compare ``user_id``:

        /broker_identity?user=sashi
        /broker_identity?user=madhu

    Same ``user_id`` means one account configured twice, not two accounts.

    Deliberately returns only ``user_id`` / ``user_name`` / ``broker``, not
    Kite's whole profile: this app listens on 0.0.0.0:80 and the rest of that
    payload (email, PAN, bank details) answers no question worth asking here.

    Uses the cached connection, so it costs nothing for an account already
    connected — but WILL trigger a full Selenium login for one that isn't.
    """
    user = _known_account(request.args.get("user"))
    if not user:
        return [False, "No such account configured"]
    try:
        profile = _call_with_reauth(user, "fetch_profile")
    except AttributeError:
        # Deploy skew. main.py and TradingBroker/kite_api_bot.py are copied to
        # the host as separate files, so this endpoint can arrive before the
        # method it calls. ``_call_with_reauth`` reaches the method through
        # getattr, so the symptom would otherwise be an unhandled 500 naming
        # neither file. db_api.py guards its own version skew for the same
        # reason — see _check_db_queries_version there.
        return [False,
                "TradingBroker/kite_api_bot.py on this host is out of date: "
                "it has no fetch_profile(). Copy the current file to this "
                "server and restart flaskapp."]
    except RuntimeError as e:
        return [False, f"Could not authenticate {user}: {e}"]
    if not profile:
        return [False, f"Broker returned no profile for {user}"]
    return [True, {
        "configured_account": user,
        "broker_user_id": profile.get("user_id"),
        "broker_user_name": profile.get("user_name"),
        "broker": profile.get("broker"),
    }]


@app.route("/get_watch_log", methods=["GET"])
def get_watch_log():
    """Today's monitor log for ``?user=``, defaulting to the trading account.

    The account is allowlisted against ``api_info.users`` before it reaches
    ``watch_log_path``, because that is what turns it into a filename — an
    unchecked value here would make this an arbitrary-file reader. The
    dashboard has always sent ``?user=``; it was ignored until the log became
    per-account.
    """
    account = _known_account(request.args.get("user"))
    if not account:
        return [False, {"Error": "No such account configured"}]
    try:
        with open(watch_log_path(account), "r") as file:
            watch_log = file.read()
        return [True, watch_log]
    except FileNotFoundError:
        # Distinct from a read error: before the first cycle of the day, or for
        # an account whose monitor has not been started, there is simply no
        # file yet. An empty log reads better than an error the operator has
        # to interpret.
        return [True, ""]
    except Exception as e:
        return [False, {"Error": f"Failed to get the watch log: {e}"}]

# Run the app
def _check_db_api_config():
    """Resolve the DB API's URL and secret at startup, or refuse to run.

    Per-request these failures degrade to a broker fallback (db_get returns
    None), which is right for one bad call but wrong as a steady state: every
    quote would bypass the snapshot cache and hit KITE, and autotrade's ledger
    proxy endpoints would fail on every cycle — stopping risk decisions. Both
    would be visible only as symptoms. Say it here instead.
    """
    try:
        url, secret = db_url(), db_api_secret()
    except RuntimeError as e:
        print(f"FATAL: {e}\n"
              f"       Add it under api_info in {WATCH_CONFIG}, matching the "
              f"value on the DB API server.")
        return False
    print(f"DB API: {url} (shared secret configured, "
          f"{len(secret)} chars)")
    return True


if __name__ == "__main__":
    print(f"Trading account: {TRADING_ACCOUNT or '(unset)'} · "
          f"market-data account: {MARKET_DATA_ACCOUNT or '(unset)'}")
    if not _check_db_api_config():
        raise SystemExit(1)
    print(web_auth.describe_config())
    # Authenticate the market-data account at startup: every quote path
    # depends on it, so failing here should stop the service rather than
    # surface as an unexplained quote failure on the first request.
    connection_status = get_broker(MARKET_DATA_ACCOUNT)
    if not connection_status[0]:
        print(f"Failed in KITE Connection for user {MARKET_DATA_ACCOUNT}")
    else:
        app.run(host='0.0.0.0', port=80, debug=False)
