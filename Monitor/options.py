import logging, os
import yaml, time, requests, re, threading
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
import datetime as dt
import pytz, csv
ist = pytz.timezone('Asia/Kolkata')
from Monitor.alert import AlertMobile
from helpers.basic import last_weekday_of_month, split_symbol, SyncFileHandler, get_config, put_config

OPTIONS_TO_AVOID = [
"NIFTY26DEC22000PE",
"NIFTY26DEC23000PE",
"NIFTY26DEC24000PE",
"NIFTY26DEC23000CE",
"NIFTY26DEC24000CE"
]

config_file = "Monitor/config.yaml"
class handle_options:
    def __init__(self, user):
        self.user = user
        self.logger = self._setup_logger()
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
        self.nearing_strike = False
        self.strategy = None
        self.config = get_config(config_file)
        self.api_url = self.config["url"]

    def _setup_logger(self):

        daystr = datetime.now(ist).strftime('%Y%m%d')
        logger_name = f"{self.user}_{daystr}_logger"
        logfile = f"{daystr}_watch.log"

        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)

        # Prevent duplicate handlers
        if logger.hasHandlers():
            logger.handlers.clear()

        fh = SyncFileHandler(logfile, mode='a')
        formatter = logging.Formatter('%(asctime)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        logger.propagate = False

        return logger

    def process_positions(self):
        #self.logger.info("Fetching options from system ...")
        url = f"{self.api_url}/get_positions?user={self.user}"
        self.positions = []
        self.closed_positions = []
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

                    self.positions.append(position_to_save)

            if len(self.positions) > 0:
                df=pd.DataFrame(self.positions)
                df.to_csv(f"positions_{dt.date.today()}.csv", header=True, mode='w', quoting=csv.QUOTE_NONNUMERIC, index=False)
                self.logger.info(f"\n{df[['tradingsymbol', 'expiry', 'strike', 'option_type', 'quantity', 'average_price', 'last_price']]}")
                return True
            else:
                self.logger.info(f"No position found.")
                return False
        else:
            self.logger.info("Failed to fetch positions.")
            return False

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
        curr_week_num = datetime.today().weekday()
        prev_week_num = curr_week_num - 1
        realized = unrealized = 0
        while 0 <= prev_week_num <= 3:
            if f"realized_pnl_{prev_week_num}" in self.config:
                realized += int(self.config[f"realized_pnl_{prev_week_num}"])
                break
            prev_week_num = prev_week_num - 1
        #
        for position in self.closed_positions:
            if position['expiry'] != self.expiry or position['tradingsymbol'] in [pos['tradingsymbol'] for pos in self.positions]:
                continue
            realized += position['pnl']
            #print(f"Closed Position: {position['tradingsymbol']}, Profit:{position['pnl']}")

        #
        for position in self.positions:
            # Net open quantity
            net_qty = position['buy_quantity'] - position['sell_quantity']

            # Average prices (safe calculation)
            avg_buy_price = position['buy_value'] / position['buy_quantity'] if position['buy_quantity'] != 0 else 0
            avg_sell_price = position['sell_value'] / position['sell_quantity'] if position['sell_quantity'] != 0 else 0

            # Realized PnL → closed quantity
            closed_qty = min(position['buy_quantity'], position['sell_quantity'])

            realized += (avg_sell_price - avg_buy_price) * closed_qty * position['multiplier']

            # Unrealized PnL → open quantity
            if net_qty > 0:
                unrealized += (position['last_price'] - avg_buy_price) * net_qty * position['multiplier']
            elif net_qty < 0:
                unrealized += (avg_sell_price - position['last_price']) * abs(net_qty) * position['multiplier']
            else:
                unrealized += 0

            # Total PnL

            #print(f"Open Position: {position['tradingsymbol']}, Profit:{ realized + unrealized}")

        self.config[f"realized_pnl_{curr_week_num}"] = realized
        put_config(config_file, "last_watched", datetime.now().strftime("%Y-%m-%d %H:%M"))
        put_config(config_file, f"realized_pnl_{curr_week_num}", realized)
        return int(realized + unrealized)

    def check_stop_loss(self):

        market_params = {}
        header_flag = True
        market_params['date_time'] = datetime.now(ist).strftime('%Y-%m-%d %H:%M')
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
        #
        total_premium_gained = self.get_pnl()
        if self.strategy == "IRON_CONDOR":
            total_premium_invested = round((self.quantity/self.config["quantity_per_lot"]) * self.config['investment'], 2)
        elif self.strategy == "SHORT_STRANGLE":
            total_premium_invested = round((self.quantity/self.config["quantity_per_lot"]) * self.config['investment'] * 2, 2)
        else:
            total_premium_invested = 0
        #
        sl_premium = -1 * int((total_premium_invested) * ((sl_factor)/100))
        self.logger.info(f"SL Factor: {round(sl_factor,2)}, VIX Factor: {vix_factor}, Base factor: {base_factor}")
        self.logger.info(f"Invested: {int(total_premium_invested)}, Loss/Gaim: {int(total_premium_gained)}")
        self.logger.info(f"StopLoss: {sl_premium}")
        if total_premium_gained <= sl_premium:
            self.logger.info(f"Stoploss premium {sl_premium} hit. Better close the positions for the day.")
            stop_loss_hit = True

    # Check if the strikes are nearing the underlying index
        self.nearing_strike = False
        # Get Option strikes
        long_put_strike, long_put_symbol, long_put_quantity = next(((p['strike'], p['tradingsymbol'], p['buy_quantity']-p['sell_quantity']) for p in self.positions if p['option_type'] == 'PE' and p['transtype'] == 'buy'), (None,None,None))
        short_put_strike, short_put_symbol, short_put_quantity = next(((p['strike'], p['tradingsymbol'], p['sell_quantity']-p['buy_quantity']) for p in self.positions if p['option_type'] == 'PE' and p['transtype'] == 'sell'), (None,None,None))
        short_call_strike, short_call_symbol, short_call_quantity = next(((p['strike'], p['tradingsymbol'], p['sell_quantity']-p['buy_quantity']) for p in self.positions if p['option_type'] == 'CE' and p['transtype'] == 'sell'), (None,None,None))
        long_call_strike, long_call_symbol, long_call_quantity = next(((p['strike'], p['tradingsymbol'], p['buy_quantity']-p['sell_quantity']) for p in self.positions if p['option_type'] == 'CE' and p['transtype'] == 'buy'), (None, None,None))

        put_distance = index_current_price - (short_put_strike if short_put_strike else long_put_strike)
        call_distance = (short_call_strike if short_call_strike else long_call_strike) - index_current_price
        self.logger.info(f"{(short_put_strike if short_put_strike else long_put_strike)}<-----{put_distance}"
                         f"----->{index_current_price}<-----"
                         f"{call_distance}----->{(short_call_strike if short_call_strike else long_call_strike)}")
        if (index_current_price <= (short_put_strike if short_put_strike else long_put_strike) + self.config['minimum_distance_from_index']
                or index_current_price >= (short_call_strike if short_call_strike else long_call_strike) - self.config['minimum_distance_from_index']):
            self.nearing_strike = True
            self.logger.info(f"<---- Strikes are neared <= {self.config['minimum_distance_from_index']}---->")
    # If the strike price is double
        strike_price_doubled = False
        short_put_sell_price, short_put_last_price = next(
            ((p['sell_price'], p['last_price']) for p in self.positions
             if p['option_type'] == 'PE' and p['transtype'] == 'sell'),
            (0, 0)
        )

        short_call_sell_price, short_call_last_price = next(
            ((p['sell_price'], p['last_price']) for p in self.positions
             if p['option_type'] == 'CE' and p['transtype'] == 'sell'),
            (0, 0)
        )

        if (short_call_last_price >= 2*short_call_sell_price) or (short_put_last_price >= 2*short_put_sell_price):
            strike_price_doubled = True
            self.logger.info(f"<------ Prices are doubled ------>")

        market_params['sl_factor'] = round(sl_factor,2)
        market_params['vix_factor'] = round(vix_factor, 2)
        market_params['base_factor'] = round(base_factor, 2)
        market_params['premium_invested'] = int(total_premium_invested)
        market_params['premium_gained'] = int(total_premium_gained)
        market_params['sl_premium'] = int(sl_premium)
        #
        market_params['short_pe_strike'] = short_put_strike
        market_params['short_pe_sell_price'] = short_put_sell_price
        market_params['short_pe_last_price'] = short_put_last_price
        market_params['put_distance'] = put_distance
        #
        market_params['short_ce_strike'] = short_call_strike
        market_params['short_ce_sell_price'] = short_call_sell_price
        market_params['short_ce_last_price'] = short_call_last_price
        market_params['call_distance'] = call_distance

        market_params_df = pd.DataFrame([market_params])
        market_params_df.to_csv(f"markets_{dt.date.today()}.csv", header=header_flag, mode='a', quoting=csv.QUOTE_NONNUMERIC, index=False)
        if header_flag:
            header_flag=False

       # Is Stop Loss hit and also the Strikes nearing the index than close the position
        if stop_loss_hit and self.nearing_strike and strike_price_doubled and self.strategy != 'LONG_STRANGLE':
            self.logger.info(f"### Close the strikes as Stop Loss Hit is {stop_loss_hit} and Nifty reaching the strikes is {self.nearing_strike} ###")
            try:
                if self.config['close_on_stoploss']:
                    self.logger.info(f"Closing the strikes for {self.strategy} because of Stoploss hit")
                    AlertMobile().send(heading=f"{self.user}_StopLoss",
                                       message=f"Closing the strikes for {self.strategy} because of Stoploss hit")
                    success = False
                    if self.strategy in ('SHORT_STRANGLE', 'IRON_CONDOR'):
                        success = self.place_order(symbol=short_call_symbol,
                                         quantity=short_call_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self.place_order(symbol=short_put_symbol,
                                             quantity=short_put_quantity,
                                             transaction_type="BUY",
                                             exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                    if self.strategy in ('IRON_CONDOR') \
                            and self.config['adjust_hedges']\
                            and success:
                        success = self.place_order(symbol=long_call_symbol,
                                         quantity=long_call_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self.place_order(symbol=long_put_symbol,
                                         quantity=long_put_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                            self.logger.info(success)
                    return success
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

        self.logger.info(f"Current Profit: {pnl}, Locked Profit: {self.lock_profit}, Trail Profit: {trail_profit}")

        # ✅ Trailing stop hit condition
        if pnl <= self.lock_profit and self.lock_profit > 0:
            self.trail_profit_hit_count += 1
            self.logger.info(f"Trailing profit hit count: {self.trail_profit_hit_count}")

            # ✅ CHECK HERE (correct place)
            if self.trail_profit_hit_count > self.config['trail_profit_threshold']:
                self.logger.info("Closing the positions")

                long_put_strike, long_put_symbol, long_put_quantity = next(
                    ((p['strike'], p['tradingsymbol'], p['buy_quantity']-p['sell_quantity']) for p in self.positions
                     if p['option_type'] == 'PE' and p['transtype'] == 'buy'),
                    (None, None,None)
                )

                short_put_strike, short_put_symbol, short_put_quantity = next(
                    ((p['strike'], p['tradingsymbol'], p['sell_quantity']-p['buy_quantity']) for p in self.positions
                     if p['option_type'] == 'PE' and p['transtype'] == 'sell'),
                    (None, None,None)
                )

                short_call_strike, short_call_symbol, short_call_quantity = next(
                    ((p['strike'], p['tradingsymbol'], p['sell_quantity']-p['buy_quantity']) for p in self.positions
                     if p['option_type'] == 'CE' and p['transtype'] == 'sell'),
                    (None, None,None)
                )

                long_call_strike, long_call_symbol, long_call_quantity = next(
                    ((p['strike'], p['tradingsymbol'], p['buy_quantity']-p['sell_quantity']) for p in self.positions
                     if p['option_type'] == 'CE' and p['transtype'] == 'buy'),
                    (None, None,None)
                )

                try:
                    if self.config['close_on_trailprofit']:
                        success = False
                        AlertMobile().send(heading=f"{self.user}_TrailProfit",
                                           message=f"Closing the strikes for {self.strategy} because of trailprofit hit")
                        if self.strategy in ('SHORT_STRANGLE', 'IRON_CONDOR'):
                            success = self.place_order(symbol=short_call_symbol,
                                             quantity=short_call_quantity,
                                             transaction_type="BUY",
                                             exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                            if success:
                               success = self.place_order(symbol=short_put_symbol,
                                             quantity=short_put_quantity,
                                             transaction_type="BUY",
                                             exchange='NFO')
                               self.logger.info(success)
                               time.sleep(2)

                        if self.strategy == 'IRON_CONDOR' \
                            and self.config['adjust_hedges']\
                            and success:
                            success = self.place_order(symbol=long_call_symbol,
                                             quantity=long_call_quantity,
                                             transaction_type="SELL",
                                             exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                            if success:
                                success = self.place_order(symbol=long_put_symbol,
                                             quantity=long_put_quantity,
                                             transaction_type="SELL",
                                             exchange='NFO')
                                self.logger.info(success)
                        self.lock_profit = 0
                        self.trail_profit_hit_count = 0
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
            self.logger.info(f"Locking Profit: {self.lock_profit}")
        else:
            self.logger.info(
                f"Will lock when P&L: {pnl} >= {self.lock_profit + trail_profit}"
            )

        return False

    def get_next_symbol(self, current_symbol, current_strike, option_type, threshold_price):
        symbol = current_symbol
        strike = new_strike = current_strike
        symbol_price = 0
        while symbol_price <= (threshold_price * ((self.config['adjust_at']+10)/100)):
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
        long_put_strike, long_put_symbol, long_put_price, long_put_quantity = next(
            ((p['strike'], p['tradingsymbol'], p['last_price'], p['buy_quantity']-p['sell_quantity']) for p in self.positions
             if p['option_type'] == 'PE' and p['transtype'] == 'buy'),
            (None, None, None,None)
        )

        short_put_strike, short_put_symbol, short_put_price, short_put_quantity = next(
            ((p['strike'], p['tradingsymbol'], p['last_price'], p['sell_quantity']-p['buy_quantity']) for p in self.positions
             if p['option_type'] == 'PE' and p['transtype'] == 'sell'),
            (None, None, None, None)
        )

        short_call_strike, short_call_symbol, short_call_price, short_call_quantity = next(
            ((p['strike'], p['tradingsymbol'], p['last_price'], p['sell_quantity']-p['buy_quantity']) for p in self.positions
             if p['option_type'] == 'CE' and p['transtype'] == 'sell'),
            (None, None, None, None)
        )

        long_call_strike, long_call_symbol, long_call_price, long_call_quantity = next(
            ((p['strike'], p['tradingsymbol'], p['last_price'], p['buy_quantity']-p['sell_quantity']) for p in self.positions
             if p['option_type'] == 'CE' and p['transtype'] == 'buy'),
            (None, None, None, None)
        )

        if short_call_price  > 0 and short_put_price > 0:
            if short_call_price <= short_put_price * self.config['adjust_at']/100:
                self.logger.info(f"PE Price: {short_put_price}, CE Price: {short_call_price}. Market is going down. Adjustment is needed on CE side")
                self.logger.info(f"as {int(min((short_call_price / short_put_price) * 100, (short_put_price / short_call_price) * 100))} is < {self.config['adjust_at']} (mentioned in config)")
                if self.config['adjustment'] and not self.nearing_strike\
                        and datetime.today().weekday() in [int(n.strip()) for n in self.config['adjustment_days'].split(",")]:
                    new_short_symbol, diff = self.get_next_symbol(short_call_symbol, short_call_strike, 'CE',
                                                                  short_put_price)
                    success = False
                    AlertMobile().send(heading=f"{self.user}_Adjustment",
                                       message=f"Adjusting the strikes for {self.strategy}")
                    if self.strategy == 'SHORT_STRANGLE' or \
                      (self.strategy == 'IRON_CONDOR' and not self.config['adjust_hedges']):
                        success = self.place_order(symbol=short_call_symbol,
                                         quantity=short_call_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self.place_order(symbol=new_short_symbol,
                                         quantity=short_call_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                            self.logger.info(success)
                    if self.strategy == 'IRON_CONDOR' and \
                       self.config['adjust_hedges']:
                        success = self.place_order(symbol=short_call_symbol,
                                         quantity=short_call_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self.place_order(symbol=long_call_symbol,
                                         quantity=long_call_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                            if success:
                                success = self.place_order(symbol=long_call_symbol.replace(str(long_call_strike), str(int(long_call_strike)-diff)),
                                         quantity=long_call_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                                self.logger.info(success)
                                time.sleep(2)
                                if success:
                                    success = self.place_order(symbol=new_short_symbol,
                                         quantity=short_call_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                                    self.logger.info(success)
                #self.logger.info(f"Need to sell {long_call_symbol} & Need to buy {self.underlying}{int(long_call_strike)-50}CE")
            elif short_put_price <= short_call_price * self.config['adjust_at']/100:
                self.logger.info(f"PE Price: {short_put_price}, CE Price: {short_call_price}. Market is going up. Adjustment is needed on PE side")
                self.logger.info(f"as {int(min((short_call_price / short_put_price) * 100, (short_put_price / short_call_price) * 100))} is < {self.config['adjust_at']} (mentioned in config)")
                if self.config['adjustment'] and not self.nearing_strike:
                    new_short_symbol, diff = self.get_next_symbol(short_put_symbol, short_put_strike, 'PE',
                                                                  short_call_price)
                    success = False
                    if self.strategy == 'SHORT_STRANGLE' or \
                      (self.strategy == 'IRON_CONDOR' and not self.config['adjust_hedges']):
                        success = self.place_order(symbol=short_put_symbol,
                                         quantity=short_put_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self.place_order(symbol=new_short_symbol,
                                         quantity=short_put_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                            self.logger.info(success)
                    if self.strategy == 'IRON_CONDOR' \
                            and self.config['adjust_hedges']:
                        success = self.place_order(symbol=short_put_symbol,
                                         quantity=short_put_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self.place_order(symbol=long_put_symbol,
                                         quantity=long_put_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                            if success:
                                success = self.place_order(symbol=long_put_symbol.replace(str(long_put_strike), str(int(long_put_strike)+diff)),
                                         quantity=long_put_quantity,
                                         transaction_type="BUY",
                                         exchange='NFO')
                                self.logger.info(success)
                                time.sleep(2)
                                if success:
                                    success = self.place_order(symbol=new_short_symbol,
                                         quantity=short_put_quantity,
                                         transaction_type="SELL",
                                         exchange='NFO')
                                    self.logger.info(success)
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
        else:
            self.logger.info(f"{transaction_type} transaction for  {symbol} of {self.user} is failed")
        return response.json()[0]

    def run(self):
        try:
            self.logger.info("#" * 100)
            self.config = get_config(config_file)
            self.logger.info(f"Users in config: {self.config['users']}\nStarted to Watch user: {self.user}")
            if self.user in self.config["users"].split(","):
                if self.process_positions():
                    pass
                else:
                    time.sleep(self.config.get('delay'))
                    return False
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
                put_config(config_file, "last_watched", datetime.now().strftime("%Y-%m-%d %H:%M"))
        except Exception as e:
            self.logger.info(f"Failed in the run() module with error: {e}")
            time.sleep(self.config.get('delay'))
            return False
        time.sleep(self.config.get('delay'))
        return True
def run_user(user, handle_obj):
    """Worker function to run each user's trading logic."""
    try:
        return handle_obj.run()
    except Exception as e:
        print(f"Error in thread for {user}: {e}")

if __name__ == "__main__":
    print("Current directory:", os.getcwd())

    handle_objs = {}
    while True:
        with open(config_file, "r") as file:
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
