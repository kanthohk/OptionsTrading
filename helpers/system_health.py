"""Host-level health probes for the dashboard: systemd unit states for the
sibling jobs and disk usage of the volume they run on.

Deliberately dependency-free and side-effect-free — everything here only
*reads* host state, so it is safe to call from a request handler and easy to
unit-test (see ``tests/test_system_health.py``).
"""

import re
import shutil
import subprocess

# systemd units backing the non-Flask processes of this deployment.
#
# ``flaskapp`` is intentionally absent: main.py *is* flaskapp, so if that unit
# were down nothing would be alive to answer the request. Its health is
# implied by the dashboard loading at all.
#
# ``MONITOR_SERVICE`` is called out separately because /status derives the
# dashboard's "Monitoring" pill from this one unit's state.
MONITOR_SERVICE = "autotrade"

# The non-monitor units, when they run on THIS host. One instance of each,
# shared by every account: the recorder captures index-wide snapshots and the
# DB API is a single reader, so neither is per-user the way the monitor is.
#
# These are only the DEFAULT. ``systemd_states`` shells out to a local
# ``systemctl show``, so it can only see units on the machine serving the
# request — and this deployment may move db_api and fetch_db_data to a
# separate DB server to free memory on the trading box. Probing for units
# that live elsewhere reports them not-found, which renders as two
# permanently red rows: an alarm that is always on, and therefore one nobody
# reads. Override with ``monitor_info.local_services`` in Monitor/config.yaml.
SHARED_SERVICES = ("db_api", "fetch_db_data")

MONITORED_SERVICES = (MONITOR_SERVICE,) + SHARED_SERVICES


def local_shared_services(config=None):
    """Shared units running on this host, from ``monitor_info.local_services``.

    Absent key keeps the historical ``SHARED_SERVICES`` default, so a
    single-box deployment is unaffected. An explicit empty list means "the
    shared services live on another server" and the card shows monitors
    only — which is honest, rather than red.
    """
    if not isinstance(config, dict):
        return SHARED_SERVICES
    monitor_info = config.get("monitor_info")
    if not isinstance(monitor_info, dict) or "local_services" not in monitor_info:
        return SHARED_SERVICES
    configured = monitor_info.get("local_services")
    if configured is None:
        return ()
    if isinstance(configured, str):
        configured = [s.strip() for s in configured.split(",")]
    return tuple(str(s).strip() for s in configured if str(s).strip())


def monitor_unit(user=None):
    """systemd unit backing ``user``'s monitor.

    The monitor runs one process per account, so it is deployed as a templated
    unit: ``autotrade@sashi``, ``autotrade@madhu``. ``systemctl show`` reports
    the instance's ``Id`` as ``autotrade@sashi.service``, which
    ``_unit_basename`` reduces to ``autotrade@sashi`` — so templated names key
    the parse map exactly like plain ones and need no special handling.

    ``user`` falsy returns the untemplated ``autotrade``, which is what a
    single-account deployment that never adopted the template still runs.
    """
    return f"{MONITOR_SERVICE}@{user}" if user else MONITOR_SERVICE


def monitored_services(users=(), config=None):
    """Units for the dashboard's services card: one monitor per account,
    then whichever shared units run on this host.

    Passing no ``users`` keeps the historical single-monitor tuple, so a
    caller that hasn't been taught about accounts still renders. Passing no
    ``config`` keeps the historical shared units, so one that hasn't been
    taught about a split deployment does too.
    """
    monitors = tuple(monitor_unit(u) for u in users) or (MONITOR_SERVICE,)
    return monitors + tuple(local_shared_services(config))

# Properties we ask systemd for. ``Id`` is what lets us key each output block
# by the unit it describes instead of trusting positional order.
_SHOW_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "ActiveEnterTimestamp",
    "NRestarts",
)

# systemctl is normally instant; this only matters if the systemd bus is wedged.
# The dashboard polls every 30s, so we must return well inside that window.
SYSTEMCTL_TIMEOUT = 5

# Volume to report. The jobs, SQLite DB and logs all live under the deployment
# root on this host, so the root filesystem is the number that matters.
DISK_MOUNT = "/"

_BYTES_PER_GB = 1024 ** 3

# Fraction of capacity at which the dashboard should start warning. Kept here
# rather than in the template so the thresholds live next to the measurement.
DISK_WARN_PCT = 80.0
DISK_CRIT_PCT = 90.0


def _blank_state(unit, state="unknown", error=None):
    """Placeholder row for a unit we could not read."""
    row = {
        "name": unit,
        "active": False,
        "state": state,
        "sub": None,
        "load": None,
        "since": None,
        "restarts": None,
    }
    if error:
        row["error"] = error
    return row


def _unit_basename(unit_id):
    """``autotrade.service`` -> ``autotrade``. Left alone for other unit types
    (``.timer``, ``.socket``) so their names stay unambiguous."""
    if unit_id.endswith(".service"):
        return unit_id[: -len(".service")]
    return unit_id


def _parse_show_output(text):
    """Parse ``systemctl show`` output into ``{unit_basename: {...}}``.

    Output is one blank-line-separated ``KEY=VALUE`` block per unit. Values may
    themselves contain ``=`` (timestamps do not, but be safe) so we split once.
    Blocks with no ``Id`` are dropped — without it we cannot say which unit
    they describe, and guessing from position is exactly the bug this avoids.
    """
    states = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        props = {}
        for line in block.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()

        unit_id = props.get("Id")
        if not unit_id:
            continue

        active_state = props.get("ActiveState") or "unknown"
        load_state = props.get("LoadState") or None
        # A unit systemd has never heard of reports LoadState=not-found with
        # ActiveState=inactive, which would otherwise read as "cleanly stopped".
        # Surface the difference — a typo'd unit name is an operator problem,
        # not a stopped job.
        state = "not-found" if load_state == "not-found" else active_state

        restarts = props.get("NRestarts")
        states[_unit_basename(unit_id)] = {
            "name": _unit_basename(unit_id),
            "active": active_state == "active",
            "state": state,
            "sub": props.get("SubState") or None,
            "load": load_state,
            # Empty when the unit has never started; normalise to None so the
            # dashboard has one "no value" case to handle.
            "since": props.get("ActiveEnterTimestamp") or None,
            "restarts": int(restarts) if restarts and restarts.isdigit() else None,
        }
    return states


def systemd_states(units=MONITORED_SERVICES):
    """Return a list of state dicts for ``units``, in the order given.

    One ``systemctl show`` call covers every unit — three subprocess spawns per
    poll per open dashboard adds up otherwise. ``systemctl show`` needs no
    privileges, so this works as the unprivileged service user.

    Never raises: a missing ``systemctl`` (e.g. running on Windows for local
    development) or a wedged systemd bus yields ``state='unavailable'`` rows so
    the caller can render a degraded card instead of a 500.
    """
    units = list(units)
    if not units:
        return []

    cmd = ["systemctl", "show", "--no-pager",
           "--property=" + ",".join(_SHOW_PROPERTIES)] + units
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT)
    except FileNotFoundError:
        return [_blank_state(u, "unavailable", "systemctl not found") for u in units]
    except subprocess.TimeoutExpired:
        return [_blank_state(u, "unavailable",
                             f"systemctl timed out after {SYSTEMCTL_TIMEOUT}s")
                for u in units]
    except (OSError, subprocess.SubprocessError) as e:
        return [_blank_state(u, "unavailable", str(e)) for u in units]

    parsed = _parse_show_output(proc.stdout or "")
    if not parsed:
        # Exit code alone is not a reliable signal (show returns 0 even for
        # unknown units), so only treat "nothing parseable" as a hard failure.
        err = (proc.stderr or "").strip() or f"systemctl exited {proc.returncode}"
        return [_blank_state(u, "unavailable", err) for u in units]

    return [parsed.get(_unit_basename(u)) or _blank_state(u) for u in units]


def disk_usage(path=DISK_MOUNT):
    """Disk usage for the filesystem containing ``path``.

    Uses ``shutil.disk_usage`` rather than shelling out to ``df`` — no parsing,
    no subprocess. GB here are GiB (1024^3), matching what ``df -h`` prints.

    Returns ``None`` if the path cannot be stat'ed, so the caller can render
    the services card without the disk line rather than failing outright.
    """
    try:
        total, used, free = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        "mount": path,
        "total_gb": round(total / _BYTES_PER_GB, 1),
        "used_gb": round(used / _BYTES_PER_GB, 1),
        "free_gb": round(free / _BYTES_PER_GB, 1),
        "used_pct": round(used / total * 100, 1) if total else None,
        "warn_pct": DISK_WARN_PCT,
        "crit_pct": DISK_CRIT_PCT,
    }


def system_status(units=MONITORED_SERVICES, path=DISK_MOUNT):
    """Combined payload for the dashboard's services card:
    ``{'services': [...], 'disk': {...} | None, 'up': n, 'total': n}``."""
    services = systemd_states(units)
    return {
        "services": services,
        "disk": disk_usage(path),
        "up": sum(1 for s in services if s.get("active")),
        "total": len(services),
    }
