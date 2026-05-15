from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import re, logging, os, yaml
def last_weekday_of_month(year, month, weekday):
    """Return last given weekday of a month (0=Mon ... 6=Sun)."""
    d = datetime(year, month, 1) + relativedelta(day=31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d

def split_symbol(symbol):
    """Parse KiteConnect option tradingsymbol into components."""
    EXPIRY_WEEKDAY = {
        "SENSEX": 3,  # Thursday
        "NIFTY": 1,  # Tuesday
        "BANKNIFTY": 1,  # Tuesday
        "FINNIFTY": 1,  # Tuesday
    }
    match = re.match(r"([A-Z]+)(.+?)(CE|PE)?$", symbol)
    if not match:
        return None
    underlying, middle, opt_type = match.groups()
    if re.search(r"[A-Z]{3}", middle):
        yy = int(middle[:2])
        mon_str = middle[2:5]
        strike = int(middle[5:])
        year = 2000 + yy
        month = datetime.strptime(mon_str, "%b").month
        expiry_dt = last_weekday_of_month(year, month, EXPIRY_WEEKDAY.get(underlying, 1))
    else:
        # Weekly expiry e.g. NIFTY2592324900PE
        date_part = middle[:-5] # assuming strike is 5 digit
        dd = int(date_part[-2:])
        mm = int(date_part[2:-2]) if len(date_part[2:-2]) > 0 else 1
        yy = int(date_part[:2])
        strike = int(middle[-5:])
        expiry_dt = datetime(2000+yy, mm, dd)
    return underlying, expiry_dt, strike, opt_type

class SyncFileHandler(logging.FileHandler):
    """FileHandler that flushes and syncs every log record."""
    def emit(self, record):
        super().emit(record)  # normal logging
        self.stream.flush()  # flush Python buffer
        os.fsync(self.stream.fileno())  # flush OS buffer

def get_config(config_file):
    with open(config_file, "r") as file:
        config = yaml.safe_load(file)
    return config
def put_config(config_file, config_item, config_value):
    watch_config = {}
    try:
        with open(config_file, "r") as file:
            watch_config = yaml.safe_load(file)
        for x, y in watch_config.items():
            if x == config_item:
                if config_value.strip().lower() in ('true', 'false'):
                    watch_config[x] = (config_value.strip().lower() == "true")
                else:
                    try:
                        watch_config[x] = int(config_value.strip())
                    except Exception as e:
                        watch_config[x] = config_value.strip()

        with open(config_file, "w") as file:
            yaml.dump(watch_config, file, default_flow_style=False, sort_keys=False)
        return [True, watch_config]
    except Exception as e:
        return [False, {"Error": f"Failed to write the config {config_file}: {e}"}]
