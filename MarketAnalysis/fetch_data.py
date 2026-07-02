"""Standalone option-chain fetcher backed by the broker (kite.quote).

Replaces the previous NSE-website scraping (which 404'd during market hours)
with calls to the Flask proxy at ``url`` from ``Monitor/config.yaml``. The
proxy forwards to ``kite.quote()`` via the ``/get_quotes`` endpoint, batching
all strikes into a single round-trip.

The downstream consumer (``strike_selection.py``) expects an NSE-shaped
option-chain dict, so this module rebuilds the same structure from the Kite
response. Kite does not return implied volatility — IV is computed locally
via Black-Scholes (Brent's method on the option premium) so the consumer's
IV filters keep working unchanged.

Monthly expiries only (e.g. ``25SEP`` symbol format). Weekly expiries use a
different symbol format (e.g. ``25923`` for 23-Sep-2025) and are TBD.
"""
from datetime import datetime
import pytz

from helpers.market_data import proxy_get, kite_expiry_code, implied_vol

ist = pytz.timezone('Asia/Kolkata')

# Index symbol → NSE display symbol used by Kite for the spot quote
INDEX_NSE_MAP = {
    'NIFTY': 'NIFTY 50',
    'BANKNIFTY': 'NIFTY BANK',
    'FINNIFTY': 'NIFTY FIN SERVICE',
    'SENSEX': 'SENSEX',
}

# Strike intervals per underlying
STRIKE_STEP = {
    'NIFTY': 50,
    'BANKNIFTY': 100,
    'FINNIFTY': 50,
    'SENSEX': 100,
}

STRIKES_AROUND_ATM = 30  # ATM ± 30 strikes covers a wide enough chain


def _get_spot(underlying):
    """Spot price for an index via /get_current_price."""
    nse_symbol = INDEX_NSE_MAP.get(underlying, underlying)
    payload = proxy_get(f"/get_current_price/{nse_symbol}/stock")
    if not payload:
        return None
    return payload.get(f"NSE:{nse_symbol}", {}).get('last_price')


def _iv_pct(spot, strike, t_years, price, opt_type):
    """IV as a percentage (NSE shape) instead of the decimal returned by
    helpers.market_data.implied_vol."""
    return implied_vol(spot, strike, t_years, price, opt_type) * 100


def _build_chain_payload(underlying, expiry_dt):
    """Enumerate strikes around ATM, batch-fetch quotes, and emit an
    NSE-shaped option chain. Returns ``None`` on broker failure."""
    spot = _get_spot(underlying)
    if not spot:
        print(f"Failed to get spot price for {underlying}")
        return None

    step = STRIKE_STEP.get(underlying, 50)
    atm = round(spot / step) * step
    expiry_code = kite_expiry_code(expiry_dt, monthly=True)

    instruments = []
    for i in range(-STRIKES_AROUND_ATM, STRIKES_AROUND_ATM + 1):
        strike = atm + i * step
        if strike <= 0:
            continue
        instruments.append(f"NFO:{underlying}{expiry_code}{strike}CE")
        instruments.append(f"NFO:{underlying}{expiry_code}{strike}PE")

    quotes = proxy_get("/get_quotes", params={'instruments': ','.join(instruments)})
    if not quotes:
        print(f"Failed to fetch quotes for {underlying} {expiry_code}")
        return None

    expiry_str = expiry_dt.strftime('%d-%b-%Y')
    t_years = max(
        (expiry_dt.date() - datetime.now(ist).date()).days / 365.0, 1.0 / 365.0
    )

    records = []
    for i in range(-STRIKES_AROUND_ATM, STRIKES_AROUND_ATM + 1):
        strike = atm + i * step
        if strike <= 0:
            continue
        ce_inst = f"NFO:{underlying}{expiry_code}{strike}CE"
        pe_inst = f"NFO:{underlying}{expiry_code}{strike}PE"
        ce_quote = quotes.get(ce_inst)
        pe_quote = quotes.get(pe_inst)
        if not ce_quote and not pe_quote:
            continue  # strike not listed on this expiry

        record = {'strikePrice': strike, 'expiryDate': expiry_str}
        if ce_quote:
            ce_price = ce_quote.get('last_price', 0)
            record['CE'] = {
                'lastPrice': ce_price,
                'impliedVolatility': _iv_pct(spot, strike, t_years, ce_price, 'CE'),
                'openInterest': ce_quote.get('oi', 0),
            }
        if pe_quote:
            pe_price = pe_quote.get('last_price', 0)
            record['PE'] = {
                'lastPrice': pe_price,
                'impliedVolatility': _iv_pct(spot, strike, t_years, pe_price, 'PE'),
                'openInterest': pe_quote.get('oi', 0),
            }
        records.append(record)

    return {'records': {'data': records}, 'underlyingValue': spot}


def get_ltp(symbol, request_type='stock', expiry=None, max_retries=3):
    """Backwards-compatible entry point for ``strike_selection.py``.

    request_type='stock': returns the spot LTP as a float (was the full NSE
        allIndices payload before; that response shape was unused downstream).

    request_type='option': returns an NSE-shaped option-chain dict::

        {'records': {'data': [
            {'strikePrice': N, 'expiryDate': 'DD-Mon-YYYY',
             'CE': {'lastPrice': X, 'impliedVolatility': Y, 'openInterest': Z},
             'PE': {'lastPrice': X, 'impliedVolatility': Y, 'openInterest': Z}},
            ...
        ]}, 'underlyingValue': spot}

        ``expiry`` may be a ``datetime``/``date`` or a string like
        ``'30-Sep-2025'``. If omitted, defaults to the current month's last
        Tuesday (NIFTY/BANKNIFTY monthly expiry).
    """
    if request_type == 'stock':
        return _get_spot(symbol)

    if isinstance(expiry, str):
        expiry_dt = datetime.strptime(expiry, '%d-%b-%Y')
    elif expiry is not None:
        expiry_dt = expiry if isinstance(expiry, datetime) else datetime(
            expiry.year, expiry.month, expiry.day
        )
    else:
        # Default: nearest monthly expiry (last Tuesday of the current month)
        from helpers.basic import last_weekday_of_month
        today = datetime.now(ist)
        expiry_dt = last_weekday_of_month(today.year, today.month, 1)

    for attempt in range(1, max_retries + 1):
        chain = _build_chain_payload(symbol, expiry_dt)
        if chain:
            return chain
        print(f"Attempt {attempt}/{max_retries} returned no chain, retrying...")

    print(f"Failed to fetch option chain for {symbol} after {max_retries} attempts")
    return None
