from flask import Flask, request, render_template
import yaml
from datetime import datetime
from urllib.parse import urlparse
import pytz
ist = pytz.timezone('Asia/Kolkata')

from kiteconnect.exceptions import TokenException
from TradingBroker.kite_api_bot import KITE_CONNECT

from Monitor.options import *
from helpers.alert import AlertMobile
from helpers.basic import get_config, put_config, split_symbol
from helpers.market_data import db_get

# Maximum age of a DB snapshot we'll trust for live quote serving. The
# recorder samples every 30s; anything older than 2 minutes is treated as
# stale and we fall through to KITE.
DB_FRESHNESS_SECONDS = 120

WATCH_CONFIG = "Monitor/config.yaml"
BROKER_CONFIG = "TradingBroker/config.yaml"

# Create Flask app
app = Flask(__name__)
broker_connections = {}
SUPER_USER = "sashi"


def _safe_alert(heading, message):
    """Best-effort alert that never raises."""
    try:
        AlertMobile().send(heading=heading, message=message)
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

    Why: KiteConnect access tokens expire daily and after long idle periods.
    Without this guard, every call after expiry silently returns errors and
    the monitor can't place orders. Limited to one reauth attempt per call so
    a persistent auth failure surfaces quickly rather than retrying forever.
    """
    broker = _ensure_broker(user)
    try:
        return getattr(broker, method_name)(*args, **kwargs)
    except TokenException as e:
        print(f"Session expired for {user} during {method_name}: {e}. Re-authenticating.")
        _safe_alert("SessionExpired", f"{user}: re-authing during {method_name}")
        broker_connections.pop(user, None)
        ok, msg = get_broker(user)
        if not ok:
            _safe_alert("ReauthFailed", f"{user}: {msg}")
            raise RuntimeError(f"Re-auth failed for {user}: {msg}")
        return getattr(broker_connections[user], method_name)(*args, **kwargs)
@app.route("/")
def home():
    # Pull username / host / port out of ``api_info`` so the dashboard
    # template doesn't carry hard-coded copies of values that already
    # live in Monitor/config.yaml. First user wins on multi-user setups;
    # url is parsed safely so a missing or malformed value falls back
    # to localhost:80 instead of crashing the page.
    api_info = (get_config(WATCH_CONFIG) or {}).get("api_info") or {}
    users_raw = str(api_info.get("users") or "").strip()
    first_user = users_raw.split(",")[0].strip() if users_raw else ""
    display_user = first_user[:1].upper() + first_user[1:] if first_user else "Trader"
    parsed = urlparse(str(api_info.get("url") or ""))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return render_template(
        "dashboard.html",
        dashboard_username=display_user,
        dashboard_username_lower=first_user.lower() or "trader",
        dashboard_host=host,
        dashboard_port=port,
    )


@app.route("/snapshots")
def snapshots_page():
    """Render the market-snapshot visualization page."""
    return render_template("snapshots.html")


def _proxy_db(path, params=None):
    """Forward a GET to the DB API server. Wraps the raw payload back in the
    ``[success, payload]`` envelope this Flask app uses, or ``[False, msg]``
    on transport failure."""
    payload = db_get(path, params=params)
    if payload is None:
        return [False, f"DB API unavailable for {path}"]
    return [True, payload]


@app.route("/api/snapshot_dates", methods=["GET"])
def api_snapshot_dates():
    """Proxy to DB API server: list of YYYY-MM-DD dates with snapshot rows."""
    return _proxy_db("/api/snapshot_dates")


@app.route("/api/index_summary", methods=["GET"])
def api_index_summary():
    """Proxy to DB API server: open/close/change/horizon-% for NIFTY 50 / INDIA VIX."""
    return _proxy_db("/api/index_summary")


@app.route("/api/strikes_by_delta", methods=["GET"])
def api_strikes_by_delta():
    """Proxy to DB API server: strikes whose |delta| sits in ``?min=&max=``
    (percent) for the latest snapshot, grouped by expiry and option_type."""
    params = {}
    if request.args.get("min") is not None:
        params["min"] = request.args.get("min")
    if request.args.get("max") is not None:
        params["max"] = request.args.get("max")
    return _proxy_db("/api/strikes_by_delta", params=params or None)


@app.route("/api/iron_condor_preset", methods=["GET"])
def api_iron_condor_preset():
    """Proxy to DB API server: iron-condor leg set for chosen expiry
    (default next-weekly)."""
    params = {}
    for k in ("delta_target_pct", "hedge_points", "expiry"):
        v = request.args.get(k)
        if v is not None:
            params[k] = v
    return _proxy_db("/api/iron_condor_preset", params=params or None)


@app.route("/api/adjustments", methods=["GET"])
def api_adjustments():
    """Proxy to DB API server: audit rows from the ``adjustments`` table
    (one row per successful inward / outward / partial-reduce). Drives
    the adjustments analytics panel on snapshots.html."""
    params = {}
    for k in ("limit", "expiry"):
        v = request.args.get(k)
        if v is not None:
            params[k] = v
    return _proxy_db("/api/adjustments", params=params or None)


@app.route("/api/open_positions", methods=["GET"])
def api_open_positions():
    """Return the open iron-condor or short-strangle legs for ``?user=``
    (or ``SUPER_USER``), enriched with delta from the latest option_snapshot.

    Algorithm:
      1. Fetch all open NFO option positions.
      2. Group by expiry and pick the **nearest** (smallest) expiry — any
         positions on later expiries are ignored.
      3. Within that expiry, group the legs by role
         (long_pe / short_pe / short_ce / long_ce) and look for a match:
            * **Iron condor** — exactly one of each role with
              long_pe < short_pe < short_ce < long_ce strikes.
            * **Short strangle** — exactly one short_pe + one short_ce
              with no longs and short_pe < short_ce strikes.
      4. On a match, return the matching legs in canonical order; otherwise
         return an empty list with an explanation.

    Returns ``[True, {'status': 'iron_condor' | 'short_strangle' | 'none',
                       'expiry': '...' | None,
                       'message': '...',
                       'legs': [...] }]``.
    """
    user = request.args.get("user") or SUPER_USER
    try:
        raw = _call_with_reauth(user, "fetch_optoin_positions")
    except RuntimeError as e:
        return [False, f"Failed to fetch positions: {e}"]
    if raw is None:
        return [False, "Broker returned no data"]

    all_legs = []
    for p in raw:
        sym = p.get("tradingsymbol")
        if not sym or p.get("exchange") != "NFO":
            continue
        buy_q = p.get("buy_quantity", 0) or 0
        sell_q = p.get("sell_quantity", 0) or 0
        net_qty = buy_q - sell_q
        if net_qty == 0:
            continue
        parsed = split_symbol(sym)
        if not parsed:
            continue
        _, expiry, strike, opt_type = parsed
        if opt_type not in ("CE", "PE"):
            continue

        buy_val = p.get("buy_value", 0) or 0
        sell_val = p.get("sell_value", 0) or 0
        if net_qty > 0 and buy_q:
            avg_price = buy_val / buy_q
        elif net_qty < 0 and sell_q:
            avg_price = sell_val / sell_q
        else:
            avg_price = None

        delta = None
        snap = _fetch_latest_option_quote(sym)
        if snap and snap.get("delta") is not None:
            delta = snap["delta"]

        last_price = p.get("last_price")
        multiplier = p.get("multiplier", 1) or 1
        # Unified mark-to-market PnL works for both sides: long → net_qty>0,
        # buying avg; short → net_qty<0, selling avg. (last - avg) * net_qty
        # flips sign correctly for shorts.
        if avg_price is not None and last_price is not None:
            pnl = (last_price - avg_price) * net_qty * multiplier
        else:
            pnl = None

        side = "long" if net_qty > 0 else "short"
        all_legs.append({
            "symbol": sym,
            "strike": strike,
            "option_type": opt_type,
            "expiry": expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry),
            "quantity": net_qty,
            "side": side,
            "avg_price": avg_price,
            "last_price": last_price,
            "delta": delta,
            "multiplier": multiplier,
            "pnl": pnl,
        })

    requested_expiry = request.args.get("expiry") or None

    by_expiry = {}
    for leg in all_legs:
        by_expiry.setdefault(leg["expiry"], []).append(leg)
    available_expiries = sorted(by_expiry.keys())

    def _none(message, chosen_expiry=None):
        return [True, {"status": "none", "expiry": chosen_expiry,
                       "available_expiries": available_expiries,
                       "message": message, "legs": []}]

    if not all_legs:
        return _none("No open NFO option positions")

    # Pick the requested expiry if it has positions; otherwise default to
    # the nearest expiry with open legs.
    if requested_expiry and requested_expiry in by_expiry:
        chosen_expiry = requested_expiry
    else:
        chosen_expiry = available_expiries[0]
    chosen_legs = by_expiry[chosen_expiry]

    by_role = {"long_pe": [], "short_pe": [], "short_ce": [], "long_ce": []}
    for leg in chosen_legs:
        role = f"{leg['side']}_{leg['option_type'].lower()}"
        if role in by_role:
            by_role[role].append(leg)

    # Iron condor check first (more specific shape).
    if all(len(v) == 1 for v in by_role.values()):
        lpe = by_role["long_pe"][0]
        spe = by_role["short_pe"][0]
        sce = by_role["short_ce"][0]
        lce = by_role["long_ce"][0]
        if lpe["strike"] < spe["strike"] < sce["strike"] < lce["strike"]:
            return [True, {
                "status": "iron_condor",
                "expiry": chosen_expiry,
                "available_expiries": available_expiries,
                "message": "OK",
                "legs": [lpe, spe, sce, lce],
            }]
        return _none(
            "4 legs found but strikes not in iron-condor order: "
            f"{lpe['strike']}, {spe['strike']}, {sce['strike']}, {lce['strike']}",
            chosen_expiry,
        )

    # Short strangle: 1 short PE + 1 short CE, no longs.
    if (len(by_role["short_pe"]) == 1 and len(by_role["short_ce"]) == 1
            and not by_role["long_pe"] and not by_role["long_ce"]):
        spe = by_role["short_pe"][0]
        sce = by_role["short_ce"][0]
        if spe["strike"] < sce["strike"]:
            return [True, {
                "status": "short_strangle",
                "expiry": chosen_expiry,
                "available_expiries": available_expiries,
                "message": "OK",
                "legs": [spe, sce],
            }]
        return _none(
            f"2 short legs found but PE strike {spe['strike']} "
            f">= CE strike {sce['strike']}",
            chosen_expiry,
        )

    counts = ", ".join(f"{k}={len(v)}" for k, v in by_role.items())
    return _none(
        f"Expiry {chosen_expiry} positions don't form an iron condor or "
        f"short strangle; got {counts}",
        chosen_expiry,
    )


@app.route("/api/market_snapshots", methods=["GET"])
def api_market_snapshots():
    """Proxy to DB API server: market_snapshot rows for ``?date=YYYY-MM-DD``."""
    return _proxy_db("/api/market_snapshots",
                     params={"date": request.args.get("date")} if request.args.get("date") else None)


@app.route("/api/option_snapshot", methods=["GET"])
def api_option_snapshot():
    """Proxy to DB API server: latest option_snapshot for ``?date=YYYY-MM-DD``."""
    return _proxy_db("/api/option_snapshot",
                     params={"date": request.args.get("date")} if request.args.get("date") else None)
@app.route("/status")
def monitor_status():
    watch_config = get_watch_config()
    monitor_info = (watch_config[1] or {}).get("monitor_info") or {}
    last_watched = monitor_info.get("last_watched")
    if monitor_info.get("autotrade"):
        return [True, "Monitoring", last_watched]
    else:
        return [False, "Not Monitoring", last_watched]
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
    try:
        user = request.args.get("user")
        if user:
            trades[user] = _call_with_reauth(user, "fetch_trades")
        else:
            for u in list(broker_connections.keys()):
                trades[u] = _call_with_reauth(u, "fetch_trades")
        return [True, trades]
    except Exception as e:
        print(f"Failed to get trades with error: {e}")
        return [False, f"Failed to get trades with error: {e}"]


@app.route("/get_pnl", methods=["GET"])
def get_pnl():
    """Per-user P&L straight from Kite (``kite.positions()['net']``).

    Authoritative replacement for the per-week ``<week>.pnl`` buckets
    currently persisted in ``Monitor/config.yaml``. Each user's payload
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


def _fetch_latest_option_quote(symbol):
    """Pull the latest option_snapshot row for ``symbol`` from the DB API
    server (or ``None``)."""
    return db_get(f"/api/latest_option_quote/{symbol}",
                  params={"max_age_seconds": DB_FRESHNESS_SECONDS})


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
            return {
                "last_price": opt["ltp"],
                "oi": opt.get("oi", 0),
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
    (e.g. record_snapshots.py) so they can't end up reading their own
    stale rows back."""
    return request.args.get("live", "").lower() == "true"


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
        ltp = _call_with_reauth(SUPER_USER, "fetch_last_price",
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
            kite_quotes = _call_with_reauth(SUPER_USER, "fetch_quotes", misses)
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


@app.route("/get_watch_config", methods=["GET"])
def get_watch_config():
    try:
        return [True, get_config(WATCH_CONFIG)]
    except Exception as e:
        return [False, {"Error": f"Failed to read the config: {e}"}]


@app.route("/put_watch_config", methods=["GET"])
def put_watch_config():
    """Update one or more keys in Monitor/config.yaml from query-string args.

    Only keys already present in the config are updated (preserves the prior
    contract that an unknown ?foo=bar can't introduce arbitrary keys via the
    Flask endpoint). Cross-process locking and value coercion are delegated to
    helpers.basic.put_config.
    """
    try:
        current = get_config(WATCH_CONFIG)
        updates = {k: request.args.get(k) for k in current if k in request.args}
        if not updates:
            return [True, current]
        ok, info = put_config(WATCH_CONFIG, updates=updates)
        if not ok:
            return [False, info]
        return [True, info]
    except Exception as e:
        return [False, {"Error": f"Failed to write the config: {e}"}]

@app.route("/get_watch_log", methods=["GET"])
def get_watch_log():
    watch_log = ""
    try:
        daystr = datetime.now(ist).strftime('%Y%m%d')
        with open(f"{daystr}_watch.log", "r") as file:
            watch_log = file.read()
        return [True, watch_log]
    except Exception as e:
        return [False, {"Error": f"Failed to get the watch log: {e}"}]

# Run the app
if __name__ == "__main__":
    connection_status = get_broker(SUPER_USER)
    if not connection_status[0]:
        print(f"Failed in KITE Connection for user {SUPER_USER}")
    else:
        app.run(host='0.0.0.0', port=80, debug=False)
