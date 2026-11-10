"""Flask API server for read-only access to ``trading.db``.

Splits the DB-backed endpoints out of ``main.py`` so the broker proxy and
the analytics DB can be deployed/scaled/restarted independently. Hosts
both the dashboard-facing endpoints (``/api/snapshot_dates``,
``/api/market_snapshots``, ``/api/option_snapshot``, ``/api/index_summary``)
and the freshness-bounded lookups used by ``main.py`` to serve DB-first
quotes (``/api/latest_market_snapshot``, ``/api/latest_option_quote``).

Listens on the port configured at ``db_api_port`` in ``Monitor/config.yaml``
(default 8080). The broker proxy at ``main.py`` continues to listen on 80.

Run: ``python db_api.py``
"""
import hmac

import yaml
from flask import Flask, request, jsonify

from helpers import market_data
from MarketAnalysis import db_queries

from MarketAnalysis.db_queries import (
    list_snapshot_dates,
    read_market_snapshots,
    read_latest_option_snapshot,
    get_index_summary,
    get_latest_market_snapshot,
    get_latest_option_quote,
    get_strikes_in_delta_band,
    get_iron_condor_preset,
    read_adjustments,
    list_recorded_underlyings,
    # Imported as a module too, for the version check below.
    # Per-account ledger, served on behalf of a trading server that no longer
    # shares a filesystem with trading.db.
    read_trades,
    persist_trades,
    record_adjustment,
    settlement_spot,
)
from helpers.trade_ledger import realised_summary
from helpers import user_store

CONFIG_FILE = "Monitor/config.yaml"

app = Flask(__name__)


def _shared_secret():
    """``api_info.db_api_secret`` from config, or raise.

    Fails loudly at startup for the same reason ``_listen_port`` does: a DB
    API that comes up without its credential is an open write endpoint onto
    the trade ledger, and the ledger feeds the adjustments engine's realised
    P&L. Better to not start than to start unauthenticated.
    """
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f) or {}
    secret = ((cfg.get("api_info") or {}).get("db_api_secret") or "")
    secret = str(secret).strip()
    if not secret:
        raise RuntimeError(
            f"{CONFIG_FILE} is missing api_info.db_api_secret. db_api will "
            f"not start without it — the write endpoints accept fills and "
            f"adjustment rows that the risk engine reads back as realised P&L."
        )
    return secret


_SECRET = _shared_secret()


def _check_db_queries_version():
    """Refuse to start against a stale ``MarketAnalysis/db_queries.py``.

    This app and db_queries are deployed as separate files onto the DB
    server, so a partial copy leaves them at different versions. The
    symptom was awful: ``record_adjustment`` gained a second return value
    (``created``, for idempotency), and an old copy still returning a bare
    id surfaced per-request as "cannot unpack non-iterable int object" —
    a message that names neither the file nor the fix. Worse, the row had
    already been inserted, so db_api reported failure for a write that
    succeeded.

    Reaching into a module-private is deliberate: ``_ADJUSTMENT_COLUMNS`` is
    the cheapest positive evidence that the deployed db_queries understands
    ``client_key``, and both files live in this same codebase.
    """
    cols = getattr(db_queries, "_ADJUSTMENT_COLUMNS", ())
    if "client_key" not in cols:
        raise RuntimeError(
            "MarketAnalysis/db_queries.py on this host is out of date: it "
            "does not know the 'client_key' adjustment column, so "
            "record_adjustment still returns a bare id instead of "
            "(id, created) and adjustment writes cannot be made idempotent. "
            "Copy the current MarketAnalysis/db_queries.py to this server "
            "and restart db_api."
        )


_check_db_queries_version()


@app.before_request
def _require_api_key():
    """Reject anything not carrying the shared secret.

    Applied to EVERY route, reads included: the read endpoints expose the
    whole trade ledger and P&L, which is no less private than the writes.

    ``compare_digest`` rather than ``==`` so a wrong key can't be recovered
    a byte at a time from response timings. Note this is plain HTTP — the
    header protects against casual access, not against a sniffer on the
    path; keep the two servers on a private link.
    """
    supplied = request.headers.get(market_data.DB_API_KEY_HEADER, "")
    if not hmac.compare_digest(str(supplied), _SECRET):
        return jsonify([False, f"unauthorised: missing or invalid "
                               f"{market_data.DB_API_KEY_HEADER}"]), 401


def _listen_port():
    """Read ``api_info.db_api_port`` from config. Fails loudly if unset/
    unreadable so the service doesn't quietly start on the wrong port."""
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)
    api_info = (cfg or {}).get("api_info") if isinstance(cfg, dict) else None
    if not isinstance(api_info, dict) or "db_api_port" not in api_info:
        raise RuntimeError(f"{CONFIG_FILE} is missing required key 'api_info.db_api_port'")
    return int(api_info["db_api_port"])


def _hedge_cfg(underlying=None):
    """``hedge_info`` (near/mid/far distances + DTE boundaries) for
    ``underlying``, falling back to the global block.

    Per-underlying because the distances are absolute POINTS: 200 points is
    0.8% of a 25,000 NIFTY but only 0.37% of a 54,000 BANKNIFTY. Serving one
    block to both would draw the dashboard's BANKNIFTY wings at under half
    their intended width — and the Deploy button places exactly what the
    card shows.

    Best-effort: any read/parse failure or a missing block returns ``{}`` so
    ``hedge_distance_for_dte`` falls back to its built-in 100/200/300 @ 7/14
    DTE defaults.
    """
    try:
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            return {}
        if underlying:
            section = (cfg.get("underlyings") or {}).get(str(underlying).upper())
            own = section.get("hedge_info") if isinstance(section, dict) else None
            if isinstance(own, dict) and own:
                return own
        hedge_info = cfg.get("hedge_info")
        return hedge_info if isinstance(hedge_info, dict) else {}
    except Exception:
        return {}


# ---- Dashboard-facing endpoints (moved from main.py) ---------------------

@app.route("/api/snapshot_dates", methods=["GET"])
def api_snapshot_dates():
    """List of YYYY-MM-DD dates with at least one market_snapshot row."""
    try:
        return [True, list_snapshot_dates()]
    except Exception as e:
        return [False, f"Failed to list snapshot dates: {e}"]


@app.route("/api/market_snapshots", methods=["GET"])
def api_market_snapshots():
    """All market_snapshot rows for ``?date=YYYY-MM-DD`` (IST today if omitted)."""
    date_str = request.args.get("date") or None
    try:
        return [True, read_market_snapshots(date_str)]
    except Exception as e:
        return [False, f"Failed to read market_snapshot: {e}"]


@app.route("/api/option_snapshot", methods=["GET"])
def api_option_snapshot():
    """Latest option_snapshot for ``?date=YYYY-MM-DD`` (IST today if omitted)
    and ``?underlying=`` (NIFTY if omitted).

    Returns ``[True, {'snapshot_time': '...', 'underlying': '...', 'rows': [...]}]``.
    """
    date_str = request.args.get("date") or None
    underlying = request.args.get("underlying") or None
    try:
        return [True, read_latest_option_snapshot(date_str, underlying=underlying)]
    except Exception as e:
        return [False, f"Failed to read option_snapshot: {e}"]


@app.route("/api/underlyings", methods=["GET"])
def api_underlyings():
    """Underlyings the recorder has actually written rows for, default
    first. Drives the dashboard's underlying selector so it never offers an
    index with no data behind it."""
    try:
        return [True, list_recorded_underlyings()]
    except Exception as e:
        return [False, f"Failed to list underlyings: {e}"]


@app.route("/api/index_summary", methods=["GET"])
def api_index_summary():
    """Open/close/change/horizon-% for NIFTY 50 and INDIA VIX."""
    try:
        return [True, get_index_summary()]
    except Exception as e:
        return [False, f"Failed to build index summary: {e}"]


@app.route("/api/strikes_by_delta", methods=["GET"])
def api_strikes_by_delta():
    """Latest option_snapshot rows whose |delta| (percent) falls in
    ``[?min=, ?max=]``, grouped by expiry and option_type. Defaults to
    [10, 15]. ``?underlying=`` scopes the query (NIFTY if omitted)."""
    try:
        min_d = float(request.args.get("min", 10))
        max_d = float(request.args.get("max", 15))
        underlying = request.args.get("underlying") or None
        return [True, get_strikes_in_delta_band(min_d, max_d,
                                                underlying=underlying)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid min/max: {e}"]
    except Exception as e:
        return [False, f"Failed to read strikes_by_delta: {e}"]


@app.route("/api/iron_condor_preset", methods=["GET"])
def api_iron_condor_preset():
    """Iron-condor leg set for the chosen expiry.

    Query params (all optional):
        delta_target_pct  short-leg target |delta| in percent (default 10)
        hedge_points      hedge distance from short strikes in points. When
                          omitted, it is derived from the expiry's DTE
                          (100/200/300 pts for <=7 / <=14 / >14 days).
                          Either way it is snapped up to the underlying's
                          strike grid.
        expiry            YYYY-MM-DD; if absent, defaults to next-weekly.
        underlying        NIFTY / BANKNIFTY / ...; NIFTY if absent.
    """
    try:
        delta_target = float(request.args.get("delta_target_pct", 10))
        hedge_points_arg = request.args.get("hedge_points")
        hedge_points = int(hedge_points_arg) if hedge_points_arg is not None else None
        expiry = request.args.get("expiry") or None
        underlying = request.args.get("underlying") or None
        return [True, get_iron_condor_preset(delta_target, hedge_points,
                                             expiry=expiry,
                                             hedge_cfg=_hedge_cfg(underlying),
                                             underlying=underlying)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid params: {e}"]
    except Exception as e:
        return [False, f"Failed to build iron_condor preset: {e}"]


@app.route("/api/adjustments", methods=["GET"])
def api_adjustments():
    """Recent adjustment audit rows for the dashboard's audit panel.

    Query params:
        user    account to report on (required — every row records the
                account its roll was placed for, so an unscoped read would
                show one account another's activity).
        limit   max rows to return, newest first (default 200, max 1000).
        expiry  YYYY-MM-DD filter; when set, only adjustments on that
                (underlying, expiry) book are returned.

    ``user`` is required rather than defaulted for the same reason
    ``/api/realised_pnl`` requires it: a default here would be a second
    place for the account name to live, and the two could disagree. main.py
    supplies it (falling back to its configured trading account) on the way
    through.
    """
    user = request.args.get("user")
    if not user:
        return [False, "user query param is required"]
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
        expiry = request.args.get("expiry") or None
        return [True, read_adjustments(limit=limit, expiry=expiry, user=user)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid params: {e}"]
    except Exception as e:
        return [False, f"Failed to read adjustments: {e}"]


# ---- Per-account ledger: the trading server's read/write path ------------
#
# These four exist so main.py / autotrade.py can run on a different host from
# trading.db. They were direct SQLite calls inside the trading process; the
# DB now lives here, so the trading side goes through HTTP for exactly the
# same operations. fetch_db_data.py, which shares this host, keeps calling
# db_queries directly.


@app.route("/api/trades", methods=["GET"])
def api_read_trades():
    """Raw ``kite_trades`` fills for ``?user=``, chronologically.

    Deliberately raw rather than pre-aggregated: the caller FIFO-matches them
    against its own broker net-quantity snapshot to find orphan lots, and the
    broker positions live on the trading server, not here.
    """
    user = request.args.get("user")
    if not user:
        return [False, "user query param is required"]
    try:
        return [True, read_trades(user=user)]
    except Exception as e:
        return [False, f"Failed to read trades: {e}"]


@app.route("/api/trades", methods=["POST"])
def api_persist_trades():
    """Append fills to ``kite_trades``. Body: ``{user, trades, fetched_at?}``.

    Idempotent — ``persist_trades`` uses INSERT OR IGNORE on Kite's
    ``trade_id``, so the trading server can retry freely and the recorder's
    own daily fetch converges on the same rows.
    """
    body = request.get_json(silent=True) or {}
    user = body.get("user")
    trades = body.get("trades")
    if not user:
        return [False, "user is required in the request body"]
    if not isinstance(trades, list):
        return [False, "trades must be a list"]
    try:
        inserted = persist_trades(trades, user, body.get("fetched_at"))
        return [True, {"inserted": inserted, "received": len(trades)}]
    except Exception as e:
        return [False, f"Failed to persist trades: {e}"]


@app.route("/api/adjustments", methods=["POST"])
def api_record_adjustment():
    """Write one ``adjustments`` audit row. Body is the row itself.

    Shares its path with the GET reader above — the method distinguishes
    them. (This was ``/api/adjustment``, singular; a path differing from its
    sibling by one letter is a typo waiting to be silently accepted.)

    Include ``client_key`` for idempotency: a repeat with the same key does
    not insert again and returns ``duplicate: true`` with the original id.
    Without one, every call inserts — which over a network means a lost
    response becomes a double-counted row in the P&L chart.
    """
    body = request.get_json(silent=True) or {}
    if not body.get("user"):
        return [False, "user is required in the request body"]
    try:
        result = record_adjustment(**body)
    except Exception as e:
        return [False, f"Failed to record adjustment: {e}"]
    # Backstop for the version skew _check_db_queries_version guards at
    # startup. Named explicitly because the row has ALREADY been written by
    # this point — reporting a bare "failed" here would have the caller
    # believe an audit row is missing when it is not.
    if not isinstance(result, tuple) or len(result) != 2:
        return [False, (
            f"record_adjustment returned {type(result).__name__} instead of "
            f"(id, created) — MarketAnalysis/db_queries.py on the DB server "
            f"is out of date. The row WAS written; only idempotency is "
            f"inactive. Update that file and restart db_api.")]
    row_id, created = result
    if row_id is None:
        return [False, "adjustment insert affected no rows"]
    return [True, {"id": row_id, "duplicate": not created}]


@app.route("/api/settlement_spot", methods=["GET"])
def api_settlement_spot():
    """Last recorded spot for ``?underlying=`` on ``?date=`` (YYYY-MM-DD).

    Used to value options that expired instead of being closed. A ``null``
    payload with success=True is a real answer — "nothing recorded for that
    date" — and must not be confused with a transport failure, which is why
    the envelope carries the distinction.
    """
    underlying = request.args.get("underlying")
    date_str = request.args.get("date")
    if not underlying or not date_str:
        return [False, "underlying and date query params are required"]
    try:
        return [True, settlement_spot(underlying, date_str)]
    except Exception as e:
        return [False, f"Failed to read settlement spot: {e}"]


# ---- Freshness-bounded lookups used by main.py for DB-first quotes -------

def _parse_max_age():
    """Parse ``?max_age_seconds``. Absent -> 120s default. A value ``<= 0``
    means "no freshness cutoff" (return None) — the caller wants the last
    available row regardless of age."""
    raw = request.args.get("max_age_seconds")
    if raw is None:
        return 120
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 120
    return None if val <= 0 else val


@app.route("/api/realised_pnl", methods=["GET"])
def api_realised_pnl():
    """Realised P&L per ``(underlying, expiry)``, FIFO-matched from the
    ``kite_trades`` ledger — i.e. from actual closed fills.

    Query params:
        user     account to report on (required — the ledger is per-user).
        charge   per-fill charge to model (default CHARGE_PER_TRADE).

    This is the same computation the risk engine uses for its realised
    component, so the dashboard and the engine cannot disagree. Unrealised
    MTM is deliberately absent: it belongs to the open-positions view, which
    needs live quotes this endpoint doesn't fetch.

    ``expired: true`` with a non-empty ``unmatched_symbols`` means fills are
    MISSING for that book — every position is closed before expiry day here,
    so a residual lot on an expired book cannot be a held position. That book's
    figure is understated by an unknown amount.
    """
    user = request.args.get("user")
    if not user:
        return [False, "user query param is required"]
    try:
        charge_raw = request.args.get("charge")
        kwargs = {"charge_per_trade": float(charge_raw)} if charge_raw else {}
        return [True, realised_summary(user, **kwargs)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid params: {e}"]
    except Exception as e:
        return [False, f"Failed to compute realised P&L: {e}"]


@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    """Check a dashboard login. Body: ``{username, password}``.

    Answers ``[True, {"ok": bool, ...}]`` for any attempt that was actually
    checked — a wrong password is a successful CALL with a negative answer.
    ``[False, ...]`` is kept for "could not check at all", so the trading
    server can tell an outage apart from a bad password and say which.

    Behind the shared-secret check like every route here: this endpoint
    records failed attempts, so leaving it open would let anyone lock the
    operator out.
    """
    body = request.get_json(silent=True) or {}
    try:
        ok, detail = user_store.verify_credentials(body.get("username"),
                                                   body.get("password"))
    except Exception as e:
        return [False, f"Failed to verify credentials: {e}"]
    if ok:
        return [True, {"ok": True, "username": detail}]
    return [True, {"ok": False, "reason": detail}]


@app.route("/api/latest_market_snapshot", methods=["GET"])
def api_latest_market_snapshot():
    """Latest market_snapshot row if its timestamp is within
    ``?max_age_seconds=N`` of now (default 120). Returns ``[True, null]``
    when no fresh row exists (no error — caller should fall through to KITE)."""
    max_age = _parse_max_age()
    try:
        return [True, get_latest_market_snapshot(max_age)]
    except Exception as e:
        return [False, f"Failed to read latest market_snapshot: {e}"]


@app.route("/api/latest_option_quote/<path:symbol>", methods=["GET"])
def api_latest_option_quote(symbol):
    """Latest option_snapshot row for ``symbol`` if fresh within
    ``?max_age_seconds=N`` of now (default 120)."""
    max_age = _parse_max_age()
    try:
        return [True, get_latest_option_quote(symbol, max_age)]
    except Exception as e:
        return [False, f"Failed to read latest option quote: {e}"]


if __name__ == "__main__":
    port = _listen_port()
    # A fresh database has no users table until someone creates a login;
    # make it here so /api/auth/verify answers "unknown user" rather than
    # "no such table".
    user_store.ensure_table()
    print(f"DB API server starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
