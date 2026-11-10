import os
import yaml, time, threading
# datetime / pytz / ist were only ever used by the monitor_info.last_watched
# write in run(); put_config likewise had no other caller here. Removed with it
# — this module no longer writes Monitor/config.yaml at all.
from helpers.basic import (get_config, setup_logger, alert, place_order,
                           configured_users, ALERT_URGENT, ALERT_INFO)
from helpers.positions_processing import (PositionProcessor,
                                          MONITORED_STRATEGIES)
from helpers.risk_management import RiskManager

OPTIONS_TO_AVOID = []

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
        # ``priority`` is optional so the many two-argument call sites are
        # unchanged; pass ALERT_INFO for "worth knowing, don't wake me".
        self._alert = lambda heading, message, priority=ALERT_URGENT: alert(
            self.user, heading, message, logger=self.logger, priority=priority
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

    # The risk-manager state accessors (`nearing_strike`, `lock_profit`,
    # `trail_profit_hit_count`) and the `_primary_expiry()` helper they shared
    # were removed with check_stop_loss / trail_profit. They read the
    # per-expiry dicts those two paths maintained; the adjustments engine
    # keeps no cross-cycle state, so there is nothing left to expose.

    def run(self):
        try:
            self.logger.info("#" * 100)
            self.config = get_config(config_file)
            self.processor.config = self.config
            self.risk_manager.config = self.config
            monitor_info = self.config.get('monitor_info') or {}
            allowed = configured_users(self.config)
            self.logger.info(f"Users in config: {allowed}\n"
                             f"Started to Watch user: {self.user}")
            # Re-checked every cycle, not just at construction, so removing an
            # account from api_info.users stops it trading without a restart.
            #
            # Was an inline `self.user in str(...).split(",")`, which did not
            # strip: `users: sashi, madhu` made this False for madhu, and a
            # False here skips the ENTIRE body below, sleeps, and returns
            # success — a monitor that looks healthy and never trades. The
            # shared parser in helpers.basic strips; see configured_users.
            if self.user in allowed:
                if self.process_positions():
                    pass
                else:
                    time.sleep(monitor_info.get('delay'))
                    return False
                self.analyze_positions()
                # Pull today's executed fills into kite_trades. Idempotent on
                # trade_id; fetch_db_data.py's daily 15:35 hook is still the
                # redundant safety net.
                #
                # No longer every cycle — see maybe_fetch_and_persist_trades.
                # It runs on an interval (monitor_info.trade_ingest_cycles)
                # plus immediately whenever the broker's net position changed,
                # which is the case this ordering exists for.
                #
                # MUST stay after analyze_positions (which populates the net
                # position map the change-detection compares) and ahead of the
                # risk pipeline below: realised P&L is FIFO-matched from
                # kite_trades on read, so a close that hasn't been persisted
                # yet is a close the adjustments engine cannot see.
                # (The old bank_closed_positions() call was here for the same
                # ordering reason, writing into <week>.pnl; nothing is written
                # now, the ledger just has to be current.)
                if self.processor.maybe_fetch_and_persist_trades() is None:
                    # Proceed anyway, on the operator's call. The ledger may be
                    # one cycle behind the broker, so realised P&L can be
                    # understated by whatever just filled — but that gap is
                    # bounded and self-correcting (the next successful ingest
                    # picks the fill up), whereas skipping the pipeline stops
                    # adjustments entirely for that interval. Given these
                    # failures are sporadic broker/network hiccups rather than
                    # sustained outages, the smaller risk is to carry on.
                    #
                    # Low priority: this should be visible in the alert history
                    # without ringing the phone. A sustained outage shows up as
                    # a run of these, and the read-failure path below (which
                    # DOES skip) still alerts loudly.
                    reason = (self.processor._last_ingest_error
                              or "reason not recorded")
                    self.logger.warning(
                        f"Ledger may be one cycle behind ({reason}); running "
                        f"the risk pipeline anyway. Realised P&L could be "
                        f"understated by any fill not yet persisted."
                    )
                    self._alert(
                        "LedgerBehind",
                        f"kite_trades not updated this cycle: {reason}. "
                        f"Risk pipeline still ran; realised P&L may be "
                        f"understated by any just-filled trade until the next "
                        f"successful update.",
                        ALERT_INFO,
                    )
                monitored_groups = [g for g in self.processor.groups
                                    if g.strategy in MONITORED_STRATEGIES]
                if monitored_groups:
                    self.logger.info(
                        f"Running risk pipeline across {len(monitored_groups)} group(s): "
                        + ", ".join(f"{g.strategy}@{g.expiry.date()}" for g in monitored_groups)
                    )
                for group in monitored_groups:
                    self.logger.info("=" * 25 + f" {group.strategy} @ {group.expiry.date()} " + "=" * 25)
                    # Skip the whole risk pipeline for this book if it still
                    # has orders working in the broker queue. place_order
                    # returns on ACCEPTANCE, not FILL, so an order hanging
                    # PENDING would otherwise leave positions unchanged and
                    # make the next cycle re-issue the same adjustment
                    # orders — the duplicate-order bug this guards against.
                    if self.risk_manager.has_pending_orders(group):
                        self.logger.info(
                            f"Skipping risk pipeline for {group.strategy} "
                            f"@ {group.expiry.date()}: orders still pending at broker."
                        )
                        continue
                    pnl = self.processor.compute_group_pnl(group)
                    self.logger.info(f"Group P&L: {pnl}")
                    # The ledger read happens lazily inside compute_group_pnl,
                    # so a read failure only becomes visible here. Same rule
                    # as above: no reliable realised figure, no risk decision.
                    # Distinct from the ingest failure above: there the ledger
                    # is at most one fill behind, here it could not be READ at
                    # all, so realised is {} and a book with banked losses
                    # reads flat. That is not a bounded understatement and it
                    # still skips.
                    if not self.processor.realised_trustworthy:
                        self.logger.error(
                            f"Skipping risk pipeline for {group.strategy} @ "
                            f"{group.expiry.date()}: ledger read failed, so "
                            f"every book would read realised=0."
                        )
                        continue
                    self.logger.info("-" * 25)
                    # check_stop_loss(group, pnl) and trail_profit(group, pnl)
                    # used to run ahead of this, each able to flatten the book
                    # and to short-circuit what followed. Both are gone: the
                    # adjustments engine manages the trade end to end, rolling
                    # a threatened short out and trimming a losing side rather
                    # than closing everything. ``pnl`` is still computed above
                    # because it is logged per group and the realised-P&L
                    # trustworthiness gate depends on that read.
                    #
                    # One entry point for every monitored book. `adjustments`
                    # dispatches on strategy internally: DIRECTIONAL goes to
                    # the one-sided path (complete the condor when the market
                    # is range-bound, roll the short inward in a downtrend),
                    # everything else runs the two-sided delta bands.
                    #
                    # Everything above this line applies to both, and that
                    # matters most for has_pending_orders: both paths place
                    # orders, and place_order returns on ACCEPTANCE, not
                    # FILL. Without that guard a book would look unchanged on
                    # the next cycle and the same orders would go out again.
                    self.risk_manager.adjustments(group)
                # The per-cycle `monitor_info.last_watched` write used to land
                # here. Removed: /status now takes the monitor's heartbeat from
                # this log file's mtime instead (helpers.basic
                # .watch_log_last_modified). Same signal, one fewer writer of
                # the shared config — and it's evidence the cycle ran rather
                # than a value the cycle reported about itself.
            else:
                # Skipping the whole cycle is the correct response to "this
                # account is no longer configured to trade", but it must not be
                # SILENT. autotrade.py refuses to start for an unconfigured
                # account, so reaching this means api_info.users was edited
                # mid-session — which is a legitimate way to stand an account
                # down, and equally a legitimate way to stand one down by
                # accident.
                self.logger.warning(
                    f"NOT TRADING: {self.user} is not in api_info.users "
                    f"({allowed}) — skipping this cycle. Nothing will be "
                    f"placed for this account until the config lists it."
                )
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

