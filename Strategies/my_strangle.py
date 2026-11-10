import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import pytz
ist = pytz.timezone('Asia/Kolkata')
from nse_data import OptionChain, live_market_data
from kite_api_bot import KITE_CONNECT
import logging

file_name = "trading_" + datetime.now(ist).strftime("%Y%m%d") + ".log"
logging.basicConfig(filename=file_name,
                    filemode="a", level=logging.INFO)
logger = logging.getLogger()

index_df = pd.DataFrame(
            {'Time': datetime.now(ist),
             'NIFTY': 0,
             'BANKNIFTY': 0,
             'PE_Strike': None,
             'PE_Premium_Entered': 0.0,
             'PE_Premium_Current': 0.0,
             'CE_Strike': None,
             'CE_Premium_Entered': 0.0,
             'CE_Premium_Current': 0.0,
             'Profit': 0}
            , index=[0])

def get_next_thursday(start_date=None):
    if not start_date:
        start_date = datetime.today()
    days_ahead = 3 - start_date.weekday()  # 3 = Thursday (Mon=0, ..., Sun=6)
    if days_ahead <= 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)

def get_strike_price(index_name, expiry_date, min_premium):
    oc = OptionChain(symbol=index_name)
    options = oc.fetch_data(expiry_date=expiry_date, starting_strike_price=None, number_of_rows=500)
    if options is None:
        return 0, 0
    options = options.loc[((options['PE.bidprice'] >= min_premium) & (options['PE.bidprice'] < min_premium + 10)) |
                          ((options['CE.bidprice'] >= min_premium) & (options['CE.bidprice'] < min_premium + 10))]
    options = options[["CE.strikePrice", "CE.bidprice", "PE.strikePrice", "PE.bidprice"]]
    PE_strikeprice_found = CE_strikeprice_found = False
    PE_strikeprice = CE_strikeprice = PE_bidprice = CE_bidprice = 0
    for i in range(0, len(options)):
        if options.iloc[i]['CE.bidprice'] >= min_premium and options.iloc[i][
            'CE.bidprice'] <= min_premium + 10 and not CE_strikeprice_found:
            CE_strikeprice = options.iloc[i]['CE.strikePrice']
            CE_bidprice = options.iloc[i]['CE.bidprice']
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: CE Strike price: {CE_strikeprice}, Premium: {CE_bidprice}")
            CE_strikeprice_found = True
        elif options.iloc[i]['PE.bidprice'] >= min_premium and options.iloc[i][
            'PE.bidprice'] <= min_premium + 10 and not PE_strikeprice_found:
            PE_strikeprice = options.iloc[i]['PE.strikePrice']
            PE_bidprice = options.iloc[i]['PE.bidprice']
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: PE Strike price: {PE_strikeprice}, Premium: {PE_bidprice}")
            PE_strikeprice_found = True
    return PE_strikeprice, CE_strikeprice

def generate_instrument_name(index_name, expiry_date, expiry_type, strikeprice, option_type):
    expiry_date_dt = datetime.strptime(expiry_date, '%d-%b-%Y')
    expiry_month_num = int(expiry_date_dt.strftime("%m"))
    expiry_month_char = expiry_date_dt.strftime("%b").upper()
    expiry_year = expiry_date_dt.strftime("%y")
    if expiry_type.upper() == 'MONTHLY':
        expiry_day = ''
        expiry_month = expiry_month_char
    else:
        expiry_day = expiry_date_dt.strftime("%d")
        expiry_month = expiry_month_num
    #
    symbol = "{}{}{}{}{}{}".format(index_name, expiry_year, expiry_month, expiry_day, int(strikeprice), option_type)
    return symbol

def strangle_orders(kc, PE_symbol, CE_symbol, no_of_lots, trans_type):
    if CE_symbol.startswith("NIFTY"):
        quantity = no_of_lots * NIFTY_Quantity
    else:
        quantity = no_of_lots * BANKNIFTY_Quantity
    kc.place_order(PE_symbol,
                   exchange="NFO",
                   transaction_type=trans_type,
                   order_type="MARKET",
                   quantity=quantity,
                   variety="regular",
                   product="NRML",
                   validity="DAY")
    kc.place_order(CE_symbol,
                   exchange="NFO",
                   transaction_type=trans_type,
                   order_type="MARKET",
                   quantity=quantity,
                   variety="regular",
                   product="NRML",
                   validity="DAY")

def get_indices():
    try:
        # Step4.1: Read current index prices
        live_data = live_market_data()
        return (live_data.get_quote('^NSEI'),
                live_data.get_quote('^NSEBANK'))
    except Exception as e:
        return 0, 0

def get_total_profits(option_positions, NIFTY_Quantity, BANKNIFTY_Quantity):
    df = pd.DataFrame(option_positions)
    logger.info(f"\n{df}")
    total_profit = PE_Price = CE_Price = 0
    PE_Symbol = CE_Symbol = None
    for position in option_positions:
        total_profit = total_profit + position["gain"]
        if position["index_name"] == "NIFTY":
            lots = position["quantity"]/NIFTY_Quantity
        else:
            lots = position["quantity"] / BANKNIFTY_Quantity
        if position["option_type"] == "PUT":
            PE_Symbol = position["symbol"]
            PE_Price = position["last_price"]
        elif position["option_type"] == "CALL":
            CE_Symbol = position["symbol"]
            CE_Price = position["last_price"]
    return total_profit, lots, PE_Symbol, CE_Symbol, PE_Price, CE_Price

if __name__ == '__main__':
    import yaml

    with open("../config.yaml", "r") as file:
        config = yaml.safe_load(file)
    # Initialize the Parameters
    expiry_date = config['expiry_date']
    expiry_type = config['expiry_type']
    index_name = config['index_name']
    premium_dict = config['minimum_premium']
    no_of_lots = config['no_of_lots']
    start_time = config['start_time']
    end_time = config['end_time']
    max_loss_per_lot = config['max_loss_per_lot']
    break_time = config['break_time']
    max_loss_wait_count = config['max_loss_wait_count'] #times approximately mins
    NIFTY_Quantity = config['NIFTY_Quantity']
    BANKNIFTY_Quantity = config['BANKNIFTY_Quantity']
    #
    max_threshold = -1 * int(no_of_lots) * int(max_loss_per_lot)
    day_of_week = datetime.now(ist).strftime("%A")
    if day_of_week in ('Wednesday','Thursday'):
        volatile = True
    else:
        volatile = False
    minimum_premium = premium_dict[day_of_week]
    if not expiry_date or len(expiry_date) < 10:
        expiry_date = get_next_thursday(datetime.today()) # + timedelta(days=1))
        expiry_date = expiry_date.strftime("%d-%b-%Y")
    logger.info(f"Expiry Date selected as : {expiry_date}")
    #
    max_threshold_hit_count = 0
    logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Today is {day_of_week}. \
    Proceeding to look for strike price with premium below for {minimum_premium}")
    #
    # Step1: Initiate a connection with KITE to place the orders
    #
    kc = KITE_CONNECT(config["credentials"]["sashi"])
    if kc.access_token is None:
        logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Failed to login to Kite. Please retry after sometime.")
        exit(1)
    #
    # Step3: Read the positions in KITE
    #
    stay_on = True
    lc = live_market_data()
    header_not_written=True
    while stay_on:
        if datetime.now(ist).strftime('%H:%M:%S') < start_time:
            now_ist = datetime.now(ist)
            string_dt = datetime.strptime(start_time, "%H:%M:%S")
            string_dt = ist.localize(datetime.combine(now_ist.date(), string_dt.time()))
            diff_seconds = (string_dt-datetime.now(ist)).total_seconds()

            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Waiting for {diff_seconds/60} minutes as the market is not yet opened.")
            time.sleep(diff_seconds)
            continue
        #
        if datetime.now(ist).strftime('%H:%M:%S') >= "15:30:00":
            stay_on = False
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Exiting the program as the markets are closed.")
            continue
        #
        option_positions = kc.fetch_optoin_positions()
        open_positions = [x for x in option_positions if x["status"] == "Open"]
        if len(open_positions) > 0:
            total_profit, options_lots,PE_Symbol, CE_Symbol, PE_Price, CE_Price = get_total_profits(open_positions, NIFTY_Quantity, BANKNIFTY_Quantity)
            with open(f"stats_{datetime.now().strftime('%Y%m%d')}.txt", "a") as file:
                if header_not_written:
                    file.write(
                        f"DateTime, NIFTY50, BANKNIFTY, INDIAVIX, {PE_Symbol}, {CE_Symbol}")
                    header_not_written = False
                file.write(
                    f"\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, {lc.get_quote('^NSEI')}, {lc.get_quote('^NSEBANK')}, {lc.get_quote('^INDIAVIX')}, {PE_Price}, {CE_Price}")
            #
            if no_of_lots != options_lots:
                no_of_lots = options_lots
                max_threshold = -1 * int(no_of_lots) * int(max_loss_per_lot)
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Overall gain: {total_profit}")
            #logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Locked the profit at: {max_threshold}")
            #opt_strategies(open_positions).check_strangle()
            # Check if the max loss is hit
            if total_profit > max_threshold:
                max_threshold_hit_count = 0
            else:
                max_threshold_hit_count = max_threshold_hit_count + 1
                if max_threshold_hit_count > max_loss_wait_count and not volatile:
                    # Close positions if needed
                    logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Closing the orders with profit/loss {total_profit} at  & max_threshold_hit_count: {max_threshold_hit_count}")
                    for position in open_positions:
                        if position["status"] == "Open" and position["trans_type"] == "SHORT":
#                            kc.place_order(position["symbol"],
#                                   exchange="NFO",
#                                   transaction_type="BUY",
#                                   order_type="MARKET",
#                                  quantity=position['quantity'],
#                                   variety="regular",
#                                   product="NRML",
#                                   validity="DAY")
                            pass
#                    stay_on = False
            # Check if the safe time crossed
            if datetime.now(ist).strftime("%H:%M:%S") >= end_time and stay_on:
                # Close positions if needed
                logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Closing the orders as the time hit ...")
                for position in open_positions:
                    if position["status"] == "Open" and position["trans_type"] == "SHORT":
#                        kc.place_order(position["symbol"],
#                                       exchange="NFO",
#                                       transaction_type="BUY",
#                                       order_type="MARKET",
#                                       quantity=position['quantity'],
#                                       variety="regular",
#                                       product="NRML",
#                                       validity="DAY")
                        pass
#                    elif position["status"] == "Open" and position["trans_type"] == "LONG":
#                        kc.place_order(position["symbol"],
#                                       exchange="NFO",
#                                       transaction_type="SELL",
#                                       order_type="MARKET",
#                                       quantity=position['quantity'],
#                                       variety="regular",
#                                       product="NRML",
#                                       validity="DAY")
                    pass
#                stay_on = False
            #Check & lock the profit
            if stay_on:
                if (total_profit/(no_of_lots * max_loss_per_lot) >= 2
                        and max_threshold < (no_of_lots * max_loss_per_lot) * round(((total_profit/(no_of_lots * max_loss_per_lot)) - 1), 0)):
                    max_threshold = (no_of_lots * max_loss_per_lot) * round(((total_profit/(no_of_lots * max_loss_per_lot)) - 1), 0)
                elif (no_of_lots * max_loss_per_lot) < total_profit < (no_of_lots * max_loss_per_lot) * 2:
                    max_threshold = 0

                logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Locked the profit at: {max_threshold}")
        #if no Options Sold yet
        elif datetime.now(ist).strftime('%H:%M:%S') >= start_time and not volatile:
            # Get the Strike prices for PE and CE as on current time
            #
            PE_symbol = CE_symbol = PE_strikeprice = CE_strikeprice = None
            while not (PE_symbol and CE_symbol and PE_strikeprice and CE_strikeprice):
                PE_symbol, PE_strikeprice, CE_symbol, CE_strikeprice = kc.fetch_option_chain(index_name, expiry_date,
                                                                                             minimum_premium)
                if not (PE_symbol and CE_symbol and PE_strikeprice and CE_strikeprice):
                    logger.info(
                        f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Failed to fetch the options data. Retrying in 10 seconds")
                    time.sleep(10)
            logger.info(
                f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Found the strikes to place order: {PE_symbol}-{PE_strikeprice}-{CE_symbol}-{CE_strikeprice}")
            #
            #Place order for strangle
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Placing the orders: {PE_symbol} & {CE_symbol} with {no_of_lots} lots each")
            strangle_orders(kc, PE_symbol, CE_symbol, no_of_lots, "SELL")
        else:
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: No Options are found & Orders are not placed.")
            with open(f"stats_{datetime.now().strftime('%Y%m%d')}.txt", "a") as file:
                if header_not_written:
                    file.write(
                        f"DateTime, NIFTY50, BANKNIFTY, INDIAVIX, PE_Symbol, CE_Symbol")
                    header_not_written = False
                file.write(
                    f"\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, {lc.get_quote('^NSEI')}, {lc.get_quote('^NSEBANK')}, {lc.get_quote('^INDIAVIX')}, 0, 0")
        #
        if stay_on:
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Waiting for {break_time}secs")
            time.sleep(break_time)
