# prediction-market-pnl

Correct realized P&L for Kalshi and Polymarket fills, using FIFO lot matching
with settlements handled as settlements.

Generic crypto-tax importers get three things wrong on these venues. This gets
them right:

**1. Kalshi prices are integer cents.** A 63¢ fill exports as `63`, not `0.63`.
Import that into a tool expecting decimals and every number is off by 100x.
Auto-detected and normalized.

**2. Recent Kalshi exports put the open and the close on one row.** One line
carries `entry_price` and `exit_price` together. It has to be split into two
events before any matching logic will work.

**3. An exit at exactly 0 or 100 means the contract resolved.** That's a
settlement, not a sale. Tools that treat it as a sale can't distinguish "I got
out at a good price" from "I was right about the outcome" — which is the only
distinction that matters if you're evaluating a strategy.

It also tracks YES and NO as separate books. Buying NO at 40¢ is economically
similar to selling YES at 60¢, but netting them destroys per-position P&L.

## Usage

```bash
python3 pmpl.py --kalshi your_2026.csv --inspect          # check column mapping
python3 pmpl.py --kalshi your_2026.csv --poly poly.csv --out ./out
```

Outputs `realized_pnl.csv` (per matched lot), `by_market.csv`, and
`open_positions.csv`.

```
==============================================================
REALIZED P&L
==============================================================
  closed lots        6
  contracts          600
  capital deployed   $290.00
  gross P&L          $97.00
  fees               $11.50
  net P&L            $85.50
  return on cost     29.48%
  win rate           5/6 (83.3%)

  by exit type:
    SELL        4 lots   $       71.50
    SETTLE      2 lots   $       14.00

  still open: 2 lots, $73.00 at cost (excluded from realized P&L)
```

The `by exit type` split is the useful part if you're grading a strategy. Only
the `SETTLE` row is evidence about whether your predictions were right.

## Getting your data

**Kalshi:** account menu → Documents → export trade history CSV (one per
calendar year — export every year you traded, or closes won't match to opens).

**Polymarket:** `data-api.polymarket.com/trades?user=<your_address>`, or the UI
export.

## Column detection

Headers vary between exports and change over time, so mapping is fuzzy rather
than hardcoded. Run `--inspect` first to see what it detected. If something's
wrong, fix `ALIASES` or `VENUE_ALIASES` at the top of the file — that's the only
place schemas live.

One collision worth knowing about: Kalshi uses `side` to mean YES/NO, Polymarket
uses `side` to mean BUY/SELL. Same header, opposite meaning. It's pinned per
venue, and there's a runtime guard that catches a BUY/SELL column mapped onto the
YES/NO field.

## Failure behavior

It warns loudly instead of silently balancing. Feed it one year when you traded
across two and you get:

```
! TOTAL 75 contracts closed with no matching open. Earlier-year exports are probably missing.
```

rather than a confident wrong number.

## Testing

`make_fixtures.py` generates synthetic exports covering the paired-row format,
FIFO across multiple lots, settlements at both bounds, and decimal-priced
Polymarket data — with hand-computed expected values in the output.

## What this doesn't do

No tax treatment. Whether event contracts fall under Section 1256, ordinary
income, or gambling is genuinely unsettled and the IRS hasn't issued guidance.
This gives you clean realized P&L per lot with dates, which is the input to any
of those treatments. Picking one is a question for a CPA.

## License

MIT. Not affiliated with Kalshi or Polymarket.
