# Live Trades Log

Raw log of live-s13 entries + user-confirmed fills + mid-candle position snapshots.
Reference for post-hoc analysis — what did we enter, what did we actually get, what resolved where.

---

## Session 2026-04-21 17:22–17:32 UTC (10-min test, all 4 markets, pre-sign enabled)

### Entries (from `/root/live_s13.log`)

| # | UTC time | Asset | Side | Log ask | Fill shares | Spent | Avg fill | Latency breakdown |
|---|---|---|---|---|---|---|---|---|
| 1 | 17:23:02.905 | SOL | Down | 0.060 | 50.000 | $2.00 | **$0.040** | pre=6, sign=0, post=425, **TOTAL=430ms** |
| 2 | 17:23:54.407 | ETH | Down | 0.030 | 200.000 | $2.00 | **$0.010** | pre=1, sign=0, post=428, **TOTAL=428ms** |
| 3 | 17:25:12.073 | BTC | Up   | 0.560 | 3.333  | $2.00 | **$0.600** | pre=1, sign=0, post=599, **TOTAL=601ms** |

All 3 used pre-signed (`[PS]`) path, sign=0ms confirmed.

### User-confirmed position snapshot (taken mid-candle)

| Asset | Side | Cost basis | Shares | Entry→Now | Value | Return |
|---|---|---|---|---|---|---|
| ETH | Down | **1¢** | 185.7 | 1¢ → 34.5¢ | $64.06 | **+$62.21 (3,349%)** |
| SOL | Down | **4¢** | 46.5  | 4¢ → 35.5¢ | $16.52 | **+$14.66 (787.5%)** |
| BTC | Up   | **60¢** | 3.2   | 60¢ → 52.5¢ | $1.70 | **−$0.24 (−12.49%)** |

Notes on share-count discrepancy:
- Log says SOL=50 shares, Polymarket UI says 46.5 → ~7% of order lost to platform fees/rounding
- Log says ETH=200 shares, UI says 185.7 → same 7% gap
- Log says BTC=3.333 shares, UI says 3.2 → ~4% gap

### Observations
- **The 1¢ and 4¢ entries are asymmetric bets.** If Down wins → ~$185 / $46 payouts. If loses → $2 each.
- **SOL and ETH Down markets both moved strongly toward our side** after entry (up 30–34¢) — we were right about the direction even though we entered at fire-sale prices.
- **BTC Up was the "normal" entry (60¢) and it's the one in the red** — 7.5¢ slippage from entry to now.
- Pattern consistent with earlier theory: on ~50¢ entries we get hammered by slippage; on sub-10¢ entries the asymmetric payoff dominates and direction alone matters.

### Questions to revisit
- Do cheap entries (<0.10 ask) have meaningfully different WR than mid-range entries (0.40–0.60)? Need candle-resolution data.
- Should we weight `TRADE_USD` higher when ask is very low? (At $0.01 ask, $2 gets you 200 shares = $200 upside. Could we safely deploy $10 there?)

---
