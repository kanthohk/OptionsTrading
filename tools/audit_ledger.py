"""Read-only audit of the kite_trades ledger, ahead of moving realised P&L
off the config buckets and onto FIFO.

Answers the three questions that gate the cutover:

  1. Is ``fill_timestamp`` ordered chronologically? FIFO is order-dependent by
     definition, and ``read_trades`` sorts on that column as a TEXT sort. If
     the stored format is an HTTP date ("Tue, 23 Sep 2026 ...") the sort runs
     alphabetically by weekday name and FIFO pairs the wrong lots. No amount
     of ledger coverage fixes that.

  2. Does the ledger cover every open book? A short opened before the ledger
     existed and closed inside it shows FIFO only the closing BUY, which is
     filed as a fresh open lot and yields realised = 0 - the loss silently
     disappears. Detected by reconciling the ledger's implied net position
     against the broker's live net quantity per symbol.

  3. How far apart are the two figures? Prints FIFO realised per
     (underlying, expiry) beside the config buckets, so the size of the jump
     is known before it moves a stop-loss.

Writes nothing: no config, no DB, no orders. Safe to run mid-session.

    python tools/audit_ledger.py                    # from the repo root
    python tools/audit_ledger.py --no-broker        # skip the HTTP call
    python tools/audit_ledger.py --user sashi --db trading.db
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# Importable when invoked as `python tools/audit_ledger.py` from the repo root,
# where sys.path[0] is tools/ rather than the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.basic import get_config, split_symbol  # noqa: E402
from MarketAnalysis.db_queries import settlement_spot  # noqa: E402
from helpers.trade_ledger import (  # noqa: E402
    CHARGE_PER_TRADE,
    LEDGER_EXCHANGE,
    expiry_settlement_fills,
    fifo_realized_pnl,
    sort_fills,
    net_realized_by_expiry,
)

CONFIG_FILE = "Monitor/config.yaml"
WEEK_GROUPS = ('first_week', 'second_week', 'third_week',
               'fourth_week', 'fifth_week')

# Formats fill_timestamp might plausibly be in. Order matters only for
# reporting; the first match wins.
_TS_FORMATS = [
    ("ISO datetime", "%Y-%m-%d %H:%M:%S"),
    ("ISO with T", "%Y-%m-%dT%H:%M:%S"),
    ("ISO date only", "%Y-%m-%d"),
    ("HTTP date (RFC 1123)", "%a, %d %b %Y %H:%M:%S %Z"),
    ("RFC 1123 no tz", "%a, %d %b %Y %H:%M:%S"),
]

OK, WARN, FAIL, INFO = "[ ok ]", "[WARN]", "[FAIL]", "[info]"

_findings = []


def emit(level, message):
    print(f"{level} {message}")
    if level in (WARN, FAIL):
        _findings.append((level, message))


def header(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


class _EmitLogger:
    """Minimal logger-shaped adapter so helpers.trade_ledger's warnings (e.g.
    "no spot recorded for this expiry") surface in the audit output and count
    toward the verdict, instead of being swallowed."""

    def info(self, message):
        print(f"{INFO} {message}")

    def warning(self, message):
        emit(WARN, message)

    def error(self, message):
        emit(FAIL, message)

    def exception(self, message):
        emit(FAIL, message)


# ---- 1. Timestamp health ------------------------------------------------

def detect_format(sample):
    for label, fmt in _TS_FORMATS:
        try:
            datetime.strptime(sample, fmt)
            return label, fmt
        except (ValueError, TypeError):
            continue
    return None, None


def audit_timestamps(rows):
    """Is the ledger's own sort order actually chronological?"""
    header("1. TIMESTAMP FORMAT AND SORT ORDER")
    stamps = [r.get("fill_timestamp") for r in rows]
    present = [s for s in stamps if s]
    if not present:
        emit(FAIL, "No fill_timestamp values at all - FIFO ordering cannot be "
                   "established. read_trades would sort on NULLs.")
        return
    if len(present) < len(stamps):
        emit(WARN, f"{len(stamps) - len(present)} of {len(stamps)} fills have "
                   f"no fill_timestamp; those sort first and pair wrongly.")

    label, fmt = detect_format(present[0])
    print(f"{INFO} sample: {present[0]!r}")
    if fmt is None:
        emit(WARN, "Unrecognised fill_timestamp format - cannot verify "
                   "ordering. Inspect manually.")
        return
    print(f"{INFO} format: {label}")

    if "HTTP date" in label or "RFC 1123" in label:
        emit(FAIL,
             "fill_timestamp is an HTTP date, so read_trades' "
             "`ORDER BY fill_timestamp` sorts ALPHABETICALLY BY WEEKDAY NAME "
             "(Fri < Mon < Sat < Sun < Thu < Tue < Wed), not chronologically. "
             "FIFO will pair the wrong lots. Fix the sort (or normalise the "
             "column) before cutover.")

    # The decisive check, independent of format: parse in the order the rows
    # came back and confirm it is non-decreasing.
    parsed, unparsed = [], 0
    for s in present:
        try:
            parsed.append(datetime.strptime(s, fmt))
        except (ValueError, TypeError):
            unparsed += 1
    if unparsed:
        emit(WARN, f"{unparsed} fill_timestamp value(s) did not match {label} "
                   f"- mixed formats in one column.")
    inversions = sum(1 for a, b in zip(parsed, parsed[1:]) if b < a)
    if inversions:
        emit(FAIL, f"{inversions} out-of-order pair(s) in the sequence "
                   f"read_trades returns. FIFO input is NOT chronological.")
    else:
        emit(OK, f"{len(parsed)} fills are in non-decreasing time order as "
                 f"returned - FIFO's ordering contract holds.")


# ---- 2. Coverage --------------------------------------------------------

def audit_coverage(rows):
    header("2. LEDGER COVERAGE")
    if not rows:
        emit(FAIL, "Ledger is empty. Every book would report realised = 0.")
        return
    users = sorted({r.get("user") for r in rows if r.get("user")})
    fetched = sorted(r["fetched_at"] for r in rows if r.get("fetched_at"))
    by_exchange = defaultdict(int)
    for r in rows:
        by_exchange[r.get("exchange")] += 1

    print(f"{INFO} {len(rows)} fills, {len(users)} user(s): {', '.join(users)}")
    print(f"{INFO} by exchange: "
          f"{', '.join(f'{k}={v}' for k, v in sorted(by_exchange.items(), key=lambda kv: str(kv[0])))}")
    if fetched:
        print(f"{INFO} fetched_at spans {fetched[0]} .. {fetched[-1]}")
        emit(INFO if len(fetched) else WARN,
             f"earliest coverage is {fetched[0][:10]} - any book opened before "
             f"that date is missing its opening fills")

    bad = [r for r in rows
           if not r.get("tradingsymbol")
           or (r.get("quantity") or 0) <= 0
           or (r.get("transaction_type") or "").upper() not in ("BUY", "SELL")]
    if bad:
        emit(WARN, f"{len(bad)} unusable row(s) will be skipped by FIFO.")
    else:
        emit(OK, "No malformed rows - FIFO input is clean.")


# ---- 3. Reconciliation against the broker -------------------------------

def ledger_net_by_symbol(rows):
    net = defaultdict(int)
    for r in rows:
        side = (r.get("transaction_type") or "").upper()
        qty = int(r.get("quantity") or 0)
        sym = r.get("tradingsymbol")
        if not sym or side not in ("BUY", "SELL") or qty <= 0:
            continue
        net[sym] += qty if side == "BUY" else -qty
    return dict(net)


def fetch_broker_positions(api_url, user):
    """``{tradingsymbol: net_qty}`` from the live broker, or None on failure."""
    from helpers.basic import api_get, ApiError
    url = f"{api_url}/get_positions?user={user}"
    try:
        success, payload = api_get(url, timeout=15, retries=0)
    except (ApiError, Exception) as e:      # noqa: BLE001 - audit must not die
        emit(WARN, f"Could not reach {url}: {e}")
        return None
    if not success:
        emit(WARN, f"Broker reported failure for /get_positions: {payload}")
        return None
    out = {}
    for p in (payload or {}).get(user) or []:
        if p.get("exchange") != LEDGER_EXCHANGE:
            continue
        out[p.get("tradingsymbol")] = int(p.get("quantity") or 0)
    return out


def audit_reconciliation(rows, broker, accept_incomplete=False):
    """THE GATE: ledger-implied net must equal the broker's net per symbol."""
    header("3. RECONCILIATION - LEDGER vs BROKER (the cutover gate)")
    ledger = ledger_net_by_symbol([r for r in rows
                                   if r.get("exchange") == LEDGER_EXCHANGE])
    open_in_ledger = {s: n for s, n in ledger.items() if n != 0}

    print(f"{INFO} {len(ledger)} NFO symbol(s) in ledger; "
          f"{len(open_in_ledger)} with a non-zero net")

    if broker is None:
        emit(WARN, "No broker data - cannot complete the gate check. "
                   "Re-run without --no-broker while flaskapp is up.")
        if open_in_ledger:
            print(f"\n{'symbol':<26}{'ledger_net':>12}")
            for sym, n in sorted(open_in_ledger.items()):
                print(f"{sym:<26}{n:>12}")
        return

    open_at_broker = {s: n for s, n in broker.items() if n != 0}
    print(f"{INFO} {len(open_at_broker)} symbol(s) open at broker")

    symbols = sorted(set(open_in_ledger) | set(open_at_broker))
    if not symbols:
        emit(OK, "Nothing open on either side - no reconciliation needed.")
        return

    # An EXPIRED contract is absent from the broker by definition, so a
    # non-zero ledger net on one is not a coverage gap - it means the position
    # was settled at expiry rather than closed by a trade, and settlement
    # generates no fill. Conflating the two sends you hunting for missing data
    # that was never going to exist.
    today = datetime.now().date()
    print(f"\n{'symbol':<26}{'expiry':<12}{'broker':>8}{'ledger':>8}{'delta':>8}  status")
    mismatched, expired_residue = [], []
    for sym in symbols:
        b = open_at_broker.get(sym, 0)
        l = open_in_ledger.get(sym, 0)
        d = l - b
        try:
            _, exp, _, _ = split_symbol(sym)
            exp_date = exp.date()
        except Exception:                    # noqa: BLE001
            exp_date = None
        is_expired = exp_date is not None and exp_date < today
        if d == 0:
            status = "ok"
        elif is_expired:
            status = "EXPIRED-RESIDUE"
            expired_residue.append((sym, b, l, d, exp_date))
        else:
            status = "MISMATCH"
            mismatched.append((sym, b, l, d))
        print(f"{sym:<26}{str(exp_date or '?'):<12}{b:>8}{l:>8}{d:>8}  {status}")

    if expired_residue:
        emit(WARN,
             f"{len(expired_residue)} symbol(s) on EXPIRED books still show a "
             f"ledger position. This deployment closes every position BEFORE "
             f"expiry day, so these cannot be held positions - they are "
             f"MISSING FILLS whose opening trade predates the ledger. The "
             f"broker cannot corroborate an expired contract, so this is the "
             f"only signal available. Their books' realised P&L is understated "
             f"by an unknown amount. (Expired books drive no live stop-loss, "
             f"so this is a historical-accuracy issue, not a live-risk one.)")
        for sym, b, l, d, exp_date in expired_residue:
            print(f"       {sym} (expired {exp_date}): ledger still holds {l}")

    if mismatched:
        # FAIL by default. --accept-incomplete downgrades to WARN for the case
        # where the gap has been consciously accepted: the opening fills exist
        # only in Kite Console, not the API, so no automated backfill can
        # recover them. The affected books are then flagged INCOMPLETE on every
        # monitor cycle instead of blocking the cutover indefinitely.
        emit(WARN if accept_incomplete else FAIL,
             f"{len(mismatched)} LIVE symbol(s) disagree with the broker. The "
             f"ledger is missing fills for these, so FIFO understates their "
             f"realised P&L"
             + (" - ACCEPTED: these books will be logged as INCOMPLETE every "
                "cycle and must not be treated as authoritative."
                if accept_incomplete else
                ". Seed lots are required before cutover."))
        for sym, b, l, d in mismatched:
            hint = ("ledger shows a phantom position - opening fills predate "
                    "the ledger") if abs(l) > abs(b) else \
                   ("broker holds more than the ledger explains - fills were "
                    "missed")
            print(f"       {sym}: broker={b} ledger={l} ({hint})")
    else:
        emit(OK, "Every open symbol reconciles exactly. The ledger fully "
                 "explains the current book - safe to cut over.")


# ---- 4. FIFO vs the config buckets --------------------------------------

def _bucket_values(cfg):
    """``{(underlying_or_None, week): pnl}`` for every bucket in the config.

    ``None`` scope means the top-level block, which an underlying falls back to
    when it defines no week blocks of its own.
    """
    out = {}
    for week in WEEK_GROUPS:
        block = cfg.get(week)
        if isinstance(block, dict) and 'pnl' in block:
            out[(None, week)] = float(block['pnl'] or 0)
    for name, spec in (cfg.get('underlyings') or {}).items():
        if not isinstance(spec, dict):
            continue
        for week in WEEK_GROUPS:
            block = spec.get(week)
            if isinstance(block, dict) and 'pnl' in block:
                out[(name, week)] = float(block['pnl'] or 0)
    return out


def audit_pnl_comparison(rows, charge, broker=None, settle_expired=False):
    header("4. FIFO REALISED P&L vs CONFIG BUCKETS")
    ledger = [r for r in rows if r.get("exchange") == LEDGER_EXCHANGE]
    if not ledger:
        emit(WARN, f"No {LEDGER_EXCHANGE} fills - nothing to compare.")
        return

    gross = net_realized_by_expiry(ledger, charge_per_trade=0)
    net = net_realized_by_expiry(ledger, charge_per_trade=charge)

    # Expiry settlement is OFF unless asked for. This deployment closes every
    # position before expiry day, so an expired residual lot is a missing fill,
    # not a held position - settling it would book a fabricated P&L (a phantom
    # long valued at intrinsic 0 invents a loss of buy_price * qty) and would
    # hide the gap. --settle-expired shows what settlement WOULD produce, for
    # the case of a confirmed unintended expiry.
    settlements = []
    if settle_expired:
        settlements = expiry_settlement_fills(ledger, settlement_spot,
                                              logger=_EmitLogger())
    settled = (net_realized_by_expiry(ledger + settlements,
                                      charge_per_trade=charge)
               if settlements else net)
    # Same computation over the order read_trades actually returned. FIFO total
    # is order-INVARIANT for a fully-closed symbol (every unit matches once, so
    # it collapses to sell_value - buy_value) but order-DEPENDENT once a lot is
    # left open. Differences here are therefore confined to partially-closed
    # books, and quantify what the stored-timestamp sort was distorting.
    unsorted_net = net_realized_by_expiry(ledger, charge_per_trade=charge,
                                          sort=False)

    print(f"{INFO} charges modelled at {charge}/fill")
    print(f"{INFO} 'settled' values expired residual lots at intrinsic; "
          f"'distortion' is what the old timestamp sort was costing\n")
    print(f"{'underlying':<12}{'expiry':<13}{'gross':>12}{'net':>12}"
          f"{'settled':>12}{'d.settle':>11}{'distortion':>12}")
    distorted, settled_books = [], []
    for key in sorted(set(net) | set(settled), key=lambda k: (str(k[1]), k[0])):
        raw = unsorted_net.get(key, 0)
        delta = net.get(key, 0) - raw
        s = settled.get(key, net.get(key, 0))
        d_settle = s - net.get(key, 0)
        if abs(delta) >= 1:
            distorted.append((key, delta))
        if abs(d_settle) >= 1:
            settled_books.append((key, d_settle))
        print(f"{key[0]:<12}{str(key[1]):<13}"
              f"{gross.get(key, 0):>12,.0f}{net.get(key, 0):>12,.0f}"
              f"{s:>12,.0f}{d_settle:>11,.0f}{delta:>12,.0f}")

    if settled_books:
        total = sum(d for _k, d in settled_books)
        emit(WARN,
             f"--settle-expired moved {len(settled_books)} book(s) by "
             f"{total:+,.0f}. TREAT WITH SUSPICION: every position here is "
             f"closed before expiry day, so these residual lots are missing "
             f"fills rather than held positions, and settling a phantom lot "
             f"invents P&L. Use this figure only for a confirmed unintended "
             f"expiry.")
    elif settle_expired:
        emit(INFO, "--settle-expired produced no change: no expired residual "
                   "lots, or no spot recorded for those expiry dates.")
    if distorted:
        (worst_u, worst_e), worst_d = max(distorted, key=lambda kv: abs(kv[1]))
        emit(WARN,
             f"{len(distorted)} book(s) differ between the stored order and "
             f"true chronological order - those are the ones the RFC 1123 sort "
             f"was corrupting. Largest: {worst_u} {worst_e} by {worst_d:,.0f}.")
    else:
        emit(OK, "No book changes under re-sorting: every book is either fully "
                 "closed (order-invariant) or its fills were already ordered.")

    cfg = get_config(CONFIG_FILE) or {}
    print(f"\n{INFO} current config buckets (what FIFO would replace):")
    any_bucket = False
    for week in WEEK_GROUPS:
        block = cfg.get(week)
        if isinstance(block, dict) and 'pnl' in block:
            any_bucket = True
            print(f"       {week:<14} pnl = {block['pnl']:>13,.2f}   (top level)")
    for name, spec in (cfg.get('underlyings') or {}).items():
        if not isinstance(spec, dict):
            continue
        for week in WEEK_GROUPS:
            block = spec.get(week)
            if isinstance(block, dict) and 'pnl' in block:
                any_bucket = True
                print(f"       {week:<14} pnl = {block['pnl']:>13,.2f}   ({name})")
    if not any_bucket:
        print("       (none found)")

    # The buckets are keyed by a book's RANK among the expiries currently HELD
    # (PositionProcessor._ordered_expiry_keys), NOT among every expiry the
    # ledger has ever seen. Ranking ledger expiries would be a different and
    # wrong mapping, so this is driven off the broker's open positions and is
    # skipped entirely when those are unavailable.
    buckets = _bucket_values(cfg)
    if broker is None:
        print(f"\n{INFO} slot mapping skipped: needs live positions "
              f"(re-run without --no-broker)")
    else:
        held = defaultdict(set)
        for sym, qty in broker.items():
            if not qty:
                continue
            try:
                underlying, exp, _, _ = split_symbol(sym)
            except Exception:                # noqa: BLE001
                continue
            held[underlying].add(exp.date())
        print(f"\n{INFO} slot mapping in force now (rank among HELD expiries), "
              f"bucket vs this book's own FIFO figure:")
        print(f"       {'book':<24}{'slot':<14}{'bucket':>13}{'fifo':>13}")
        used_slots = set()
        for underlying, expiries in sorted(held.items()):
            for idx, expiry in enumerate(sorted(expiries)):
                slot = WEEK_GROUPS[idx] if idx < len(WEEK_GROUPS) else "(no slot)"
                scope = underlying if (underlying, slot) in buckets else None
                bucket = buckets.get((scope, slot), buckets.get((None, slot)))
                used_slots.add((scope, slot))
                fifo = net.get((underlying, expiry))
                print(f"       {underlying + ' ' + str(expiry):<24}{slot:<14}"
                      f"{(f'{bucket:,.0f}' if bucket is not None else '-'):>13}"
                      f"{(f'{fifo:,.0f}' if fifo is not None else '-'):>13}")
        orphans = [(scope, slot, val) for (scope, slot), val in buckets.items()
                   if (scope, slot) not in used_slots and val]
        if orphans:
            emit(WARN,
                 f"{len(orphans)} bucket(s) hold P&L with NO book mapped to "
                 f"them - residue from expiries that have rolled off, which "
                 f"the next book to occupy that slot will inherit: "
                 + ", ".join(f"{s or 'top-level'}.{w}={v:,.0f}"
                             for s, w, v in orphans))
    emit(INFO, "If an expiry rolls off, every book below it shifts up one slot "
               "and inherits the previous occupant's accumulated total.")


# ---- 5. Per-symbol detail ----------------------------------------------

def audit_symbol_detail(rows, limit):
    header("5. PER-SYMBOL FIFO DETAIL")
    ledger = [r for r in rows if r.get("exchange") == LEDGER_EXCHANGE]
    # Sorted, so these figures agree with section 4's 'net' column rather than
    # silently reporting the distorted as-stored ordering.
    per_symbol = fifo_realized_pnl(sort_fills(ledger))
    fills = defaultdict(int)
    for r in ledger:
        fills[r.get("tradingsymbol")] += 1

    # Scan EVERY symbol in the ledger, not just those with realised P&L. A
    # symbol that is currently open has no FIFO match yet, so checking only
    # per_symbol would miss an unparseable name until the day it closes -
    # exactly when its P&L silently disappears.
    unparseable = []
    for sym in fills:
        try:
            parsed = split_symbol(sym)
            if not parsed or len(parsed) != 4:
                unparseable.append(sym)
        except Exception:                    # noqa: BLE001
            unparseable.append(sym)
    if unparseable:
        emit(WARN, f"{len(unparseable)} symbol(s) unparseable by split_symbol. "
                   f"Their realised P&L and charges are DROPPED from every "
                   f"per-book total: {sorted(unparseable)}")

    ranked = sorted(per_symbol.items(), key=lambda kv: -abs(kv[1]))
    print(f"{'symbol':<26}{'realised':>13}{'fills':>7}")
    for sym, pnl in ranked[:limit]:
        print(f"{sym:<26}{pnl:>13,.0f}{fills.get(sym, 0):>7}")
    if len(ranked) > limit:
        print(f"       ... {len(ranked) - limit} more (raise with --limit)")


# ---- main --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", help="account to audit (default: first in api_info.users)")
    ap.add_argument("--db", default=None, help="path to trading.db")
    ap.add_argument("--api-url", default=None, help="override api_info.url")
    ap.add_argument("--settle-expired", action="store_true",
                    help="value expired residual lots at intrinsic. OFF by "
                         "default: positions are closed before expiry here, so "
                         "expired residue means missing fills, and settling it "
                         "invents P&L")
    ap.add_argument("--accept-incomplete", action="store_true",
                    help="downgrade unreconcilable live symbols from FAIL to "
                         "WARN (their opening fills exist only in Kite Console)")
    ap.add_argument("--no-broker", action="store_true",
                    help="skip the /get_positions reconciliation")
    ap.add_argument("--charge", type=float, default=CHARGE_PER_TRADE,
                    help=f"per-fill charge to model (default {CHARGE_PER_TRADE})")
    ap.add_argument("--limit", type=int, default=25,
                    help="rows in the per-symbol table (default 25)")
    args = ap.parse_args()

    cfg = get_config(CONFIG_FILE) or {}
    api_info = cfg.get("api_info") or {}
    user = args.user or str(api_info.get("users") or "").split(",")[0].strip()
    api_url = args.api_url or api_info.get("url")
    if not user:
        print("No user: pass --user or set api_info.users in the config.")
        return 2

    # read_trades reads MarketAnalysis.db_queries.DB_FILE; point it at --db.
    from MarketAnalysis import db_queries
    if args.db:
        db_queries.DB_FILE = args.db
    db_path = db_queries.DB_FILE

    print(f"ledger audit - user={user} db={db_path} api={api_url or '(none)'}")
    if not os.path.exists(db_path):
        emit(FAIL, f"{db_path} does not exist. Run from the repo root, or pass --db.")
        return 1
    try:
        rows = db_queries.read_trades(user=user)
    except sqlite3.Error as e:
        emit(FAIL, f"Could not read the ledger: {e}")
        return 1
    if not rows:
        emit(FAIL, f"No fills for user {user!r}. Either kite_trades is absent, "
                   f"or the user string does not match the ledger.")
        return 1

    audit_timestamps(rows)
    audit_coverage(rows)
    broker = None if args.no_broker else (
        fetch_broker_positions(api_url, user) if api_url else None)
    if not args.no_broker and not api_url:
        emit(WARN, "api_info.url is unset - skipping broker reconciliation.")
    audit_reconciliation(rows, broker,
                         accept_incomplete=args.accept_incomplete)
    audit_pnl_comparison(rows, args.charge, broker=broker,
                         settle_expired=args.settle_expired)
    audit_symbol_detail(rows, args.limit)

    header("VERDICT")
    fails = [m for lvl, m in _findings if lvl == FAIL]
    warns = [m for lvl, m in _findings if lvl == WARN]
    if fails:
        print(f"{FAIL} NOT safe to cut over - {len(fails)} blocking issue(s):")
        for m in fails:
            print(f"       - {m}")
    elif warns:
        print(f"{WARN} Cutover plausible, {len(warns)} thing(s) to review:")
        for m in warns:
            print(f"       - {m}")
    else:
        print(f"{OK} No blocking issues. FIFO can replace the config buckets.")
    if warns and fails:
        print(f"\n{WARN} plus {len(warns)} warning(s):")
        for m in warns:
            print(f"       - {m}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
