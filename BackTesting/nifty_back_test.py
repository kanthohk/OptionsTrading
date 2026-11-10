import pandas as pd
import numpy as np
import numpy_financial as npf
from collections import deque

# Load your file
file_path = "ETF BackTesting - Nifty50.csv"  # update path if needed
df = pd.read_csv(file_path)

# Ensure Date column is datetime
df['Date'] = pd.to_datetime(df['Date'])
df = df.sort_values("Date").reset_index(drop=True)

# Strategy parameters
initial_investment = 1000000  # 10 Lakhs INR initial investment
invest_amount = 100000  # subsequent investments
withdraw_amount = 100000
threshold_invest = -1  # -5% fall triggers investment
threshold_withdraw = 12  # +5% rise triggers withdrawal
max_investment = 0
min_investment = initial_investment
new_threshold_withdraw = threshold_withdraw

# Minimum gaps (in days) between actions
min_invest_gap_days = 30
min_withdraw_gap_days = 30

# Initialize
cashflows = []
units = 0
peak_value = 0
drawdown = 0
cumulative_invested = 0
cumulative_gain = 0
total_withdrawal=0
total_invested=0

# FIFO lot ledger: each entry is [units, price]
lot_ledger = deque()

# Track portfolio value and date at last action for threshold calculation
last_action_value = None
last_invest_date = None
last_withdraw_date = None

for i, row in df.iterrows():
    price = row['Close']
    date = row['Date']

    market_value = units * price

    if last_action_value is None:
        # First investment using initial_investment
        bought_units = initial_investment / price
        units += bought_units
        lot_ledger.append([bought_units, price])
        market_value = units * price
        cumulative_invested += initial_investment
        cumulative_gain = market_value - cumulative_invested
        gain_percent = cumulative_gain / cumulative_invested * 100
        cashflows.append((date, initial_investment, price, "Initial Invest", round(market_value,0), round(units,2), initial_investment,
                          market_value, cumulative_gain, gain_percent))
        last_action_value = market_value
        last_invest_date = date
        peak_value = last_action_value
        continue

    # % change from previous transaction
    change = (market_value - last_action_value) / last_action_value * 100
    cumulative_gain = market_value - cumulative_invested
    gain_percent = cumulative_gain / cumulative_invested * 100

    # Invest if portfolio down by threshold and min gap passed
    invest_allowed = last_invest_date is None or (date - last_invest_date).days >= min_invest_gap_days
    if (change <= threshold_invest or change >= -threshold_invest) and invest_allowed:
        bought_units = invest_amount / price
        units += bought_units
        lot_ledger.append([bought_units, price])
        market_value = units * price
        cumulative_invested += invest_amount
        cumulative_gain = market_value - cumulative_invested
        prev_gain_percent = gain_percent
        gain_percent = cumulative_gain / cumulative_invested * 100
        total_invested += invest_amount
        cashflows.append((date, invest_amount, price, "Invest", round(market_value,0), units, cumulative_invested, round(market_value,0),
                          round(cumulative_gain,0), round(prev_gain_percent), round(gain_percent,0)))
        last_action_value = market_value
        last_invest_date = date
        peak_value = max(peak_value, market_value)
        continue

    # Withdraw if portfolio up by threshold and min gap passed
    withdraw_allowed = last_withdraw_date is None or (date - last_withdraw_date).days >= min_withdraw_gap_days
    if gain_percent >= new_threshold_withdraw and units > (withdraw_amount / price) and withdraw_allowed:
        new_threshold_withdraw += threshold_withdraw
        print(f"{date},{cumulative_invested},{market_value}, {gain_percent}, {new_threshold_withdraw}")
        remaining_withdraw_units = withdraw_amount / price
        withdrawn_amount = 0

        # FIFO withdrawal
        while remaining_withdraw_units > 0 and lot_ledger:
            lot_units, lot_price = lot_ledger[0]
            if lot_units <= remaining_withdraw_units:
                withdrawn_amount += lot_units * price
                remaining_withdraw_units -= lot_units
                units -= lot_units
                lot_ledger.popleft()
            else:
                withdrawn_amount += remaining_withdraw_units * price
                lot_ledger[0][0] -= remaining_withdraw_units
                units -= remaining_withdraw_units
                remaining_withdraw_units = 0

        market_value = units * price
        cumulative_gain = market_value - cumulative_invested
        prev_gain_percent = gain_percent
        gain_percent = cumulative_gain / cumulative_invested * 100
        total_withdrawal += withdrawn_amount
        cashflows.append((date, -withdrawn_amount, price, "Withdraw", round(market_value,0), round(units,2), cumulative_invested,
                          round(market_value,0), round(cumulative_gain,0), round(prev_gain_percent), round(gain_percent,0)))
        last_action_value = market_value
        last_withdraw_date = date
        peak_value = max(peak_value, market_value)
        continue

    # Update drawdown tracking
    max_investment = max(max_investment, cumulative_invested)
    min_investment = min(min_investment, cumulative_invested)
    peak_value = max(peak_value, market_value)
    dd = (peak_value - market_value) / peak_value if peak_value > 0 else 0
    drawdown = max(drawdown, dd)

# Final redemption
final_value = units * df.iloc[-1]['Close']
cumulative_gain = final_value - cumulative_invested
gain_percent = cumulative_gain / cumulative_invested * 100
cashflows.append((df.iloc[-1]['Date'], round(final_value,0), df.iloc[-1]['Close'], "Final Redemption", round(final_value,0), units,
                  round(cumulative_invested,0), round(final_value,0), round(cumulative_gain,0), round(prev_gain_percent,0), round(gain_percent,0)))

# Convert to DataFrame
cashflow_df = pd.DataFrame(cashflows, columns=[
    "Date", "Cashflow", "Price", "Type", "MarketValue", "Units",
    "InvestmentAmount", "MarketValueAtAction", "CumulativeGain", "PrevGainPercent", "GainPercent"
])


# --- Metrics ---
from scipy.optimize import newton,brentq

def xnpv(rate, cashflows):
    t0 = cashflows[0][0]
    return sum(
        cf / (1 + rate) ** ((d - t0).days / 365.0)
        for d, cf in cashflows
    )

def xirr(cashflows, guess=0.1):
    try:
        # first try Newton
        return newton(lambda r: xnpv(r, cashflows), guess)
    except RuntimeError:
        # if Newton fails, expand bounds dynamically for brentq
        a, b = -0.9999, 10
        fa, fb = xnpv(a, cashflows), xnpv(b, cashflows)

        # expand upper bound until sign changes or limit
        while fa * fb > 0 and b < 1e6:
            b *= 2
            fb = xnpv(b, cashflows)

        if fa * fb > 0:
            raise ValueError("No IRR found: NPV does not cross zero.")

        return brentq(lambda r: xnpv(r, cashflows), a, b)


# Example usage
cashflow_tuples_clean = [
    (pd.Timestamp(t[0]).to_pydatetime(), -1 * round(float(t[1]), 2))
    for t in cashflows[:-1]]
irr_value = 0 #xirr(cashflow_tuples_clean)


# Average gap between investments and withdrawals
investment_dates = cashflow_df[cashflow_df["Type"].isin(["Invest", "Initial Invest"])]["Date"].tolist()
withdrawal_dates = cashflow_df[cashflow_df["Type"] == "Withdraw"]["Date"].tolist()

avg_invest_gap = np.mean(np.diff(investment_dates).astype('timedelta64[D]').astype(int)) if len(
    investment_dates) > 1 else None
avg_withdraw_gap = np.mean(np.diff(withdrawal_dates).astype('timedelta64[D]').astype(int)) if len(
    withdrawal_dates) > 1 else None

# Print results
print("===== Strategy Results =====")
print(f"XIRR: {irr_value * 100:.2f}%")
print(f"Maximum Drawdown: {drawdown * 100:.2f}%")
print(f"Average Investment Frequency: {avg_invest_gap} days")
print(f"Average Withdrawal Frequency: {avg_withdraw_gap} days")
print(f"Total Invested: {initial_investment+total_invested}")
print(f"Total Withdrawn: {total_withdrawal}")
print(f"Final Portfolio value: {final_value}")
print(f"Absolute Gain: {round(final_value+total_withdrawal-initial_investment-total_invested,0)}")
print(f"Minimum Investment: {min_investment}")
print(f"Maximum Investment: {max_investment}")
print(f"Absolute Gain %: {round((final_value+total_withdrawal-initial_investment-total_invested)/max_investment*100,0)}")
print("\n===== Cashflow Log =====")

cashflow_df.to_csv("cashflow.csv", index=False)
