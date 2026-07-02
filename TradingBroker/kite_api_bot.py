import logging, time, traceback, pytz, yaml
from datetime import datetime
#
from pyotp import TOTP
from kiteconnect import KiteConnect
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
ist = pytz.timezone('Asia/Kolkata')

class KITE_CONNECT:
    def __init__(self, user, credentials):
        try:
            file_name = f"TradingBroker/{user}_connection_{datetime.now(ist).strftime('%Y%m%d')}.log"
            logging.basicConfig(filename=file_name,filemode="w", level=logging.INFO)
            self.logger = logging.getLogger(self.__class__.__name__)
            self.logger.info(f"logger created")
            self.credentials = credentials
            self.logger.info(f"Loaded credentials for user (api_key set: {bool(credentials.get('api_key'))})")
            self.kite = KiteConnect(api_key=credentials["api_key"])
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Login URL generated")
            request_token = self.__auto_login(self.kite.login_url())
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Request token received: {'yes' if request_token else 'no'}")
            data = self.kite.generate_session(request_token, api_secret=credentials["secret_key"])
            self.access_token = data.get("access_token") or data.get("sess_id")
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Session connected (access_token set: {bool(self.access_token)})")
            self.kite.set_access_token(self.access_token)
        except Exception as e:
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Login failed: {e}")
            self.access_token = None
    def __auto_login(self, login_url, with_head=False):
        self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Got into auto login")
        try:
            options = webdriver.ChromeOptions()
            #
            options.add_argument("window-size=1400,1500")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("start-maximized")
            options.add_argument("enable-automation")
            options.add_argument("--disable-infobars")
            options.add_argument("--disable-dev-shm-usage")
            if with_head is True:
                pass
            else:
                options.add_argument("--headless")
            #
            driver = webdriver.Chrome(options)
            driver.get(login_url)
            driver.implicitly_wait(10)
        except Exception as e:
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: {traceback.print_exc()}")
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Failed while connecting to Chrome: {e}")
            return None
        #
        username_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[@id='userid']")))
        password_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[@id='password']")))
        username_input.send_keys(self.credentials["username"])
        password_input.send_keys(self.credentials["password"])
        WebDriverWait(driver, 10).until(EC.presence_of_element_located(
            (By.XPATH, "//*[@id='container']/div/div/div[2]/form/div[4]/button"))).click()
        time.sleep(1)
        pin_input = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//input[@id='userid']")))
        totp = TOTP(self.credentials["authenticator_token"])
        request_token = None
        attempts = 1
        while not request_token and attempts <=3:
            attempts = attempts +1
            pin = totp.now()
            pin_input.send_keys(pin)
            time.sleep(1)
    #        WebDriverWait(driver, 10).until(EC.element_to_be_clickable(
    #            (By.XPATH, "//button[@type='submit']"))).click()
    #        time.sleep(1)
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: URL Received: {driver.current_url}")
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(driver.current_url)
            request_token = parse_qs(parsed.query).get('request_token', [None])[0]
            time.sleep(10)

        driver.quit()
        return request_token
    def get_connection(self):
        return self.kite
    def __split_strike(self, strike):
        #BANKNIFTY2461949800PE
    #    print(strike)
        if strike[:9] == 'BANKNIFTY':
            index_name = 'BANKNIFTY'
            expiry = (strike[9:15] if (int(strike[11:13]) <= 12) else strike[9:14]) if strike[11:13].isnumeric() else strike[9:14]
        elif strike[:5] == 'NIFTY':
            index_name = 'NIFTY'
            expiry = (strike[5:11] if (int(strike[7:9]) <= 12) else strike[5:10]) if strike[7:9].isnumeric() else strike[5:10]
        strike_price = strike[-7:-2]
        option_type = strike[-2:]
    #    print(index_name, expiry, strike_price, option_type)
        return index_name, expiry, strike_price, option_type
    def fetch_orders(self): # return list of dictionaries
        orders = None
        try:
            orders = self.kite.orders()
        except Exception as e:
            self.logger.error(f"Kite API (orders) failed with error: {e}")
        return (orders)

    def fetch_trades(self):
        """Fetch the day's executed trades from Kite (``kite.trades()``).

        Each trade dict carries ``tradingsymbol``, ``exchange``,
        ``transaction_type`` (``"BUY"`` / ``"SELL"``), ``quantity``
        (in shares — already lot_size-multiplied), ``average_price``
        (per share), ``trade_id`` / ``order_id``, and timestamps
        (``fill_timestamp``, ``exchange_timestamp``).

        Important: Kite returns trades for the **current trading day
        only**. For historical FIFO P&L across multiple days, callers
        must persist these locally — there is no official Kite
        back-office API for the trade ledger beyond today.

        Returns the raw list of trade dicts, or ``None`` on failure.
        """
        try:
            return self.kite.trades()
        except Exception as e:
            self.logger.error(f"Kite API (trades) failed with error: {e}")
            return None
    def fetch_option_chain(self):
        try:
            data = self.kite.instruments("NFO")
        except Exception as e:
            self.logger.error(f"Kite API (instruments) failed with error: {e}")
            return None
    def fetch_last_price(self, symbol, exchange="NFO"):
        ltp = None
        try:
            ltp = self.kite.ltp([f"{exchange}:{symbol}"])
        except Exception as e:
            self.logger.error(f"Kite API (ltp) failed with error: {e}")
        return ltp

    # Kite's /quote endpoint caps at 500 instruments per call. fetch_quotes
    # chunks transparently so callers don't have to think about it (the
    # recorder routinely asks for 600+ when covering 5 weekly expiries).
    _KITE_QUOTE_BATCH = 500

    def fetch_quotes(self, instruments):
        """Batch-fetch full quotes (LTP, OHLC, OI, depth, volume) for a list
        of instruments in ``EXCHANGE:SYMBOL`` form (e.g.
        ``["NFO:NIFTY25SEP24500PE", ...]``).

        Chunks the upstream ``kite.quote()`` call into batches of
        ``_KITE_QUOTE_BATCH`` instruments since Kite caps each call at 500;
        the returned dict is the union of all chunk responses, keyed by
        the same instrument strings the caller passed in.

        Returns None if any chunk fails (so the caller treats it as a hard
        miss rather than silently working with partial data).
        """
        if not instruments:
            return {}
        merged = {}
        for start in range(0, len(instruments), self._KITE_QUOTE_BATCH):
            chunk = instruments[start:start + self._KITE_QUOTE_BATCH]
            try:
                quotes = self.kite.quote(chunk)
            except Exception as e:
                self.logger.error(
                    f"Kite API (quote) failed for chunk "
                    f"{start}-{start + len(chunk)} of {len(instruments)}: {e}"
                )
                return None
            if quotes:
                merged.update(quotes)
        return merged
    def fetch_optoin_positions(self):
        positions_list = None
        try:
            positions_list = self.kite.positions()["net"]
        except Exception as e:
            self.logger.error(f"Kite API (positions) failed with error: {e}")
            return None
        return positions_list

    # P&L fields Kite returns on each net-positions row. Pulled out as a
    # constant so the fetcher below and any downstream aggregator stay in
    # lockstep.
    _KITE_PNL_FIELDS = ("pnl", "realised", "unrealised", "m2m")

    def fetch_pnl(self):
        """Fetch per-position P&L straight from Kite (``kite.positions()['net']``).

        Built to replace the running ``<week>.pnl`` buckets currently
        persisted in ``Monitor/config.yaml`` — those are an in-house
        approximation that re-derives realized P&L from broker
        ``closed_positions`` each cycle. This method returns the broker's
        authoritative P&L view instead, so downstream code (brain.py, the
        dashboard) can stop depending on the config buckets.

        Each row in ``positions`` carries the four P&L breakdowns Kite
        publishes::

            pnl        # total P&L since the position was opened (cumulative)
            realised   # realised portion (closed quantity x price delta)
            unrealised # mark-to-market on still-open quantity
            m2m        # intraday MTM change for the trading day

        plus identification fields (``tradingsymbol``, ``exchange``,
        ``product``, ``quantity``, ``average_price``, ``last_price``,
        ``value``) so callers can group by expiry / underlying without a
        second roundtrip.

        ``totals`` is the sum of each of the four P&L fields across all
        net positions — handy for a quick portfolio-level glance.

        Returns ``None`` on failure (so the caller treats it as a hard
        miss rather than silently working with stale data).
        """
        try:
            raw = self.kite.positions()
        except Exception as e:
            self.logger.error(f"Kite API (positions/pnl) failed with error: {e}")
            return None

        net = raw.get("net", []) if isinstance(raw, dict) else []
        positions = []
        totals = {k: 0.0 for k in self._KITE_PNL_FIELDS}
        for p in net:
            row = {
                "tradingsymbol": p.get("tradingsymbol"),
                "exchange": p.get("exchange"),
                "product": p.get("product"),
                "quantity": p.get("quantity"),
                "average_price": p.get("average_price"),
                "last_price": p.get("last_price"),
                "value": p.get("value"),
            }
            for k in self._KITE_PNL_FIELDS:
                # Kite sends None on a fresh-cycle position before m2m is
                # computed — coerce to 0 so the aggregate stays numeric.
                v = p.get(k) or 0
                row[k] = v
                totals[k] += v
            positions.append(row)
        return {"positions": positions, "totals": totals}
    def place_order(self, symbol,quantity=1,transaction_type="BUY",exchange="NSE",order_type="MARKET",price="1",variety="regular",product="NRML",validity="DAY", market_protection=2):
        try:
            order_id = self.kite.place_order(tradingsymbol=symbol,
                                             exchange=exchange,
                                             transaction_type=transaction_type,
                                             quantity=quantity,
                                             variety=variety,
                                             order_type=order_type,
                                             price=price,
                                             product=product,
                                             validity=validity,
                                             market_protection=market_protection)

            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Order placed. ID is: {order_id}")
            return order_id
        except Exception as e:
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: {traceback.print_exc()}")
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Order placement failed: {e}")
            return None
'''
if __name__ == "__main__":
    #
    with open("config.yaml", "r") as file:
        config = yaml.safe_load(file)
    #
    user="madhu"
    credentials = config["credentials"][user]
    kc = KITE_CONNECT(user, credentials)
    if not kc.access_token:
        exit(1)
    #
    logger = logging.getLogger()
    #
    logger.info("Kite Order book: ")
    logger.info(kc.fetch_orders())
    #
    logger.info("Option Chain: ")   # Needs Historical data previllages
    logger.info(kc.fetch_option_chain())
    #
    logger.info("Option Positions: ")
    logger.info(kc.fetch_optoin_positions())
    #
    logger.info("Infosys Last price: ")
    logger.info(kc.fetch_last_price("INFY", "NFO")) # Needs Historical data previllages
    #
'''
