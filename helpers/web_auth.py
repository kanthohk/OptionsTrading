"""JWT authentication for flaskapp (``main.py``).

Two kinds of caller reach flaskapp, and they authenticate differently:

  * PEOPLE, through the dashboard. They log in at ``/login``; the password is
    checked by db_api against trading.db's ``users`` table (see
    ``helpers/user_store.py``), and on success this module issues a signed
    JWT in an HttpOnly cookie. The browser then sends it on every request by
    itself — none of the dashboard's many ``fetch`` calls had to change, and
    page script can't read the token, so an injected script can't steal it.

  * MACHINES — the autotrade monitors on this host and the recorder on the
    DB server. They can't log in, so they send the existing shared secret
    (``api_info.db_api_secret``) in the same ``X-Api-Key`` header db_api
    already requires. Both servers have that value configured.

The cookie is ``SameSite=Strict``, and that is load-bearing: ``/place_order``
is a GET, so under ``Lax`` a link on any other site would arrive WITH the
cookie and place an order. Strict means a cross-site link lands on the login
page instead.

Config, optional, in ``Monitor/config.yaml`` on the trading server::

    auth:
      jwt_secret: "<long random string>"   # e.g. python -c "import secrets; print(secrets.token_urlsafe(48))"
      token_hours: 12
      cookie_secure: false                 # true once served over HTTPS
"""
import hmac
import secrets
from datetime import datetime, timedelta, timezone

try:
    import jwt
except ImportError as exc:  # pragma: no cover - import-time guard for env drift
    raise RuntimeError(
        "This project requires PyJWT. Install 'PyJWT' and remove the unrelated 'jwt' package."
    ) from exc

if not hasattr(jwt, "encode") or not hasattr(jwt, "decode"):
    raise RuntimeError(
        "A conflicting 'jwt' package is installed. Install PyJWT (pip install PyJWT) and remove the wrong package."
    )

import yaml

from helpers.market_data import DB_API_KEY_HEADER, db_api_secret

CONFIG_FILE = "Monitor/config.yaml"
COOKIE_NAME = "auth_token"
SERVICE_KEY_HEADER = DB_API_KEY_HEADER
# What ``request_identity`` returns for a machine caller. Contains a space so
# it can never collide with a real username's JWT ``sub``.
SERVICE_IDENTITY = "service client"
_ALGORITHM = "HS256"
_DEFAULT_TOKEN_HOURS = 12

# Read at call time by main.py's before_request. The test suite switches it
# off in conftest so the existing endpoint tests needn't each log in; the
# auth tests switch it back on. Nothing in production touches it.
ENFORCE = True

_settings_cache = None


def _settings():
    """``(secret, token_hours, cookie_secure, secret_is_ephemeral)``.

    Resolved once per process. A missing ``auth.jwt_secret`` does NOT stop
    flaskapp — autotrade depends on it to place and monitor orders, and that
    matters more than a convenient login. A random per-process secret is
    used instead, which is just as secure; its only cost is that every
    restart signs everyone out.
    """
    global _settings_cache
    if _settings_cache is None:
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            cfg = {}
        auth = cfg.get("auth") if isinstance(cfg, dict) else None
        auth = auth if isinstance(auth, dict) else {}
        secret = str(auth.get("jwt_secret") or "").strip()
        ephemeral = not secret
        if ephemeral:
            secret = secrets.token_urlsafe(48)
        try:
            hours = float(auth.get("token_hours") or _DEFAULT_TOKEN_HOURS)
        except (TypeError, ValueError):
            hours = _DEFAULT_TOKEN_HOURS
        _settings_cache = (secret, hours, bool(auth.get("cookie_secure")),
                           ephemeral)
    return _settings_cache


def describe_config():
    """One line for flaskapp's startup banner."""
    secret, hours, secure, ephemeral = _settings()
    if ephemeral:
        return (f"Auth: WARNING — no auth.jwt_secret in {CONFIG_FILE}; using "
                f"a random one, so every restart signs dashboard users out. "
                f"Tokens last {hours:g}h.")
    note = "" if len(secret) >= 32 else " (WARNING: jwt_secret is short; use 32+ chars)"
    return (f"Auth: JWT secret configured{note}, tokens last {hours:g}h, "
            f"cookie {'Secure' if secure else 'not Secure (plain HTTP)'}.")


def issue_token(username):
    """Sign a token for ``username``. Returns ``(token, expires_at)``."""
    secret, hours, _, _ = _settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=hours)
    token = jwt.encode({"sub": username, "iat": now, "exp": expires},
                       secret, algorithm=_ALGORITHM)
    return token, expires


def decode_token(token):
    """The username in a valid, unexpired token; ``None`` otherwise."""
    if not token:
        return None
    secret = _settings()[0]
    try:
        # algorithms is pinned so a token can't choose its own (``none``,
        # or RS256 with our secret treated as a public key).
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM],
                            options={"require": ["sub", "exp"]})
    except jwt.InvalidTokenError:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def _is_service_call(req):
    supplied = req.headers.get(SERVICE_KEY_HEADER)
    if not supplied:
        return False
    try:
        expected = db_api_secret()
    except RuntimeError:
        # Not configured — main.py refuses to start in that state anyway.
        return False
    return hmac.compare_digest(str(supplied), str(expected))


def request_identity(req):
    """Who is making ``req``: a username, ``SERVICE_IDENTITY``, or ``None``.

    The token is taken from the cookie, or from ``Authorization: Bearer``
    for scripted use.
    """
    if _is_service_call(req):
        return SERVICE_IDENTITY
    token = req.cookies.get(COOKIE_NAME)
    if not token:
        header = req.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return decode_token(token)


def set_auth_cookie(resp, token, expires):
    resp.set_cookie(COOKIE_NAME, token, expires=expires, httponly=True,
                    samesite="Strict", secure=_settings()[2], path="/")


def clear_auth_cookie(resp):
    resp.delete_cookie(COOKIE_NAME, path="/", samesite="Strict",
                       secure=_settings()[2], httponly=True)
