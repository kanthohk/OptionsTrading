from Monitor.options import *
from helpers.alert import AlertMobile
from helpers.basic import get_config, put_config
import time, yaml, os

worker_thread = None
stop_event = threading.Event()
market_start = "09:20"
market_stop = "15:30"
api_start = "08:00"
api_stop = "17:00"

config_file = "Monitor/config.yaml"

def background_task():
    handle_objs = {}
    print("Monitoring task started")
    watch_config = get_config(config_file)
    user = watch_config["users"].split(",")[0]
    AlertMobile().send(heading=f"{user}_Monitor",
                       message=f"Monitoring started for {user}")

    while not stop_event.is_set():
        if user not in handle_objs:
            handle_objs[user] = handle_options(user)
        if run_user(user, handle_objs[user]):
            pass
        else:
            del handle_objs[user]
    AlertMobile().send(heading=f"{user}_Monitor",
                       message=f"Monitoring stopped for {user}")
    print("Monitoring task stopped")
def start_watch():
    global worker_thread, stop_event

    if worker_thread and worker_thread.is_alive():
        return {"message": "Task already running"}

    stop_event.clear()
    worker_thread = threading.Thread(target=background_task)
    worker_thread.start()

    return {"message": "Monitoring Task started"}
def stop_watch():
    global worker_thread, stop_event

    if not worker_thread or not worker_thread.is_alive():
        return {"message": "No task running"}

    stop_event.set()
    worker_thread.join(timeout=10)

    return {"message":"Monitoring Task stopped"}

if __name__ == "__main__":
    prev_trade_status = trade_status = None
    while True:
    # (1) Start or Stop the Monitoring
        print("\n\nAuto Checking Market hours ...")
        now = datetime.now()
        is_start_time = (now.hour == int(market_start.split(":")[0].strip()) and
                         now.minute == int(market_start.split(":")[1].strip()))
        is_stop_time = (now.hour == int(market_stop.split(":")[0].strip()) and
                        now.minute == int(market_stop.split(":")[1].strip()))
        is_weekday = now.weekday() < 5
        if is_start_time and is_weekday:
            trade_status = True
        elif is_stop_time and is_weekday:
            trade_status = False
        if trade_status != prev_trade_status:
            print("Status changed, updating config ...")
            put_config(config_file,"autotrade", str(trade_status))
        #
        watch_config = get_config(config_file)
        trade_status = watch_config["autotrade"]
        if trade_status != prev_trade_status:
            print("Status changed, triggering monitor ...")
            if trade_status:
                print(start_watch())
            else:
                print(stop_watch())
            prev_trade_status = trade_status
    # (2) Stop or start flaskapp
        api_start_time = (now.hour == int(api_start.split(":")[0].strip()) and
                         now.minute == int(api_start.split(":")[1].strip()))
        api_stop_time = (now.hour == int(api_stop.split(":")[0].strip()) and
                        now.minute == int(api_stop.split(":")[1].strip()))
        if api_stop_time:
            os.system("sudo systemctl stop flaskapp")
        if api_start_time:
            os.system("sudo systemctl start flaskapp")

    # Go to Sleep
        for i in range(10, 0, -1):
            #print(f"\rRecheck after: {i} secs", end="")
            time.sleep(1)
