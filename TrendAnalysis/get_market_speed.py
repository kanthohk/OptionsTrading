import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")

import pytz
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot as plt
except Exception:
    raise

IST = pytz.timezone("Asia/Kolkata")


def resolve_path(path_value: str | None, base_dir: Path | None = None) -> Path | None:
    if path_value is None:
        return None

    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate

    base = base_dir or Path.cwd()
    return (base / candidate).resolve()


def normalize_symbol(symbol: str) -> str:
    raw_value = str(symbol).strip()
    if not raw_value:
        return ""

    value = raw_value.upper()
    if ":" in value:
        exchange, instrument = value.split(":", 1)
        return f"{exchange.strip()}:{instrument.strip()}".strip()

    aliases = {
        "NIFTY": "NSE:NIFTY 50",
        "NIFTY 50": "NSE:NIFTY 50",
        "NIFTY50": "NSE:NIFTY 50",
        "NSE:NIFTY": "NSE:NIFTY 50",
        "NSE:NIFTY50": "NSE:NIFTY 50",
        "BANKNIFTY": "NSE:NIFTY BANK",
        "NIFTY BANK": "NSE:NIFTY BANK",
        "NIFTYBANK": "NSE:NIFTY BANK",
        "NSE:BANKNIFTY": "NSE:NIFTY BANK",
        "NSE:NIFTYBANK": "NSE:NIFTY BANK",
        "INDIAVIX": "NSE:INDIA VIX",
        "INDIA VIX": "NSE:INDIA VIX",
        "NSE:INDIAVIX": "NSE:INDIA VIX",
        "NSE:INDIA VIX": "NSE:INDIA VIX",
        "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "NSE:FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "NIFTYIT": "NSE:NIFTY IT",
        "NSE:NIFTYIT": "NSE:NIFTY IT",
        "SENSEX": "BSE:SENSEX",
        "BSE:SENSEX": "BSE:SENSEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY",
        "NSE:MIDCPNIFTY": "NSE:MIDCPNIFTY",
    }

    compact = value.replace(" ", "")
    if value in aliases:
        return aliases[value]
    if compact in aliases:
        return aliases[compact]
    return value


def normalize_symbols(symbols_value):
    if symbols_value is None:
        return []

    if isinstance(symbols_value, str):
        items = [part.strip() for part in symbols_value.split(",") if part.strip()]
    elif isinstance(symbols_value, (list, tuple)):
        items = [str(part).strip() for part in symbols_value if str(part).strip()]
    else:
        items = [str(symbols_value).strip()]

    normalized = []
    seen = set()
    for item in items:
        symbol = normalize_symbol(item)
        if symbol and symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def load_chart_config(config_path: Path):
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        return {}
    return raw.get("market_speed", {})


def load_credentials(user: str):
    config_path = Path(__file__).resolve().parents[1] / "TradingBroker" / "config.yaml"
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    credentials = config.get("credentials", {}).get(user)
    if not credentials:
        raise ValueError(f"No credentials found for user '{user}' in {config_path}")
    return credentials


def fetch_underlying_ltp(kite, symbol: str):
    symbol = normalize_symbol(symbol)
    quote = kite.quote(symbol)
    record = quote.get(symbol)
    if not record:
        raise RuntimeError(f"{symbol} quote was not returned by Kite")
    return float(record.get("last_price"))


def fetch_indices_ltp(kite, symbols: list):
    """Fetch LTP for multiple instruments."""
    normalized = [normalize_symbol(sym) for sym in symbols]
    quotes = kite.quote(normalized)
    result = {}

    for symbol in normalized:
        #print("Trying to fetch quote for:", symbol)
        quote_data = quotes.get(symbol)
        if quote_data is None:
            raise RuntimeError(f"{symbol} quote was not returned by Kite")
        result[symbol] = float(quote_data.get("last_price"))

    return result


def ensure_csv_file(output_csv: Path, symbols: list):
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    if not output_csv.exists():
        with output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            header = ["datetime"] + [f"{sym} price" for sym in symbols]
            writer.writerow(header)


def append_row(output_csv: Path, dt: datetime, prices: dict):
    with output_csv.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        row = [dt.strftime("%Y-%m-%d %H:%M:%S")] + [f"{prices.get(sym, 0):.2f}" for sym in prices.keys()]
        writer.writerow(row)


def plot_market_speed(points, output_path: Path):
    if not points:
        return

    symbols = list(points[0]["prices"].keys())
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = ["tab:blue", "tab:green", "tab:red", "tab:purple", "tab:brown"]

    for idx, symbol in enumerate(symbols):
        series_times = [point["time"] for point in points if symbol in point["prices"]]
        series_values = [point["prices"][symbol] for point in points if symbol in point["prices"]]
        if not series_values:
            continue
        ax.plot(
            series_times,
            series_values,
            color=colors[idx % len(colors)],
            marker="o",
            linewidth=2,
            label=symbol,
        )

    ax.set_xlabel("Time")
    ax.set_ylabel("Price")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def normalize_market_speed_symbol(symbol: str) -> str:
    """Canonicalise a symbol for KITE market-speed lookups."""
    raw_value = str(symbol).strip()
    if not raw_value:
        return ""

    value = raw_value.upper()
    if ":" in value:
        exchange, instrument = value.split(":", 1)
        return f"{exchange.strip()}:{instrument.strip()}".strip()

    aliases = {
        "NIFTY": "NSE:NIFTY 50",
        "NIFTY 50": "NSE:NIFTY 50",
        "NIFTY50": "NSE:NIFTY 50",
        "NSE:NIFTY": "NSE:NIFTY 50",
        "NSE:NIFTY50": "NSE:NIFTY 50",
        "BANKNIFTY": "NSE:NIFTY BANK",
        "NIFTY BANK": "NSE:NIFTY BANK",
        "NIFTYBANK": "NSE:NIFTY BANK",
        "NSE:BANKNIFTY": "NSE:NIFTY BANK",
        "NSE:NIFTYBANK": "NSE:NIFTY BANK",
        "INDIAVIX": "NSE:INDIA VIX",
        "INDIA VIX": "NSE:INDIA VIX",
        "NSE:INDIAVIX": "NSE:INDIA VIX",
        "NSE:INDIA VIX": "NSE:INDIA VIX",
        "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "NSE:FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "NIFTYIT": "NSE:NIFTY IT",
        "NSE:NIFTYIT": "NSE:NIFTY IT",
        "SENSEX": "BSE:SENSEX",
        "BSE:SENSEX": "BSE:SENSEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY",
        "NSE:MIDCPNIFTY": "NSE:MIDCPNIFTY",
    }

    compact = value.replace(" ", "")
    if value in aliases:
        return aliases[value]
    if compact in aliases:
        return aliases[compact]
    return value


def normalize_market_speed_symbols(symbols_value):
    if symbols_value is None:
        return []
    if isinstance(symbols_value, str):
        items = [part.strip() for part in symbols_value.split(",") if part.strip()]
    elif isinstance(symbols_value, (list, tuple)):
        items = [str(part).strip() for part in symbols_value if str(part).strip()]
    else:
        items = [str(symbols_value).strip()]

    normalized = []
    seen = set()
    for item in items:
        symbol = normalize_market_speed_symbol(item)
        if symbol and symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def fetch_market_speed_ltp(kite, symbol: str):
    symbol = normalize_market_speed_symbol(symbol)
    quote = kite.quote(symbol)
    record = quote.get(symbol)
    if not record:
        raise RuntimeError(f"{symbol} quote was not returned by Kite")
    return float(record.get("last_price"))


def fetch_market_speed_ltp_batch(kite, symbols: list):
    normalized = [normalize_market_speed_symbol(sym) for sym in symbols]
    quotes = kite.quote(normalized)
    result = {}
    for symbol in normalized:
        quote_data = quotes.get(symbol)
        if quote_data is None:
            raise RuntimeError(f"{symbol} quote was not returned by Kite")
        result[symbol] = float(quote_data.get("last_price"))
    return result


def _market_csv_name(prefix: str, dt: datetime | None = None):
    stamp = (dt or datetime.now(IST)).strftime("%Y%m%d")
    return f"{prefix}_{stamp}.csv"


def _resolve_market_speed_path(path_value: Path | str):
    output_csv = Path(path_value)
    lowered = output_csv.name.lower()
    if lowered == "market_speed.csv":
        return output_csv.with_name(_market_csv_name("market_speed"))
    if lowered.startswith("market_speed_") and output_csv.suffix.lower() == ".csv":
        return output_csv
    if lowered.startswith("market_indices_") and output_csv.suffix.lower() == ".csv":
        return output_csv.with_name(_market_csv_name("market_speed"))
    return output_csv


def _resolve_market_indices_path(path_value: Path | str):
    output_csv = Path(path_value)
    lowered = output_csv.name.lower()
    if lowered == "market_indices.csv":
        return output_csv.with_name(_market_csv_name("market_indices"))
    if lowered.startswith("market_indices_") and output_csv.suffix.lower() == ".csv":
        return output_csv
    if lowered.startswith("market_speed_") and output_csv.suffix.lower() == ".csv":
        return output_csv.with_name(_market_csv_name("market_indices"))
    if lowered == "market_speed.csv":
        return output_csv.with_name(_market_csv_name("market_indices"))
    return output_csv


def ensure_market_speed_csv(output_csv: Path, symbols: list):
    output_csv = _resolve_market_speed_path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    if not output_csv.exists():
        with output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["datetime", "price", "speed"])
    return output_csv


def ensure_market_indices_csv(output_csv: Path):
    output_csv = _resolve_market_indices_path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    if not output_csv.exists():
        with output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["datetime", "NIFTY_50_price", "NIFTY_BANK_price", "INDIA_VIX"])
    return output_csv


def append_market_speed_row(output_csv: Path, dt: datetime, price: float, speed: float):
    with _resolve_market_speed_path(output_csv).open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), f"{price:.2f}", f"{speed:.6f}"])


def append_market_indices_row(output_csv: Path, dt: datetime, prices: dict):
    index_csv = _resolve_market_indices_path(output_csv)
    with index_csv.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        nifty_price = prices.get("NSE:NIFTY 50", prices.get("NSE:NIFTY", 0.0))
        bank_price = prices.get("NSE:NIFTY BANK", prices.get("NSE:BANKNIFTY", 0.0))
        vix_price = prices.get("NSE:INDIA VIX", prices.get("NSE:INDIAVIX", 0.0))
        writer.writerow([
            dt.strftime("%Y-%m-%d %H:%M:%S"),
            f"{nifty_price:.2f}",
            f"{bank_price:.2f}",
            f"{vix_price:.2f}",
        ])


def plot_market_speed_points(points, output_path: Path):
    if not points:
        return
    symbols = list(points[0]["prices"].keys())
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = ["tab:blue", "tab:green", "tab:red", "tab:purple", "tab:brown"]

    for idx, symbol in enumerate(symbols):
        series_times = [point["time"] for point in points if symbol in point["prices"]]
        series_values = [point["prices"][symbol] for point in points if symbol in point["prices"]]
        if not series_values:
            continue
        ax.plot(series_times, series_values, color=colors[idx % len(colors)], marker="o", linewidth=2, label=symbol)

    ax.set_xlabel("Time")
    ax.set_ylabel("Price")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def monitor_market_speed(kite, symbols, interval=60, output_csv=None, plot_path=None,
                        max_iterations=None, stop_after_seconds=None):
    """Monitor price movement and average speed for a list of KITE symbols."""
    symbols = normalize_market_speed_symbols(symbols)
    if not symbols:
        raise ValueError("At least one symbol must be provided")
    if interval <= 0:
        raise ValueError("interval must be a positive integer")

    output_path = Path(output_csv) if output_csv else None
    index_output_path = None
    if output_path is not None:
        ensure_market_speed_csv(output_path, symbols)
        index_output_path = ensure_market_indices_csv(output_path)

    start_time = datetime.now(IST)
    previous_prices = fetch_market_speed_ltp_batch(kite, symbols)
    total_distance = {sym: 0.0 for sym in symbols}
    tracked_points = []
    current_average_speed = {}
    iteration = 0

    while True:
        if max_iterations is not None and iteration >= max_iterations:
            break
        if stop_after_seconds is not None:
            if (datetime.now(IST) - start_time).total_seconds() >= stop_after_seconds:
                break

        current_time = datetime.now(IST)
        current_prices = fetch_market_speed_ltp_batch(kite, symbols)
        elapsed_hours = max((current_time - start_time).total_seconds() / 3600.0, 1e-9)

        current_average_speed = {}
        for sym in symbols:
            if sym not in previous_prices or sym not in current_prices:
                continue
            distance = abs(current_prices[sym] - previous_prices[sym])
            total_distance[sym] += distance
            current_average_speed[sym] = total_distance[sym] / elapsed_hours

        tracked_points.append({
            "time": current_time,
            "prices": current_prices.copy(),
            "average_speed": current_average_speed.copy(),
        })

        if output_path is not None:
            nifty_price = current_prices.get("NSE:NIFTY 50", current_prices.get("NSE:NIFTY", 0.0))
            nifty_speed = current_average_speed.get("NSE:NIFTY 50", current_average_speed.get("NSE:NIFTY", 0.0))
            append_market_speed_row(output_path, current_time, nifty_price, nifty_speed)
            append_market_indices_row(index_output_path or output_path, current_time, current_prices)

        previous_prices = current_prices.copy()
        iteration += 1

        if max_iterations is not None and iteration >= max_iterations:
            break
        if stop_after_seconds is not None:
            if (datetime.now(IST) - start_time).total_seconds() >= stop_after_seconds:
                break

        time.sleep(interval)

    if plot_path and tracked_points:
        plot_market_speed_points(tracked_points, Path(plot_path))

    return {
        "symbols": symbols,
        "tracked_points": tracked_points,
        "total_distance": total_distance,
        "current_average_speed": current_average_speed,
    }


def main():
    parser = argparse.ArgumentParser(description="Print and log the selected market symbol prices on a fixed interval.")
    parser.add_argument("--config", default="chart_config.yaml", help="Path to the YAML config file")
    parser.add_argument("--user", default=None, help="Zerodha account name from TradingBroker/config.yaml")
    parser.add_argument("--interval", type=int, default=None, help="Seconds between price checks")
    parser.add_argument("--symbols", default=None, help="Comma-separated list of indices to monitor (e.g., BANKNIFTY,SENSEX,INDIAVIX)")
    parser.add_argument("--csv", default=None, help="CSV file path to log date,time,prices rows")
    parser.add_argument("--plot", default=None, help="Optional PNG file path to save a price vs average-speed chart when the script stops")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    config_path = resolve_path(args.config, base_dir=script_dir)
    config = load_chart_config(config_path)

    user = args.user if args.user is not None else config.get("user", "sashi")
    interval = args.interval if args.interval is not None else config.get("interval", 60)
    configured_symbols = config.get("symbols")
    if configured_symbols is None:
        configured_symbols = config.get("symbol")
    symbols = normalize_symbols(args.symbols if args.symbols is not None else configured_symbols or ["NSE:NIFTY 50"])
    csv_path = args.csv if args.csv is not None else config.get("csv", "market_speed.csv")
    plot_path_value = args.plot if args.plot is not None else config.get("plot")

    if interval <= 0:
        raise ValueError("--interval must be a positive integer")
    if not symbols:
        raise ValueError("At least one symbol must be provided")

    output_csv = resolve_path(csv_path, base_dir=script_dir)
    ensure_csv_file(output_csv, symbols)

    ok, message = get_broker(user)
    if not ok:
        raise RuntimeError(f"Could not connect to Zerodha for user '{user}': {message}")

    if user not in broker_connections or not getattr(broker_connections[user], "access_token", None):
        raise RuntimeError(f"Broker connection for user '{user}' is not available")

    kite = broker_connections[user].get_connection()
    print(f"Connected to Zerodha for user '{user}'. Fetching {', '.join(symbols)} every {interval} seconds...")
    print(f"Logging to CSV: {output_csv}")

    start_time = datetime.now(IST)
    previous_prices = fetch_indices_ltp(kite, symbols)
    total_distance = {sym: 0.0 for sym in symbols}
    tracked_points = []

    try:
        while True:
            current_time = datetime.now(IST)
            current_prices = fetch_indices_ltp(kite, symbols)
            elapsed_hours = max((current_time - start_time).total_seconds() / 3600.0, 1e-9)

            current_average_speed = {}
            for sym in symbols:
                if sym not in previous_prices or sym not in current_prices:
                    continue
                distance = abs(current_prices[sym] - previous_prices[sym])
                total_distance[sym] += distance
                current_average_speed[sym] = total_distance[sym] / elapsed_hours

            tracked_points.append({
                "time": current_time,
                "prices": current_prices.copy(),
                "average_speed": current_average_speed,
            })

            line_parts = [f"{sym}={current_prices[sym]:.2f}" for sym in sorted(current_prices.keys())]
            speed_parts = [f"{sym}={current_average_speed.get(sym, 0.0):.6f}" for sym in sorted(current_average_speed.keys())]
            print(f"{current_time.strftime('%H:%M:%S')} | Prices: {' = '.join(line_parts)} | average_speeds: {' = '.join(speed_parts)}")

            append_row(output_csv, current_time, current_prices)
            previous_prices = current_prices.copy()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopping market speed monitor.")
        if plot_path_value:
            plot_path = resolve_path(plot_path_value, base_dir=script_dir)
            plot_path.parent.mkdir(parents=True, exist_ok=True)
            plot_market_speed(tracked_points, plot_path)


if __name__ == "__main__":
    main()
