from kiteconnect import KiteConnect
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
import matplotlib.pyplot as plt

# ---- Config ----
api_key = "your_api_key"
api_secret = "your_api_secret"
access_token = "your_access_token"   # refresh daily
kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

LOT_SIZE = 75
CAPITAL_PER_LOT = 150000
STRIKE_PCT = 0.03   # 3% OTM
OPEN_TIME = "09:20:00"
CLOSE_TIME = "15:25:00"

# ---- Helper to get instrument token ----
def get_instrument_token(symbol, expiry, strike, option_type):
    insts = kite.instruments("NFO")   # list of all NSE F&O instruments
    expiry_str = expiry.strftime("%Y-%m-%d")
    for inst in insts:
        if (inst["name"] == "NIFTY" and
            inst["expiry"].strftime("%Y-%m-%d") == expiry_str and
            inst["strike"] == strike and
            inst["instrument_type"] == option_type):
            return inst["instrument_token"]
    return None

# ---- Get open/close price from Kite historical data ----
def get_open_close(token, date):
    from_date = to_date = date.strftime("%Y-%m-%d")
    data = kite.historical_data(token, from_date, to_date, "5minute")  # 5-min candles
    df = pd.DataFrame(data)
    if df.empty:
        return None, None
    df["date"] = pd.to_datetime(df["date"])
    open_row = df[df["date"].dt.strftime("%H:%M:%S") == OPEN_TIME]
    close_row = df[df["date"].dt.strftime("%H:%M:%S") == CLOSE_TIME]
    if open_row.empty or close_row.empty:
        return None, None
    return float(open_row["close"].iloc[0]), float(close_row["close"].iloc[0])

# ---- Run backtest ----
def run_backtest(start_date, end_date):
    results = []
    current = start_date
    while current <= end_date:
        try:
            # Get spot Nifty
            spot = kite.quote("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]
            # Find nearest monthly expiry
            insts = kite.instruments("NFO")
            exp_dates = sorted(list({i["expiry"] for i in insts if i["name"]=="NIFTY"}))
            expiry = [e for e in exp_dates if e > current][0]

            # Strikes
            ce_strike = round(spot * (1 + STRIKE_PCT) / 50) * 50
            pe_strike = round(spot * (1 - STRIKE_PCT) / 50) * 50

            # Tokens
            ce_token = get_instrument_token("NIFTY", expiry, ce_strike, "CE")
            pe_token = get_instrument_token("NIFTY", expiry, pe_strike, "PE")
            if not ce_token or not pe_token:
                current += timedelta(days=1)
                continue

            # Prices
            ce_open, ce_close = get_open_close(ce_token, current)
            pe_open, pe_close = get_open_close(pe_token, current)
            if ce_open is None or pe_open is None:
                current += timedelta(days=1)
                continue

            # P&L per leg
            pl_ce = ce_open - ce_close
            pl_pe = pe_open - pe_close
            pl_total = (pl_ce + pl_pe) * LOT_SIZE

            results.append({
                "date": current,
                "spot": spot,
                "ce_strike": ce_strike,
                "pe_strike": pe_strike,
                "pl": pl_total
            })
        except Exception as e:
            print(f"Error on {current}: {e}")
        current += timedelta(days=1)

    return pd.DataFrame(results)

# ---- Example usage ----
start = datetime(2024, 1, 1)
end   = datetime(2024, 12, 31)
from kite_api_bot import KITE_CONNECT

df = run_backtest(start, end)

print(df.describe())
df["cum_pl"] = df["pl"].cumsum()
df.set_index("date")["cum_pl"].plot(title="Equity Curve", figsize=(10,5))
plt.show()
