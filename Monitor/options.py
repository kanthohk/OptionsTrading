import logging, os
import yaml, time, requests, re, threading
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

EXPIRY_WEEKDAY = {
    "SENSEX": 3, # Thursday
    "NIFTY": 1,  # Tuesday
    "BANKNIFTY": 1,  # Tuesday
    "FINNIFTY": 1,  # Tuesday
}

OPTIONS_TO_AVOID = [
"NIFTY26DEC22000PE",
"NIFTY26DEC23000PE",
"NIFTY26DEC24000PE",
"NIFTY26DEC24000CE"
]

def last_weekday_of_month(year, month, weekday):
    """Return last given weekday of a month (0=Mon ... 6=Sun)."""
    d = datetime(year, month, 1) + relativedelta(day=31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d

def split_symbol(symbol):
    """Parse KiteConnect option tradingsymbol into components."""
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
        yy = int(date_part[-2:])
        mm = int(date_part[2:-2]) if len(date_part[2:-2]) > 0 else 1
        dd = int(date_part[:2])
        strike = int(middle[-5:])
        expiry_dt = datetime(2000+yy, mm, dd)
    return underlying, expiry_dt, strike, opt_type

class SyncFileHandler(logging.FileHandler):
    """FileHandler that flushes and syncs every log record."""

    def emit(self, record):
        super().emit(record)  # normal logging
        self.stream.flush()  # flush Python buffer
        os.fsync(self.stream.fileno())  # flush OS buffer

class handle_options:
    def __init__(self, user):
        self.user = user
        self.logger = self._setup_logger()
        self.api_url = "http://localhost:5000"
        self.lock_profit = 0
        self.trail_profit_hit_count = 0
        self.positions = []
        self.closed_positions = []
        self.quantity = 0
        self.long_put_symbol = None
        self.short_put_symbol = None
        self.short_call_symbol = None
        self.long_call_symbol = None
        self.short_put_entry = 0
        self.short_call_entry = 0
        self.long_call_entry = 0
        self.long_put_price = 0
        self.short_put_price = 0
        self.short_call_price = 0
        self.long_call_entry = 0
        self.long_call_price = 0
        self.total_premium_collected = 0
        self.total_premium_earned = 0
        self.strategy = None
        self.config = self.get_config()

    def get_config(self):
        with open("Monitor/config.yaml", "r") as file:
            self.config = yaml.safe_load(file)
        return self.config

    def put_config(self):
        try:
            self.config['last_watched'] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open("Monitor/config.yaml", "w") as file:
                yaml.dump(self.config, file)
        except Exception as e:
            self.logger.info(f"Failed to update the config with error {e}")

    def _setup_logger(self):
        """Create a per-user thread-safe logger with immediate disk writes."""
        logger = logging.getLogger(f"{self.user}_logger")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            fh = SyncFileHandler(f"{self.user}_watch.log", mode='w')
            formatter = logging.Formatter(
                #'%(asctime)s - %(threadName)s - %(levelname)s - %(message)s'
                '%(asctime)s - %(message)s'
            )
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        return logger


    def process_positions(self):
        #self.logger.info("Fetching options from system ...")
        url = f"{self.api_url}/get_positions?user={self.user}"
        self.positions = []
        self.closed_positions = []
        self.total_premium_collected = self.total_premium_earned = 0
        response = requests.get(url)
        success = response.json()[0]
        if success:
            positions_list = response.json()[1][self.user]
            #self.logger.info(f"Received {len(positions_list)} positions, processing them")
            for position in positions_list:
                if position['tradingsymbol'] in OPTIONS_TO_AVOID:
                    continue
                position_to_save = {}
                position_to_save = position
                position_to_save['transtype'] = 'buy' if position['buy_quantity'] > position['sell_quantity'] else 'sell'
                #
                ##### Splitting the symbol to get insights  ################

                if position_to_save['tradingsymbol']:
                    position_to_save['underlying'], position_to_save['expiry'], position_to_save['strike'], position_to_save['option_type'] \
                        = split_symbol(position_to_save['tradingsymbol'])


                if position['quantity'] == 0:
                    self.closed_positions.append(position_to_save)
                    self.total_premium_earned -= position_to_save['pnl']
                else:
                    #####Get Option latest price ################

                    url = f"{self.api_url}/get_current_price/{position_to_save['tradingsymbol']}/option"
                    response = requests.get(url)
                    if response.json()[0]:
                        position_to_save['last_price']  = response.json()[1][f"NFO:{position_to_save['tradingsymbol']}"]['last_price']
                    else:
                        self.logger.info(f"Failed to get {position_to_save['tradingsymbol']} ...{response.json()}")
                    #
                    self.quantity = max(self.quantity, abs(position_to_save['quantity'])) if abs(position_to_save['quantity'])>0 else self.quantity
#                    print(f"{position_to_save['buy_price']}, {position_to_save['buy_quantity']}, {position_to_save['last_price']},{position_to_save['sell_quantity']}, {position_to_save['sell_price']}")
                    self.total_premium_collected -= position_to_save['buy_price'] * position_to_save['buy_quantity']
                    self.total_premium_earned -= position_to_save['last_price'] * position_to_save['buy_quantity']
                    self.total_premium_collected += position_to_save['sell_price'] * position_to_save['sell_quantity']
                    self.total_premium_earned += position_to_save['last_price'] * position_to_save['sell_quantity']

                    self.positions.append(position_to_save)

            import pandas as pd
            df=pd.DataFrame(self.positions)
            self.logger.info(f"\n{df[['expiry', 'strike', 'option_type', 'quantity', 'average_price', 'last_price']]}")

    def analyze_positions(self):
        #
        call_long_strike = call_short_strike = put_short_strike = put_long_strike = 0
        put_long_expiry = put_short_expiry = call_short_expiry = call_long_expiry = date(2000, 1, 1)
        put_long_underlying = put_short_underlying = call_short_underlying = call_long_underlying = None
        long_strangle_found = short_strangle_found = False
        for position in self.positions:
            if position['transtype'] == "buy" and position['option_type'] == "PE":
                put_long_strike = position['strike']
                put_long_expiry = position['expiry']
                put_long_underlying = position['underlying']
                if (call_long_strike > 0
                        and put_long_strike < call_long_strike
                        and put_long_expiry == call_long_expiry
                        and put_long_underlying == call_long_underlying):
                        long_strangle_found = True
            elif position['transtype'] == "sell" and position['option_type'] == "PE":
                put_short_strike = position['strike']
                put_short_expiry = position['expiry']
                put_short_underlying = position['underlying']
                if (call_short_strike > 0
                        and put_short_strike < call_short_strike
                        and put_short_expiry == call_short_expiry
                        and put_short_underlying == call_short_underlying):
                        short_strangle_found = True
            elif position['transtype'] == "sell" and position['option_type'] == "CE":
                call_short_strike = position['strike']
                call_short_expiry = position['expiry']
                call_short_underlying = position['underlying']
                if (0 < put_short_strike < call_short_strike
                        and put_short_expiry == call_short_expiry
                        and put_short_underlying == call_short_underlying):
                        short_strangle_found = True
            elif position['transtype'] == "buy" and position['option_type'] == "CE":
                call_long_strike = position['strike']
                call_long_expiry = position['expiry']
                call_long_underlying = position['underlying']
                if (0 < put_long_strike < call_long_strike
                        and put_long_expiry == call_long_expiry
                        and put_long_underlying == call_long_underlying):
                        long_strangle_found = True
        if long_strangle_found and short_strangle_found:
            self.strategy = "IRON_CONDOR"
            self.underlying = put_short_underlying
            self.expiry = put_short_expiry
        elif long_strangle_found:
            self.strategy = "LONG_STRANGLE"
            self.underlying = put_long_underlying
            self.expiry = put_long_expiry
        elif short_strangle_found:
            self.strategy = "SHORT_STRANGLE"
            self.underlying = put_short_underlying
            self.expiry = put_short_expiry


        self.logger.info(f"Strategy identified {self.strategy}.")

    def get_pnl(self):
        pnl=0
        for position in self.positions:
            # Net open quantity
            net_qty = position['buy_quantity'] - position['sell_quantity']

            # Average prices (safe calculation)
            avg_buy_price = position['buy_value'] / position['buy_quantity'] if position['buy_quantity'] != 0 else 0
            avg_sell_price = position['sell_value'] / position['sell_quantity'] if position['sell_quantity'] != 0 else 0

            # Realized PnL → closed quantity
            closed_qty = min(position['buy_quantity'], position['sell_quantity'])

            realized = (avg_sell_price - avg_buy_price) * closed_qty * position['multiplier']

            # Unrealized PnL → open quantity
            if net_qty > 0:
                unrealized = (position['last_price'] - avg_buy_price) * net_qty * position['multiplier']
            elif net_qty < 0:
                unrealized = (avg_sell_price - position['last_price']) * abs(net_qty) * position['multiplier']
            else:
                unrealized = 0

            # Total PnL
            pnl += realized + unrealized
            #print(f"Open Position: {position['tradingsymbol']}, Profit:{ realized + unrealized}")

        for position in self.closed_positions:
            if position['expiry'] != self.expiry or position['tradingsymbol'] in [pos['tradingsymbol'] for pos in self.positions]:
                continue
            pnl += position['pnl']
            #print(f"Closed Position: {position['tradingsymbol']}, Profit:{position['pnl']}")

        return int(pnl)
    def check_stop_loss(self):

    # Generate base factor based on distance from expiry
        dte = (self.expiry.date() - date.today()).days

        if dte > 20:
            base_factor = 1.5
        elif 15 < dte <= 20:
            base_factor = 1.4
        elif 10 < dte <= 15:
            base_factor = 1.3
        elif 5 < dte <= 10:
            base_factor = 1.2
        else:
            base_factor = 1.0

    # Get IndiaVix
        url = f"{self.api_url}/get_current_price/INDIA VIX/stock"
        response = requests.get(url)
        if response.json()[0]:
            vix_value = response.json()[1]["NSE:INDIA VIX"]["last_price"]
            self.logger.info(f"VIX: {vix_value}")
        else:
            self.logger.info(f"Failed to get the VIX. Using default value")
            return False

    # Get Index price
        if self.underlying == 'NIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY 50/stock"
            underlying_str = 'NSE:NIFTY 50'
        elif self.underlying == 'BANKNIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY BANK/stock"
            underlying_str = 'NSE:NIFTY BANK'
        else:
            url = f"{self.api_url}/get_current_price/{self.underlying}/stock"
            underlying_str = f'NSE:{self.underlying}'

        response = requests.get(url)
        if response.json()[0]:
            index_current_price = int(response.json()[1][underlying_str]['last_price'])
            self.logger.info(f"{underlying_str}: {index_current_price}")
        else:
            self.logger.info(f"Failed to get the Index price.")
            return False

    # Generate IndiaVix factor based on INDIAVIX
        if vix_value < 10:
            vix_factor = 0.8
        elif 10 <= vix_value < 12:
            vix_factor = 0.9
        elif 12 <= vix_value < 15:
            vix_factor = 1
        elif 15 <= vix_value < 18:
            vix_factor = 1.2
        elif 18 <= vix_value < 22:
            vix_factor = 1.5
        else:
            vix_factor = 1.6

    # Calculate Stop Loss Factor & Premium
        #sl_factor = 1 + ( - 1) * vix_factor
        #sl_factor = (0.6 * base_factor) + (0.4 * vix_factor)
        sl_factor = base_factor * vix_factor
        if self.strategy == 'LONG_STRANGLE':
            sl_factor = 2 - sl_factor
        stop_loss_hit = False
        sl_premium = int((self.total_premium_collected) * sl_factor)
        self.logger.info(f"SL Factor: {sl_factor}, VIX Factor: {vix_factor}, Base factor: {base_factor}")
        self.logger.info(f"Collected: {int(self.total_premium_collected)}, Earned: {int(self.total_premium_earned)}")
        self.logger.info(f"StopLoss: {sl_premium}")
        if self.total_premium_earned >= sl_premium:
            self.logger.info(f"Stoploss premium {sl_premium} hit. Better close the positions for the day.")
            stop_loss_hit = True
        #self.logger.info(f"Base_factor: {base_factor}, VIX_factor: {vix_factor}, SL Factor: {sl_factor}")
        #self.logger.info(f"Premium Collected: {round(self.total_premium_collected,2)}, Premium Earned: {round(self.total_premium_earned,2)}, StopLoss Premium: {round(sl_premium,2)}")

    # Check if the strikes are nearing the underlying index
        nearing_strike = False
        # Get Option strikes
        long_put_strike, long_put_symbol = next(((p['strike'], p['tradingsymbol']) for p in self.positions if p['option_type'] == 'PE' and p['transtype'] == 'buy'), (None,None))
        short_put_strike, short_put_symbol = next(((p['strike'], p['tradingsymbol']) for p in self.positions if p['option_type'] == 'PE' and p['transtype'] == 'sell'), (None,None))
        short_call_strike, short_call_symbol = next(((p['strike'], p['tradingsymbol']) for p in self.positions if p['option_type'] == 'CE' and p['transtype'] == 'sell'), (None,None))
        long_call_strike, long_call_symbol = next(((p['strike'], p['tradingsymbol']) for p in self.positions if p['option_type'] == 'CE' and p['transtype'] == 'buy'), (None, None))

    # Check if strikes nearing the index
        put_distance = index_current_price - (short_put_strike if short_put_strike else long_put_strike)
        call_distance = (short_call_strike if short_call_strike else long_call_strike) - index_current_price
        self.logger.info(f"{(short_put_strike if short_put_strike else long_put_strike)}<-----{put_distance}"
                         f"----->{index_current_price}<-----"
                         f"{call_distance}----->{(short_call_strike if short_call_strike else long_call_strike)}")
        if (index_current_price <= (short_put_strike if short_put_strike else long_put_strike) + self.config['minimum_distance_from_index']
                or index_current_price >= (short_call_strike if short_call_strike else long_call_strike) - self.config['minimum_distance_from_index']):
            nearing_strike = True

    # Is Stop Loss hit and also the Strikes nearing the index than close the position
        if stop_loss_hit or (nearing_strike and self.strategy != 'LONG_STRANGLE'):
            self.logger.info(f"### Close the strikes as Stop Loss Hit is {stop_loss_hit} and Nifty reaching the strikes is {nearing_strike} ###")
            try:
                if self.config['close_on_stoploss']:
                    self.logger.info(f"Closing the strikes for {self.strategy}")

                    if self.strategy in ('SHORT_STRANGLE', 'IRON_CONDOR'):
                        self.place_order(symbol=short_call_symbol,
                                         quantity=self.quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.place_order(symbol=short_put_symbol,
                                         quantity=self.quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                    if self.strategy in ('LONG_STRANGLE', 'IRON_CONDOR') \
                            and self.config['adjust_hedges']:
                        self.place_order(symbol=long_call_symbol,
                                         quantity=self.quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                        self.place_order(symbol=long_put_symbol,
                                         quantity=self.quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                    return True
                else:
                    self.logger.info(f"Not closing the positions as the indicator is {self.config['close_on_stoploss']} in config")
            except Exception as e:
                self.logger.error(f"Failed to close the Iron Condor: {e}")
#        else:
#            self.logger.info(f"Stop Loss premium {sl_premium} not hit OR PE/CE distance less than minimum distance {self.config['minimum_distance_from_index']}")
        return False

    def trail_profit(self):
        pnl = self.get_pnl()
        trail_profit = round(self.quantity * self.config['trailing_profit_multiplier'], 2)

        self.logger.info(f"Current Profit: {pnl}, Lock Profit: {self.lock_profit}, Trail Profit: {trail_profit}")

        # ✅ Trailing stop hit condition
        if pnl <= self.lock_profit and self.lock_profit > 0:
            self.trail_profit_hit_count += 1
            self.logger.info(f"Trailing profit hit count: {self.trail_profit_hit_count}")

            # ✅ CHECK HERE (correct place)
            if self.trail_profit_hit_count > self.config['trail_profit_threshold']:
                self.logger.info("Closing the positions")

                long_put_strike, long_put_symbol = next(
                    ((p['strike'], p['tradingsymbol']) for p in self.positions
                     if p['option_type'] == 'PE' and p['transtype'] == 'buy'),
                    (None, None)
                )

                short_put_strike, short_put_symbol = next(
                    ((p['strike'], p['tradingsymbol']) for p in self.positions
                     if p['option_type'] == 'PE' and p['transtype'] == 'sell'),
                    (None, None)
                )

                short_call_strike, short_call_symbol = next(
                    ((p['strike'], p['tradingsymbol']) for p in self.positions
                     if p['option_type'] == 'CE' and p['transtype'] == 'sell'),
                    (None, None)
                )

                long_call_strike, long_call_symbol = next(
                    ((p['strike'], p['tradingsymbol']) for p in self.positions
                     if p['option_type'] == 'CE' and p['transtype'] == 'buy'),
                    (None, None)
                )

                try:
                    if self.config['close_on_trailprofit']:
                        if self.strategy in ('SHORT_STRANGLE', 'IRON_CONDOR'):
                            self.place_order(symbol=short_call_symbol,
                                             quantity=self.quantity,
                                             transaction_type="BUY",
                                             exchange='NFO')
                            self.place_order(symbol=short_put_symbol,
                                             quantity=self.quantity,
                                             transaction_type="BUY",
                                             exchange='NFO')

                        if self.strategy in ('LONG_STRANGLE', 'IRON_CONDOR') \
                            and self.config['adjust_hedges']:
                            self.place_order(symbol=long_call_symbol,
                                             quantity=self.quantity,
                                             transaction_type="SELL",
                                             exchange='NFO')
                            self.place_order(symbol=long_put_symbol,
                                             quantity=self.quantity,
                                             transaction_type="SELL",
                                             exchange='NFO')
                        return True

                except Exception as e:
                    self.logger.info(f"Failed to close positions: {e}")

        else:
            # ✅ Reset only when condition fails
            if self.trail_profit_hit_count != 0:
                self.logger.info("Trailing profit hit count reset")
            self.trail_profit_hit_count = 0

        # ✅ Update trailing lock profit
        if pnl >= (self.lock_profit + trail_profit):
            self.lock_profit = max(self.lock_profit, pnl - trail_profit)
            self.logger.info(f"Locked Profit: {self.lock_profit}")
        else:
            self.logger.info(
                f"Will lock when P&L: {pnl} >= {self.lock_profit + trail_profit}"
            )

        return False

    def get_next_symbol(self, current_symbol, current_strike, option_type, threshold_price):
        symbol = current_symbol
        strike = new_strike = current_strike
        symbol_price = 0
        while symbol_price <= (threshold_price * ((self.config['adjust_at']+15)/100)):
            new_strike = strike-50 if option_type == 'CE' else strike+50
            symbol = symbol.replace(str(strike), str(new_strike))
            strike = new_strike
            url = f"{self.api_url}/get_current_price/{symbol}/option"
            response = requests.get(url)
            if response.json()[0]:
                symbol_price = response.json()[1][f"NFO:{symbol}"]['last_price']
                self.logger.info(f"Got the next {symbol} with price {symbol_price}")
            else:
                self.logger.info(f"Failed to get {symbol} ...{response.json()}")
        diff = (current_strike - new_strike) if option_type == 'CE' else (new_strike - current_strike)
        return symbol, diff


    def adjustments(self):
    #
        long_put_strike, long_put_symbol, long_put_price = next(
            ((p['strike'], p['tradingsymbol'], p['last_price']) for p in self.positions
             if p['option_type'] == 'PE' and p['transtype'] == 'buy'),
            (None, None, None)
        )

        short_put_strike, short_put_symbol, short_put_price = next(
            ((p['strike'], p['tradingsymbol'], p['last_price']) for p in self.positions
             if p['option_type'] == 'PE' and p['transtype'] == 'sell'),
            (None, None, None)
        )

        short_call_strike, short_call_symbol, short_call_price = next(
            ((p['strike'], p['tradingsymbol'], p['last_price']) for p in self.positions
             if p['option_type'] == 'CE' and p['transtype'] == 'sell'),
            (None, None, None)
        )

        long_call_strike, long_call_symbol, long_call_price = next(
            ((p['strike'], p['tradingsymbol'], p['last_price']) for p in self.positions
             if p['option_type'] == 'CE' and p['transtype'] == 'buy'),
            (None, None, None)
        )

        if short_call_price  > 0 and short_put_price > 0:
            if short_call_price <= short_put_price * self.config['adjust_at']/100:
                self.logger.info(f"PE Price: {short_put_price}, CE Price: {short_call_price}. Market is going down. Adjustment is needed on CE side")
                self.logger.info(f"as {int(min((short_call_price / short_put_price) * 100, (short_put_price / short_call_price) * 100))} is < {self.config['adjust_at']} (mentioned in config)")
                self.logger.info(f"Buying {short_call_symbol} & Selling {self.underlying}{int(short_call_strike)-50}CE")
                if self.config['adjustment']:
                    new_short_symbol, diff = self.get_next_symbol(short_call_symbol, short_call_strike, 'CE',
                                                                  short_put_price)
                    if self.strategy in ('SHORT_STRANGLE','IRON_CONDOR'):
                        self.place_order(symbol=short_call_symbol,
                                         quantity=self.quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.place_order(symbol=new_short_symbol,
                                         quantity=self.quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                    if self.strategy in ('LONG_STRANGLE', 'IRON_CONDOR') \
                            and self.config['adjust_hedges']:
                        self.place_order(symbol=long_call_symbol,
                                         quantity=self.quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                        self.place_order(symbol=long_call_symbol.replace(str(long_call_strike), str(int(long_call_strike)-diff)),
                                         quantity=self.quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                #self.logger.info(f"Need to sell {long_call_symbol} & Need to buy {self.underlying}{int(long_call_strike)-50}CE")
            elif short_put_price <= short_call_price * self.config['adjust_at']/100:
                self.logger.info(f"PE Price: {short_put_price}, CE Price: {short_call_price}. Market is going up. Adjustment is needed on PE side")
                self.logger.info(f"as {int(min((short_call_price / short_put_price) * 100, (short_put_price / short_call_price) * 100))} is < {self.config['adjust_at']} (mentioned in config)")
                self.logger.info(f"Buying {short_put_symbol} & Selling {self.underlying}{int(short_put_strike)+50}PE")
                if self.config['adjustment']:
                    new_short_symbol, diff = self.get_next_symbol(short_put_symbol, short_put_strike, 'PE',
                                                                  short_call_price)
                    if self.strategy in ('SHORT_STRANGLE','IRON_CONDOR'):
                        self.place_order(symbol=short_put_symbol,
                                         quantity=self.quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.place_order(symbol=new_short_symbol,
                                         quantity=self.quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                    if self.strategy in ('LONG_STRANGLE', 'IRON_CONDOR') \
                            and self.config['adjust_hedges']:
                        self.place_order(symbol=long_put_symbol,
                                         quantity=self.quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                        self.place_order(symbol=long_put_symbol.replace(str(long_put_strike), str(int(long_put_strike)+diff)),
                                         quantity=self.quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                #self.logger.info(f"Need to sell {long_put_symbol} & Need to buy {self.underlying}{int(long_put_strike)+50}PE")
            else:
                self.logger.info(f"PE Price: {short_put_price}, CE Price: {short_call_price}. No need of any adjustments as {int(min((short_call_price / short_put_price) * 100, (short_put_price / short_call_price) * 100))} is > {self.config['adjust_at']} (mentioned in config)")
        else:
            self.logger.info(f"Either PE_price or CE_price is not retrieved properly")

    def place_order(self, symbol, quantity, transaction_type, exchange):
        url = f"{self.api_url}/place_order?user={self.user}&symbol={symbol}&quantity={quantity}&transaction_type={transaction_type}&exchange={exchange}"
        response = requests.get(url)
        success = response.json()[0]
        if success:
            self.logger.info(f"{transaction_type} transaction for  {symbol} of {self.user} is successful")

    def run(self):
        self.logger.info("#" * 100)
        self.get_config()
        self.logger.info(f"Users in config: {self.config['users']}\nStarted to Watch user: {self.user}")
        if self.user in self.config["users"].split(","):
            self.process_positions()
            self.analyze_positions()
            if self.strategy in ('IRON_CONDOR', 'SHORT_STRANGLE'):
                self.logger.info("-" * 25)
                #self.logger.info("Got the positions. Proceeding for stop loss")
                closed = self.check_stop_loss()
                if not closed:
                    self.logger.info("-" * 25)
                    #self.logger.info("Stop loss is verified. Proceeding to trail profit")
                    closed = self.trail_profit()
                    if not closed:
                        self.logger.info("-" * 25)
                        #self.logger.info("Trail Profit is done. Proceeding to check adjustments")
                        self.adjustments()
            self.put_config()
            time.sleep(self.config.get('delay'))

def run_user(user, handle_obj):
    """Worker function to run each user's trading logic."""
    try:
        handle_obj.run()
    except Exception as e:
        print(f"Error in thread for {user}: {e}")

if __name__ == "__main__":
    print("Current directory:", os.getcwd())

    handle_objs = {}
    while True:
        with open("Monitor/config.yaml", "r") as file:
            watch_config = yaml.safe_load(file)
        if watch_config:
            remove_users = []
            # Delete the users in handle_objs but not in watch_sync
            for user in handle_objs:
                if user not in watch_config['users'].split(","):
                    remove_users.append(user)
            for user in remove_users:
                del handle_objs[user]
            # Add the users in handle_objs that are in watch_sync
            for user in watch_config['users'].split(","):
                if user not in handle_objs:
                    handle_objs[user] = handle_options(user)
            # Spawn a thread for each user
            threads = []
            for user, handle_obj in handle_objs.items():
                t = threading.Thread(target=run_user, args=(user, handle_obj))
                t.daemon = True  # allows program to exit even if threads are running
                t.start()
                threads.append(t)

            # Join all threads (wait for them to finish one cycle)
            for t in threads:
                t.join()
        # Sleep before next iteration (to avoid hammering file + APIs)
        time.sleep(20)
