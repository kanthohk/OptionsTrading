from Monitor.options import handle_options, run_user
import threading
from helpers.alert import AlertMobile
from helpers.basic import get_config, put_config
import time, yaml, os
import pytz
ist = pytz.timezone('Asia/Kolkata')

worker_thread = None
stop_event = threading.Event()
import datetime as dt
market_start = dt.time(9, 20)
market_stop = dt.time(15, 31)
api_start = dt.time(8, 0)
api_stop = dt.time(17, 0)

config_file = "Monitor/config.yaml"

def _safe_alert(heading, message, priority):
    """Best-effort alert that never raises."""
    try:
        AlertMobile().send(heading=heading, message=message, priority=priority)
    except Exception as e:
        print(f"AlertMobile failed for {heading}: {e} -- original message: {message}")

def background_task():
    handle_objs = {}
    print("Monitoring task started")
    user = None
    try:
        watch_config = get_config(config_file)
        user = watch_config["api_info"]["users"].split(",")[0]
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
            f"{user or 'unknown'}_MonitorCrash",
            f"background_task raised {type(e).__name__}: {e}. Monitoring has stopped.", priority=5
        )
    finally:
        _safe_alert(f"{user or 'unknown'}_Monitor",
                    f"Monitoring stopped for {user or 'unknown'}", priority=3)
        print("Monitoring task stopped")
def start_watch():
    global worker_thread, stop_event

    if worker_thread and worker_thread.is_alive():
        return {"message": "Task already running"}

    stop_event.clear()
    worker_thread = threading.Thread(target=background_task, daemon=True)
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

if __name__ == "__main__":
    prev_trade_status = trade_status = None
    prev_api_status = api_status = None

    while True:
        try:
            now = dt.datetime.now(ist)
            current_time = now.time()
            watch_config = get_config(config_file)
            monitor_info = watch_config.get("monitor_info") or {}
            prev_trade_status = str(monitor_info.get("autotrade", "false")).lower() == "true"
        # (1) Start or Stop the Monitoring
            #print("\n\nAuto Checking Market hours ...")
            is_weekday = now.weekday() < 5
            trade_status = (
                    is_weekday and
                    market_start <= current_time < market_stop
            )
            if trade_status != prev_trade_status:
                print("Status changed, updating config ...")
                put_config(config_file, "monitor_info.autotrade", str(trade_status))
            #
                print("Status changed, triggering monitor ...")
                if trade_status:
                    print(start_watch())
                else:
                    print(stop_watch())

        # (2) Stop or start flaskapp
            #
            api_status = (
                    is_weekday and
                    api_start <= current_time < api_stop
            )
            if api_status != prev_api_status:
                prev_api_status = api_status
                if api_status:
                    os.system("sudo systemctl start flaskapp")
                else:
                    os.system("sudo systemctl stop flaskapp")
        except Exception as e:
            print(f"autotrade supervisor loop error: {e}")
            _safe_alert("AutotradeSupervisorError",
                        f"autotrade loop raised {type(e).__name__}: {e}. Will retry in 10s.", priority=5)

    # Go to Sleep
        for i in range(30, 0, -1):
            #print(f"\rRecheck after: {i} secs", end="")
            time.sleep(1)
