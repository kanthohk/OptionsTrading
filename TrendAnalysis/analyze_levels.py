#!/usr/bin/env python3
"""
Market Speed Analysis Script (v3)
Analyzes market_speed.csv to:
1. Calculate hourly trends
2. Identify Support and Resistance levels
3. Provide comprehensive market analysis
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import warnings

warnings.filterwarnings('ignore')


class MarketAnalyzer:
    """Comprehensive market data analyzer for trend and level detection."""
    
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.df = None
        self.daily_open_prices = {}
        self.hourly_data = defaultdict(list)
        self.hourly_trends = []
        self.support_levels = {}
        self.resistance_levels = {}
        
    def load_data(self):
        """Load and preprocess the CSV data."""
        print(f"Loading data from: {self.csv_path}")
        self.df = pd.read_csv(self.csv_path)

        if 'time' not in self.df.columns and 'datetime' in self.df.columns:
            self.df.rename(columns={'datetime': 'time'}, inplace=True)
        elif 'time' not in self.df.columns and 'date' in self.df.columns:
            self.df.rename(columns={'date': 'time'}, inplace=True)

        if 'time' not in self.df.columns:
            raise ValueError("CSV must include a 'time' or 'datetime' column.")

        if 'price' not in self.df.columns:
            numeric_columns = [
                col for col in self.df.columns
                if col.lower() not in {'time', 'date', 'average_speed', 'avg_speed', 'speed'}
                and pd.api.types.is_numeric_dtype(self.df[col])
            ]
            if not numeric_columns:
                raise ValueError("CSV must include a numeric price column.")
            self.df.rename(columns={numeric_columns[0]: 'price'}, inplace=True)

        if 'average_speed' in self.df.columns and 'speed' not in self.df.columns:
            self.df.rename(columns={'average_speed': 'speed'}, inplace=True)
        elif 'avg_speed' in self.df.columns and 'speed' not in self.df.columns:
            self.df.rename(columns={'avg_speed': 'speed'}, inplace=True)
        elif 'speed' not in self.df.columns and 'price' in self.df.columns:
            self.df['speed'] = 0.0

        self.df['time'] = pd.to_datetime(self.df['time'], errors='coerce')
        if self.df['time'].isna().all():
            self.df['time'] = pd.to_datetime(self.df['time'], format='%H:%M:%S', errors='coerce')
        self.df = self.df.dropna(subset=['time', 'price']).sort_values('time').reset_index(drop=True)
        print(f"Loaded {len(self.df)} records")
        return True
    
    def calculate_daily_open_prices(self):
        """Calculate the opening price for each day."""
        self.daily_open_prices = {}
        
        df_date_grouped = self.df.groupby(self.df['time'].dt.date)
        
        for name, group in df_date_grouped:
            time_diff = (group['time'] - group['time'].min()).dt.total_seconds() / 60
            open_group = group[time_diff <= 5]
            
            if not open_group.empty:
                avg_open = open_group['price'].mean()
                self.daily_open_prices[name] = avg_open
    
    def analyze_hourly_trends(self):
        """Analyze hourly price trends with proper calculations."""
        print("\n=== HOURLY TREND ANALYSIS ===")

        hour_data = defaultdict(list)
        self.hourly_trends = []
        current_hour = None
        current_hour_key = None
        hour_open_price = None
        hour_high = None
        hour_low = None

        def finalize_hour(hour_key, open_price, high_price, low_price, data_points):
            if not data_points:
                return

            close_price = data_points[-1]['price']
            if open_price == 0:
                trend_direction = 'SIDEWAYS'
                trend_strength_num = 0.0
            elif close_price > open_price:
                trend_direction = 'UPTREND'
                trend_strength_num = abs(close_price - open_price) / open_price * 100
            elif close_price < open_price:
                trend_direction = 'DOWNTREND'
                trend_strength_num = abs(open_price - close_price) / open_price * 100
            else:
                trend_direction = 'SIDEWAYS'
                trend_strength_num = 0.0

            if trend_strength_num >= 1:
                trend_strength = f"STRONG ({trend_strength_num:.2f}%)"
            elif trend_strength_num >= 0.5:
                trend_strength = f"MODERATE ({trend_strength_num:.2f}%)"
            elif trend_strength_num > 0:
                trend_strength = f"WEAK ({trend_strength_num:.2f}%)"
            else:
                trend_strength = "FLAT (0.00%)"

            result = {
                'hour': hour_key,
                'open': round(float(open_price), 2),
                'high': round(float(high_price), 2),
                'low': round(float(low_price), 2),
                'close': round(float(close_price), 2),
                'trend': trend_direction,
                'strength': trend_strength,
            }
            self.hourly_trends.append(result)

            print(f"{hour_key}: Open={open_price:.2f}, High={high_price:.2f}, Low={low_price:.2f}, "
                  f"Close={close_price:.2f} | Trend: {trend_direction} | {trend_strength}")

            hour_data[hour_key] = data_points

        for _, row in self.df.iterrows():
            dt = row['time']
            hour_key = f"{dt.hour:02d}:00"
            price = float(row['price'])

            if current_hour is None:
                current_hour = dt.hour
                current_hour_key = hour_key
                hour_open_price = price
                hour_high = price
                hour_low = price
                hour_data[current_hour_key].append({
                    'price': price,
                    'speed': row['speed'] if pd.notnull(row['speed']) else 0,
                    'time': dt
                })
                continue

            if dt.hour != current_hour:
                finalize_hour(current_hour_key, hour_open_price, hour_high, hour_low, hour_data[current_hour_key])

                current_hour = dt.hour
                current_hour_key = hour_key
                hour_open_price = price
                hour_high = price
                hour_low = price
                hour_data[current_hour_key] = []

            hour_high = max(hour_high, price)
            hour_low = min(hour_low, price)
            hour_data[current_hour_key].append({
                'price': price,
                'speed': row['speed'] if pd.notnull(row['speed']) else 0,
                'time': dt
            })

        if current_hour_key is not None:
            finalize_hour(current_hour_key, hour_open_price, hour_high, hour_low, hour_data[current_hour_key])

        print(f"  Remaining hours found: {len(hour_data)}")
        return self.hourly_trends

    def detect_support_resistance_levels(self, lookback=10, atr_period=14, atr_multiplier=0.75, max_levels=5):
        """Detect S/R pivots using local minima/maxima with ATR-based filtering."""
        self.support_levels = {}
        self.resistance_levels = {}

        price_data = self.df['price'].astype(float).to_numpy()
        if len(price_data) < atr_period + 2:
            return self.support_levels, self.resistance_levels

        tr_values = []
        for i in range(1, len(price_data)):
            prev = float(price_data[i - 1])
            curr = float(price_data[i])
            high = max(prev, curr)
            low = min(prev, curr)
            tr = max(high - low, abs(high - price_data[i - 1]), abs(low - price_data[i - 1])) if i > 0 else 0.0
            tr_values.append(tr)

        atr = np.mean(tr_values[:atr_period]) if len(tr_values) >= atr_period else np.mean(tr_values)
        if not np.isfinite(atr) or atr <= 0:
            atr = float(np.median(np.abs(np.diff(price_data)))) if len(price_data) > 1 else 1.0

        min_strength = max(0.25, atr * atr_multiplier)
        merge_tolerance = max(0.5, atr * 0.5)

        def store_level(level_map, level, strength, timestamp, prefix):
            rounded_level = round(float(level), 2)
            if rounded_level not in level_map:
                level_map[rounded_level] = {
                    'level': rounded_level,
                    'type': prefix,
                    'strength': float(strength),
                    'timestamp': timestamp
                }
            else:
                existing = level_map[rounded_level]
                existing['strength'] = max(existing['strength'], float(strength))
                if existing['strength'] == float(strength):
                    existing['timestamp'] = timestamp

        for i in range(lookback, len(price_data) - lookback):
            current_price = float(price_data[i])
            left_window = price_data[i-lookback:i]
            right_window = price_data[i+1:i+lookback+1]

            if left_window.size == 0 or right_window.size == 0:
                continue

            local_min = min(left_window.min(), right_window.min())
            local_max = max(left_window.max(), right_window.max())

            if current_price <= min(left_window) and current_price <= min(right_window):
                support_strength = max(0.0, local_max - current_price)
                if support_strength >= min_strength:
                    store_level(
                        self.support_levels,
                        current_price,
                        support_strength,
                        self.df.iloc[i]['time'].strftime('%Y-%m-%d %H:%M:%S'),
                        'S'
                    )

            if current_price >= max(left_window) and current_price >= max(right_window):
                resistance_strength = max(0.0, current_price - local_min)
                if resistance_strength >= min_strength:
                    store_level(
                        self.resistance_levels,
                        current_price,
                        resistance_strength,
                        self.df.iloc[i]['time'].strftime('%Y-%m-%d %H:%M:%S'),
                        'R'
                    )

        def merge_levels(level_map, tolerance):
            merged = {}
            for level in sorted(level_map.keys()):
                if not merged:
                    merged[level] = dict(level_map[level])
                    continue

                last_level = max(merged.keys())
                if abs(level - last_level) <= tolerance:
                    current = merged[last_level]
                    candidate = level_map[level]
                    if candidate['strength'] > current['strength']:
                        current['strength'] = candidate['strength']
                        current['timestamp'] = candidate['timestamp']
                else:
                    merged[level] = dict(level_map[level])
            return merged

        self.support_levels = merge_levels(self.support_levels, merge_tolerance)
        self.resistance_levels = merge_levels(self.resistance_levels, merge_tolerance)

        def sort_and_label(level_map, prefix):
            ranked = sorted(level_map.items(), key=lambda kv: kv[1]['strength'], reverse=True)[:max_levels]
            result = {}
            for idx, (_, info) in enumerate(ranked, start=1):
                info['type'] = f'{prefix}{idx}'
                result[info['level']] = info
            return result

        self.support_levels = sort_and_label(self.support_levels, 'S')
        self.resistance_levels = sort_and_label(self.resistance_levels, 'R')

        return self.support_levels, self.resistance_levels


def main():
    """Main execution function."""
    print("=" * 80)
    print("       MARKET SPEED ANALYSIS V3")
    print("=" * 80)
    
    csv_path = 'market_speed.csv'
    
    try:
        analyzer = MarketAnalyzer(csv_path)
        analyzer.load_data()
        analyzer.calculate_daily_open_prices()
        analyzer.analyze_hourly_trends()
        
        # Calculate support/resistance levels from local pivots and merge near-duplicate levels.
        analyzer.detect_support_resistance_levels(lookback=10, atr_multiplier=0.75, max_levels=5)
        
        print("\n=== SUPPORT & RESISTANCE LEVEL DETECTION ===")
        print("\n--- SUPPORT LEVELS ---")
        for level in sorted(analyzer.support_levels.keys()):
            info = analyzer.support_levels[level]
            print(f"{info['type']}: {info['level']} | Strength: {info['strength']:.2f}")
        
        print("\n--- RESISTANCE LEVELS ---")
        for level in sorted(analyzer.resistance_levels.keys()):
            info = analyzer.resistance_levels[level]
            print(f"{info['type']}: {info['level']} | Strength: {info['strength']:.2f}")
        
        print("\n" + "=" * 80)
        print("ANALYSIS COMPLETE!")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ ERROR: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
