import os
import yaml, time, threading
from datetime import datetime
import pytz
ist = pytz.timezone('Asia/Kolkata')
from helpers.basic import (get_config, put_config,
                           setup_logger, alert, place_order)
from helpers.positions_processing import PositionProcessor
from helpers.risk_management import RiskManager

OPTIONS_TO_AVOID = [
"NIFTY26DEC22000PE",
"NIFTY26DEC23000PE",
"NIFTY26DEC24000PE",
"NIFTY26DEC23000CE",
"NIFTY26DEC24000CE"
]

config_file = "Monitor/config.yaml"
class handle_options:
    def __init__(self, user):
        self.user = user
        self.logger = setup_logger(user)
        self.config = get_config(config_file)
        self.api_url = self.config["api_info"]["url"]

        # Bound callables for alert and order placement. They wrap the free
        # functions in helpers.basic with this user's identity, api_url, and
        # live state. ``self.config`` is captured by reference inside the
        # lambdas so dict reassignments in run() propagate without re-binding.
        self._alert = lambda heading, message: alert(
            self.user, heading, message, logger=self.logger
        )
        self.place_order = lambda symbol, quantity, transaction_type, exchange: place_order(
            api_url=self.api_url, user=self.user,
            symbol=symbol, quantity=quantity,
            transaction_type=transaction_type, exchange=exchange,
            config=self.config, logger=self.logger, alert_fn=self._alert,
        )

        self.processor = PositionProcessor(
            user=user,
            api_url=self.api_url,
            config=self.config,
            logger=self.logger,
            config_file=config_file,
            alert_fn=self._alert,
            options_to_avoid=OPTIONS_TO_AVOID,
        )
        self.risk_manager = RiskManager(
            user=user,
            api_url=self.api_url,
            config=self.config,
            logger=self.logger,
            processor=self.processor,
            place_order_fn=self.place_order,
            alert_fn=self._alert,
        )

    # Read-through accessors for state owned by the processor. Other methods
    # in handle_options (and tests / external callers) historically read these
    # off ``self``; the properties preserve that interface.
    @property
    def positions(self):
        return self.processor.positions

    @property
    def closed_positions(self):
        return self.processor.closed_positions

    @property
    def quantity(self):
        return self.processor.quantity

    @property
    def strategy(self):
        return self.processor.strategy

    @property
    def underlying(self):
        return self.processor.underlying

    @property
    def expiry(self):
        return self.processor.expiry

    def process_positions(self):
        return self.processor.process_positions()

    def analyze_positions(self):
        return self.processor.analyze_positions()

    def get_pnl(self, persist=False):
        return self.processor.get_pnl(persist=persist)

    # Risk-manager state accessors. Per-expiry state lives in dicts on the
    # risk manager; these properties expose the "primary" (nearest-tradable)
    # group's state for legacy readers — internal code iterates groups
    # directly in run().
    def _primary_expiry(self):
        groups = self.processor.groups
        if not groups:
            return None
        primary = next(
            (g for g in groups if g.strategy in ('IRON_CONDOR', 'SHORT_STRANGLE')),
            groups[0],
        )
        return primary.expiry

    @property
    def nearing_strike(self):
        return any(self.risk_manager.nearing_strike_by_expiry.values())

    @nearing_strike.setter
    def nearing_strike(self, value):
        # Apply uniformly across every tracked expiry. Used by tests only.
        for k in list(self.risk_manager.nearing_strike_by_expiry.keys()):
            self.risk_manager.nearing_strike_by_expiry[k] = value

    @property
    def lock_profit(self):
        exp = self._primary_expiry()
        return self.risk_manager.lock_profit_by_expiry.get(exp, 0) if exp else 0

    @lock_profit.setter
    def lock_profit(self, value):
        exp = self._primary_expiry()
        if exp is not None:
            self.risk_manager.lock_profit_by_expiry[exp] = value

    @property
    def trail_profit_hit_count(self):
        exp = self._primary_expiry()
        return self.risk_manager.trail_profit_hit_count_by_expiry.get(exp, 0) if exp else 0

    @trail_profit_hit_count.setter
    def trail_profit_hit_count(self, value):
        exp = self._primary_expiry()
        if exp is not None:
            self.risk_manager.trail_profit_hit_count_by_expiry[exp] = value

    def run(self):
        try:
            self.logger.info("#" * 100)
            self.config = get_config(config_file)
            self.processor.config = self.config
            self.risk_manager.config = self.config
            api_info = self.config.get('api_info') or {}
            monitor_info = self.config.get('monitor_info') or {}
            self.logger.info(f"Users in config: {api_info.get('users')}\nStarted to Watch user: {self.user}")
            if self.user in str(api_info.get('users', '')).split(","):
                if self.process_positions():
                    pass
                else:
                    time.sleep(monitor_info.get('delay'))
                    return False
                self.analyze_positions()
                # Pull today's executed fills into kite_trades so the
                # local ledger stays current across the cycle. Idempotent
                # on trade_id; record_snapshots.py's daily 15:35 hook is
                # still the redundant safety net.
                self.processor.fetch_and_persist_trades()
                # Bank any newly-closed legs into their week_pnl bucket
                # BEFORE per-group risk decisions read the bucket, so the
                # cycle following a close already reflects the realized
                # P&L in stop-loss / trail-profit logic.
                self.processor.bank_closed_positions()
                tradable_groups = [g for g in self.processor.groups
                                   if g.strategy in ('IRON_CONDOR', 'SHORT_STRANGLE')]
                if tradable_groups:
                    self.logger.info(
                        f"Running risk pipeline across {len(tradable_groups)} group(s): "
                        + ", ".join(f"{g.strategy}@{g.expiry.date()}" for g in tradable_groups)
                    )
                for group in tradable_groups:
                    self.logger.info("=" * 25 + f" {group.strategy} @ {group.expiry.date()} " + "=" * 25)
                    pnl = self.processor.compute_group_pnl(group)
                    self.logger.info(f"Group P&L: {pnl}")
                    self.logger.info("-" * 25)
                    closed = self.risk_manager.check_stop_loss(group, pnl)
                    if not closed:
                        self.logger.info("-" * 25)
                        closed = self.risk_manager.trail_profit(group, pnl)
                        if not closed:
                            self.logger.info("-" * 25)
                            self.risk_manager.adjustments(group)
                ok, info = put_config(config_file, "monitor_info.last_watched",
                                      datetime.now(ist).strftime("%Y-%m-%d %H:%M"))
                if not ok:
                    self.logger.error(f"Failed to persist last_watched: {info}")
        except Exception as e:
            self.logger.exception(f"Failed in the run() module with error: {e}")
            self._alert("RunFailure", f"run() raised {type(e).__name__}: {e}")
            time.sleep((self.config.get('monitor_info') or {}).get('delay'))
            return False
        time.sleep((self.config.get('monitor_info') or {}).get('delay'))
        return True
def run_user(user, handle_obj):
    """Worker function to run each user's trading logic."""
    try:
        return handle_obj.run()
    except Exception as e:
        handle_obj.logger.exception(f"Unhandled error in thread for {user}: {e}")
        try:
            handle_obj._alert("ThreadFailure", f"Unhandled exception in thread: {e}")
        except Exception:
            pass

