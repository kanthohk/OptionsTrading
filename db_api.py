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
import yaml
from flask import Flask, request

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
)

CONFIG_FILE = "Monitor/config.yaml"

app = Flask(__name__)


def _listen_port():
    """Read ``api_info.db_api_port`` from config. Fails loudly if unset/
    unreadable so the service doesn't quietly start on the wrong port."""
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)
    api_info = (cfg or {}).get("api_info") if isinstance(cfg, dict) else None
    if not isinstance(api_info, dict) or "db_api_port" not in api_info:
        raise RuntimeError(f"{CONFIG_FILE} is missing required key 'api_info.db_api_port'")
    return int(api_info["db_api_port"])


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
    """Latest option_snapshot for ``?date=YYYY-MM-DD`` (IST today if omitted).

    Returns ``[True, {'snapshot_time': '...', 'rows': [...]}]``.
    """
    date_str = request.args.get("date") or None
    try:
        return [True, read_latest_option_snapshot(date_str)]
    except Exception as e:
        return [False, f"Failed to read option_snapshot: {e}"]


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
    [10, 15]."""
    try:
        min_d = float(request.args.get("min", 10))
        max_d = float(request.args.get("max", 15))
        return [True, get_strikes_in_delta_band(min_d, max_d)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid min/max: {e}"]
    except Exception as e:
        return [False, f"Failed to read strikes_by_delta: {e}"]


@app.route("/api/iron_condor_preset", methods=["GET"])
def api_iron_condor_preset():
    """Iron-condor leg set for the chosen expiry.

    Query params (all optional):
        delta_target_pct  short-leg target |delta| in percent (default 10)
        hedge_points      hedge distance from short strikes in points
                          (default 200)
        expiry            YYYY-MM-DD; if absent, defaults to next-weekly.
    """
    try:
        delta_target = float(request.args.get("delta_target_pct", 10))
        hedge_points = int(request.args.get("hedge_points", 200))
        expiry = request.args.get("expiry") or None
        return [True, get_iron_condor_preset(delta_target, hedge_points,
                                             expiry=expiry)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid params: {e}"]
    except Exception as e:
        return [False, f"Failed to build iron_condor preset: {e}"]


@app.route("/api/adjustments", methods=["GET"])
def api_adjustments():
    """Recent adjustment audit rows for snapshots.html.

    Query params (all optional):
        limit   max rows to return, newest first (default 200, max 1000).
        expiry  YYYY-MM-DD filter; when set, only adjustments on that
                (underlying, expiry) book are returned.
    """
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
        expiry = request.args.get("expiry") or None
        return [True, read_adjustments(limit=limit, expiry=expiry)]
    except (TypeError, ValueError) as e:
        return [False, f"Invalid params: {e}"]
    except Exception as e:
        return [False, f"Failed to read adjustments: {e}"]


# ---- Freshness-bounded lookups used by main.py for DB-first quotes -------

def _parse_max_age():
    raw = request.args.get("max_age_seconds")
    if raw is None:
        return 120
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 120


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
    print(f"DB API server starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
