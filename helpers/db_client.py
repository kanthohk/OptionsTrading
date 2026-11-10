"""Remote stand-in for the per-account parts of ``MarketAnalysis.db_queries``.

``autotrade.py`` no longer shares a filesystem with ``trading.db``: the
database, ``db_api.py`` and ``fetch_db_data.py`` live on their own host.
Everything the trading side used to do with a local SQLite handle now goes
over HTTP.

Routed through ``main.py``, not straight to ``db_api``. main.py is already
the single point of contact between the trading server and everything else —
it proxies the dashboard's DB reads and every broker call — so it is the only
component that needs to know ``db_api_host`` or hold the shared secret.
Going direct would have made autotrade the one exception, and would have
added no reliability either way: autotrade cannot function without main.py
regardless, since positions, trades and orders all pass through it.

The four functions here mirror their ``db_queries`` counterparts' signatures
exactly, so switching a module from local to remote is an import change and
nothing else. Modules that share a host with the database — ``db_api.py``,
``fetch_db_data.py``, ``tools/audit_ledger.py`` — keep importing
``db_queries`` directly. That split is also why ``helpers/trade_ledger.py``
still defaults to the local functions: it is imported by ``db_api`` itself,
and pointing it here would have the DB server making HTTP calls to itself.
Callers on the trading side inject the remote versions instead (see
``PositionProcessor.realised_by_expiry``).

Failure policy: every function raises ``DbApiError`` rather than returning a
falsy value. A ledger that reads as empty is indistinguishable from a flat
book, and it feeds the stop-loss — so an unreachable DB has to be an
exception the caller must handle, not a zero it can quietly average in.
"""
import uuid

from helpers.market_data import proxy_request, proxy_post


class DbApiError(RuntimeError):
    """A DB API call failed: unreachable, HTTP error, or server-reported
    failure. Never raised for a legitimately empty or null result."""


def read_trades(user=None):
    """All persisted fills for ``user``, chronologically. Mirrors
    ``db_queries.read_trades``.

    ``user`` is optional in the local version (omitting it returns every
    account's fills); here it is required, because the endpoint requires it.
    Nothing on the trading side wants the unscoped read.
    """
    if not user:
        raise DbApiError("read_trades requires a user over the DB API — an "
                         "unscoped ledger read is not available remotely")
    ok, payload = proxy_request("GET", "/api/trades", params={"user": user})
    if not ok:
        raise DbApiError(payload)
    return payload or []


def settlement_spot(underlying, date_str):
    """Last recorded spot for ``underlying`` on ``date_str``, or ``None``.

    ``None`` is a real answer — nothing was recorded that day — and is
    distinct from a transport failure, which raises.
    """
    ok, payload = proxy_request("GET", "/api/settlement_spot",
                                params={"underlying": underlying,
                                        "date": date_str})
    if not ok:
        raise DbApiError(payload)
    return payload


def persist_trades(trades, user, fetched_at=None):
    """Append ``trades`` to the remote ledger; returns rows newly inserted.

    Safe to retry: the server side is INSERT OR IGNORE on Kite's
    ``trade_id``, so a lost response costs a duplicate request, not a
    duplicate row.
    """
    ok, payload = proxy_post("/api/trades", {
        "user": user,
        "trades": list(trades or []),
        "fetched_at": fetched_at,
    })
    if not ok:
        raise DbApiError(payload)
    return int((payload or {}).get("inserted") or 0)


def record_adjustment(**kwargs):
    """Write one adjustment audit row remotely. Returns ``(id, created)``,
    matching ``db_queries.record_adjustment``.

    A ``client_key`` is generated here when the caller didn't supply one, so
    the idempotency guarantee holds without every call site remembering it.
    It must be created BEFORE the request rather than per attempt: the whole
    point is that a retry carries the same key, so a response lost after the
    row landed resolves to ``created=False`` instead of a second audit row
    double-counting in the P&L chart.
    """
    kwargs.setdefault("client_key", str(uuid.uuid4()))
    ok, payload = proxy_post("/api/adjustments", kwargs)
    if not ok:
        raise DbApiError(payload)
    payload = payload or {}
    return payload.get("id"), not payload.get("duplicate", False)
