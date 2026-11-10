"""Dashboard login accounts, stored in ``trading.db``'s ``users`` table.

Runs on the DB server only. The trading server never opens trading.db — it
asks db_api's ``/api/auth/verify`` instead — so password hashes never leave
this host; only "yes, this is <username>" or "no" goes back over the wire.

These are logins for the DASHBOARD, unrelated to the broker accounts in
``api_info.users``. A dashboard user can view every configured account, as
the unauthenticated page could before.

Hashing is werkzeug's (ships with Flask), so this adds no dependency on the
DB server. Accounts are created with ``tools/manage_users.py``; there is no
sign-up page.
"""
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

DB_FILE = "trading.db"

# Consecutive failures before an account is locked, and for how long. The
# dashboard is reachable on a public IP over plain HTTP, so an unthrottled
# login endpoint is an open invitation to guess passwords.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

MIN_PASSWORD_LENGTH = 8

USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NOCASE so "Sashi" and "sashi" can't become two accounts, and either
    -- spelling logs in.
    username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash    TEXT NOT NULL,
    -- 0 disables the login without deleting its history. Takes effect at the
    -- next login; a token already issued lives until it expires.
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    last_login_at    TEXT,
    failed_attempts  INTEGER NOT NULL DEFAULT 0,
    locked_until     TEXT
)
"""

# Verified against when the username doesn't exist, so an unknown user costs
# the same hash work as a wrong password and response timing can't be used
# to enumerate accounts.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _now():
    return datetime.now().replace(microsecond=0)


def _connect(db_file):
    # Resolved per call, not as a default argument, so DB_FILE can be
    # repointed after import.
    conn = sqlite3.connect(db_file or DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(db_file=None):
    """Create the ``users`` table if it's missing. Idempotent."""
    with _connect(db_file) as conn:
        conn.execute(USERS_DDL)


def _validate_password(password):
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} "
                         f"characters")


def create_user(username, password, db_file=None):
    """Add a login. Raises ``ValueError`` on a bad name, short password or
    a username that already exists (in any casing)."""
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    _validate_password(password)
    ensure_table(db_file)
    try:
        with _connect(db_file) as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (username, generate_password_hash(password),
                 _now().isoformat()),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"user {username!r} already exists")


def set_password(username, password, db_file=None):
    """Reset a password, clearing any lockout. Returns False if no such user."""
    _validate_password(password)
    with _connect(db_file) as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, failed_attempts = 0, "
            "locked_until = NULL WHERE username = ?",
            (generate_password_hash(password), username),
        )
        return cur.rowcount > 0


def set_active(username, active, db_file=None):
    """Enable or disable a login, clearing any lockout. Returns False if no
    such user."""
    with _connect(db_file) as conn:
        cur = conn.execute(
            "UPDATE users SET is_active = ?, failed_attempts = 0, "
            "locked_until = NULL WHERE username = ?",
            (1 if active else 0, username),
        )
        return cur.rowcount > 0


def list_users(db_file=None):
    """Every login, without hashes."""
    ensure_table(db_file)
    with _connect(db_file) as conn:
        rows = conn.execute(
            "SELECT username, is_active, created_at, last_login_at, "
            "failed_attempts, locked_until FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def verify_credentials(username, password, db_file=None):
    """Check a login attempt and record its outcome.

    Returns ``(True, canonical_username)`` or ``(False, reason)``. The reason
    is for the server log; ``"locked"`` is the only one worth telling the
    person at the login form apart from a generic refusal.
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False, "missing username or password"

    now = _now()
    with _connect(db_file) as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, is_active, failed_attempts, "
            "locked_until FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if row is None:
            check_password_hash(_DUMMY_HASH, password)
            return False, "unknown user"

        if row["locked_until"] and \
                datetime.fromisoformat(row["locked_until"]) > now:
            return False, "locked"

        if not check_password_hash(row["password_hash"], password):
            failures = row["failed_attempts"] + 1
            locked_until = None
            if failures >= MAX_FAILED_ATTEMPTS:
                locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)
                                ).isoformat()
                failures = 0
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? "
                "WHERE id = ?",
                (failures, locked_until, row["id"]),
            )
            return False, "locked" if locked_until else "wrong password"

        # Checked after the password so a disabled account can't be told
        # apart from a wrong password by someone who doesn't know it.
        if not row["is_active"]:
            return False, "disabled"

        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, "
            "last_login_at = ? WHERE id = ?",
            (now.isoformat(), row["id"]),
        )
        return True, row["username"]
