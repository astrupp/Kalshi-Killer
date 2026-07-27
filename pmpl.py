#!/usr/bin/env python3
"""
pmpl.py -- Prediction-market P&L and settlement matcher.

Takes raw Kalshi and/or Polymarket fill exports and produces correct realized
P&L using FIFO lot matching, with settlements handled as settlements rather
than as phantom sales.

Three things this exists to get right, because generic importers get them wrong:

  1. Kalshi prices are integer cents. A 63c fill reads as `63`, not `0.63`.
     Auto-detected and normalized to dollars.
  2. Recent Kalshi exports collapse an open and a close into ONE row
     (entry_price / exit_price). Split into two events before matching.
  3. An exit at exactly 0 or 100 cents means the contract RESOLVED. That is a
     settlement, not a trade. Tagged separately so you can tell held-to-expiry
     outcomes from ones you actively closed.

YES and NO are tracked as separate instruments. On these venues they are
distinct contracts, and netting them silently corrupts per-position P&L.

Usage:
    python pmpl.py --kalshi kalshi_2026.csv --poly poly_trades.csv --out ./out
    python pmpl.py --kalshi kalshi_2026.csv --inspect     # show detected columns

If auto-detection picks the wrong columns, fix ALIASES below. That is the only
place schemas are hardcoded.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Schema mapping. Edit here if your export uses different headers.
# Keys are canonical names; values are candidate source headers (lowercased,
# non-alphanumerics stripped) in priority order.
# --------------------------------------------------------------------------

ALIASES: dict[str, list[str]] = {
    "market":       ["ticker", "marketticker", "market", "conditionid", "symbol",
                     "eventticker", "slug", "title"],
    "outcome":      ["side", "outcome", "yesno", "position", "contractside"],
    "action":       ["action", "type", "transactiontype", "tradetype", "buysell",
                     "direction", "orderside"],
    "quantity":     ["count", "quantity", "contracts", "size", "shares", "filledsize",
                     "numcontracts"],
    "price":        ["price", "priceusd", "fillprice", "avgprice", "executionprice",
                     "priceincents", "yesprice"],
    "entry_price":  ["entrypricecents", "entryprice", "openprice", "buyprice"],
    "exit_price":   ["exitpricecents", "exitprice", "closeprice", "sellprice"],
    "fee":          ["fee", "fees", "feepaid", "feeusd", "feecents", "totalfee",
                     "tradingfee"],
    "entry_fee":    ["entryfee", "entryfeecents", "openfee", "buyfee"],
    "exit_fee":     ["exitfee", "exitfeecents", "closefee", "sellfee"],
    "timestamp":    ["timestamp", "createdtime", "createdat", "time", "date",
                     "filledtime", "tradedate", "datetime"],
    "entry_ts":     ["entrytime", "entryts", "opentime", "entrytimestamp"],
    "exit_ts":      ["exittime", "exitts", "closetime", "exittimestamp"],
    "ref":          ["tradeid", "id", "transactionhash", "txhash", "orderid",
                     "fillid", "hash"],
}

# Venue-specific overrides. These matter: Kalshi uses "side" to mean YES/NO,
# Polymarket uses "side" to mean BUY/SELL. Same header, opposite meaning.
# Getting this backwards silently mislabels every row, so it is pinned per venue
# rather than guessed.
VENUE_ALIASES: dict[str, dict[str, list[str]]] = {
    "kalshi": {
        "outcome": ["side", "outcome", "yesno", "contractside"],
        "action":  ["action", "type", "transactiontype", "tradetype"],
    },
    "polymarket": {
        "outcome": ["outcome", "outcomeindex", "yesno"],
        "action":  ["side", "action", "type", "tradetype"],
        "market":  ["title", "slug", "conditionid", "market", "asset"],
        "ref":     ["transactionhash", "txhash", "id"],
    },
}

BUY_WORDS = {"buy", "b", "bought", "open", "long", "purchase", "yes_buy", "debit"}
SELL_WORDS = {"sell", "s", "sold", "close", "short", "yes_sell", "credit"}
SETTLE_WORDS = {"settle", "settlement", "settled", "resolve", "resolution",
                "expiry", "expire", "expired", "payout"}


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def detect_columns(df: pd.DataFrame, venue: str = "") -> dict[str, str]:
    """Map canonical field names onto whatever headers this file actually has.

    Venue-specific aliases take priority over the generic ones.
    """
    lookup = {_norm(c): c for c in df.columns}
    overrides = VENUE_ALIASES.get(venue, {})
    found: dict[str, str] = {}
    for canon, candidates in ALIASES.items():
        for cand in overrides.get(canon, []) + candidates:
            if cand in lookup:
                found[canon] = lookup[cand]
                break
    return found


def _outcome_column_is_sane(series: pd.Series) -> bool:
    """Guard against mapping a BUY/SELL column onto the YES/NO field."""
    vals = {_norm(v) for v in series.dropna().unique()[:50]}
    return not (vals & (BUY_WORDS | SELL_WORDS))


def looks_like_cents(series: pd.Series) -> bool:
    """Prediction-market prices live in (0,1). Anything above ~1.5 is cents."""
    vals = pd.to_numeric(series, errors="coerce").dropna().abs()
    vals = vals[vals > 0]
    if vals.empty:
        return False
    return vals.max() > 1.5


def to_dollars(series: pd.Series, force: str | None = None) -> pd.Series:
    vals = pd.to_numeric(series, errors="coerce").fillna(0.0)
    if force == "cents" or (force is None and looks_like_cents(series)):
        return vals / 100.0
    return vals


# --------------------------------------------------------------------------
# Canonical event model
# --------------------------------------------------------------------------

@dataclass
class Event:
    venue: str
    market: str
    outcome: str          # YES / NO
    action: str           # BUY / SELL / SETTLE
    quantity: float
    price_usd: float
    fee_usd: float
    timestamp: pd.Timestamp
    ref: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.venue, self.market, self.outcome)


def _classify_action(raw: str) -> str:
    n = _norm(raw)
    if n in SETTLE_WORDS or any(w in n for w in SETTLE_WORDS):
        return "SETTLE"
    if n in BUY_WORDS or n.startswith("buy"):
        return "BUY"
    if n in SELL_WORDS or n.startswith("sell"):
        return "SELL"
    return "BUY" if "b" == n[:1] else "SELL"


def _classify_outcome(raw: str) -> str:
    n = _norm(raw)
    if n.startswith("n") or n == "0" or "no" == n:
        return "NO"
    return "YES"


def load_events(path: Path, venue: str, price_unit: str | None = None,
                verbose: bool = False) -> tuple[list[Event], list[str]]:
    """Read one export file into canonical events. Returns (events, warnings)."""
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    cols = detect_columns(df, venue)
    warnings: list[str] = []

    if "outcome" in cols and not _outcome_column_is_sane(df[cols["outcome"]]):
        warnings.append(
            f"{path.name}: column '{cols['outcome']}' was mapped to YES/NO but "
            f"contains buy/sell values -- ignoring it and treating all rows as YES. "
            f"Fix VENUE_ALIASES['{venue}']['outcome'] if that is wrong."
        )
        cols.pop("outcome")

    if verbose:
        print(f"\n[{venue}] {path.name}: {len(df)} rows")
        print(f"  headers found : {list(df.columns)}")
        print(f"  mapped        : {cols}")

    if "market" not in cols:
        raise SystemExit(
            f"{path.name}: could not find a market/ticker column. "
            f"Headers were: {list(df.columns)}. Add the right name to ALIASES['market']."
        )
    if "quantity" not in cols:
        raise SystemExit(
            f"{path.name}: could not find a quantity/count column. "
            f"Headers were: {list(df.columns)}. Add it to ALIASES['quantity']."
        )

    paired = "entry_price" in cols and "exit_price" in cols
    events: list[Event] = []

    qty = pd.to_numeric(df[cols["quantity"]], errors="coerce").fillna(0.0).abs()
    market = df[cols["market"]].astype(str)
    outcome = (df[cols["outcome"]].map(_classify_outcome)
               if "outcome" in cols else pd.Series(["YES"] * len(df)))
    ref = (df[cols["ref"]].astype(str) if "ref" in cols
           else pd.Series([f"row{i}" for i in range(len(df))]))

    if paired:
        # ---- one row = an open AND a close -------------------------------
        warnings.append(
            f"{path.name}: paired-row format detected (entry+exit on one line); "
            f"split into {len(df)} opens and {len(df)} closes."
        )
        entry_px = to_dollars(df[cols["entry_price"]], price_unit)
        exit_px_raw = pd.to_numeric(df[cols["exit_price"]], errors="coerce").fillna(0.0)
        exit_px = to_dollars(df[cols["exit_price"]], price_unit)
        exit_is_cents = looks_like_cents(df[cols["exit_price"]]) or price_unit == "cents"
        bound_hi = 100.0 if exit_is_cents else 1.0

        entry_fee = (to_dollars(df[cols["entry_fee"]], price_unit)
                     if "entry_fee" in cols else pd.Series([0.0] * len(df)))
        exit_fee = (to_dollars(df[cols["exit_fee"]], price_unit)
                    if "exit_fee" in cols else pd.Series([0.0] * len(df)))

        entry_ts = pd.to_datetime(
            df[cols.get("entry_ts", cols.get("timestamp"))], errors="coerce", utc=True)
        exit_ts = pd.to_datetime(
            df[cols.get("exit_ts", cols.get("timestamp"))], errors="coerce", utc=True)

        n_settled = 0
        for i in range(len(df)):
            if qty.iloc[i] <= 0:
                continue
            events.append(Event(venue, market.iloc[i], outcome.iloc[i], "BUY",
                                qty.iloc[i], entry_px.iloc[i], entry_fee.iloc[i],
                                entry_ts.iloc[i], ref.iloc[i]))
            # exit at exactly 0 or max => resolved, not sold
            raw = exit_px_raw.iloc[i]
            is_settle = (raw == 0.0) or (abs(raw - bound_hi) < 1e-9)
            if is_settle:
                n_settled += 1
            events.append(Event(venue, market.iloc[i], outcome.iloc[i],
                                "SETTLE" if is_settle else "SELL",
                                qty.iloc[i], exit_px.iloc[i], exit_fee.iloc[i],
                                exit_ts.iloc[i], ref.iloc[i]))
        if n_settled:
            warnings.append(
                f"{path.name}: {n_settled} exits were at 0 or {bound_hi:g} "
                f"-> tagged SETTLE (held to resolution), not SELL."
            )
    else:
        # ---- one row = one transaction -----------------------------------
        if "price" not in cols:
            raise SystemExit(
                f"{path.name}: no price column found. Headers: {list(df.columns)}"
            )
        price = to_dollars(df[cols["price"]], price_unit)
        fee = (to_dollars(df[cols["fee"]], price_unit)
               if "fee" in cols else pd.Series([0.0] * len(df)))
        ts = pd.to_datetime(df[cols["timestamp"]], errors="coerce", utc=True) \
            if "timestamp" in cols else pd.Series([pd.NaT] * len(df))
        action = (df[cols["action"]].map(_classify_action)
                  if "action" in cols else pd.Series(["BUY"] * len(df)))

        if "action" not in cols:
            warnings.append(
                f"{path.name}: no action/side column found; assumed every row is a BUY. "
                f"Check this."
            )
        if looks_like_cents(df[cols["price"]]):
            warnings.append(f"{path.name}: prices looked like cents; divided by 100.")

        for i in range(len(df)):
            if qty.iloc[i] <= 0:
                continue
            events.append(Event(venue, market.iloc[i], outcome.iloc[i],
                                action.iloc[i], qty.iloc[i], price.iloc[i],
                                fee.iloc[i], ts.iloc[i], ref.iloc[i]))

    events.sort(key=lambda e: (pd.Timestamp.min.tz_localize("UTC")
                               if pd.isna(e.timestamp) else e.timestamp))
    return events, warnings


# --------------------------------------------------------------------------
# FIFO matching
# --------------------------------------------------------------------------

@dataclass
class Lot:
    qty: float
    price: float
    fee_per_unit: float
    timestamp: pd.Timestamp
    ref: str


@dataclass
class Book:
    lots: dict[tuple, deque] = field(default_factory=dict)

    def open(self, e: Event) -> None:
        fpu = (e.fee_usd / e.quantity) if e.quantity else 0.0
        self.lots.setdefault(e.key, deque()).append(
            Lot(e.quantity, e.price_usd, fpu, e.timestamp, e.ref))

    def close(self, e: Event) -> tuple[list[dict], float]:
        """Match a close against open lots FIFO. Returns (rows, unmatched_qty)."""
        rows: list[dict] = []
        remaining = e.quantity
        exit_fpu = (e.fee_usd / e.quantity) if e.quantity else 0.0
        q = self.lots.get(e.key)
        while remaining > 0 and q:
            lot = q[0]
            matched = min(remaining, lot.qty)
            gross = (e.price_usd - lot.price) * matched
            fees = (lot.fee_per_unit + exit_fpu) * matched
            rows.append({
                "venue": e.venue,
                "market": e.market,
                "outcome": e.outcome,
                "quantity": matched,
                "entry_price": round(lot.price, 4),
                "exit_price": round(e.price_usd, 4),
                "exit_type": e.action,             # SELL or SETTLE
                "gross_pnl": round(gross, 4),
                "fees": round(fees, 4),
                "net_pnl": round(gross - fees, 4),
                "opened_at": lot.timestamp,
                "closed_at": e.timestamp,
                "entry_ref": lot.ref,
                "exit_ref": e.ref,
            })
            lot.qty -= matched
            remaining -= matched
            if lot.qty <= 1e-9:
                q.popleft()
        return rows, remaining

    def open_positions(self) -> list[dict]:
        out = []
        for (venue, market, outcome), q in self.lots.items():
            for lot in q:
                if lot.qty > 1e-9:
                    out.append({
                        "venue": venue, "market": market, "outcome": outcome,
                        "quantity": lot.qty, "entry_price": round(lot.price, 4),
                        "cost_basis": round(lot.qty * lot.price, 4),
                        "opened_at": lot.timestamp, "entry_ref": lot.ref,
                    })
        return out


def match(events: list[Event]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    book = Book()
    closed: list[dict] = []
    warnings: list[str] = []
    unmatched = 0.0

    for e in events:
        if e.action == "BUY":
            book.open(e)
        else:
            rows, left = book.close(e)
            closed.extend(rows)
            if left > 1e-9:
                unmatched += left
                warnings.append(
                    f"unmatched close: {left:g} of {e.market} {e.outcome} "
                    f"({e.action}, ref {e.ref}) had no open lot -- "
                    f"missing history, or a sell-to-open."
                )

    if unmatched:
        warnings.insert(0, f"TOTAL {unmatched:g} contracts closed with no matching open. "
                           f"Earlier-year exports are probably missing.")

    return (pd.DataFrame(closed), pd.DataFrame(book.open_positions()), warnings)


def summarize(closed: pd.DataFrame) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    g = closed.groupby(["venue", "market", "outcome"], as_index=False).agg(
        contracts=("quantity", "sum"),
        gross_pnl=("gross_pnl", "sum"),
        fees=("fees", "sum"),
        net_pnl=("net_pnl", "sum"),
        n_closes=("net_pnl", "size"),
    )
    return g.sort_values("net_pnl", ascending=False).round(4)


def report(closed: pd.DataFrame, open_pos: pd.DataFrame) -> str:
    lines = ["", "=" * 62, "REALIZED P&L", "=" * 62]
    if closed.empty:
        lines.append("No closed positions matched.")
        return "\n".join(lines)

    net = closed["net_pnl"].sum()
    gross = closed["gross_pnl"].sum()
    fees = closed["fees"].sum()
    n = len(closed)
    wins = (closed["net_pnl"] > 0).sum()
    contracts = closed["quantity"].sum()
    cost = (closed["entry_price"] * closed["quantity"]).sum()

    lines += [
        f"  closed lots        {n}",
        f"  contracts          {contracts:,.0f}",
        f"  capital deployed   ${cost:,.2f}",
        f"  gross P&L          ${gross:,.2f}",
        f"  fees               ${fees:,.2f}",
        f"  net P&L            ${net:,.2f}",
        f"  return on cost     {(net / cost * 100) if cost else 0:,.2f}%",
        f"  win rate           {wins}/{n} ({wins / n * 100:.1f}%)",
    ]

    by_exit = closed.groupby("exit_type").agg(
        lots=("net_pnl", "size"), net=("net_pnl", "sum"))
    lines += ["", "  by exit type:"]
    for k, r in by_exit.iterrows():
        lines.append(f"    {k:<8} {int(r['lots']):>4} lots   ${r['net']:>12,.2f}")

    if not open_pos.empty:
        lines += ["", f"  still open: {len(open_pos)} lots, "
                      f"${open_pos['cost_basis'].sum():,.2f} at cost "
                      f"(excluded from realized P&L)"]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kalshi", type=Path, action="append", default=[],
                   help="Kalshi CSV export (repeatable, one per year)")
    p.add_argument("--poly", type=Path, action="append", default=[],
                   help="Polymarket trades CSV (repeatable)")
    p.add_argument("--out", type=Path, default=Path("./out"))
    p.add_argument("--price-unit", choices=["cents", "dollars"], default=None,
                   help="Override cents auto-detection")
    p.add_argument("--inspect", action="store_true",
                   help="Show detected column mapping and exit")
    args = p.parse_args()

    if not args.kalshi and not args.poly:
        p.error("give at least one --kalshi or --poly file")

    events: list[Event] = []
    warnings: list[str] = []
    for f in args.kalshi:
        ev, w = load_events(f, "kalshi", args.price_unit, verbose=True)
        events += ev
        warnings += w
    for f in args.poly:
        ev, w = load_events(f, "polymarket", args.price_unit, verbose=True)
        events += ev
        warnings += w

    if args.inspect:
        print(f"\nParsed {len(events)} events. Rerun without --inspect to match.")
        return

    events.sort(key=lambda e: (pd.Timestamp.min.tz_localize("UTC")
                               if pd.isna(e.timestamp) else e.timestamp))
    closed, open_pos, mwarn = match(events)
    warnings += mwarn

    args.out.mkdir(parents=True, exist_ok=True)
    if not closed.empty:
        closed.to_csv(args.out / "realized_pnl.csv", index=False)
        summarize(closed).to_csv(args.out / "by_market.csv", index=False)
    if not open_pos.empty:
        open_pos.to_csv(args.out / "open_positions.csv", index=False)

    print(report(closed, open_pos))
    if warnings:
        print("\n" + "-" * 62 + "\nWARNINGS\n" + "-" * 62)
        for w in warnings[:25]:
            print(f"  ! {w}")
        if len(warnings) > 25:
            print(f"  ... and {len(warnings) - 25} more")
    print(f"\nwrote -> {args.out.resolve()}\n")


if __name__ == "__main__":
    main()
