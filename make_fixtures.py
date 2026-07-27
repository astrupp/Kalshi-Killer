"""Build synthetic exports covering the edge cases, with hand-computed answers."""
import pandas as pd
from pathlib import Path

d = Path(__file__).parent / "fixtures"
d.mkdir(exist_ok=True)

# 1. Kalshi PAIRED format: entry+exit on one row, prices in cents,
#    settlements at 0 / 100.
pd.DataFrame([
    # win held to resolution: (1.00-0.63)*100 = 37.00 gross, 2.00 fee -> 35.00
    dict(trade_id="k1", ticker="KXNBA-LAL", side="yes", count=100,
         entry_price_cents=63, exit_price_cents=100,
         entry_fee_cents=200, exit_fee_cents=0,
         entry_time="2026-03-01T14:00:00Z", exit_time="2026-03-02T02:00:00Z"),
    # loss held to resolution: (0.00-0.40)*50 = -20.00 gross, 1.00 fee -> -21.00
    dict(trade_id="k2", ticker="KXNBA-BOS", side="no", count=50,
         entry_price_cents=40, exit_price_cents=0,
         entry_fee_cents=100, exit_fee_cents=0,
         entry_time="2026-03-01T15:00:00Z", exit_time="2026-03-02T02:00:00Z"),
    # actively sold: (0.71-0.55)*200 = 32.00 gross, 5.50 fee -> 26.50
    dict(trade_id="k3", ticker="KXFED-JUN", side="yes", count=200,
         entry_price_cents=55, exit_price_cents=71,
         entry_fee_cents=300, exit_fee_cents=250,
         entry_time="2026-03-03T10:00:00Z", exit_time="2026-03-05T10:00:00Z"),
]).to_csv(d / "kalshi_paired.csv", index=False)

# 2. Kalshi LEDGER format: one row per transaction, tests FIFO across two lots
#    plus a leftover open position.
pd.DataFrame([
    dict(ticker="KXBTC-Z", side="yes", action="buy", count=100, price=30,
         fee=100, created_time="2026-04-01T09:00:00Z", trade_id="l1"),
    dict(ticker="KXBTC-Z", side="yes", action="buy", count=100, price=50,
         fee=100, created_time="2026-04-02T09:00:00Z", trade_id="l2"),
    # sells 150: 100 from the 30c lot, 50 from the 50c lot; 50 stay open
    dict(ticker="KXBTC-Z", side="yes", action="sell", count=150, price=60,
         fee=150, created_time="2026-04-03T09:00:00Z", trade_id="l3"),
]).to_csv(d / "kalshi_ledger.csv", index=False)

# 3. Polymarket: decimal prices, "side" means BUY/SELL (not YES/NO).
pd.DataFrame([
    dict(transactionHash="0xaa", title="will-x-happen", outcome="Yes",
         side="BUY", size=100, price=0.42, timestamp="2026-05-01T12:00:00Z"),
    dict(transactionHash="0xbb", title="will-x-happen", outcome="Yes",
         side="SELL", size=100, price=0.55, timestamp="2026-05-04T12:00:00Z"),
    # NO leg on the same market must be tracked separately from YES
    dict(transactionHash="0xcc", title="will-x-happen", outcome="No",
         side="BUY", size=80, price=0.60, timestamp="2026-05-02T12:00:00Z"),
]).to_csv(d / "poly_trades.csv", index=False)

print(f"fixtures -> {d}")
print("""
expected:
  kalshi_paired  net  35.00 + -21.00 + 26.50            =  40.50
  kalshi_ledger  net  28.00 +   4.00                    =  32.00  (50 left open)
  poly           net  13.00                             =  13.00  (80 NO open)
  ------------------------------------------------------------------
  TOTAL NET                                             =  85.50
  open lots: 2  (50 KXBTC-Z YES @ .50, 80 will-x-happen NO @ .60)
""")
