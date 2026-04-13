from flask import Flask, request, jsonify,render_template
import yaml
from TradingBroker.kite_api_bot import KITE_CONNECT
from MarketAnalysis.fetch_data import get_ltp, get_option_chain

from Monitor.options import *
# Create Flask app
app = Flask(__name__)
broker_connections = {}
SUPER_USER = "sashi"
USER = "sashi"
# Home route
# Global control variables
worker_thread = None
stop_event = threading.Event()

def background_task():
    global USER
    print("Monitoring task started")
    while not stop_event.is_set():
        monitor_obj = handle_options(USER)
        run_user(USER, monitor_obj)
    print("Monitoring task stopped")

def get_credentials():
    with open("TradingBroker/config.yaml", "r") as file:
        config = yaml.safe_load(file)
    return config["credentials"]

# Example API: greet user
#@app.route("/get_broker", methods=["GET"])
def get_broker(user):
    global broker_connections
    #name = request.args.get("name")
    credentials = get_credentials()
    if not user or not user in credentials:
        return [False, "Either user or his credentials are missing"]
    #
    kc = KITE_CONNECT(user, credentials.get(user))
    if kc:
        broker_connections[user]= kc
        return [True, "Got Connection successfully"]
    else:
        return [False, "Failed to get connection"]

@app.route("/start_watch")
def start_watch():
    global worker_thread, stop_event

    if worker_thread and worker_thread.is_alive():
        return jsonify({"message": "Task already running"})

    stop_event.clear()
    worker_thread = threading.Thread(target=background_task)
    worker_thread.start()

    return [True, f"Monitoring Task started"]


@app.route("/stop_watch")
def stop_watch():
    global worker_thread, stop_event

    if not worker_thread or not worker_thread.is_alive():
        return jsonify({"message": "No task running"})

    stop_event.set()
    worker_thread.join()

    return [True, f"Monitoring Task stopped"]

@app.route("/")
def home():
    return render_template("dashboard.html")


@app.route("/get_orders", methods=["GET"])
def get_orders():
    global broker_connections
    user = request.args.get("user")
    orders = {}
    try:
        if user:
            if not user in broker_connections:
                get_broker(user)
            orders[user] = broker_connections[user].fetch_orders()
        else:
            for user in broker_connections:
                orders[user] = broker_connections[user].fetch_orders()
        return [True, orders]
    except Exception as e:
        print(f"Failed to get orders with error: {e}")
        return [False, f"Failed to get orders with error: {e}"]
@app.route("/get_positions", methods=["GET"])
def get_positions():
    global broker_connections
    positions = {}
    try:
        user = request.args.get("user")
        print(f"Trying to get positions for user {user}")
        positions = {}
        if user:
            if not user in broker_connections:
                print(f"Get the broker connection for {user}...")
                get_broker(user)
            positions[user] =  broker_connections[user].fetch_optoin_positions()
        else:
            for user in broker_connections:
                positions[user] = broker_connections[user].fetch_optoin_positions()
        return [True, positions]
    except Exception as e:
        print(f"Failed to get orders with error: {e}")
        return [False, f"Failed to get orders with error: {e}"]
# Example API: add numbers
@app.route("/get_current_price/<symbol>/<request_type>", methods=["GET"])
def get_current_price(symbol, request_type):
    global broker_connections
    if not symbol:
        symbol = request.args.get("symbol")
    if not request_type:
        request_type = request.args.get("request_type")
    if request_type == 'option':
        exchange='NFO'
    else:
        exchange='NSE'
#    ltp = get_ltp(symbol=symbol, request_type=request_type)
    print(f"Sending request fetch_last_price to broker with symbol {symbol}, exchange {exchange}")
    ltp = broker_connections[SUPER_USER].fetch_last_price(symbol=symbol,exchange=exchange)
    if ltp:
        return [True, ltp]
    else:
        return [False, None]
@app.route("/get_optionchain", methods=["GET"])
def get_optionchain(symbol, expiry):
    try:
        if not symbol:
            symbol = request.args.get("symbol")
        if not expiry:
            expiry = request.args.get("expiry")
        if symbol in ('NIFTY', 'NIFTY 50'):
            index = 'NIFTY'
            symbol_price = get_current_price('NIFTY 50', 'stock')
            symbol_price = symbol_price[1]['NSE:NIFTY 50']['last_price']
        #
        optionchain = {'PE': {}, 'CE': {}}
        for option_type in ('PE', 'CE'):
            strike_price = 1000
            if option_type == 'PE':
                strike_position = int(symbol_price / 100) * 100 - 300
            else:
                strike_position = int(symbol_price / 100) * 100 + 400
            while strike_price >= 10:
                strike = index + str(expiry) + str(strike_position) + option_type
                strike_price = get_current_price(strike, 'option')
                if strike_price[1]:
                    strike_price = strike_price[1][f"NFO:{strike}"]["last_price"]
                else:
                    strike_price = 1000
                optionchain[option_type][strike] = strike_price if strike_price != 1000 else 0
                strike_position = (strike_position - 50) if option_type == 'PE' else (strike_position + 50)
        return [True, optionchain]
    except Exception as e:
        return [False, f"Could not get the optionchain: {e}"]

@app.route("/place_order", methods=["GET"])
def place_order():
    user = request.args.get("user")
    symbol = request.args.get("symbol")
    quantity = request.args.get("quantity")
    transaction_type = request.args.get("transaction_type")
    exchange = request.args.get("exchange")
    try:
        if user:
            if not user in broker_connections:
                get_broker(user)
            order_id = broker_connections[user].place_order(symbol=symbol, quantity=quantity, transaction_type=transaction_type, exchange=exchange)
            return [True, f"Order is placed successfully:{order_id}"]
        else:
            raise Exception(f"User cannot be null")
    except Exception as e:
        print(f"Failed to place order with error: {e}")
        return [False, f"Failed to place order with error: {e}"]

@app.route("/get_watch_config", methods=["GET"])
def get_watch_config():
    #user = request.args.get("user")
    watch_config = {}
    try:
        with open("Monitor/config.yaml", "r") as file:
            watch_config = yaml.safe_load(file)
        return [True, watch_config]
    except Exception as e:
        return [False, {"Error": f"Failed to read the config: {e}"}]

@app.route("/put_watch_config", methods=["GET"])
def put_watch_config():
    watch_config = {}
    try:
        with open("Monitor/config.yaml", "r") as file:
            watch_config = yaml.safe_load(file)
        for x, y in watch_config.items():
            if x in request.args:
                if request.args.get(x).strip().lower() in ('true', 'false'):
                    watch_config[x] = (request.args.get(x).strip().lower() == "true")
                else:
                    try:
                        watch_config[x] = int(request.args.get(x).strip())
                    except Exception as e:
                        watch_config[x] = request.args.get(x).strip()

        with open("Monitor/config.yaml", "w") as file:
            yaml.dump(watch_config, file, default_flow_style=False, sort_keys=False)
        return [True, watch_config]
    except Exception as e:
        return [False, {"Error": f"Failed to write the config: {e}"}]


@app.route("/get_watch_log", methods=["GET"])
def get_watch_log():
    watch_log = ""
    try:
        user = request.args.get("user")
        with open(f"{user}_watch.log", "r") as file:
            watch_log = file.read()
        return [True, watch_log]
    except Exception as e:
        return [False, {"Error": f"Failed to get the watch log: {e}"}]

# Run the app
if __name__ == "__main__":
    get_broker(SUPER_USER)
    app.run(debug=True, port=5000)
