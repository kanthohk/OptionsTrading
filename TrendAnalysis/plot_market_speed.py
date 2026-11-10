import argparse
import csv
import os
from datetime import datetime, timedelta
import random
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - optional dependency for config files
    yaml = None

import matplotlib
matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt
from matplotlib.animation import FuncAnimation


SCRIPT_DIR = Path(__file__).resolve().parent


def load_chart_config(config_path: Path):
    if not config_path.exists() or yaml is None:
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        return {}
    return raw.get("plot_market_speed", {})


def resolve_script_relative_path(path_value):
    if path_value is None:
        return None

    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return (SCRIPT_DIR / candidate).resolve()


def resolve_cli_path(path_value):
    if path_value is None:
        return None

    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate

    candidate_in_cwd = (Path.cwd() / candidate).resolve()
    if candidate_in_cwd.exists():
        return candidate_in_cwd

    return (SCRIPT_DIR / candidate).resolve()


def _as_float(value):
    if value is None:
        return 0.0
    value = str(value).strip()
    if not value:
        return 0.0
    return float(value)


def _parse_time(value):
    value = str(value).strip()
    if len(value) == 8 and value.count(":") == 2:
        return datetime.strptime(value, "%H:%M:%S")
    if len(value) >= 19 and " " in value:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return None


def load_rows(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = None
        for raw_row in reader:
            if not raw_row or all(not cell.strip() for cell in raw_row):
                continue

            cells = [cell.strip().lstrip("\ufeff") for cell in raw_row]
            if not cells:
                continue

            if header is None:
                header = cells
                if header and header[0].lower() in {"time", "date", "datetime"}:
                    if len(header) > 1 and any("nifty" in name.lower() or "bank" in name.lower() or "vix" in name.lower() for name in header[1:]):
                        continue
                    continue
                continue

            if cells[0].lower() in {"time", "date", "datetime", "price", "average_speed", "speed"}:
                continue

            try:
                if header and header[0].lower() in {"time", "date", "datetime"} and len(header) > 1:
                    time_value = _parse_time(cells[0])
                    if time_value is not None:
                        values = {}
                        avg_speed = 0.0
                        for idx, column in enumerate(header[1:], start=1):
                            if idx >= len(cells):
                                continue
                            lower_name = column.lower()
                            if lower_name in {"average_speed", "avg_speed", "speed"}:
                                avg_speed = _as_float(cells[idx])
                                continue
                            values[column] = _as_float(cells[idx])
                        if values:
                            rows.append({
                                "time": time_value,
                                "prices": values,
                                "average_speed": avg_speed,
                            })
                        continue

                if len(cells) >= 3 and " " in cells[0] and cells[0].count("-") == 2:
                    time_value = _parse_time(cells[0])
                    if time_value is not None:
                        price = _as_float(cells[1])
                        avg_speed = _as_float(cells[2])
                        rows.append({
                            "time": time_value,
                            "price": price,
                            "average_speed": avg_speed,
                        })
                    continue

                if len(cells) >= 3 and ":" in cells[0] and not cells[0].count("-") == 2:
                    time_value = _parse_time(cells[0])
                    price = _as_float(cells[1])
                    avg_speed = _as_float(cells[2])
                    if time_value is not None:
                        rows.append({
                            "time": time_value,
                            "price": price,
                            "average_speed": avg_speed,
                        })
                    continue

                if len(cells) >= 4 and cells[0].count("-") == 2:
                    time_value = _parse_time(f"{cells[0]} {cells[1]}")
                    if time_value is not None:
                        price = _as_float(cells[2])
                        avg_speed = _as_float(cells[-1])
                        rows.append({
                            "time": time_value,
                            "price": price,
                            "average_speed": avg_speed,
                        })
                    continue

            except (ValueError, TypeError):
                continue

    # Multi-index CSVs are stored as datetime + price columns, but callers expect an
    # object keyed by "prices" so the normalized multi-series chart works.
    if rows and rows[0].get("prices") is None and header and len(header) >= 4 and any(
        "nifty" in col.lower() or "bank" in col.lower() or "vix" in col.lower() for col in header[1:]
    ):
        cleaned = []
        for row in rows:
            if "price" in row:
                cleaned.append({
                    "time": row["time"],
                    "prices": {"NIFTY_50_price": row["price"]},
                    "average_speed": row.get("average_speed", 0.0),
                })
        if cleaned:
            return cleaned

    return rows


def plot_market_speed(rows, output_path: Path, mode: str = "single"):
    if not rows:
        raise ValueError(f"No data available in {output_path}")

    chart_mode = (mode or "single").lower()
    if chart_mode == "multi" and rows and isinstance(rows[0], dict) and "prices" in rows[0]:
        times = [row["time"] for row in rows]
        symbol_names = sorted({sym for row in rows for sym in row["prices"].keys()})
        fig, ax_left = plt.subplots(figsize=(12, 6))
        ax_right = ax_left.twinx()
        colors = ["tab:blue", "tab:green", "tab:red", "tab:purple", "tab:brown", "tab:orange"]

        for idx, symbol in enumerate(symbol_names):
            series_times = [point["time"] for point in rows if symbol in point["prices"]]
            series_values = [point["prices"][symbol] for point in rows if symbol in point["prices"]]
            if not series_values:
                continue

            first_value = series_values[0]
            normalized_values = [100.0] if not series_values else [float(value) / float(first_value) * 100.0 for value in series_values]

            if "INDIAVIX" in symbol.upper() or "INDIA VIX" in symbol.upper():
                ax_right.plot(series_times, normalized_values, color=colors[idx % len(colors)], marker="o", linewidth=2, label=symbol)
            else:
                ax_left.plot(series_times, normalized_values, color=colors[idx % len(colors)], marker="o", linewidth=2, label=symbol)

        ax_left.set_xlabel("Time")
        ax_left.set_ylabel("Left axis indices (base = 100)")
        ax_left.tick_params(axis="x", rotation=45)
        ax_left.set_title("Normalized multi-index comparison")

        ax_right.set_ylabel("India VIX (base = 100)")
        ax_right.tick_params(axis="y", labelcolor="tab:orange")

        handles_left, labels_left = ax_left.get_legend_handles_labels()
        handles_right, labels_right = ax_right.get_legend_handles_labels()
        ax_left.legend(handles_left + handles_right, labels_left + labels_right, loc="best")

        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
        return

    if rows and isinstance(rows[0], dict) and "prices" in rows[0]:
        # Convert the combined multi-index CSV into one primary series for the speed chart
        primary_symbol = next(iter(rows[0]["prices"].keys()))
        times = [row["time"] for row in rows]
        prices = [row["prices"][primary_symbol] for row in rows]
        speeds = [row.get("average_speed", 0.0) for row in rows]
    else:
        times = [row["time"] for row in rows]
        prices = [row["price"] for row in rows]
        speeds = [row["average_speed"] for row in rows]

    fig, ax1 = plt.subplots(figsize=(12, 6))
    line1, = ax1.plot(times, prices, color="tab:blue", marker="o", linewidth=2, label="Price")
    ax1.set_xlabel("Time")
    ax1.set_ylabel("Price", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")
    ax1.tick_params(axis="x", rotation=45)

    ax2 = ax1.twinx()
    line2, = ax2.plot(times, speeds, color="tab:orange", linestyle="--", marker="s", linewidth=2, label="Average speed")
    ax2.set_ylabel("Average speed", color="tab:orange")
    ax2.tick_params(axis="y", labelcolor="tab:orange")

    lines = [line1, line2]
    labels = [line.get_label() for line in lines]
    ax1.legend(lines, labels, loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_market_speed_live(csv_path: Path, refresh_seconds: float = 2.0):
    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax2 = ax1.twinx()
    line1, = ax1.plot([], [], color="tab:blue", marker="o", linewidth=2, label="Price")
    line2, = ax2.plot([], [], color="tab:orange", linestyle="--", marker="s", linewidth=2, label="Average speed")

    ax1.set_xlabel("Time")
    ax1.set_ylabel("Price", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")
    ax2.set_ylabel("Average speed", color="tab:orange")
    ax2.tick_params(axis="y", labelcolor="tab:orange")

    def update(_frame):
        rows = load_rows(csv_path)
        if not rows:
            ax1.set_title("No chart data available")
            return line1, line2

        times = [row["time"] for row in rows]
        prices = [row["price"] for row in rows]
        speeds = [row["average_speed"] for row in rows]

        x_values = [datetime.combine(datetime.today().date(), t.time()) for t in times]
        line1.set_data(x_values, prices)
        line2.set_data(x_values, speeds)
        ax1.set_xlim(min(x_values), max(x_values))
        ax2.set_xlim(min(x_values), max(x_values))
        ax1.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%H:%M:%S"))
        ax1.tick_params(axis="x", rotation=45)
        ax1.relim()
        ax1.autoscale_view()
        ax2.relim()
        ax2.autoscale_view()
        ax1.set_title("")
        fig.tight_layout()
        return line1, line2

    ani = FuncAnimation(fig, update, interval=int(refresh_seconds * 1000), blit=False, cache_frame_data=False)
    plt.show()
    return ani


def main():
    parser = argparse.ArgumentParser(description="Plot price and average speed from the market_speed CSV export.")
    parser.add_argument("--config", default="chart_config.yaml", help="Path to YAML configuration file")
    parser.add_argument("csv_path", nargs="?", default=None, help="Path to the CSV file with columns: time,price,average_speed")
    parser.add_argument("output_path", nargs="?", default=None, help="PNG output path for the chart")
    parser.add_argument("--live", action="store_true", help="Show a continuously updating chart window instead of saving a PNG once")
    parser.add_argument("--refresh-seconds", type=float, default=None, help="Refresh interval for the live chart in seconds")
    args = parser.parse_args()

    config_path = resolve_script_relative_path(args.config)
    config = load_chart_config(config_path)

    csv_path_value = args.csv_path if args.csv_path is not None else config.get("csv_path", config.get("csv", "market_speed.csv"))
    output_path_value = args.output_path if args.output_path is not None else config.get("output_path", config.get("plot", "market_speed_chart.png"))
    live_value = args.live if args.live else config.get("live", False)
    refresh_value = args.refresh_seconds if args.refresh_seconds is not None else config.get("refresh_seconds", 2.0)

    csv_path = resolve_cli_path(csv_path_value) if args.csv_path is not None else resolve_script_relative_path(csv_path_value)
    output_path = resolve_cli_path(output_path_value) if args.output_path is not None else resolve_script_relative_path(output_path_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    if live_value:
        plot_market_speed_live(csv_path, refresh_seconds=refresh_value)
        return

    rows = load_rows(csv_path)
    if not rows:
        raise ValueError(f"No valid rows found in CSV: {csv_path}")

    plot_market_speed(rows, output_path)
    print(f"Chart saved to {output_path}")


def generate_mock_tracked_points():
    """Generate deterministic mock tracked points for testing."""
    # Fixed dates and times based on seed
    base_time = datetime(2026, 5, 1, 9, 30, 0)
    
    # Mock data structure matching expected format
    mock_data = {
        "instrument_token": 1018748,  # AAPL futures token example
        "tracked_points": [
            {"date": datetime(2026, 5, 1, 9, 35, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 1.2},
            {"date": datetime(2026, 5, 1, 9, 40, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 2.8},
            {"date": datetime(2026, 5, 1, 9, 45, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 1.5},
            {"date": datetime(2026, 5, 1, 9, 50, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 3.1},
            {"date": datetime(2026, 5, 1, 9, 55, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 0.9},
            {"date": datetime(2026, 5, 1, 10, 0, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": -0.3},
            {"date": datetime(2026, 5, 1, 10, 5, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 4.2},
            {"date": datetime(2026, 5, 1, 10, 10, 0).strftime("%Y-%m-%d %H:%M:%S"), "value": 5.7},
        ],
    }
    return mock_data


if __name__ == "__main__":
    main()
