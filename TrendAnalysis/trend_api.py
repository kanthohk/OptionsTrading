from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLBACKEND", "Agg")

from flask import Flask, jsonify, render_template, request, send_file

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from analyze_levels import MarketAnalyzer
from get_market_speed import (
    IST,
    KITE_CONNECT,
    fetch_indices_ltp,
    load_credentials,
    normalize_symbol,
)
from plot_market_speed import load_rows, plot_market_speed

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DEFAULT_CSV = APP_DIR / "market_speed.csv"
DEFAULT_INDICES_CSV = APP_DIR / "market_indices.csv"
DEFAULT_PLOT = APP_DIR / "data" / "market_speed_chart.png"

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))


def resolve_csv_path(csv_path: str | None = None) -> Path:
    if csv_path:
        candidate = Path(csv_path)
        if candidate.is_absolute():
            return candidate

        candidates = [APP_DIR / candidate, PROJECT_ROOT / candidate]
        for item in candidates:
            if item.exists():
                return item.resolve()
        return (APP_DIR / candidate).resolve()
    return DEFAULT_CSV


def resolve_indices_csv_path(csv_path: str | None = None) -> Path:
    if csv_path:
        candidate = Path(csv_path)
        if candidate.is_absolute():
            return candidate

        basename = candidate.name
        if basename.lower() == "market_speed.csv":
            candidate = Path("market_indices.csv")
        elif basename.lower() == "market_indices.csv":
            candidate = Path("market_indices.csv")
        else:
            candidate = Path(candidate.parent / "market_indices.csv")

        candidates = [APP_DIR / candidate, PROJECT_ROOT / candidate]
        for item in candidates:
            if item.exists() or item.parent.exists():
                return item.resolve()
        return (APP_DIR / "market_indices.csv").resolve()
    return DEFAULT_INDICES_CSV


def resolve_plot_path(output_path: str | None = None, csv_path: str | None = None) -> Path:
    if output_path:
        candidate = Path(output_path)
        if candidate.is_absolute():
            return candidate

        for base in [APP_DIR, PROJECT_ROOT]:
            resolved = (base / candidate).resolve()
            if resolved.parent.exists() or not candidate.parent.parts:
                return resolved
        return (APP_DIR / candidate).resolve()

    source_csv = resolve_csv_path(csv_path)
    return source_csv.with_name(f"{source_csv.stem}_chart.png")


def _ensure_csv_header(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            header = fh.readline().strip().lower()
        if header in {"time,price,speed", "datetime,price,speed"}:
            return
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["datetime", "price", "speed"])


def _ensure_indices_header(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            header = fh.readline().strip().lower()
        if header == "datetime,nifty50,banknifty,sensex,indiavix":
            return
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["datetime", "NIFTY50", "BANKNIFTY", "SENSEX", "INDIAVIX"])


def _read_last_snapshot(csv_path: Path) -> tuple[datetime | None, float | None, float | None]:
    if not csv_path.exists():
        return None, None, None

    try:
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            last_time = None
            last_price = None
            last_speed = None
            for row in reader:
                time_value = (row.get("datetime") or row.get("time") or "").strip()
                if not time_value:
                    continue
                try:
                    if " " in time_value and len(time_value) >= 16:
                        current_time = datetime.strptime(time_value, "%Y-%m-%d %H:%M:%S")
                    else:
                        current_time = datetime.strptime(time_value, "%H:%M:%S")
                    current_price = float(row["price"])
                    current_speed = float(row.get("speed") or 0.0)
                    last_time = current_time
                    last_price = current_price
                    last_speed = current_speed
                except (TypeError, ValueError):
                    continue
            return last_time, last_price, last_speed
    except (OSError, ValueError):
        return None, None, None


def _calculate_speed(csv_path: Path, current_price: float) -> float:
    last_time, last_price, _ = _read_last_snapshot(csv_path)
    now = datetime.now(IST)

    if last_time is None or last_price is None:
        return 0.0

    if " " in str(last_time):
        last_datetime = last_time
    else:
        last_datetime = datetime.combine(now.date(), last_time.time())

    if last_datetime.tzinfo is None:
        last_datetime = IST.localize(last_datetime)
    else:
        last_datetime = last_datetime.replace(tzinfo=last_datetime.tzinfo)

    elapsed_seconds = (now - last_datetime).total_seconds()
    elapsed_hours = max(elapsed_seconds / 3600.0, 1 / 3600.0)
    distance = abs(float(current_price) - float(last_price))
    return distance / elapsed_hours


def fetch_market_speed(user: str = "sashi", symbol: str = "NIFTY", csv_path: str | None = None) -> dict[str, Any]:
    symbol = normalize_symbol(symbol or "NIFTY")
    csv_file = resolve_csv_path(csv_path)
    indices_csv_file = resolve_indices_csv_path(csv_path)
    _ensure_csv_header(csv_file)
    _ensure_indices_header(indices_csv_file)

    credentials = load_credentials(user)
    kite_connect = KITE_CONNECT(user, credentials)
    if not kite_connect.access_token:
        raise RuntimeError(f"Could not connect to Zerodha for user '{user}'")

    kite = kite_connect.get_connection()
    index_symbols = [normalize_symbol(item) for item in ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"]]
    index_prices = fetch_indices_ltp(kite, index_symbols)
    current_price = float(index_prices.get(symbol, index_prices.get(normalize_symbol(symbol), 0.0)))
    current_time = datetime.now(IST)
    average_speed = _calculate_speed(csv_file, current_price)

    with csv_file.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([current_time.strftime("%Y-%m-%d %H:%M:%S"), f"{current_price:.2f}", f"{average_speed:.6f}"])

    with indices_csv_file.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            current_time.strftime("%Y-%m-%d %H:%M:%S"),
            f"{index_prices.get('NSE:NIFTY 50', 0.0):.2f}",
            f"{index_prices.get('NSE:NIFTY BANK', 0.0):.2f}",
            f"{index_prices.get('BSE:SENSEX', 0.0):.2f}",
            f"{index_prices.get('NSE:INDIA VIX', 0.0):.2f}",
        ])

    return {
        "symbol": symbol,
        "price": round(current_price, 2),
        "average_speed": round(average_speed, 6),
        "timestamp": current_time.strftime("%Y-%m-%d %H:%M:%S"),
        "csv_path": str(csv_file),
    }


def plot_market_chart(csv_path: str | None = None, output_path: str | None = None, chart_type: str = "single") -> str:
    csv_file = resolve_csv_path(csv_path)
    plot_file = resolve_plot_path(output_path, str(csv_file))
    plot_file.parent.mkdir(parents=True, exist_ok=True)

    if not csv_file.exists() or csv_file.stat().st_size == 0:
        _ensure_csv_header(csv_file)

    rows = load_rows(csv_file)
    if not rows:
        from matplotlib import pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No market data available yet", ha="center", va="center", fontsize=12)
        ax.set_axis_off()
        fig.tight_layout()
        fig.savefig(plot_file, dpi=150)
        plt.close(fig)
        return str(plot_file)

    plot_market_speed(rows, plot_file, mode=chart_type)
    return str(plot_file)


def analyze_market_levels(csv_path: str | None = None) -> dict[str, Any]:
    csv_file = resolve_csv_path(csv_path)
    analyzer = MarketAnalyzer(str(csv_file))
    analyzer.load_data()
    analyzer.calculate_daily_open_prices()
    analyzer.analyze_hourly_trends()
    analyzer.detect_support_resistance_levels(lookback=10, atr_multiplier=0.75, max_levels=5)

    def serialise_levels(level_map: dict) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for level_key, info in sorted(level_map.items(), key=lambda item: float(item[0])):
            serialized.append({
                "level": float(info["level"]),
                "type": info.get("type"),
                "strength": float(info.get("strength", 0.0)),
                "timestamp": info.get("timestamp"),
            })
        return serialized

    return {
        "daily_open_prices": [{"date": str(k), "open": float(v)} for k, v in sorted(analyzer.daily_open_prices.items())],
        "hourly_trends": analyzer.hourly_trends,
        "support_levels": serialise_levels(analyzer.support_levels),
        "resistance_levels": serialise_levels(analyzer.resistance_levels),
    }


@app.route("/")
def index():
    return render_template("trend_dashboard.html")


@app.route("/api/market-speed/fetch", methods=["GET", "POST"])
def api_fetch_market_speed():
    payload = request.get_json(silent=True) or request.form or {}
    user = str(payload.get("user") or "sashi")
    symbol = str(payload.get("symbol") or payload.get("symbols") or "NIFTY")
    csv_path = payload.get("csv_path")

    try:
        result = fetch_market_speed(user=user, symbol=symbol, csv_path=csv_path)
        return jsonify({"status": "ok", "data": result})
    except Exception as exc:  # pragma: no cover - runtime integration only
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/market-speed/chart-image")
def api_chart_image():
    csv_path = request.args.get("csv_path")
    output_path = request.args.get("output_path")
    chart_type = request.args.get("chart_type", "single")
    try:
        chart_path = plot_market_chart(csv_path=csv_path, output_path=output_path, chart_type=chart_type)
        return send_file(chart_path, mimetype="image/png")
    except Exception as exc:  # pragma: no cover - runtime integration only
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/market-speed/analyze", methods=["GET", "POST"])
def api_analyze_market_levels():
    payload = request.get_json(silent=True) or request.form or {}
    csv_path = payload.get("csv_path")

    try:
        analysis = analyze_market_levels(csv_path)
        return jsonify({"status": "ok", "analysis": analysis})
    except Exception as exc:  # pragma: no cover - runtime integration only
        return jsonify({"status": "error", "message": str(exc)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
