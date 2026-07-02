from fetch_data import get_ltp
import pandas as pd
import numpy as np
from scipy.stats import norm
from datetime import datetime

# ---------- Parse NSE option chain ----------
def parse_option_chain(option_chain_json):
    ce_list = []
    pe_list = []
    records = option_chain_json['records']['data']

    for r in records:
        strike = r['strikePrice']
        ce = r.get('CE')
        pe = r.get('PE')
        expiry_date = r['expiryDate']

        if ce:
            ce_list.append({
                'strikePrice': strike,
                'lastPrice': ce['lastPrice'],
                'impliedVolatility': ce['impliedVolatility'],
                'expiryDate': expiry_date
            })
        if pe:
            pe_list.append({
                'strikePrice': strike,
                'lastPrice': pe['lastPrice'],
                'impliedVolatility': pe['impliedVolatility'],
                'expiryDate': expiry_date
            })

    ce_df = pd.DataFrame(ce_list)
    pe_df = pd.DataFrame(pe_list)
    return ce_df, pe_df

# ---------- Probability OTM calculation ----------
def probability_otm(spot, strike, time_to_expiry_years, iv, option_type='call'):
    if iv == 0:
        return 1.0 if (option_type == 'call' and strike > spot) or (option_type == 'put' and strike < spot) else 0.0

    d2 = (np.log(spot / strike) - 0.5 * iv ** 2 * time_to_expiry_years) / (iv * np.sqrt(time_to_expiry_years))
    if option_type == 'call':
        return norm.cdf(-d2)
    else:
        return norm.cdf(d2)

# ---------- Conservative Strangle Selection with Buffer, Expiry, and IV ----------
def select_prob_strangle(
    spot, ce_df, pe_df, target_prob=0.85, buffer_percent=5, expiry_date_str=None, iv_threshold=0
):
    """
    Select conservative strangle strikes with the following filters:
    - target probability of staying OTM
    - minimum buffer distance from spot
    - required expiry
    - minimum implied volatility (iv_threshold)
    Picks strike with the highest IV among candidates
    """
    buffer_points = spot * buffer_percent / 100

    # Expiry
    if expiry_date_str is None:
        expiry_str = ce_df['expiryDate'].iloc[0]
    else:
        expiry_str = expiry_date_str

    expiry_date = datetime.strptime(expiry_str, '%d-%b-%Y')
    today = datetime.today()
    t = max((expiry_date - today).days / 365, 0.01)

    # Filter CE and PE for expiry
    ce_df = ce_df[ce_df['expiryDate'] == expiry_str].copy()
    pe_df = pe_df[pe_df['expiryDate'] == expiry_str].copy()

    # CE probability + buffer + IV filter
    ce_df['prob_otm'] = ce_df.apply(
        lambda row: probability_otm(spot, row['strikePrice'], t, row['impliedVolatility'] / 100, 'call'), axis=1
    )
    ce_candidates = ce_df[
        (ce_df['prob_otm'] >= target_prob) &
        (ce_df['strikePrice'] >= spot + buffer_points) &
        (ce_df['impliedVolatility'] >= iv_threshold)
    ]
    # CE
    if not ce_candidates.empty:
        ce_strike_row = ce_candidates.sort_values(by='impliedVolatility', ascending=True).iloc[0]
        print(ce_candidates.sort_values(by='impliedVolatility', ascending=True))
    else:
        print("Could not match all the 3 criteria for PE strike...Trying with prob & distance only ")
        ce_candidates = ce_df[
            (ce_df['prob_otm'] >= target_prob) &
            (ce_df['strikePrice'] >= spot + buffer_points)
            ]
        if not ce_candidates.empty:
            ce_strike_row = ce_candidates.sort_values(by='prob_otm', ascending=True).iloc[0]
            print(ce_candidates.sort_values(by='prob_otm', ascending=True))
        else:
            print("Could not match all the 3 criteria for CE strike...Trying with distance only ")
            ce_candidates = ce_df[
                (ce_df['strikePrice'] >= spot + buffer_points)
                ]
            if not ce_candidates.empty:
                print(ce_candidates.sort_values(by='strikePrice', ascending=False))
                ce_strike_row = ce_candidates.sort_values(by='strikePrice', ascending=False)
            else:
                print(ce_df.sort_values(by='strikePrice', ascending=False))
                ce_strike_row = ce_df.sort_values(by='strikePrice', ascending=False)

    # PE probability + buffer + IV filter
    pe_df['prob_otm'] = pe_df.apply(
        lambda row: probability_otm(spot, row['strikePrice'], t, row['impliedVolatility'] / 100, 'put'), axis=1
    )
    pe_candidates = pe_df[
        (pe_df['prob_otm'] >= target_prob) &
        (pe_df['strikePrice'] <= spot - buffer_points) &
        (pe_df['impliedVolatility'] >= iv_threshold)
    ]
    # PE
    if not pe_candidates.empty:
        pe_strike_row = pe_candidates.sort_values(by='impliedVolatility', ascending=True).iloc[0]
        print(pe_candidates.sort_values(by='impliedVolatility', ascending=True))
    else:
        print("Could not match all the 3 criteria for PE strike...Trying with prob & distance only ")
        pe_candidates = pe_df[
            (pe_df['prob_otm'] >= target_prob) &
            (pe_df['strikePrice'] <= spot - buffer_points)
            ]
        if not pe_candidates.empty:
            pe_strike_row = pe_candidates.sort_values(by='prob_otm', ascending=True).iloc[0]
            print(pe_candidates.sort_values(by='prob_otm', ascending=True))
        else:
            print("Could not match all the 3 criteria for PE strike...Trying with distance only ")
            pe_candidates = pe_df[
                (pe_df['strikePrice'] <= spot - buffer_points)
                ]
            if not pe_candidates.empty:
                print(pe_candidates.sort_values(by='strikePrice', ascending=False))
                pe_strike_row = pe_candidates.sort_values(by='strikePrice', ascending=False)
            else:
                print(pe_df.sort_values(by='strikePrice', ascending=False))
                pe_strike_row = pe_df.sort_values(by='strikePrice', ascending=False)

    return pe_strike_row, ce_strike_row

# ------------------------- Example Usage -------------------------
nifty_spot = 24000
required_expiry = "30-Jun-2026"
option_chain = get_ltp(symbol='NIFTY', expiry=required_expiry, request_type='option')

iv_min = 10  # minimum IV threshold
probability_of_success = 0.75
distance_perc_from_index = 2


if option_chain:
    ce_df, pe_df = parse_option_chain(option_chain)
    pe_strike_row, ce_strike_row = select_prob_strangle(
        nifty_spot, ce_df, pe_df, target_prob=probability_of_success, buffer_percent=distance_perc_from_index,
        expiry_date_str=required_expiry, iv_threshold=iv_min
    )

    print("----- Selected Conservative Strangle -----\n")
    if pe_strike_row is not None:
        print(f"PE Strike: {pe_strike_row['strikePrice']}")
        print(f"  Last Price: {pe_strike_row['lastPrice']}")
        print(f"  IV: {pe_strike_row['impliedVolatility']}")
        print(f"  Probability OTM: {pe_strike_row['prob_otm']}")
        print(f"  Expiry: {pe_strike_row['expiryDate']}\n")
    else:
        print("No PE strike available for the given filters")

    if ce_strike_row is not None:
        print(f"CE Strike: {ce_strike_row['strikePrice']}")
        print(f"  Last Price: {ce_strike_row['lastPrice']}")
        print(f"  IV: {ce_strike_row['impliedVolatility']}")
        print(f"  Probability OTM: {ce_strike_row['prob_otm']}")
        print(f"  Expiry: {ce_strike_row['expiryDate']}")
    else:
        print("No CE strike available for the given filters")