"""Per-account monitor supervisor.

Runs ONE account. Which one comes from ``--user``; there is deliberately no
default, because inferring it from ``api_info.users`` is what limited this to
the first entry. Start one process per account:

    python autotrade.py --user sashi
    python autotrade.py --user madhu

The broker sessions are NOT here — every quote and order goes over HTTP to the
single Flask proxy (``main.py``), which holds one KiteConnect session per
account and takes ``?user=``. That is why N of these processes cost nothing
extra at the broker: they are HTTP clients, not API sessions. Running one
proxy per account would instead have had each instance re-authenticating the
same market-data account and invalidating the others' access tokens.

Each process writes its own ``{date}_{user}_watch.log`` and is expected to be
its own systemd instance (``autotrade@sashi``), which is what lets /status
report a dead monitor for one account while another is healthy.
"""
import argparse
import threading
import time

from Monitor.options import handle_options, run_user
from helpers.alert import AlertMobile
import yaml

from helpers.basic import get_config, is_market_hours, configured_users

worker_thread = None
stop_event = threading.Event()

# How often the supervisor re-checks the clock. Only affects how promptly the
# monitor thread starts at 09:20 and stops at 15:31.
SUPERVISOR_POLL_SECONDS = 30

config_file = "Monitor/config.yaml"
BROKER_CONFIG = "TradingBroker/config.yaml"

def _safe_alert(heading, message, priority):
    """Best-effort alert that never raises."""
    try:
        AlertMobile().send(heading=heading, message=message, priority=priority)
    except Exception as e:
        print(f"AlertMobile failed for {heading}: {e} -- original message: {message}")

def background_task(user):
    """Monitor loop for ONE account, until ``stop_event`` is set.

    ``user`` is passed in rather than resolved from config. It used to come
    from ``trading_account()`` — the FIRST entry of ``api_info.users`` — which
    is exactly what made this single-account: adding names to the config gave
    every process the same book. Validation happens once in ``main()``, so by
    here the account is known-good.
    """
    handle_objs = {}
    print(f"Monitoring task started for {user}")
    try:
        _safe_alert(f"{user}_Monitor", f"Monitoring started for {user}", priority=3)

        while not stop_event.is_set():
            try:
                if user not in handle_objs:
                    handle_objs[user] = handle_options(user)
                if run_user(user, handle_objs[user]):
                    pass
                else:
                    del handle_objs[user]
            except Exception as e:
                _safe_alert(
                    "MonitorLoopError",
                    f"{type(e).__name__}: {e}",
                    priority=3
                )
                time.sleep(5)
    except Exception as e:
        print(f"background_task crashed: {e}")
        _safe_alert(
            f"{user}_MonitorCrash",
            f"background_task raised {type(e).__name__}: {e}. Monitoring has stopped.", priority=5
        )
    finally:
        _safe_alert(f"{user}_Monitor",
                    f"Monitoring stopped for {user}", priority=3)
        print(f"Monitoring task stopped for {user}")
def start_watch(user):
    global worker_thread, stop_event

    if worker_thread and worker_thread.is_alive():
        return {"message": "Task already running"}

    stop_event.clear()
    worker_thread = threading.Thread(target=background_task, args=(user,),
                                     daemon=True)
    worker_thread.start()

    return {"message": "Monitoring Task started"}
def stop_watch():
    global worker_thread, stop_event

    if not worker_thread or not worker_thread.is_alive():
        return {"message": "No task running"}

    stop_event.set()
    if worker_thread:
        worker_thread.join(timeout=10)

    if worker_thread and worker_thread.is_alive():
        return {"message": "Unable to stop thread"}

    if worker_thread and not worker_thread.is_alive():
        worker_thread = None

    return {"message":"Monitoring Task stopped"}

def _resolve_user(argv=None):
    """Parse ``--user`` and check it against config AND broker credentials.

    Refusing to start is the whole point of doing this here. The failure this
    guards against is not a crash, it is a monitor that comes up, logs
    cheerfully, keeps its heartbeat green and never places an order — which is
    what an account missing from ``api_info.users`` produces (the per-cycle
    gate in ``Monitor/options.py`` skips the body and returns success).
    Better to never start than to look like a working monitor.

    Credentials are checked too because they live in a DIFFERENT file
    (``TradingBroker/config.yaml``). An account listed in one and absent from
    the other is a misconfiguration that would otherwise surface hours later
    as a BrokerAuthFailed alert on the first order.
    """
    ap = argparse.ArgumentParser(
        description="Run the trading monitor for one account.")
    ap.add_argument("--user", required=True,
                    help="account to monitor; must be listed in "
                         "api_info.users and have broker credentials")
    args = ap.parse_args(argv)
    user = args.user.strip()

    try:
        allowed = configured_users(get_config(config_file) or {})
    except Exception as e:
        raise SystemExit(f"FATAL: could not read {config_file}: {e}")
    if not allowed:
        raise SystemExit(
            f"FATAL: api_info.users is empty in {config_file}. Refusing to "
            f"start rather than trading an unidentified book."
        )
    if user not in allowed:
        raise SystemExit(
            f"FATAL: account {user!r} is not in api_info.users ({allowed}) "
            f"in {config_file}. Refusing to start: the monitor would run, log "
            f"and hold its heartbeat green without ever placing an order."
        )

    try:
        with open(BROKER_CONFIG) as f:
            credentials = (yaml.safe_load(f) or {}).get("credentials") or {}
    except Exception as e:
        raise SystemExit(f"FATAL: could not read {BROKER_CONFIG}: {e}")
    if user not in credentials:
        raise SystemExit(
            f"FATAL: no broker credentials for {user!r} in {BROKER_CONFIG} "
            f"(has: {sorted(credentials)}). The proxy could not authenticate "
            f"this account, so every order would be rejected."
        )

    return user


if __name__ == "__main__":
    USER = _resolve_user()
    print(f"autotrade supervisor for account: {USER}")

    # Process-local, and deliberately NOT persisted anywhere.
    #
    # This used to be re-read from monitor_info.autotrade at the top of every
    # iteration, which made "has the session state changed?" a question about
    # SHARED state rather than about this process. Two consequences, both of
    # which this fixes:
    #
    #   * With one instance it still worked, but only because the same process
    #     was the sole writer.
    #   * With one instance per user (the direction this is heading), the first
    #     process to flip the flag would suppress every other process's
    #     start_watch() — they'd read the already-updated value, see no change,
    #     and never start monitoring. Silently, while the dashboard reported
    #     "Monitoring" off the same global flag.
    #
    # None (not False) so the first iteration always acts, which re-establishes
    # the correct state after a restart mid-session.
    prev_trade_status = None

    while True:
        try:
            # Start or stop the monitoring thread on the session boundary.
            # is_market_hours lives in helpers.basic so main.py's /status
            # derives the dashboard pill from this exact same window.
            trade_status = is_market_hours()
            if trade_status != prev_trade_status:
                prev_trade_status = trade_status
                print(f"Session state changed to {trade_status}, triggering monitor ...")
                if trade_status:
                    print(start_watch(USER))
                else:
                    print(stop_watch())
        except Exception as e:
            print(f"autotrade supervisor loop error: {e}")
            _safe_alert("AutotradeSupervisorError",
                        f"autotrade loop raised {type(e).__name__}: {e}. "
                        f"Will retry in {SUPERVISOR_POLL_SECONDS}s.", priority=5)

        # The flaskapp start/stop that used to live here moved to systemd
        # timers. It controlled a SHARED unit from a loop that is now per
        # account, and it needed passwordless sudo to do it — neither
        # survives one instance per user.
        time.sleep(SUPERVISOR_POLL_SECONDS)
