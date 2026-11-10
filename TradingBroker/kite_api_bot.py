import logging, shutil, time, traceback, pytz, yaml
from datetime import datetime
from pathlib import Path
#
from pyotp import TOTP
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
ist = pytz.timezone('Asia/Kolkata')

# Credential keys the login needs. Listed once so a missing one is reported
# by NAME up front, instead of surfacing as a bare KeyError from whichever
# line happened to reach it first.
#
# api_key / secret_key identify the Kite Connect APP and may legitimately be
# shared between accounts — one app can serve many logins, and Zerodha issues
# a separate access token per (api_key, user_id). The other three identify
# the PERSON and must be that account's own.
_REQUIRED_CREDENTIALS = ("api_key", "secret_key", "username", "password",
                         "authenticator_token")


# Chrome wants well over the 64MB a container gives /dev/shm by default, but
# is comfortable in a few hundred. Below this we redirect it to /tmp; at or
# above, /dev/shm is the better home — especially when / is short of space.
_MIN_SHM_BYTES = 256 * 1024 * 1024

# Headless Chrome needs room for its profile, cache and crash dumps. Below
# this much free on /, it tends not to fail cleanly: chromedriver starts and
# accepts commands while the browser never comes up, so the caller sits in
# driver.get() until its 120s timeout and reports a read timeout that names
# nothing useful.
#
# 1GB is measured on this deployment, not guessed: logins worked with 1.0GB
# free and began timing out at 721MB. Warn at the level where it is known to
# break, not at some tidier round number below it.
_MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024

# Headless Chrome's own footprint is ~300-500MB. Below this much AVAILABLE
# memory it will not come up, and the way it fails is the problem: the
# chromedriver session is created fine and answers commands, then the
# renderer is OOM-killed during navigation — so the caller blocks in
# driver.get() for the full 120s client timeout and reports a read timeout
# that says nothing about memory.
#
# This is the constraint that actually bites here: /dev/shm reporting 467MB
# implies ~1GB of physical RAM (tmpfs defaults to half), and flaskapp, db_api,
# fetch_db_data and one autotrade process PER ACCOUNT each carry a CPython
# interpreter with scipy and pandas loaded. Adding an account adds ~150MB, so
# the box crosses the line quietly and an unrelated account's login is what
# breaks.
_MIN_FREE_RAM_BYTES = 400 * 1024 * 1024


def _available_ram_bytes():
    """MemAvailable from /proc/meminfo, or None where that isn't readable."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:      # noqa: BLE001 - not Linux, or unreadable
        return None
    return None


def _shm_too_small_for_chrome():
    """True when Chrome's shared memory should go to /tmp instead of /dev/shm.

    Two reasons, and the second is easy to get backwards — I did:

      * /dev/shm is genuinely tiny (the container default of 64MB), which is
        the case the --disable-dev-shm-usage flag was invented for; or
      * free RAM is low. /dev/shm is a TMPFS, so it is RAM. On a 1GB host it
        reports a roomy-looking 467MB (tmpfs defaults to half of physical
        memory) while there is nothing actually spare — and pointing Chrome
        at it spends memory the box does not have. Disk is the right trade
        there, even slow disk.

    So size alone is the wrong question. A large /dev/shm on a host with no
    free memory is not capacity, it is an accounting artefact.
    """
    available = _available_ram_bytes()
    if available is not None and available < _MIN_FREE_RAM_BYTES:
        return True
    try:
        return shutil.disk_usage("/dev/shm").total < _MIN_SHM_BYTES
    except Exception:      # noqa: BLE001 - no /dev/shm (Windows, odd mounts)
        return True


def _resource_warnings():
    """Messages for any resource shortage that stops Chrome starting.

    Advisory only — never blocks a login attempt, because a wrong guess here
    must not be the thing that stops trading. The value is purely that the
    log names the cause BEFORE the symptom, instead of leaving a 120s read
    timeout to be interpreted after the fact.
    """
    warnings = []
    try:
        usage = shutil.disk_usage("/")
        if usage.free < _MIN_FREE_DISK_BYTES:
            pct = (usage.used / usage.total * 100) if usage.total else 0
            warnings.append(
                f"LOW DISK: / is {pct:.0f}% full with "
                f"{usage.free / 1024 / 1024:.0f}MB free (want "
                f"{_MIN_FREE_DISK_BYTES / 1024 / 1024:.0f}MB+).")
    except Exception:      # noqa: BLE001
        pass

    available = _available_ram_bytes()
    if available is not None and available < _MIN_FREE_RAM_BYTES:
        warnings.append(
            f"LOW MEMORY: {available / 1024 / 1024:.0f}MB available (want "
            f"{_MIN_FREE_RAM_BYTES / 1024 / 1024:.0f}MB+ for headless "
            f"Chrome). Each configured account runs its own autotrade "
            f"process; adding one costs ~150MB.")

    if warnings:
        warnings.append(
            "Chrome fails these as a 120s read timeout in driver.get(), not "
            "as an out-of-resource error. Fix the resource, not the login.")
    return warnings


def _build_user_logger(user):
    """A logger writing to ``TradingBroker/{user}_connection_{date}.log``."""
    logger = logging.getLogger(f"KITE_CONNECT.{user}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    log_dir = Path(__file__).resolve().parent
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{user}_connection_{datetime.now(ist).strftime('%Y%m%d')}.log"

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


class KITE_CONNECT:
    def __init__(self, user, credentials):
        self.access_token = None
        self.logger = logging.getLogger(f"KITE_CONNECT.{user}.fallback")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
            self.logger.addHandler(handler)
        self.logger.propagate = False

        try:
            self.logger = _build_user_logger(user)
            self.logger.info(f"=== login attempt for local account '{user}' ===")
            self.credentials = credentials or {}
            # Fail with the missing NAMES rather than a KeyError from line 22
            # or line 88. Values are never logged — only whether each is set.
            missing = [k for k in _REQUIRED_CREDENTIALS
                       if not self.credentials.get(k)]
            self.logger.info(
                "Credential fields present: "
                + ", ".join(f"{k}={'set' if self.credentials.get(k) else 'MISSING'}"
                            for k in _REQUIRED_CREDENTIALS)
            )
            if missing:
                raise KeyError(
                    f"credentials for '{user}' are missing or empty: "
                    f"{', '.join(missing)}. Add them under credentials.{user} "
                    f"in TradingBroker/config.yaml."
                )
            self.kite = KiteConnect(api_key=credentials["api_key"])
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Login URL generated")
            request_token = self.__auto_login(self.kite.login_url())
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Request token received: {'yes' if request_token else 'no'}")
            if not request_token:
                # Stop here rather than handing None to generate_session,
                # which fails deep inside kiteconnect as "'NoneType' object
                # has no attribute 'encode'". __auto_login has already logged
                # the actual reason (consent screen, bad TOTP, Chrome down) —
                # and that cryptic AttributeError landed AFTER it, making the
                # last line of the log the least useful one. The tail is what
                # anyone reads first.
                raise RuntimeError(
                    "no request_token from the web login — the reason is "
                    "logged immediately above this line. Not calling "
                    "generate_session()."
                )
            data = self.kite.generate_session(request_token, api_secret=credentials["secret_key"])
            self.access_token = data.get("access_token") or data.get("sess_id")
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Session connected (access_token set: {bool(self.access_token)})")
            self.kite.set_access_token(self.access_token)
            # Record WHICH Zerodha account this local name actually reached.
            # Without it there is no way to tell two configured accounts apart
            # from inside the system: copy one credentials block, change only
            # the key, and both names log into the same broker account. Every
            # per-account view then shows identical positions — correctly, and
            # indistinguishably from a bug.
            #
            # Nested try: a failed profile read must NOT fall through to the
            # outer handler, which sets access_token = None. This is a
            # diagnostic, and a diagnostic that can break the login it is
            # describing is worse than no diagnostic.
            try:
                profile = self.kite.profile() or {}
                self.logger.info(
                    f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                    f"Broker identity for local account '{user}': "
                    f"user_id={profile.get('user_id')} "
                    f"user_name={profile.get('user_name')}"
                )
            except Exception as e:
                self.logger.info(
                    f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                    f"Could not read broker profile for '{user}' "
                    f"(login itself succeeded): {e}"
                )
        except Exception as e:
            logger = getattr(self, "logger", logging.getLogger(f"KITE_CONNECT.{user}.fallback"))
            logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Login failed: {e}")
            self.access_token = None
    def __auto_login(self, login_url, with_head=False):
        """Drive Zerodha's web login and return a ``request_token``, or None.

        Every exit from here MUST quit the browser — see ``_close_driver``
        below. Two paths used not to: the Chrome-connection handler returned
        None without quitting, and a stage timeout re-raised past it. Each
        failed login therefore leaked a headless Chrome AND its chromedriver,
        which survive the request that spawned them because nothing owns
        them afterwards.

        That is a slow poison rather than an immediate error: the leaks
        accumulate across retries, and the first visible symptom is an
        UNRELATED account's login hanging in ``driver.get()`` until the
        120s client timeout, because the host has run out of memory or /tmp
        for another browser. An account that worked yesterday failing today,
        right after a different account started failing repeatedly, is this.
        """
        driver = None

        def _close_driver():
            if driver is None:
                return
            try:
                driver.quit()
            except Exception as e:      # noqa: BLE001 - cleanup, never fatal
                self.logger.info(
                    f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                    f"driver.quit() failed ({type(e).__name__}); the browser "
                    f"process may be orphaned"
                )

        self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Got into auto login")
        for warning in _resource_warnings():
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: {warning}")
        try:
            options = webdriver.ChromeOptions()
            #
            # 1024x768 rather than 1400x1500. Headless Chrome allocates a
            # compositing surface proportional to the viewport, and a login
            # form needs nothing like that much. Everything the flow touches
            # (userid, password, TOTP) is above the fold at this size.
            options.add_argument("window-size=1024,768")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("enable-automation")
            options.add_argument("--disable-infobars")
            # Trim subsystems a one-page form login never uses. Each is
            # worth only a few MB, but this host has ~200MB of headroom and
            # none of them can affect whether the form submits.
            #
            # Deliberately NOT disabling images: it would save a little more,
            # but a login page that fails to render correctly costs far more
            # than the memory is worth on the one path that must not break.
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-default-apps")
            options.add_argument("--disable-software-rasterizer")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            # --disable-dev-shm-usage ONLY when /dev/shm is genuinely too
            # small. The flag is a container workaround: Docker defaults
            # /dev/shm to 64MB, which Chrome exhausts and crashes on, so the
            # flag redirects its shared memory to /tmp instead.
            #
            # On a normal host that trade is backwards. This box has 467MB of
            # /dev/shm sitting at 0% while / is 93% full — so the flag was
            # moving Chrome's memory OFF the empty filesystem and ONTO the
            # full one, and Chrome then failed to start at all. Deciding from
            # the actual size is correct in both environments.
            if _shm_too_small_for_chrome():
                options.add_argument("--disable-dev-shm-usage")
                self.logger.info(
                    f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                    f"/dev/shm is small; routing Chrome shared memory to /tmp"
                )
            if with_head is True:
                pass
            else:
                options.add_argument("--headless")
            #
            driver = webdriver.Chrome(options)
            driver.get(login_url)
            driver.implicitly_wait(10)
        except Exception as e:
            # format_exc(), not print_exc(): the latter PRINTS to stdout and
            # returns None, so this line used to log the literal string
            # "None" and the Chrome failure path recorded nothing usable.
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                             f"Failed while connecting to Chrome: {e}")
            self.logger.info(traceback.format_exc())
            # driver may be a live handle even here: webdriver.Chrome() can
            # succeed and the subsequent driver.get() time out, which is
            # precisely the case that leaked a browser per attempt.
            _close_driver()
            return None
        #
        try:
            # Each stage is logged before it is attempted. A Selenium timeout
            # otherwise reaches __init__'s handler as a bare TimeoutException with
            # no indication of WHICH element was missing — and the three stages
            # fail for completely different reasons: a missing userid field means
            # Zerodha changed the page, a missing TOTP field means the
            # username/password were rejected.
            def _stage(name, condition, timeout=10):
                self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                                 f"login stage: {name}")
                try:
                    return WebDriverWait(driver, timeout).until(condition)
                except Exception as e:
                    self.logger.info(
                        f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                        f"login FAILED at stage '{name}' after {timeout}s: "
                        f"{type(e).__name__}. Current URL: {driver.current_url}"
                    )
                    raise

            username_input = _stage("waiting for userid field",
                EC.presence_of_element_located((By.XPATH, "//input[@id='userid']")))
            password_input = _stage("waiting for password field",
                EC.presence_of_element_located((By.XPATH, "//input[@id='password']")))
            username_input.send_keys(self.credentials["username"])
            password_input.send_keys(self.credentials["password"])
            _stage("submitting username/password",
                EC.presence_of_element_located(
                    (By.XPATH, "//*[@id='container']/div/div/div[2]/form/div[4]/button"))).click()
            time.sleep(1)
            # If the credentials were rejected, Zerodha never renders the TOTP
            # step — so a timeout HERE means "wrong username or password", which
            # is the single most likely cause for a newly added account.
            pin_input = _stage("waiting for TOTP field (absent => username/password rejected)",
                EC.presence_of_element_located((By.XPATH, "//input[@id='userid']")))
            try:
                totp = TOTP(self.credentials["authenticator_token"])
                totp.now()
            except Exception as e:
                # pyotp accepts the string and only fails when generating, so an
                # invalid seed surfaces here rather than at construction.
                self.logger.info(
                    f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                    f"authenticator_token is not a usable TOTP seed "
                    f"({type(e).__name__}: {e}). It must be the base32 SECRET "
                    f"from Zerodha's 2FA setup, not a 6-digit code."
                )
                driver.quit()
                return None
            from urllib.parse import urlparse, parse_qs

            def _safe_url(url):
                """Scheme+host+path plus the NAMES of the query params.

                The raw URL at this point carries ``sess_id``, a live session
                handle — and these logs get copied into tickets and chat windows.
                Which parameters are present is the whole diagnostic; their values
                add nothing and are worth protecting.
                """
                try:
                    p = urlparse(url)
                    names = sorted(parse_qs(p.query))
                    return f"{p.scheme}://{p.netloc}{p.path} [params: {', '.join(names) or 'none'}]"
                except Exception:
                    return "<unparseable url>"

            request_token = None
            attempts = 0
            max_attempts = 3
            while not request_token and attempts < max_attempts:
                attempts += 1
                # Re-locate the field EVERY attempt. Submitting the TOTP navigates
                # the page, which makes the element captured before the loop
                # stale — so attempt 2 raised StaleElementReferenceException and
                # that exception, not the actual reason for there being no
                # request_token, is what reached the caller.
                try:
                    field = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, "//input[@id='userid']")))
                    field.send_keys(totp.now())
                except Exception as e:
                    self.logger.info(
                        f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                        f"TOTP attempt {attempts}/{max_attempts}: field not "
                        f"available ({type(e).__name__}) — the page has already "
                        f"moved on. Now at {_safe_url(driver.current_url)}"
                    )
                time.sleep(2)

                current = driver.current_url
                self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                                 f"after TOTP attempt {attempts}: {_safe_url(current)}")
                parsed = urlparse(current)
                request_token = parse_qs(parsed.query).get('request_token', [None])[0]
                if request_token:
                    break

                # Landing on /connect/authorize means 2FA SUCCEEDED and Zerodha is
                # now asking this user to grant the app access. It is a one-time,
                # per-user consent that persists once given, which is exactly why
                # an established account works and a newly added one does not.
                # Retrying cannot clear it, so stop and say so.
                if "/connect/authorize" in parsed.path:
                    self.logger.info(
                        f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                        f"STOPPING: login and 2FA both succeeded, but this account "
                        f"has not authorised the Kite Connect app. Zerodha is "
                        f"showing its consent screen (no request_token is issued "
                        f"until it is accepted). Log in as this account ONCE in a "
                        f"normal browser using the app's login URL and click "
                        f"Authorize; the consent persists, and this automated "
                        f"login will work from then on. Nothing is wrong with the "
                        f"credentials."
                    )
                    break
                time.sleep(8)

            if not request_token:
                self.logger.info(
                    f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                    f"no request_token after {attempts} TOTP attempt(s). "
                    f"Final URL: {_safe_url(driver.current_url)}"
                )
            return request_token
        finally:
            # The one place the browser is guaranteed to be closed,
            # whichever way the block above exits — return, break,
            # or an exception raised past it by a stage timeout.
            _close_driver()

    # ---- Session expiry --------------------------------------------------
    #
    # Kite access tokens die daily (~06:00 IST), so a Flask app left running
    # overnight starts the next session holding a dead token. main.py has
    # always had the cure — ``_call_with_reauth`` catches TokenException,
    # drops the cached connection, logs in again and retries once — but it
    # could never fire, because every fetcher below caught the exception
    # itself and returned None. The symptom was "no open positions" on the
    # first request of the day, with the real reason only in this class's own
    # log file, and it persisted until someone restarted the service.
    #
    # So TokenException is re-raised while other failures keep returning
    # None. That distinction matters: a transient Kite error should stay a
    # local miss the caller can degrade around, whereas an auth failure is
    # precisely the case that needs to reach main.py.
    #
    # Retrying after a TokenException is safe even for place_order: the
    # request was rejected at authentication, so it never reached the
    # exchange and cannot double-fill.
    _TOKEN_EXPIRY_NOTE = ("Kite session expired; propagating so the caller "
                          "can re-authenticate and retry")

    def get_connection(self):
        return self.kite
    def __split_strike(self, strike):
        #BANKNIFTY2461949800PE
    #    print(strike)
        if strike[:9] == 'BANKNIFTY':
            index_name = 'BANKNIFTY'
            expiry = (strike[9:15] if (int(strike[11:13]) <= 12) else strike[9:14]) if strike[11:13].isnumeric() else strike[9:14]
        elif strike[:5] == 'NIFTY':
            index_name = 'NIFTY'
            expiry = (strike[5:11] if (int(strike[7:9]) <= 12) else strike[5:10]) if strike[7:9].isnumeric() else strike[5:10]
        strike_price = strike[-7:-2]
        option_type = strike[-2:]
    #    print(index_name, expiry, strike_price, option_type)
        return index_name, expiry, strike_price, option_type
    def fetch_orders(self): # return list of dictionaries
        orders = None
        try:
            orders = self.kite.orders()
        except TokenException:
            self.logger.error(f"Kite API (orders): {self._TOKEN_EXPIRY_NOTE}")
            raise
        except Exception as e:
            self.logger.error(f"Kite API (orders) failed with error: {e}")
        return (orders)

    def fetch_trades(self):
        """Fetch the day's executed trades from Kite (``kite.trades()``).

        Each trade dict carries ``tradingsymbol``, ``exchange``,
        ``transaction_type`` (``"BUY"`` / ``"SELL"``), ``quantity``
        (in shares — already lot_size-multiplied), ``average_price``
        (per share), ``trade_id`` / ``order_id``, and timestamps
        (``fill_timestamp``, ``exchange_timestamp``).

        Important: Kite returns trades for the **current trading day
        only**. For historical FIFO P&L across multiple days, callers
        must persist these locally — there is no official Kite
        back-office API for the trade ledger beyond today.

        Returns the raw list of trade dicts. RAISES on failure — it does not
        return None.

        It used to swallow the exception and return None, which had two bad
        consequences. main.py saw no exception, so it answered ``[True,
        {user: None}]`` — success — and the caller read that as "no fills
        today": a Kite outage was indistinguishable from a flat day, and the
        trade ledger silently stopped advancing. And Kite's actual message
        went only to this class's own log file, so the operator alert could
        say nothing more useful than "check the broker call".

        Propagating also lets ``main._call_with_reauth`` do its job: a
        TokenException mid-session now triggers re-authentication and a
        retry, where before it produced an empty trade list.
        """
        try:
            return self.kite.trades()
        except Exception as e:
            # Keep the breadcrumb in the broker log, then let the caller
            # decide — it is the one that can reach the operator.
            self.logger.error(f"Kite API (trades) failed with error: {e}")
            raise
    def fetch_instruments(self, exchange="NFO"):
        """The exchange's full instrument dump, exactly as Kite returns it.

        Kite regenerates this once a day (~08:00 IST) and the NFO dump is
        ~100k rows, so it is expensive in both bandwidth and RSS on a 933MB
        host. Callers MUST cache it and reduce it to what they actually need
        straight away — see ``_expiry_map`` in main.py, which keeps only the
        distinct expiry dates per (name, segment) and drops the rest. Nothing
        should hold the whole list, and nothing should ship it over the wire
        to the recorder.

        Returns the list of instrument dicts, or None when Kite fails for a
        non-token reason. TokenException is re-raised so ``_call_with_reauth``
        can refresh the session and retry, on the same terms as quotes and
        trades — swallowing it here would make the daily token expiry present
        as "this underlying has no listed expiries".

        Replaces ``fetch_option_chain``, which fetched the dump into a local
        and then fell off the end of the function without returning it: it
        handed back None on SUCCESS as well as on failure. It had no callers,
        which is the only reason that never showed up as a bug.
        """
        try:
            return self.kite.instruments(exchange)
        except TokenException:
            self.logger.error(f"Kite API (instruments): {self._TOKEN_EXPIRY_NOTE}")
            raise
        except Exception as e:
            self.logger.error(f"Kite API (instruments) failed for {exchange}: "
                              f"{type(e).__name__}: {e}")
            return None

    def fetch_last_price(self, symbol, exchange="NFO"):
        """LTP for ONE instrument, as ``{'EXCHANGE:SYMBOL': {...}}`` or None.

        Always a single instrument — so any Kite complaint about the SIZE of
        the request is about how often it is being called, not what is in it.
        Kite rate-limits the quote endpoints to roughly one request a second
        ACROSS the whole API key, and every account here shares one key.
        """
        instrument = f"{exchange}:{symbol}"
        ltp = None
        try:
            ltp = self.kite.ltp([instrument])
        except TokenException:
            self.logger.error(f"Kite API (ltp) for {instrument}: "
                              f"{self._TOKEN_EXPIRY_NOTE}")
            raise
        except Exception as e:
            # Name the instrument AND the exception class. Without both, a
            # one-line message like "Too many parameters" is unattributable:
            # you cannot tell which symbol provoked it, and you cannot tell
            # an InputException (the request was malformed) from a
            # NetworkException (the request was throttled) — which have
            # opposite fixes.
            self.logger.error(
                f"Kite API (ltp) failed for {instrument}: "
                f"{type(e).__name__}: {e}"
            )
        return ltp

    # Kite's /quote endpoint caps at 500 instruments per call. fetch_quotes
    # chunks transparently so callers don't have to think about it (the
    # recorder routinely asks for 600+ when covering 5 weekly expiries).
    _KITE_QUOTE_BATCH = 500

    def fetch_quotes(self, instruments):
        """Batch-fetch full quotes (LTP, OHLC, OI, depth, volume) for a list
        of instruments in ``EXCHANGE:SYMBOL`` form (e.g.
        ``["NFO:NIFTY25SEP24500PE", ...]``).

        Chunks the upstream ``kite.quote()`` call into batches of
        ``_KITE_QUOTE_BATCH`` instruments since Kite caps each call at 500;
        the returned dict is the union of all chunk responses, keyed by
        the same instrument strings the caller passed in.

        Returns None if any chunk fails (so the caller treats it as a hard
        miss rather than silently working with partial data).
        """
        if not instruments:
            return {}
        merged = {}
        for start in range(0, len(instruments), self._KITE_QUOTE_BATCH):
            chunk = instruments[start:start + self._KITE_QUOTE_BATCH]
            try:
                quotes = self.kite.quote(chunk)
            except TokenException:
                self.logger.error(f"Kite API (quote): {self._TOKEN_EXPIRY_NOTE}")
                raise
            except Exception as e:
                self.logger.error(
                    f"Kite API (quote) failed for chunk "
                    f"{start}-{start + len(chunk)} of {len(instruments)}: {e}"
                )
                return None
            if quotes:
                merged.update(quotes)
        return merged
    def fetch_profile(self):
        """The Zerodha account this connection is actually logged into.

        Exists to answer one question the rest of the system cannot: are two
        locally-configured accounts two real broker accounts, or one account
        configured twice? Every other call is scoped to ``self.kite``, so
        both cases behave identically — same positions, same orders, same
        P&L — and only the broker's own ``user_id`` distinguishes them.

        Returns ``None`` on failure rather than raising, matching the other
        fetchers: this is diagnostic, never on a trading path.
        """
        try:
            return self.kite.profile()
        except Exception as e:
            self.logger.error(f"Kite API (profile) failed with error: {e}")
            return None

    def fetch_optoin_positions(self):
        positions_list = None
        try:
            positions_list = self.kite.positions()["net"]
        except TokenException:
            # The overnight case: this is what used to surface as "no open
            # positions" the morning after, until the service was restarted.
            self.logger.error(f"Kite API (positions): {self._TOKEN_EXPIRY_NOTE}")
            raise
        except Exception as e:
            self.logger.error(f"Kite API (positions) failed with error: {e}")
            return None
        return positions_list

    # P&L fields Kite returns on each net-positions row. Pulled out as a
    # constant so the fetcher below and any downstream aggregator stay in
    # lockstep.
    _KITE_PNL_FIELDS = ("pnl", "realised", "unrealised", "m2m")

    def fetch_pnl(self):
        """Fetch per-position P&L straight from Kite (``kite.positions()['net']``).

        Built to replace the running ``<week>.pnl`` buckets currently
        persisted in ``Monitor/config.yaml`` — those are an in-house
        approximation that re-derives realized P&L from broker
        ``closed_positions`` each cycle. This method returns the broker's
        authoritative P&L view instead, so downstream code (brain.py, the
        dashboard) can stop depending on the config buckets.

        Each row in ``positions`` carries the four P&L breakdowns Kite
        publishes::

            pnl        # total P&L since the position was opened (cumulative)
            realised   # realised portion (closed quantity x price delta)
            unrealised # mark-to-market on still-open quantity
            m2m        # intraday MTM change for the trading day

        plus identification fields (``tradingsymbol``, ``exchange``,
        ``product``, ``quantity``, ``average_price``, ``last_price``,
        ``value``) so callers can group by expiry / underlying without a
        second roundtrip.

        ``totals`` is the sum of each of the four P&L fields across all
        net positions — handy for a quick portfolio-level glance.

        Returns ``None`` on failure (so the caller treats it as a hard
        miss rather than silently working with stale data).
        """
        try:
            raw = self.kite.positions()
        except TokenException:
            self.logger.error(f"Kite API (positions/pnl): {self._TOKEN_EXPIRY_NOTE}")
            raise
        except Exception as e:
            self.logger.error(f"Kite API (positions/pnl) failed with error: {e}")
            return None

        net = raw.get("net", []) if isinstance(raw, dict) else []
        positions = []
        totals = {k: 0.0 for k in self._KITE_PNL_FIELDS}
        for p in net:
            row = {
                "tradingsymbol": p.get("tradingsymbol"),
                "exchange": p.get("exchange"),
                "product": p.get("product"),
                "quantity": p.get("quantity"),
                "average_price": p.get("average_price"),
                "last_price": p.get("last_price"),
                "value": p.get("value"),
            }
            for k in self._KITE_PNL_FIELDS:
                # Kite sends None on a fresh-cycle position before m2m is
                # computed — coerce to 0 so the aggregate stays numeric.
                v = p.get(k) or 0
                row[k] = v
                totals[k] += v
            positions.append(row)
        return {"positions": positions, "totals": totals}
    def place_order(self, symbol,quantity=1,transaction_type="BUY",exchange="NSE",order_type="MARKET",price="1",variety="regular",product="NRML",validity="DAY", market_protection=2):
        try:
            order_id = self.kite.place_order(tradingsymbol=symbol,
                                             exchange=exchange,
                                             transaction_type=transaction_type,
                                             quantity=quantity,
                                             variety=variety,
                                             order_type=order_type,
                                             price=price,
                                             product=product,
                                             validity=validity,
                                             market_protection=market_protection)

            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Order placed. ID is: {order_id}")
            return order_id
        except TokenException:
            # Safe to propagate and let the caller retry after re-auth: the
            # request was rejected at authentication, so it never reached the
            # exchange and cannot produce a duplicate fill.
            self.logger.error(
                f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: "
                f"Order placement: {self._TOKEN_EXPIRY_NOTE}")
            raise
        except Exception as e:
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: {traceback.print_exc()}")
            self.logger.info(f"{datetime.now(ist).strftime('%Y%m%d %H:%M:%S')}: Order placement failed: {e}")
            return None
'''
if __name__ == "__main__":
    #
    with open("config.yaml", "r") as file:
        config = yaml.safe_load(file)
    #
    user="madhu"
    credentials = config["credentials"][user]
    kc = KITE_CONNECT(user, credentials)
    if not kc.access_token:
        exit(1)
    #
    logger = logging.getLogger()
    #
    logger.info("Kite Order book: ")
    logger.info(kc.fetch_orders())
    #
    logger.info("NFO instrument dump: ")
    logger.info(kc.fetch_instruments("NFO"))
    #
    logger.info("Option Positions: ")
    logger.info(kc.fetch_optoin_positions())
    #
    logger.info("Infosys Last price: ")
    logger.info(kc.fetch_last_price("INFY", "NFO")) # Needs Historical data previllages
    #
'''
