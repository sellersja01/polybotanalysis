# Polymarket Trading Bot — Project Context

## What This Project Is
Automated analysis and trading system for Polymarket Up/Down binary markets.
Every 5 or 15 minutes, Polymarket opens a BTC/ETH/SOL/XRP market: Up token pays $1 if price went up, Down pays $0 (and vice versa). We collect live odds, backtest strategies, and are now building a **cross-platform arbitrage bot** between Polymarket and Kalshi.

## Infrastructure
- **VPS**: Oracle Cloud Always Free — `132.145.168.14` (opc user)
- **SSH key (PC)**: `C:\Users\James\btc-oracle-key\oracle-btc-collector.key`
- **SSH key (Laptop)**: `C:\Users\selle\oracle.key`
- **SSH command (PC)**: `ssh -i "C:\Users\James\btc-oracle-key\oracle-btc-collector.key" opc@132.145.168.14`
- **SSH command (Laptop)**: `ssh -i "C:\Users\selle\oracle.key" opc@132.145.168.14`
- **NOTE**: User will specify PC or Laptop at the start of each session — use the correct key
- **Storage**: 30GB boot volume (~11GB used), expandable to 200GB free
- **Oracle SSH rate limiter**: Blocks after ~4 rapid connections — if SSH fails, wait 10-30 min or have user SSH manually

## Kalshi API Credentials
- **Key ID**: `d307ccc8-df96-4210-8d42-8d70c75fe71f`
- **Key file (local)**: `C:\Users\James\kalshi_key.pem.txt`
- **Key file (VPS)**: `/home/opc/kalshi_key.pem`
- **Signing**: RSA-PSS with SHA-256 (NOT PKCS1v15)
- Kalshi crypto 15m Up/Down markets are **legal in the US** (CFTC-regulated)

## Processes Running on VPS (all nohup)
| Process | Log | Purpose |
|---------|-----|---------|
| `arb_collector.py` | `arb_collector.log` | **PRIMARY** — collects Polymarket + Kalshi prices simultaneously for BTC/ETH/SOL/XRP 15m |

**All old processes (collector_v2, paper traders, wallet_collector) were killed on 2026-03-26 to clear the VPS for the arb collector.**

**Restart arb collector after reboot:**
```bash
nohup python3 -u /home/opc/arb_collector.py > /home/opc/arb_collector.log 2>&1 &
```

## Databases on VPS (`/home/opc/`)
| DB | Contents |
|----|----------|
| `arb_collector.db` | **PRIMARY** — live side-by-side Polymarket + Kalshi prices for BTC/ETH/SOL/XRP 15m |
| `paper_v8_single.db` | Paper trader results (single entry) — no longer running |
| `paper_v8_layered.db` | Paper trader results (layered) — no longer running |
| `paper_contrarian.db` | Paper trader results (contrarian DCA) — no longer running |
| `wallet_trades.db` | All 9 wallet trade history — collector no longer running |

**Note**: The 5m/15m market DBs (market_btc_5m.db etc.) were wiped on 2026-03-26 to free space.

---

## Current Focus: Cross-Platform Arbitrage (Strategy 3)

### The Core Idea
Polymarket and Kalshi both run BTC/ETH/SOL/XRP 15-minute Up/Down binary markets simultaneously. When their prices diverge, you can buy the cheap side on one platform and the cheap side on the other — if `poly_up_ask + kalshi_dn_ask < 1.00`, you lock in a guaranteed profit at settlement regardless of outcome.

**Confirmed live**: We observed gaps of 5-6¢ between platforms in early live data (e.g. ETH: Poly up=0.64 vs Kalshi up=0.59).

### Fee Structure
**Polymarket taker fee:**
```python
fee = shares * price * 0.25 * (price * (1 - price)) ** 2
```
Peaks at ~1.56% at p=0.50. Maker rebate = 20%.

**Kalshi taker fee:**
```python
fee = 0.07 * p * (1 - p)
```
Peaks at ~1.75% at p=0.50. **Kalshi maker fee = 0% (limit orders).**

**Break-even**: Combined fees ~1.6-3.3% depending on price. Need at least that much gap to profit.
- A 6¢ gap at p=0.50 → ~3¢ net after fees ✓ profitable

### Kalshi Market Tickers (15m series)
| Asset | Series | Example ticker |
|-------|--------|----------------|
| BTC | `KXBTC15M` | `KXBTC15M-26MAR260745-45` |
| ETH | `KXETH15M` | `KXETH15M-26MAR260745-45` |
| SOL | `KXSOL15M` | `KXSOL15M-26MAR260745-45` |
| XRP | `KXXRP15M` | `KXXRP15M-26MAR260745-45` |

Kalshi ticker format: `{SERIES}-{DDMMMYY}{HHMM}-{MM}` where the last two digits are the minute offset.

### Polymarket WebSocket
- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market` (public, no auth)
- Market slug format: `{asset}-updown-15m-{unix_ts_aligned_to_900}`
- Subscribe with `{"type": "subscribe", "channel": "live_activity", "assets_ids": [up_token, dn_token]}`
- Prices come as `book` events with `bids`/`asks` arrays

### Kalshi WebSocket
- URL: `wss://api.elections.kalshi.com/trade-api/ws/v2`
- Requires RSA-PSS signed headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- Sign: `f"{timestamp_ms}GET/trade-api/ws/v2"` with RSA-PSS SHA-256
- Use `additional_headers=` (NOT `extra_headers=`) for websockets library on VPS
- Subscribe: `{"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"], "market_ticker": ticker}}`
- Messages: `{"type": "ticker", "msg": {"yes_bid_dollars": "0.59", "yes_ask_dollars": "0.60", ...}}`
- **Note**: WS ticker messages only include `yes_bid/ask`. Derive no prices: `no_bid = 1 - yes_ask`, `no_ask = 1 - yes_bid`

### arb_collector.py — The Data Collector
**File**: `c:\Users\James\polybotanalysis\arb_collector.py` → deployed at `/home/opc/arb_collector.py`

**What it does**:
- Connects to both Polymarket WS and Kalshi WS simultaneously
- Every price update from either platform writes a full snapshot row with BOTH platforms' prices
- Auto-reconnects on candle rollover every 15 minutes
- Waits for Kalshi to publish new candle market before subscribing (prevents stale-price bug)
- Records candle outcomes when yes_bid >= 0.99 or no_bid >= 0.99
- Prints status every 60s with current prices and gaps

**DB Schema** (`/home/opc/arb_collector.db`):
```sql
snapshots: ts, asset, candle_id, trigger, p_up_bid, p_up_ask, p_dn_bid, p_dn_ask,
           k_up_bid, k_up_ask, k_dn_bid, k_dn_ask
outcomes:  candle_id, asset, outcome (Up/Down), ts
```

**Status as of 2026-03-26**:
- Running since ~05:00 UTC, collecting all 4 assets
- ~255k total rows, ~77k active-range rows (both platforms in 0.05-0.95 range)
- Confirmed gaps of 5-6¢ observed in early data

**Known bugs fixed**:
- `extra_headers` → `additional_headers` (websockets version on VPS)
- Kalshi WS ticker only has `yes_bid/ask` — derive `no` prices mathematically
- After candle rollover, Kalshi API returns old resolved ticker — fixed by polling until `open_time >= candle_end`
- RSA-PSS (not PKCS1v15) required for Kalshi auth

### Arb Bot Files
| File | Purpose |
|------|---------|
| `arb_collector.py` | Live data collector — Poly + Kalshi side-by-side |
| `arb_bot/kalshi_client.py` | Kalshi REST + WS client with RSA-PSS auth |
| `arb_bot/polymarket_client.py` | Polymarket REST + WS client |
| `arb_bot/arb_detector.py` | `check_arb()` function, `ArbState` class |
| `arb_bot/main.py` | Full async arb bot (DRY_RUN mode) |
| `arb_bot/live_prices.py` | Snapshot price checker — both platforms |

---

## Previous Strategies (Archived — VPS processes killed 2026-03-26)

### Strategy 1: Wait-for-Divergence (V8)
- Buy BOTH sides when either mid drops to 0.25
- Hold winner to $1.00, early-exit loser at mid=0.20
- **Results**: BTC_5m 80.5% WR, 3.6% ROI (backtest). Live paper trading near-zero ROI (suspected weekend effect, never re-confirmed on weekdays)
- **Status**: Killed — pivoting to arb strategy

### Strategy 2: Contrarian Cheap-Side DCA
- DCA-buy the falling side at 0.40→0.35→0.30→0.25→0.20→0.15→0.10
- Hedge by buying expensive side once at mid=0.25; early-exit hedge at mid=0.20
- **Results**: 36% WR, ~23% ROI on 3,340 candle backtest
- **Status**: Killed — pivoting to arb strategy

### Fee Formula (Polymarket)
```python
fee = shares * price * 0.25 * (price * (1 - price)) ** 2
```
Peaks at 1.56% at p=0.50. Maker rebate = 20%.

---

## Wallet_7 Analysis (Concluded 2026-03-25)

- **67.0% WR | +$42,154 net | 2.04% ROI** on 315 candles
- **Conclusion**: Edge is NOT from directional prediction (share ratio only 1.04× — barely tilted)
- **Hypothesis**: Exploiting cross-platform mispricings between Polymarket and Binance/Kalshi — same idea as our arb bot
- IPWDCA strategy (two-sided symmetric DCA) tested and failed: -18% to -29% ROI

---

## Backtesting Rules (for any future backtests)
- **100% of candles** — never filter to decisive only
- **Winner = highest mid at last observed tick** (not >= 0.85 threshold)
- **Real fees** applied on every entry
- **Bid = 2×mid − ask** for exit prices (not mid)
- Entry at **ask price** (not mid)

---

## Next Steps
1. **Analyze arb_collector.db** — download after 24h+ of data, measure:
   - How often do gaps > 3¢ appear? (min profitable threshold after fees)
   - How large are typical gaps?
   - How long do gaps last? (determines if manual or bot execution is needed)
   - Which assets have the most/biggest gaps?
   - Do gaps correlate with time of day or market volatility?
2. **Build the live arb executor** once gap analysis confirms edge
3. **VPS storage**: expand boot volume to 200GB when approaching 25GB used
