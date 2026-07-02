"""Stop-loss and trailing-profit risk management.

Extracted from ``Monitor/options.py`` so the same logic can be reused outside
``handle_options`` (backtesting, dry-run replays, integration tests). The
manager reads positional state from a ``PositionProcessor`` and places orders
via an injected callable, so it has no direct dependency on the broker
plumbing or the orchestrator.
"""
import csv
import time
from datetime import datetime
import pandas as pd
import pytz

from helpers.basic import api_get, ApiError
from MarketAnalysis.db_queries import record_adjustment

IST = pytz.timezone('Asia/Kolkata')


class RiskManager:
    """Encapsulates stop-loss and trailing-profit decisions for a strategy.

    Why this exists as a separate class: stop-loss and trail-profit each carry
    their own mutable state (``nearing_strike``, ``lock_profit``,
    ``trail_profit_hit_count``) that must persist between cycles for one user.
    Bundling them with the decision logic keeps that state cohesive and lets
    the orchestrator stay focused on cycle sequencing.
    """

    def __init__(self, user, api_url, config, logger, processor,
                 place_order_fn, alert_fn=None):
        self.user = user
        self.api_url = api_url
        self.config = config
        self.logger = logger
        self.processor = processor
        self._place_order = place_order_fn
        self._alert = alert_fn or (lambda heading, message: None)

        # Per-expiry state. Keyed by ``group.expiry``. Trail-profit and
        # nearing-strike are both decisions that must persist between
        # cycles for one strategy, but each open (underlying, expiry)
        # book has its own state — earlier these were single attributes
        # which meant the next-weekly iron condor's trail-profit counter
        # would silently reset whenever the near-weekly one tripped.
        self.lock_profit_by_expiry = {}
        self.trail_profit_hit_count_by_expiry = {}
        self.nearing_strike_by_expiry = {}

    def _spot_price(self, underlying):
        """Fetch current spot for ``underlying`` via the broker proxy.

        Same call as ``check_stop_loss`` uses; isolated here so the
        adjustment branches (inward roll boundary, partial-reduce hedge
        floor) can re-fetch without depending on check_stop_loss having
        run first. Returns ``None`` on any failure — callers treat that
        as "skip the min_distance check this cycle" rather than fail
        the whole adjustment.
        """
        if underlying == 'NIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY 50/stock"
            key = 'NSE:NIFTY 50'
        elif underlying == 'BANKNIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY BANK/stock"
            key = 'NSE:NIFTY BANK'
        else:
            url = f"{self.api_url}/get_current_price/{underlying}/stock"
            key = f'NSE:{underlying}'
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"Spot fetch for {underlying} failed: {e}")
            return None
        if not ok:
            self.logger.warning(f"Broker reported failure on spot for {underlying}: {payload}")
            return None
        try:
            return int(payload[key]['last_price'])
        except (KeyError, TypeError, ValueError):
            return None

    def _fetch_option_quote(self, symbol):
        """Fetch a single option's quote dict via the broker proxy.

        Returns:
            dict — quote payload (``last_price``, ``delta`` etc.) on
                success.
            ``{}`` — broker responded successfully but had no data for
                this exact symbol (strike not listed by the exchange).
            ``None`` — transport failure or broker reported failure;
                caller must distinguish this from "no data" because in
                the unverifiable case we don't want to silently fall
                back as if the strike were missing.
        """
        url = f"{self.api_url}/get_current_price/{symbol}/option"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"  LTP check for {symbol} failed: {e}")
            return None
        if not ok:
            return None
        return payload.get(f"NFO:{symbol}") or {}

    def _india_vix(self):
        """Fetch the current India VIX last_price via the broker proxy.

        Companion to ``_spot_price`` — used when recording an adjustment
        audit row so the market-vol context is preserved. Returns
        ``None`` on any failure; the row will then carry NULL for
        ``india_vix`` rather than failing the whole write.
        """
        url = f"{self.api_url}/get_current_price/INDIA VIX/stock"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.warning(f"VIX fetch failed: {e}")
            return None
        if not ok:
            return None
        try:
            return float(payload['NSE:INDIA VIX']['last_price'])
        except (KeyError, TypeError, ValueError):
            return None

    def _record_adjustment_safely(self, **kwargs):
        """Wrap ``record_adjustment`` in try/except so a DB error in
        the audit path never crashes the live trading loop. The
        adjustment already happened at the broker by the time we get
        here — losing the audit row is regrettable but recoverable;
        crashing the cycle would be worse.
        """
        try:
            return record_adjustment(**kwargs)
        except Exception as e:
            self.logger.exception(
                f"Failed to persist adjustment audit row: {e}. "
                f"kwargs={kwargs}"
            )
            self._alert(
                "AdjustmentAuditFailed",
                f"DB write failed for adjustment audit; orders went through. "
                f"Detail: {e}"
            )
            return None

    def check_stop_loss(self, group, pnl):
        """Per-group stop-loss check.

        Reads from the supplied ``StrategyGroup`` (strategy / expiry /
        underlying / quantity / legs) instead of the legacy single-expiry
        attributes on the processor, so multiple open books are each
        evaluated on their own merit. ``close_on_stoploss`` and
        ``adjust_hedges`` are read off the group's per-week config block
        (see ``PositionProcessor.get_group_config``) so each book can be
        wired differently. Also records the per-expiry ``nearing_strike``
        flag that ``adjustments(group)`` consults.
        """
        group_cfg = self.processor.get_group_config(group)
        market_params = {}
        header_flag = True
        market_params['date_time'] = datetime.now(IST).strftime('%Y-%m-%d %H:%M')
    # Generate base factor based on distance from expiry
        dte = (group.expiry.date() - datetime.now(IST).date()).days

        if dte > 20:
            base_factor = 1.5
        elif 15 < dte <= 20:
            base_factor = 1.4
        elif 10 < dte <= 15:
            base_factor = 1.3
        elif 5 < dte <= 10:
            base_factor = 1.2
        else:
            base_factor = 1.0

    # Get IndiaVix
        url = f"{self.api_url}/get_current_price/INDIA VIX/stock"
        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.error(f"Failed to get VIX: {e}")
            self._alert("ApiError", f"Failed to get VIX: {e}")
            return False
        if ok:
            vix_value = payload["NSE:INDIA VIX"]["last_price"]
        #    self.logger.info(f"VIX: {vix_value}")
        else:
            self.logger.error(f"Broker returned failure for VIX: {payload}")
            self._alert("VixFetchFailed", f"Broker returned failure for VIX: {payload}")
            return False

    # Get Index price
        underlying = group.underlying
        if underlying == 'NIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY 50/stock"
            underlying_str = 'NSE:NIFTY 50'
        elif underlying == 'BANKNIFTY':
            url = f"{self.api_url}/get_current_price/NIFTY BANK/stock"
            underlying_str = 'NSE:NIFTY BANK'
        else:
            url = f"{self.api_url}/get_current_price/{underlying}/stock"
            underlying_str = f'NSE:{underlying}'

        try:
            ok, payload = api_get(url, logger=self.logger)
        except ApiError as e:
            self.logger.error(f"Failed to get index price for {underlying_str}: {e}")
            self._alert("ApiError", f"Failed to get index price for {underlying_str}: {e}")
            return False
        if ok:
            index_current_price = int(payload[underlying_str]['last_price'])
            #self.logger.info(f"{underlying_str}: {index_current_price}")
        else:
            self.logger.error(f"Broker returned failure for {underlying_str}: {payload}")
            self._alert("IndexFetchFailed",
                        f"Broker returned failure for {underlying_str}: {payload}")
            return False

    # Generate IndiaVix factor based on INDIAVIX
        if vix_value < 10:
            vix_factor = 0.8
        elif 10 <= vix_value < 12:
            vix_factor = 0.9
        elif 12 <= vix_value < 15:
            vix_factor = 1
        elif 15 <= vix_value < 18:
            vix_factor = 1.2
        elif 18 <= vix_value < 22:
            vix_factor = 1.5
        else:
            vix_factor = 1.6

    # Calculate Stop Loss Factor & Premium
        sl_factor = base_factor * vix_factor
        if group.strategy == 'LONG_STRANGLE':
            sl_factor = 2 - sl_factor
        stop_loss_hit = False
        total_premium_gained = pnl
        stoploss_info = self.config.get('stoploss_info') or {}
        if group.strategy == "IRON_CONDOR":
            total_premium_invested = round((group.quantity / stoploss_info["quantity_per_lot"]) * stoploss_info['investment'], 2)
        elif group.strategy == "SHORT_STRANGLE":
            total_premium_invested = round((group.quantity / stoploss_info["quantity_per_lot"]) * stoploss_info['investment'] * 2, 2)
        else:
            total_premium_invested = 0

        sl_premium = -1 * int((total_premium_invested) * ((sl_factor) / 100))
        self.logger.info(f"SL Factor: {round(sl_factor,2)}, VIX Factor: {vix_factor}, Base factor: {base_factor}")
        #self.logger.info(f"Invested: {int(total_premium_invested)}, Loss/Gaim: {int(total_premium_gained)}")
        #self.logger.info(f"StopLoss: {sl_premium}")
        if total_premium_gained <= sl_premium:
            self.logger.info(f"Stoploss premium {sl_premium} hit. Better close the positions for the day.")
            stop_loss_hit = True

    # Check if the strikes are nearing the underlying index
        nearing_strike = False
        legs = group.get_legs()
        long_put_strike, long_put_symbol, long_put_quantity = legs['long_put']['strike'], legs['long_put']['symbol'], legs['long_put']['quantity']
        short_put_strike, short_put_symbol, short_put_quantity = legs['short_put']['strike'], legs['short_put']['symbol'], legs['short_put']['quantity']
        short_call_strike, short_call_symbol, short_call_quantity = legs['short_call']['strike'], legs['short_call']['symbol'], legs['short_call']['quantity']
        long_call_strike, long_call_symbol, long_call_quantity = legs['long_call']['strike'], legs['long_call']['symbol'], legs['long_call']['quantity']

        put_distance = index_current_price - (short_put_strike if short_put_strike else long_put_strike)
        call_distance = (short_call_strike if short_call_strike else long_call_strike) - index_current_price
        self.logger.info(f"[{group.expiry.date()}] {(short_put_strike if short_put_strike else long_put_strike)}<-----{put_distance}"
                         f"----->{index_current_price}<-----"
                         f"{call_distance}----->{(short_call_strike if short_call_strike else long_call_strike)}")
        adjustment_info = self.config.get('adjustment_info') or {}
        # Min distance from spot is a percent of spot so it scales with
        # NIFTY's absolute level. 1.7% of a 25,000 spot ~ 425 points.
        min_distance_pct = float(adjustment_info.get('minimum_distance_pct', 1.7))
        min_distance = int(round(index_current_price * min_distance_pct / 100))
        if (index_current_price <= (short_put_strike if short_put_strike else long_put_strike) + min_distance
                or index_current_price >= (short_call_strike if short_call_strike else long_call_strike) - min_distance):
            nearing_strike = True
            self.logger.info(
                f"<---- Strikes are neared <= {min_distance} pts "
                f"({min_distance_pct}% of spot {index_current_price}) ---->"
            )
        # Persist for adjustments() to consult on this group.
        self.nearing_strike_by_expiry[group.expiry] = nearing_strike
    # If the strike price is double
        strike_price_doubled = False
        short_put_sell_price, short_put_last_price = next(
            ((p['sell_price'], p['last_price']) for p in group.positions
             if p['option_type'] == 'PE' and p['transtype'] == 'sell'),
            (0, 0)
        )

        short_call_sell_price, short_call_last_price = next(
            ((p['sell_price'], p['last_price']) for p in group.positions
             if p['option_type'] == 'CE' and p['transtype'] == 'sell'),
            (0, 0)
        )

        if (short_call_last_price >= 2 * short_call_sell_price) or (short_put_last_price >= 2 * short_put_sell_price):
            strike_price_doubled = True
            self.logger.info(f"<------ Prices are doubled ------>")

        market_params['sl_factor'] = round(sl_factor, 2)
        market_params['vix_factor'] = round(vix_factor, 2)
        market_params['base_factor'] = round(base_factor, 2)
        market_params['premium_invested'] = int(total_premium_invested)
        market_params['premium_gained'] = int(total_premium_gained)
        market_params['sl_premium'] = int(sl_premium)
        market_params['short_pe_strike'] = short_put_strike
        market_params['short_pe_sell_price'] = short_put_sell_price
        market_params['short_pe_last_price'] = short_put_last_price
        market_params['put_distance'] = put_distance
        market_params['short_ce_strike'] = short_call_strike
        market_params['short_ce_sell_price'] = short_call_sell_price
        market_params['short_ce_last_price'] = short_call_last_price
        market_params['call_distance'] = call_distance

        market_params_df = pd.DataFrame([market_params])
        #market_params_df.to_csv(f"markets_{datetime.now(IST).date()}.csv", header=header_flag, mode='a',
        #                        quoting=csv.QUOTE_NONNUMERIC, index=False)

        if stop_loss_hit and nearing_strike and strike_price_doubled and group.strategy != 'LONG_STRANGLE':
            self.logger.info(f"### Close the strikes as Stop Loss Hit is {stop_loss_hit} and Nifty reaching the strikes is {nearing_strike} ###")
            try:
                if group_cfg['close_on_stoploss']:
                    self.logger.info(f"Closing the strikes for {group.strategy} ({group.expiry.date()}) because of Stoploss hit")
                    self._alert("StopLoss",
                                f"Closing the strikes for {group.strategy} ({group.expiry.date()}) because of Stoploss hit")
                    success = False
                    if group.strategy in ('SHORT_STRANGLE', 'IRON_CONDOR'):
                        success = self._place_order(symbol=short_call_symbol,
                                                    quantity=short_call_quantity,
                                                    transaction_type="BUY",
                                                    exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self._place_order(symbol=short_put_symbol,
                                                        quantity=short_put_quantity,
                                                        transaction_type="BUY",
                                                        exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                    if group.strategy == 'IRON_CONDOR' \
                            and group_cfg['adjust_hedges'] \
                            and success:
                        success = self._place_order(symbol=long_call_symbol,
                                                    quantity=long_call_quantity,
                                                    transaction_type="SELL",
                                                    exchange='NFO')
                        self.logger.info(success)
                        time.sleep(2)
                        if success:
                            success = self._place_order(symbol=long_put_symbol,
                                                        quantity=long_put_quantity,
                                                        transaction_type="SELL",
                                                        exchange='NFO')
                            self.logger.info(success)
                    return success
                else:
                    self.logger.info(f"Not closing the positions as the indicator is {group_cfg['close_on_stoploss']} in {self.processor._group_block_name(group)}")
            except Exception as e:
                self.logger.exception(f"Failed to close positions during stop-loss for {group.strategy}: {e}")
                self._alert(
                    "StopLossCloseFailed",
                    f"Exception while closing {group.strategy} ({group.expiry.date()}) for stop-loss: {e}. "
                    f"Positions may be partially closed — verify manually."
                )
        return False

    def trail_profit(self, group, pnl):
        """Per-group trailing-profit check.

        ``lock_profit`` and ``trail_profit_hit_count`` are kept in
        ``*_by_expiry`` dicts keyed on ``group.expiry`` so each open book
        runs its own high-water mark independently. ``close_on_trailprofit``
        and ``adjust_hedges`` are read off the group's per-week config block.
        """
        group_cfg = self.processor.get_group_config(group)
        trailprofit_info = self.config.get('trailprofit_info') or {}
        trail_profit = round(group.quantity * trailprofit_info['trailing_profit_multiplier'], 2)

        lock_profit = self.lock_profit_by_expiry.get(group.expiry, 0)
        hit_count = self.trail_profit_hit_count_by_expiry.get(group.expiry, 0)

        self.logger.info(f"[{group.expiry.date()}] Current Profit: {pnl}, Locked Profit: {lock_profit}, Trail Profit: {trail_profit}")

        if pnl <= lock_profit and lock_profit > 0:
            hit_count += 1
            self.trail_profit_hit_count_by_expiry[group.expiry] = hit_count
            self.logger.info(f"Trailing profit hit count: {hit_count}")

            if hit_count > trailprofit_info['trail_profit_threshold']:
                self.logger.info("Closing the positions")

                legs = group.get_legs()
                long_put_strike, long_put_symbol, long_put_quantity = legs['long_put']['strike'], legs['long_put']['symbol'], legs['long_put']['quantity']
                short_put_strike, short_put_symbol, short_put_quantity = legs['short_put']['strike'], legs['short_put']['symbol'], legs['short_put']['quantity']
                short_call_strike, short_call_symbol, short_call_quantity = legs['short_call']['strike'], legs['short_call']['symbol'], legs['short_call']['quantity']
                long_call_strike, long_call_symbol, long_call_quantity = legs['long_call']['strike'], legs['long_call']['symbol'], legs['long_call']['quantity']

                try:
                    if group_cfg['close_on_trailprofit']:
                        success = False
                        self._alert("TrailProfit",
                                    f"Closing the strikes for {group.strategy} ({group.expiry.date()}) because of trailprofit hit")
                        if group.strategy in ('SHORT_STRANGLE', 'IRON_CONDOR'):
                            success = self._place_order(symbol=short_call_symbol,
                                                        quantity=short_call_quantity,
                                                        transaction_type="BUY",
                                                        exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                            if success:
                                success = self._place_order(symbol=short_put_symbol,
                                                            quantity=short_put_quantity,
                                                            transaction_type="BUY",
                                                            exchange='NFO')
                                self.logger.info(success)
                                time.sleep(2)

                        if group.strategy == 'IRON_CONDOR' \
                                and group_cfg['adjust_hedges'] \
                                and success:
                            success = self._place_order(symbol=long_call_symbol,
                                                        quantity=long_call_quantity,
                                                        transaction_type="SELL",
                                                        exchange='NFO')
                            self.logger.info(success)
                            time.sleep(2)
                            if success:
                                success = self._place_order(symbol=long_put_symbol,
                                                            quantity=long_put_quantity,
                                                            transaction_type="SELL",
                                                            exchange='NFO')
                                self.logger.info(success)
                        self.lock_profit_by_expiry[group.expiry] = 0
                        self.trail_profit_hit_count_by_expiry[group.expiry] = 0
                        return True
                except Exception as e:
                    self.logger.exception(f"Failed to close positions during trail-profit for {group.strategy}: {e}")
                    self._alert(
                        "TrailProfitCloseFailed",
                        f"Exception while closing {group.strategy} ({group.expiry.date()}) for trail-profit: {e}. "
                        f"Positions may be partially closed — verify manually."
                    )

        else:
            if hit_count != 0:
                self.logger.info("Trailing profit hit count reset")
            self.trail_profit_hit_count_by_expiry[group.expiry] = 0

        if pnl >= (lock_profit + trail_profit):
            new_lock = max(lock_profit, pnl - trail_profit)
            self.lock_profit_by_expiry[group.expiry] = new_lock
            self.logger.info(f"Locking Profit: {new_lock}")
        else:
            self.logger.info(
                f"Will lock when P&L: {pnl} >= {lock_profit + trail_profit}"
            )

        return False

    _STRIKE_STEP = 50  # NIFTY uses 50-point strike intervals
    _MAX_WALK_STEPS = 30  # safety cap when walking strikes toward ATM

    def _strike_at_delta(self, current_symbol, current_strike, opt_type,
                         target_abs_delta, direction,
                         spot_price=None, min_distance=None):
        """Walk strikes from ``current_strike`` looking for one whose
        ``|delta|`` meets the target.

        ``direction='inward'`` (toward ATM): PE walks UP, CE walks DOWN.
            Matches the first strike where ``|delta| >= target_abs_delta``.
            Used when a short leg drifted too far OTM and needs to be
            brought closer to ATM.

        ``direction='outward'`` (away from ATM): PE walks DOWN, CE walks UP.
            Matches the first strike where ``|delta| <= target_abs_delta``.
            Used when a short leg got too close to ATM and needs to be
            backed off.

        Per-strike delta is read off ``/get_current_price`` which serves
        from the DB snapshot table when the recorder has covered the strike.
        Strikes outside the recorder's delta band come back with
        ``delta=None`` and the walk skips them — never matched as a target.

        When ``spot_price`` and ``min_distance`` are both supplied AND
        the direction is ``'inward'``, the walk also stops the first
        time a candidate strike would come within ``min_distance`` of
        spot — i.e., the rule "don't bring the gaining leg too close to
        spot" is enforced on the leg actively being moved. Outward
        walks ignore the boundary (they move AWAY from spot).
        """
        if direction == 'inward':
            step_signed = self._STRIKE_STEP if opt_type == 'PE' else -self._STRIKE_STEP
            def meets(d): return d >= target_abs_delta
            cmp_sym = '>='
        elif direction == 'outward':
            step_signed = -self._STRIKE_STEP if opt_type == 'PE' else self._STRIKE_STEP
            def meets(d): return d <= target_abs_delta
            cmp_sym = '<='
        else:
            raise ValueError(f"direction must be 'inward' or 'outward', got {direction!r}")

        # Inward-only spot boundary: PE strike must stay <= spot - min_dist,
        # CE strike must stay >= spot + min_dist. Stops the walk before
        # the first strike that would cross.
        enforce_boundary = (
            direction == 'inward'
            and spot_price is not None
            and min_distance is not None
        )

        for n in range(1, self._MAX_WALK_STEPS + 1):
            new_strike = current_strike + step_signed * n
            if enforce_boundary:
                crosses = (opt_type == 'PE' and new_strike > spot_price - min_distance) or \
                          (opt_type == 'CE' and new_strike < spot_price + min_distance)
                if crosses:
                    self.logger.info(
                        f"  delta-walk (inward): {new_strike} would come within "
                        f"{min_distance} pts of spot {spot_price}; stopping "
                        f"walk before crossing the safety boundary"
                    )
                    return None
            new_symbol = current_symbol.replace(str(current_strike), str(new_strike))
            url = f"{self.api_url}/get_current_price/{new_symbol}/option"
            try:
                ok, payload = api_get(url, logger=self.logger)
            except ApiError as e:
                self.logger.warning(f"  delta-walk: {new_symbol} fetch failed: {e}")
                continue
            if not ok:
                self.logger.info(f"  delta-walk: {new_symbol} broker reported failure")
                continue
            quote = payload.get(f"NFO:{new_symbol}", {})
            delta = quote.get('delta')
            if delta is None:
                self.logger.info(f"  delta-walk: {new_symbol} delta unknown — skipping")
                continue
            abs_d = abs(delta)
            if meets(abs_d):
                self.logger.info(
                    f"  delta-walk ({direction}): {new_symbol} |delta|={abs_d:.3f} "
                    f"{cmp_sym} {target_abs_delta:.2f} — selected"
                )
                return new_symbol, new_strike
            self.logger.info(
                f"  delta-walk ({direction}): {new_symbol} |delta|={abs_d:.3f} "
                f"not {cmp_sym} {target_abs_delta:.2f} — continuing"
            )
        return None

    def _roll_short_leg(self, side, short_leg, hedge_leg, target_abs_delta,
                        direction, strategy, adjust_hedges,
                        spot_price=None, min_distance=None, group=None):
        """Buy back the short leg and sell a new strike whose ``|delta|``
        meets the target.

        ``direction='inward'`` walks toward ATM (used when the short drifted
        too far OTM); ``direction='outward'`` walks away from ATM (used when
        the short got too close to ATM). When the strategy is IRON_CONDOR
        and ``adjust_hedges`` is set, the long hedge is rolled by the same
        strike distance so the spread width is preserved in either case.

        When ``group`` is supplied, a row is written to the ``adjustments``
        audit table after all orders succeed — with order_ids harvested
        from each ``_place_order`` call so the snapshot UI can join back
        to ``kite_trades`` and show what actually filled.
        """
        found = self._strike_at_delta(
            current_symbol=short_leg['symbol'],
            current_strike=short_leg['strike'],
            opt_type=side,
            target_abs_delta=target_abs_delta,
            direction=direction,
            spot_price=spot_price,
            min_distance=min_distance,
        )
        if not found:
            msg = (f"No {side} strike found within {self._MAX_WALK_STEPS} steps "
                   f"of {short_leg['symbol']} with |delta| >= {target_abs_delta:.2f}; "
                   f"holding existing position")
            self.logger.error(msg)
            self._alert("AdjustmentSkipped", msg)
            return

        new_short_symbol, new_short_strike = found
        strike_shift = new_short_strike - short_leg['strike']

        # Quantity for the new legs. INWARD rolls stay 1:1 with the
        # existing same-side qty — repositioning, not resizing. OUTWARD
        # rolls instead size the new legs to match the OPPOSITE side's
        # short qty, restoring the iron condor's symmetry when the
        # losing side has been previously trimmed by partial-reduce.
        # Falls back to same-side qty when there's no opposite leg to
        # reference (e.g. a one-sided strangle).
        new_short_qty = int(short_leg['quantity'])
        new_hedge_qty = (int(hedge_leg['quantity'])
                         if hedge_leg.get('quantity') else None)
        if direction == 'outward' and group is not None:
            all_legs = group.get_legs()
            opp_key = 'short_call' if side == 'PE' else 'short_put'
            opp = all_legs.get(opp_key) or {}
            opp_qty = opp.get('quantity')
            if opp_qty and int(opp_qty) > 0 and int(opp_qty) != new_short_qty:
                self.logger.info(
                    f"Outward roll: sizing new {side} legs to opposite "
                    f"{opp_key} qty {int(opp_qty)} (existing {side} short "
                    f"was {new_short_qty})"
                )
                new_short_qty = int(opp_qty)
                new_hedge_qty = int(opp_qty)

        self.logger.info(
            f"Rolling {side} short: {short_leg['symbol']}@{short_leg['strike']} -> "
            f"{new_short_symbol}@{new_short_strike} (shift {strike_shift:+d}, "
            f"new qty {new_short_qty})"
        )
        self._alert(
            "Adjustment",
            f"Rolling {side} short {short_leg['symbol']} -> {new_short_symbol}"
        )

        # Look up the avg entry prices on the about-to-close legs so we
        # can compute realised P&L for the audit row. Skip silently if
        # the broker record can't be found — we'll still write the row
        # with realized_pnl=None.
        short_pos = None
        hedge_pos = None
        if group is not None:
            short_pos = next(
                (p for p in group.positions
                 if p.get('tradingsymbol') == short_leg['symbol']),
                None,
            )
            hedge_pos = next(
                (p for p in group.positions
                 if p.get('tradingsymbol') == hedge_leg.get('symbol')),
                None,
            )

        order_ids = []
        new_hedge_strike = None
        new_hedge_symbol = None

        # 1. Buy back the existing short leg
        result = self._place_order(symbol=short_leg['symbol'],
                                   quantity=short_leg['quantity'],
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            return
        order_ids.append(str(result))

        # 2-3. Roll the hedge by the same strike distance (iron condor only)
        hedge_needed = (
            strategy == 'IRON_CONDOR'
            and adjust_hedges
            and hedge_leg['symbol']
        )
        if hedge_needed:
            new_hedge_strike = int(hedge_leg['strike']) + strike_shift
            new_hedge_symbol = hedge_leg['symbol'].replace(
                str(hedge_leg['strike']), str(new_hedge_strike)
            )
            # Inward AND outward rolls: the simple "preserve spread
            # width" math can land on a far-OTM strike that the
            # exchange hasn't listed, which makes the broker reject
            # the BUY order. (Outward rolls move the hedge further
            # OTM and are actually MORE susceptible to this.) Verify
            # the proposed strike has a tradable LTP at the broker;
            # if not, step ONE strike at a time toward the new short
            # — preserving as much of the original spread width as
            # possible — until a tradable strike is found or we hit
            # ``short ± STRIKE_STEP`` (the safety floor).
            #
            # Example: CE short=24700, proposed hedge=24900. If 24900
            # has no LTP, we try 24850, then 24800, …, stopping at
            # 24750 (one strike from the short).
            quote = self._fetch_option_quote(new_hedge_symbol)
            if quote is None:
                # Transport / broker error — can't verify. Keep the
                # proposed strike and let the place_order retry
                # path catch a genuine "strike missing" rejection.
                self.logger.info(
                    f"  {direction.capitalize()} roll: LTP check for "
                    f"{new_hedge_symbol} could not complete; "
                    f"proceeding with proposed strike"
                )
            elif not quote.get('last_price'):
                # Direction "toward short": PE hedge moves UP (the
                # short sits at a HIGHER strike); CE hedge moves DOWN
                # (the short sits LOWER). Same regardless of whether
                # this is an inward or outward roll.
                step_toward_short = (
                    self._STRIKE_STEP if side == 'PE'
                    else -self._STRIKE_STEP
                )
                short_floor = (
                    new_short_strike - self._STRIKE_STEP if side == 'PE'
                    else new_short_strike + self._STRIKE_STEP
                )
                proposed_strike_orig = new_hedge_strike
                proposed_symbol_orig = new_hedge_symbol
                candidate_strike = new_hedge_strike
                candidate_symbol = new_hedge_symbol
                steps = 0
                # Hard cap so a misconfigured grid can't infinite-loop.
                # Practically the short_floor crossing kicks in first.
                for _ in range(self._MAX_WALK_STEPS):
                    next_strike = candidate_strike + step_toward_short
                    crossed = (
                        (side == 'PE' and next_strike > short_floor) or
                        (side == 'CE' and next_strike < short_floor)
                    )
                    if crossed:
                        self.logger.info(
                            f"  {direction.capitalize()} roll: reached "
                            f"safety floor at {candidate_strike} "
                            f"(1 strike from new short {new_short_strike}); "
                            f"using as final fallback even though no "
                            f"LTP was confirmed beyond"
                        )
                        break
                    candidate_strike = next_strike
                    candidate_symbol = proposed_symbol_orig.replace(
                        str(proposed_strike_orig), str(candidate_strike)
                    )
                    steps += 1
                    next_quote = self._fetch_option_quote(candidate_symbol)
                    if next_quote is None:
                        # Transport error mid-walk — bail to last candidate.
                        self.logger.info(
                            f"  {direction.capitalize()} roll: LTP check "
                            f"for {candidate_symbol} could not complete "
                            f"mid-walk; using as fallback"
                        )
                        break
                    if next_quote.get('last_price'):
                        break  # tradable strike found
                self.logger.info(
                    f"  {direction.capitalize()} roll: proposed hedge "
                    f"{proposed_symbol_orig}@{proposed_strike_orig} not "
                    f"tradable; stepped {steps} strike(s) toward short to "
                    f"{candidate_symbol}@{candidate_strike}"
                )
                new_hedge_strike = candidate_strike
                new_hedge_symbol = candidate_symbol
            self.logger.info(
                f"Rolling {side} hedge: {hedge_leg['symbol']}@{hedge_leg['strike']} -> "
                f"{new_hedge_symbol}@{new_hedge_strike}"
            )
            result = self._place_order(symbol=hedge_leg['symbol'],
                                       quantity=hedge_leg['quantity'],
                                       transaction_type="SELL",
                                       exchange='NFO')
            self.logger.info(result)
            time.sleep(2)
            if not result:
                return
            order_ids.append(str(result))
            # New hedge sized to ``new_hedge_qty`` (set above): same as
            # the existing hedge for inward rolls; matches the opposite
            # side's short qty for outward rolls.
            result = self._place_order(symbol=new_hedge_symbol,
                                       quantity=(new_hedge_qty
                                                 if new_hedge_qty
                                                 else hedge_leg['quantity']),
                                       transaction_type="BUY",
                                       exchange='NFO')
            self.logger.info(result)
            time.sleep(2)
            if not result:
                return
            order_ids.append(str(result))

        # 4. Sell the new short leg (sized to ``new_short_qty``)
        result = self._place_order(symbol=new_short_symbol,
                                   quantity=new_short_qty,
                                   transaction_type="SELL",
                                   exchange='NFO')
        self.logger.info(result)
        if not result:
            return
        order_ids.append(str(result))

        # Audit row — write only when every order succeeded. realized_pnl
        # is computed from the broker's avg entry prices on the just-
        # closed legs vs their last_price at adjustment time; the new
        # legs are open, so they don't contribute to realised yet.
        if group is not None:
            realized_pnl = None
            try:
                qty = abs(int(short_leg.get('quantity') or 0))
                rp = 0.0
                if short_pos and short_pos.get('sell_quantity'):
                    short_avg_sell = float(short_pos['sell_value']) / float(short_pos['sell_quantity'])
                    rp += (short_avg_sell - float(short_leg.get('last_price') or 0)) * qty
                if hedge_needed and hedge_pos and hedge_pos.get('buy_quantity'):
                    hedge_avg_buy = float(hedge_pos['buy_value']) / float(hedge_pos['buy_quantity'])
                    hqty = abs(int(hedge_leg.get('quantity') or 0))
                    rp += (float(hedge_leg.get('last_price') or 0) - hedge_avg_buy) * hqty
                realized_pnl = float(rp)
            except (TypeError, ValueError, ZeroDivisionError) as e:
                self.logger.info(
                    f"Could not compute realized_pnl for audit row: {e}"
                )

            self._record_adjustment_safely(
                timestamp=datetime.now(IST).isoformat(),
                user=self.user,
                underlying=group.underlying,
                expiry=group.expiry.date().isoformat(),
                side=side,
                adjustment_type=direction,  # 'inward' or 'outward'
                short_strike_before=int(short_leg['strike']),
                short_strike_after=int(new_short_strike),
                short_delta_before=float(short_leg.get('delta') or 0),
                short_delta_after=None,
                hedge_strike_before=int(hedge_leg['strike']) if hedge_leg.get('strike') else None,
                hedge_strike_after=int(new_hedge_strike) if new_hedge_strike is not None else None,
                hedge_delta_before=float(hedge_leg.get('delta') or 0) if hedge_leg.get('delta') is not None else None,
                hedge_delta_after=None,
                nifty_spot=float(spot_price) if spot_price else None,
                india_vix=self._india_vix(),
                order_ids=",".join(order_ids),
                realized_pnl=realized_pnl,
            )

    def _partial_reduce_losing_side(self, side, short_leg, hedge_leg, group):
        """Funding-conditional partial close of a losing iron-condor side.

        Fires when ``|short_delta|`` sits between ``delta_partial_close``
        and ``delta_high_close`` AND the hedge has accumulated enough
        profit to cover at least one lot of short-buyback loss. Outcome:
        a smaller short and a smaller, tighter hedge — the realised P&L
        of steps (a)+(b) is ≈ 0 by construction, step (c) is a fresh
        debit paid for forward protection of the remaining short.

        Mechanics (in order):
          a. Close the hedge fully (SELL — it's a long).
          b. Partial-close ``q1`` of the short (BUY back), where
             ``q1 = floor(hedge_profit / per_share_short_loss / lot_size)
             * lot_size``. Lot-aligned, rounded DOWN so the funding
             constraint is conservative.
          c. Open a new hedge at strike ±``_STRIKE_STEP`` (one step
             closer to the short) sized to FULLY COVER the residual
             short (``short_open_qty − q1``) — so the post-trade
             shape is a smaller short with a 1:1 hedge ratio, not the
             partial-protection shape an earlier version had. The only
             cap is the short-floor: the new hedge must remain on its
             proper side of the short (PE hedge < short, CE hedge >
             short) with at least one strike of gap. ``minimum_distance``
             from spot is NOT enforced here — partial-reduce trusts
             that the trader wants to keep tightening protection even
             when spot is close.

        Defensive skips (log + no orders): missing broker rows, zero
        open quantity on either leg, ``q1`` rounds to 0 lots, ``q1``
        would fully close the short (defer to the high-delta path), or
        the existing hedge is already at/past the short-floor (no room
        to move closer).
        """
        short_pos = next(
            (p for p in group.positions
             if p.get('tradingsymbol') == short_leg['symbol']),
            None,
        )
        hedge_pos = next(
            (p for p in group.positions
             if p.get('tradingsymbol') == hedge_leg['symbol']),
            None,
        )
        if not short_pos or not hedge_pos:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"missing broker position(s); skipping"
            )
            return

        sell_value = float(short_pos.get('sell_value') or 0)
        sell_qty_filled = float(short_pos.get('sell_quantity') or 0)
        buy_value = float(hedge_pos.get('buy_value') or 0)
        buy_qty_filled = float(hedge_pos.get('buy_quantity') or 0)
        if sell_qty_filled <= 0 or buy_qty_filled <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"no buy/sell history on a leg; skipping"
            )
            return

        short_avg_sell = sell_value / sell_qty_filled
        hedge_avg_buy = buy_value / buy_qty_filled
        short_last = float(short_pos.get('last_price') or 0)
        hedge_last = float(hedge_pos.get('last_price') or 0)
        short_open_qty = abs(int(short_pos.get('quantity') or 0))
        hedge_open_qty = abs(int(hedge_pos.get('quantity') or 0))
        if short_open_qty <= 0 or hedge_open_qty <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"a leg has zero open qty; skipping"
            )
            return

        per_share_short_loss = short_last - short_avg_sell
        per_share_hedge_profit = hedge_last - hedge_avg_buy
        short_total_loss = per_share_short_loss * short_open_qty
        hedge_total_profit = per_share_hedge_profit * hedge_open_qty

        stoploss_info = self.config.get('stoploss_info') or {}
        lot_size = int(stoploss_info.get('quantity_per_lot') or 0)
        if lot_size <= 0:
            self.logger.warning(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"stoploss_info.quantity_per_lot is missing; skipping"
            )
            return
        per_lot_short_loss = per_share_short_loss * lot_size

        # Gate 1: side must be net-losing (else nothing to fix here).
        if short_total_loss <= hedge_total_profit:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"side is net-profitable (short loss {short_total_loss:+.2f} "
                f"<= hedge profit {hedge_total_profit:+.2f}); skipping"
            )
            return
        # Gate 2: hedge profit must cover ≥ one lot of short loss. This
        # gate naturally rate-limits repeat triggers: right after a
        # successful partial-reduce the new hedge has ~0 profit, so the
        # next cycle skips here. The next trigger only fires once the
        # freshly-opened (smaller, nearer) hedge has appreciated enough
        # to fund another lot of short buyback.
        if hedge_total_profit <= per_lot_short_loss:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"hedge profit ({hedge_total_profit:+.2f}) doesn't cover one lot of "
                f"short loss ({per_lot_short_loss:+.2f}); skipping"
            )
            return
        # Gate 3: per-share short loss must be positive (sanity — if it's
        # not, the side isn't actually losing and earlier gates should
        # have caught it).
        if per_share_short_loss <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"per-share short loss non-positive ({per_share_short_loss:.2f}); skipping"
            )
            return

        # q1 (shares) where partial-short-loss == hedge-profit. Round
        # DOWN to lot-aligned shares (conservative — funded fully).
        q1_raw = hedge_total_profit / per_share_short_loss
        q1_lots = int(q1_raw // lot_size)
        q1 = q1_lots * lot_size
        if q1 <= 0:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"q1 ({q1_raw:.2f}) rounds to 0 lots; skipping"
            )
            return
        if q1 >= short_open_qty:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"q1 ({q1}) >= short open qty ({short_open_qty}); "
                f"deferring to high-delta close path"
            )
            return

        # New hedge strike: one STRIKE_STEP nearer the short, with the
        # ONE constraint that the hedge stays on its proper side of
        # the short with at least one strike of gap (PE hedge < short,
        # CE hedge > short). No spot / min_distance check — partial-
        # reduce intentionally keeps tightening protection even when
        # spot is close. Repeated cycles ratchet the hedge inward
        # until the short-floor is hit; once there, the gap doesn't
        # shrink further and subsequent triggers reduce the hedge's
        # quantity at the same strike.
        step = self._STRIKE_STEP if side == 'PE' else -self._STRIKE_STEP
        proposed_strike = int(hedge_leg['strike']) + step
        short_strike = int(short_leg['strike'])
        existing_hedge_strike = int(hedge_leg['strike'])
        if side == 'PE':
            short_floor = short_strike - self._STRIKE_STEP
            new_hedge_strike = min(proposed_strike, short_floor)
        else:
            short_floor = short_strike + self._STRIKE_STEP
            new_hedge_strike = max(proposed_strike, short_floor)

        # Defensive: if the short-floor would force the hedge AWAY from
        # the short (further OTM than where it already sits), the
        # existing hedge is already at/past the floor. Skip — there's
        # no room to move closer without crossing.
        moves_backward = (
            (side == 'PE' and new_hedge_strike < existing_hedge_strike) or
            (side == 'CE' and new_hedge_strike > existing_hedge_strike)
        )
        if moves_backward:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"short-floor ({short_floor}) would move hedge backward "
                f"from {existing_hedge_strike} to {new_hedge_strike}; skipping"
            )
            return

        if new_hedge_strike != proposed_strike:
            self.logger.info(
                f"  [{group.expiry.date()}] {side} partial-reduce: "
                f"proposed hedge @{proposed_strike} capped to @{new_hedge_strike} "
                f"(short ± {self._STRIKE_STEP}pt floor)"
            )
        new_hedge_symbol = hedge_leg['symbol'].replace(
            str(hedge_leg['strike']), str(new_hedge_strike)
        )

        self.logger.info(
            f"  [{group.expiry.date()}] {side} partial-reduce TRIGGERED: "
            f"|delta|={abs(short_leg['delta']):.3f}, "
            f"short loss={short_total_loss:+.2f}, hedge profit={hedge_total_profit:+.2f}, "
            f"per-lot short loss={per_lot_short_loss:+.2f}; "
            f"closing hedge {hedge_open_qty}, partial-closing short {q1}/{short_open_qty}, "
            f"reopening hedge {new_hedge_symbol}@{new_hedge_strike} qty {short_open_qty - q1}"
        )
        self._alert(
            "PartialReduce",
            f"{side} partial-reduce ({group.expiry.date()}): "
            f"close hedge {hedge_open_qty}, partial-close short {q1}, "
            f"reopen hedge {new_hedge_symbol} qty {short_open_qty - q1}"
        )

        order_ids = []

        # (a) Close the hedge fully — it's a long, so SELL.
        result = self._place_order(symbol=hedge_leg['symbol'],
                                   quantity=hedge_open_qty,
                                   transaction_type="SELL",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            return
        order_ids.append(str(result))

        # (b) Partial-close the short — it's a short, so BUY back q1.
        result = self._place_order(symbol=short_leg['symbol'],
                                   quantity=q1,
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        time.sleep(2)
        if not result:
            return
        order_ids.append(str(result))

        # (c) Open the new hedge sized to fully cover the remaining
        # short ((short_open_qty − q1)), at the new (nearer) strike.
        # Earlier this matched the close-pair (q1) which left the
        # leftover short uncovered; the post-trade shape now is a
        # smaller short with a 1:1 hedge ratio on the residual qty.
        new_hedge_qty = short_open_qty - q1
        result = self._place_order(symbol=new_hedge_symbol,
                                   quantity=new_hedge_qty,
                                   transaction_type="BUY",
                                   exchange='NFO')
        self.logger.info(result)
        if not result:
            return
        order_ids.append(str(result))

        # Audit row: realized P&L on the close pair is ≈ 0 by
        # construction (q1 sized to make hedge-gain ≈ partial-short-loss),
        # but compute it from the snapshot prices so any rounding shows.
        realized_pnl = (per_share_hedge_profit * hedge_open_qty
                        - per_share_short_loss * q1)
        self._record_adjustment_safely(
            timestamp=datetime.now(IST).isoformat(),
            user=self.user,
            underlying=group.underlying,
            expiry=group.expiry.date().isoformat(),
            side=side,
            adjustment_type='partial_reduce',
            short_strike_before=int(short_leg['strike']),
            short_strike_after=int(short_leg['strike']),  # unchanged
            short_delta_before=float(short_leg.get('delta') or 0),
            short_delta_after=None,  # next cycle will see updated delta
            hedge_strike_before=int(hedge_leg['strike']),
            hedge_strike_after=int(new_hedge_strike),
            hedge_delta_before=float(hedge_leg.get('delta') or 0),
            hedge_delta_after=None,
            # Fetch spot just for the audit-row context — partial-reduce
            # no longer needs it for any sizing/placement decision.
            nifty_spot=self._spot_price(group.underlying),
            india_vix=self._india_vix(),
            order_ids=",".join(order_ids),
            realized_pnl=float(realized_pnl),
        )

    def adjustments(self, group):
        """Per-group delta-driven adjustments.

        Three triggers, all in percent via ``adjustment_info`` in
        ``Monitor/config.yaml``:

        - **Low-delta (too far OTM):** |delta| <= ``delta_low_close``
          rolls the short INWARD to a strike with |delta| >=
          ``delta_low_open``. Hedge follows by the same strike distance
          when ``adjust_hedges`` is set.
        - **Mid-high (partial-reduce window):** ``delta_partial_close``
          <= |delta| < ``delta_high_close``. Fires the partial-reduce
          path (see ``_partial_reduce_losing_side``) iff the side is
          net-losing AND the hedge has enough profit to cover at least
          one lot of partial short close. Otherwise the leg is left
          alone for this cycle.
        - **High-delta (close + reopen wide):** |delta| >=
          ``delta_high_close`` rolls the short OUTWARD to |delta| <=
          ``delta_high_open``, full close + reopen.

        Legs whose |delta| sits inside the band ``(delta_low_close,
        delta_partial_close)`` are left alone. The ``adjustment`` master
        switch and ``adjust_hedges`` are read off the group's per-week
        config block — so one week can be in active adjustment mode
        while another is frozen. Reads the per-expiry ``nearing_strike``
        flag set by ``check_stop_loss(group)`` so the skip-when-near-
        strike check is scoped to this group only.
        """
        group_cfg = self.processor.get_group_config(group)
        if not group_cfg.get('adjustment'):
            return

        adjustment_info = self.config.get('adjustment_info') or {}
        delta_low_close = adjustment_info.get('delta_low_close', 5) / 100.0
        delta_low_open = adjustment_info.get('delta_low_open', 10) / 100.0
        delta_partial_close = adjustment_info.get('delta_partial_close', 20) / 100.0
        delta_high_close = adjustment_info.get('delta_high_close', 25) / 100.0
        delta_high_open = adjustment_info.get('delta_high_open', 20) / 100.0
        min_distance_pct = float(adjustment_info.get('minimum_distance_pct', 1.7))
        adjust_hedges = bool(group_cfg.get('adjust_hedges'))
        legs = group.get_legs()

        # min_distance is now enforced PER SIDE inside the inward roll
        # walk and partial-reduce hedge placement — not as a global gate.
        # Outward rolls move legs AWAY from spot, so the rule is silent
        # there. Fetch spot once at the top so both branches share it,
        # then derive the absolute boundary as a percent of spot so it
        # scales with NIFTY's level.
        spot_price = self._spot_price(group.underlying) if min_distance_pct > 0 else None
        if min_distance_pct > 0 and spot_price is None:
            self.logger.warning(
                f"[{group.expiry.date()}] Adjustments: spot fetch failed; "
                f"min_distance checks will be skipped this cycle"
            )
            min_distance = 0
        elif spot_price is not None:
            min_distance = int(round(spot_price * min_distance_pct / 100))
            self.logger.info(
                f"[{group.expiry.date()}] Adjustments: min_distance = "
                f"{min_distance} pts ({min_distance_pct}% of spot {spot_price})"
            )
        else:
            min_distance = 0

        for side, short_key, hedge_key in (('PE', 'short_put',  'long_put'),
                                           ('CE', 'short_call', 'long_call')):
            short_leg = legs[short_key]
            hedge_leg = legs[hedge_key]

            if not short_leg['symbol']:
                continue
            if short_leg.get('delta') is None:
                self.logger.info(
                    f"Skipping {side} adjustment: delta unknown for "
                    f"{short_leg['symbol']} (strike outside recorder band)"
                )
                continue

            abs_delta = abs(short_leg['delta'])

            if abs_delta <= delta_low_close:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"<= {delta_low_close:.2f} — rolling INWARD to |delta| >= {delta_low_open:.2f}"
                )
                try:
                    self._roll_short_leg(side, short_leg, hedge_leg,
                                         delta_low_open, 'inward',
                                         group.strategy, adjust_hedges,
                                         spot_price=spot_price,
                                         min_distance=min_distance,
                                         group=group)
                except Exception as e:
                    self.logger.exception(f"Failed to roll {side} short: {e}")
                    self._alert(
                        "AdjustmentFailed",
                        f"Roll {side} failed for {short_leg['symbol']} "
                        f"({group.expiry.date()}): {e}. Position may be "
                        f"partially adjusted — verify manually."
                    )
            elif abs_delta >= delta_high_close:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f">= {delta_high_close:.2f} — rolling OUTWARD to |delta| <= {delta_high_open:.2f}"
                )
                try:
                    self._roll_short_leg(side, short_leg, hedge_leg,
                                         delta_high_open, 'outward',
                                         group.strategy, adjust_hedges,
                                         group=group)
                except Exception as e:
                    self.logger.exception(f"Failed to roll {side} short: {e}")
                    self._alert(
                        "AdjustmentFailed",
                        f"Roll {side} failed for {short_leg['symbol']} "
                        f"({group.expiry.date()}): {e}. Position may be "
                        f"partially adjusted — verify manually."
                    )
            elif abs_delta >= delta_partial_close:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"in [{delta_partial_close:.2f}, {delta_high_close:.2f}) — "
                    f"attempting partial-reduce (funding-conditional)"
                )
                try:
                    self._partial_reduce_losing_side(side, short_leg, hedge_leg, group)
                except Exception as e:
                    self.logger.exception(
                        f"Partial-reduce failed for {side}: {e}"
                    )
                    self._alert(
                        "AdjustmentFailed",
                        f"{side} partial-reduce failed for {group.expiry.date()}: {e}. "
                        f"Position may be partially adjusted — verify manually."
                    )
            else:
                self.logger.info(
                    f"[{group.expiry.date()}] {side} short {short_leg['symbol']} |delta|={abs_delta:.3f} "
                    f"within band ({delta_low_close:.2f}, {delta_partial_close:.2f}) — no roll"
                )
