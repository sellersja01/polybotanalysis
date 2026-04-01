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
- ~305k total rows, ~114k active-range rows (both platforms in 0.05-0.95 range)
- Confirmed gaps of 5-6¢ observed in early data

### Arb Gap Analysis Results (7.7 hours of data, 2026-03-26)

**Tick-level profitability (Scenario B: Poly taker + Kalshi maker):**
- **68.8% of all ticks are net profitable** after fees
- Avg net gap on profitable ticks: **4.8¢ per $1 contract**
- Best asset: BTC (79% profitable, avg 5.1¢) and XRP (67.8%, avg 6.8¢)
- SOL worst (44.5% profitable, avg 2.8¢)

**Per-candle execution simulation:**
- 93% of candles have at least one arb opportunity (gap >= 2¢)
- Avg best gap per candle: **15.9¢** (= $15.90 per $100 deployed)
- At $100/trade, 1 trade/candle: **~$4,200/day extrapolated**
- At $1,000/trade: **~$42,000/day extrapolated**

**Gap persistence:**
- Median streak duration: **1 second** (most gaps flash open/close)
- But avg is 13.5s — pulled up by fat, long-lasting windows
- Some windows last **minutes to hours** (longest: 3.3 hours on BTC)
- 20% of windows last >= 5 seconds; 3% >= 30 seconds
- Big gaps (>20¢) last avg **29 minutes** before closing

**Fee regime change March 30, 2026:**
- Polymarket taker formula changing: `0.25 * (p*(1-p))^2` → `0.072 * (p*(1-p))^1`
- Peak fee goes from 1.56% to 1.80% — slightly higher across all prices
- Impact on arb: minimal (~1% drop in daily PnL)

**Combined fee breakdowns at p=0.50:**
| Scenario | Combined Fee | Break-even Gap |
|----------|-------------|----------------|
| Poly taker + Kalshi taker (now) | 2.53¢ | ~3¢ |
| Poly taker + Kalshi maker (now) | 0.78¢ | ~1¢ |
| Poly taker + Kalshi taker (Mar 30) | 2.65¢ | ~3¢ |
| Poly taker + Kalshi maker (Mar 30) | 0.90¢ | ~1¢ |

**Known bugs fixed**:
- `extra_headers` → `additional_headers` (websockets version on VPS)
- Kalshi WS ticker only has `yes_bid/ask` — derive `no` prices mathematically
- After candle rollover, Kalshi API returns old resolved ticker — fixed by polling until `open_time >= candle_end`
- RSA-PSS (not PKCS1v15) required for Kalshi auth

### Arb Bot Files
| File | Purpose |
|------|---------|
| `arb_collector.py` | Live data collector — Poly + Kalshi side-by-side |
| `arb_bot/config.py` | All constants, fee functions (auto-switches Mar 30), thresholds |
| `arb_bot/kalshi_client.py` | Kalshi REST + WS — persistent session, RSA-PSS auth |
| `arb_bot/polymarket_client.py` | Polymarket REST + WS — persistent session, placeholder order signing |
| `arb_bot/arb_detector.py` | Reverse ticker index (O(1)), dedup cooldown, stale price rejection |
| `arb_bot/executor.py` | Parallel execution, nanosecond latency tracking, SQLite trade log |
| `arb_bot/main.py` | Entrypoint — session warmup, wires feeds + detector + executor |
| `arb_bot/market_mapper.py` | Maps Poly markets to Kalshi tickers |
| `arb_bot/live_prices.py` | Snapshot price checker — both platforms |
| `arb_analysis.py` | Arb gap analysis (tick-level, per-asset, per-candle) |
| `arb_analysis_v2.py` | Full fee scenario comparison + execution simulation |
| `arb_gap_speed.py` | Gap persistence / closing speed analysis |

### Arb Bot Architecture (built 2026-03-26)
Optimized for sub-50ms execution:
1. **Persistent aiohttp sessions** — TLS handshake once at startup, reused for every trade
2. **Connection warmup** — throwaway GETs at startup to pre-establish TCP+TLS
3. **Sequential legs** — Polymarket fires first; if it fails, Kalshi never fires (prevents unhedged positions)
4. **Reverse ticker index** — O(1) Kalshi ticker → condition_id lookup
5. **Dedup** — won't re-fire same pair+direction within 30s cooldown
6. **Stale price rejection** — ignores prices older than 12s
7. **Latency tracking** — every trade logs detect→fire, per-leg, and total latency to SQLite

**Measured latency from PC (local):** ~800ms total (acceptable since gaps last seconds to minutes)

---

### Live Trading — Confirmed Working (2026-03-28)

**Bot successfully placed real orders on both platforms.**

#### Credentials / Wallet Setup
- **Polymarket proxy wallet** (where funds sit): `0x6826c3197fff281144b07fe6c3e72636854769ab`
- **MetaMask EOA** (signs transactions): `0x4795e77317792011c8967de46441f586987101fc`
- **Polymarket API key**: `019d323e-f149-794e-8c3b-6c1df3877250`
- **POLY_PRIVATE_KEY** env var = MetaMask private key for `0x4795e...`
- **KALSHI_KEY_PATH** env var = `C:\Users\James\kalshi_key.pem.txt`

#### Run the bot
```powershell
$env:DRY_RUN = "false"
$env:POLY_PRIVATE_KEY = "0x<metamask_private_key>"
python main.py
```

#### py_clob_client API (local version — differs from docs)
- **No `LimitOrderArgs`** — use `OrderArgs` from `py_clob_client.clob_types`
- **No `BUY` constant** — use the string `"BUY"` directly
- **No `create_limit_order`** — use `clob.create_order(args)`
- **`post_order(signed, order_type)`** — use `"FOK"` (Fill or Kill) for arb
- `OrderType` enum only has `"GTC"`, `"FOK"`, `"GTD"` — `"IOC"` is not valid, defaults to GTC

#### ClobClient initialization (proxy wallet mode)
```python
from py_clob_client.client import ClobClient
clob = ClobClient(
    host=POLY_CLOB_URL,
    key=POLY_PRIVATE_KEY,       # MetaMask EOA private key
    chain_id=137,                # Polygon
    signature_type=2,            # proxy wallet mode
    funder="0x6826c3197fff281144b07fe6c3e72636854769ab",  # proxy wallet
)
creds = clob.create_or_derive_api_creds()
clob.set_api_creds(creds)
```

#### Kalshi order placement — confirmed working
- **Signing path**: must include `/trade-api/v2` prefix — sign `f"{ts}POST/trade-api/v2/portfolio/orders"`
- **Remove `time_in_force` and `post_only`** from order body — these fields cause 400 validation errors
- Body format that works:
```python
{"ticker": ticker, "side": side, "action": "buy", "count": count, "yes_price": price_cents}
```

#### Polymarket order execution — confirmed working approach
- **Use `MarketOrderArgs` with `amount = size * price` USDC + `side="BUY"`** — fills immediately at AMM price
- **`post_order(signed, "FAK")`** — FAK (not FOK) for market orders; FOK fails with "order couldn't be fully filled"
- `signature_type=2` + `funder=0x6826c...` required or you get "balance: 0" error
- Limit orders (OrderArgs + FAK/FOK) fail with "no orders found to match" — AMM has no depth at exact price
- AMM fills ~3.7 shares when requesting 5 at $2.90 (slippage eats into share count)

#### Share matching between legs
- Polymarket AMM fills a non-integer number of shares (e.g. 3.7179)
- Executor reads `takingAmount` from Poly response → rounds to nearest integer → sends to Kalshi
- **Kalshi `count` field is strictly an integer** — rejects ALL decimals (3.7, 6.2, 4.6, 2.0) with `"cannot unmarshal number X into Go struct field CreateOrderRequest.count of type int"`
- Kalshi fires AFTER Poly confirms fill — no unhedged positions
- Max share mismatch = 0.5 shares (round to nearest integer)

#### Kalshi order placement — updated
- **Remove `time_in_force` and `post_only`** — cause 400 errors
- **`count` must be an integer** — `int(round(float(count)))`
- Add **+10¢ buffer** to `k_price_cents` so limit order crosses spread immediately: `min(int(kalshi_ask * 100) + 10, 99)`
- Without buffer, GTC order sits resting when price moves away

#### Current bot config (2026-03-28 evening)
- `SHARES_PER_TRADE = 10` (raised from 5 to reduce relative rounding error)
- `MAX_TRADES_PER_CANDLE = 3`
- `MIN_PROFIT_CENTS = 2.0` (lowered from 5.0 — 2¢ is profitable above break-even)
- `DRY_RUN` defaults to `true` — must explicitly set `$env:DRY_RUN = "false"` to go live

#### Reference price gap filter (IN PROGRESS — not yet validated)
- Polymarket "price to beat" and Kalshi "at least" target price should match each candle
- If gap > $6, platforms are tracking different strike prices — arb may not hedge correctly
- `market_mapper.py` now fetches `poly_ref_price` and `kalshi_ref_price` for each pair
- **TODO**: Validate that the correct API fields are being read (check `ref_gap=` in startup logs)
  - Polymarket: trying `event.startValue` / `start_value` / `openValue` / `open_value`
  - Kalshi: trying `floor_strike` / `cap_strike` / `strike` / `yes_sub_title`
  - If startup shows `ref_gap=unknown`, fields are wrong and need to be fixed

#### Successful trades confirmed
- Kalshi: 201 responses, shares filled correctly
- Polymarket: 200 `"status": "matched", "success": true`
- Multiple confirmed live trades on 2026-03-28 evening
- Both legs executing reliably as of 2026-03-28

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
- **Conclusion**: Wallet_7 was almost certainly running **latency arbitrage** — monitoring Binance price and buying the correct side on Polymarket before odds reprice
- IPWDCA strategy (two-sided symmetric DCA) tested and failed: -18% to -29% ROI

---

## NEW PRIMARY STRATEGY: Latency Arbitrage (Strategy 4, 2026-03-30)

### The Edge
Polymarket reprices its crypto Up/Down contracts **slower than Binance moves**. When BTC moves on Binance, there is a measurable lag before Polymarket odds reflect the move. During this lag, the bot buys the "correct" side at stale odds.

### Backtest Results — HONEST (184 hours, BTC 5m, 30s exit)
Enter on EVERY BTC move >= 0.05% in 15s. No cherry-picking. Track real outcomes.

| Metric | Value |
|--------|-------|
| Total trades | 2,398 |
| **Win rate** | **49.5%** |
| **Avg win** | **+$23.25** per $100 |
| **Avg loss** | **-$3.60** per $100 |
| **Win/Loss ratio** | **6.47x** (wins 6.5x bigger than losses) |
| Avg profit per trade | +$9.68 per $100 |
| Max loss | -$61.32 (entry at 0.90 — too high) |
| Median loss | -$1.22 (most losses are tiny) |
| Daily PnL at $100/trade | ~$3,027/day |

**Key insight: edge is NOT win rate — it's asymmetry.** Wins average $23, losses average $3.60.

### Backtest Results — ALL MARKETS (184 hours, 30s exit)
| Market | Trades | WR% | Avg $/trade | Daily PnL | Median Lag |
|--------|--------|-----|-------------|-----------|------------|
| BTC 5m | 2,398 | 49.5% | +$9.68 | $3,027/day | 7.5s |
| BTC 15m | 2,205 | 48.6% | +$6.73 | $1,485/day | 6.8s |
| ETH 5m | 3,487 | 47.4% | +$10.88 | $3,793/day | 4.7s |
| ETH 15m | 3,490 | 48.6% | +$7.03 | $3,605/day | 4.6s |
| SOL 15m | 3,638 | 48.3% | +$7.66 | $4,089/day | 6.3s |
| XRP 15m | 3,428 | 48.0% | +$7.80 | $3,929/day | 6.3s |
| **TOTAL** | **19,772** | | | **~$25,026/day** | |

### Max Entry Price Sweep (BTC 5m backtest)
| Max Entry | Trades | WR% | Avg Loss | Max Loss | $/day |
|-----------|--------|-----|----------|----------|-------|
| 0.40 | 1,665 | 48.3% | -$3.20 | -$38 | $2,311 |
| 0.50 | 2,179 | 47.9% | -$3.41 | -$49 | $2,764 |
| 0.60 | 2,397 | 49.5% | -$3.55 | -$50 | **$3,035** |
| 0.70 | 2,398 | 49.5% | -$3.60 | -$61 | $3,027 |
| 0.80+ | 2,398 | 49.5% | -$3.60 | -$61 | $3,027 |

Above 0.60 adds no trades but increases max loss. Big losses (-$64 live) come from entries > 0.80.

### How It Works
1. Monitor Binance BTC WebSocket for real-time price
2. When BTC moves >= 0.05% in 15 seconds, determine direction (up/down)
3. Check current Polymarket odds — if they haven't repriced yet, buy the correct side
4. Exit after Polymarket reprices (typically 2-30 seconds later)

### Why It Works
- Polymarket is a CLOB — prices only update when traders post orders
- After a Binance move, there's a 2-30 second window where Polymarket odds are stale
- Buying the correct side at stale odds is not prediction — it's reading information that already exists
- The lag has compressed from 12s (2024) to ~7.5s median (2026) but still very exploitable

### Comparison to Cross-Platform Arb (Strategy 3)
| | Latency Arb | Cross-Platform Arb |
|---|---|---|
| Platforms needed | Polymarket + Binance (free feed) | Polymarket + Kalshi |
| Capital locked | 1 platform | 2 platforms |
| Win rate | ~49% (but 6.5x win/loss ratio) | ~100% (risk-free) |
| Avg profit/trade | **$9.68 per $100** | $4.80 per $100 |
| Complexity | Simpler (one trade) | Two coordinated trades |
| Risk | Lag shrinking over time | One leg fails |
| Daily PnL ($100/trade) | **~$3,027** (BTC 5m alone) | ~$4,200 |

**Latency arb is the primary strategy. Cross-platform arb is secondary/backup.**

### Live Paper Trading Results (2026-03-30)
- Ran `paper_test.py` on laptop — connects to public Binance + Polymarket WebSockets
- **First session**: 5 trades, 5 wins, +$89.58 in 5 minutes
- **Second session**: 41 trades, 36 wins (88%), +$121.91
- Big losses came from entries at 0.82-0.94 ask — fixed by capping max entry
- Bot only enters when BTC is moving (>0.05% in 15s) — goes quiet in flat markets
- **Cannot run on Oracle VPS** — Binance.com blocks US IPs (HTTP 451)
- Binance.us works but has almost no volume (3 ticks/20s vs 6,000/min on .com)

### Latency Bot Files (built 2026-03-30)
| File | Purpose |
|------|---------|
| `arb_bot/binance_feed.py` | Real-time BTC price from Binance WS, rolling buffer, move detection |
| `arb_bot/latency_detector.py` | Compares Binance moves vs Poly odds, fires signal when stale |
| `arb_bot/latency_bot.py` | Main bot — wires feeds + detector + executor, DRY_RUN, SQLite logging |
| `arb_bot/paper_test.py` | Self-contained paper trading test (no auth needed) |
| `latency_lag_v2.py` | Fast backtest — loads all data into memory |
| `latency_lag_honest.py` | Honest backtest — enters on every signal, no cherry-picking |
| `latency_lag_all.py` | Multi-market backtest (BTC/ETH/SOL/XRP × 5m/15m) |

### Paper Test Config (`paper_test.py`)
```python
LOOKBACK = 15        # BTC move lookback (seconds)
MOVE_THRESH = 0.05   # min BTC move % to trigger
COOLDOWN = 2         # seconds between trades
MAX_ENTRY_PRICE = 0.80  # don't buy above this
MAX_OPEN = 5         # max simultaneous open trades
EXIT_TIMEOUT = 60    # close after 60s if no 2c reprice
```

### Risk: Edge Compression
- Lag was 12s in 2024, now ~7.5s median. Trend is clear — more bots = shorter window
- At some point the lag will be smaller than execution latency (~50-200ms with VPN)
- Treat this as a **time-limited opportunity** — extract value while edge exists
- Pivot to cross-platform arb or market making when latency arb stops working

### Risk Management (from 0x8dxd analysis)
- Max single position: **8% of portfolio**
- Daily loss limit: **-20%** with automatic halt
- Total drawdown kill switch: **-40%**
- Position sizing: **Kelly Criterion**
- Paper trade for **minimum 1 week** (200+ trades) before going live

---

## Galindrast Wallet Analysis (2026-03-31)

### Profile
- **Wallet**: `0xeebde7a0e019a63e6b476eb425505b7b3e6eba30`
- **Username**: Galindrast (pseudonym: Popular-Insurrection)
- **Joined**: March 25, 2026 (6 days before analysis)
- **$1,500 → $128,000** in 6 days = **85x return**
- **4,485 trades** | $14M volume | **+$133k net PnL**
- **Markets**: BTC 5m (67%), ETH 5m (31%), BTC hourly + 4h (2%)
- **Collector running on Oracle VPS**: `galindrast_collector.py` → `/home/opc/galindrast_trades.db`

### Strategy Breakdown (from 24,604 trades over 8.7 hours)

**Phase 1 — Initial Entry (0-15 seconds):**
- Enters BOTH sides within 5-11 seconds of candle start
- 94% of first trades happen in the first 30 seconds
- First trade avg price: 0.51 (near 50/50 — before knowing direction)
- Small size: avg 35 shares, ~$17 per side

**Phase 2 — DCA Throughout (15-250 seconds):**
- Buys BOTH sides throughout the entire candle
- 97% of candles have direction switches (buying both Up and Down)
- 63% of trades happen in the SAME SECOND (burst execution)
- 86% of trades within 5 seconds of previous trade
- 2,149 bursts of 3+ trades in 3 seconds detected

**Phase 3 — Resolution Scalp (when one side hits 0.90+):**
- Deploys **10x more capital** at 0.90-1.00 (avg 195 shares vs 22 at other levels)
- 512,529 shares at 0.90-1.00 = **$505k** (69% of total volume)
- This is where the profit comes from: buy at $0.95, resolve at $1.00 = $0.05/share × thousands

**Phase 4 — Cut Losses (late candle):**
- 3.7% of trades are SELLS at avg price 0.17
- Sells happen avg 207 seconds into candle (late)
- Dumping the losing side before resolution to recover some capital

### Key Metrics
| Metric | Value |
|--------|-------|
| Trades/candle | 99.6 avg (86 median) |
| USDC/candle | $2,960 avg ($1,261 median) |
| Bought both sides | 96% of candles |
| Share ratio (primary/secondary) | 3.6x avg, 2.5x median |
| Direction switches mid-candle | 97% of candles |
| Candles where BOTH sides bought above 0.70 | 48/160 (30%) — reversals |

### How Reversals Are Handled
- 30% of candles have a full reversal (one side goes to 0.80+ then crashes)
- When reversal happens, bot **flips to the new winning side** and starts resolution scalping that side
- Accepts the loss on the first direction, tries to make it back on the reversal
- Does NOT stop buying — just switches which side it's scaling into

### Profitability Math
- Early both-sides buying: ~$80 per candle (small, informational)
- Resolution scalp (winner at 0.95): ~$950 deployed → $1,000 payout = **$50 profit**
- Losing side: $40 cost - $13.60 sold at 0.17 = **-$26.40 loss**
- Net per candle: ~$23.60 (but varies widely)
- Slippage at their scale ($10k+/candle) eats ~1-2%, reducing PnL/volume from ~2.9% (backtest) to ~0.96% (actual)

### DCA Backtest Results (our data, 184 hours, BTC 5m)
Simulated Galindrast-style: BTC signal → DCA every 15s → scale up at high confidence → hold to resolution.

| Metric | Value |
|--------|-------|
| Candles with signal | 790 |
| Win rate | 71.0% |
| Avg win | +$60.24 |
| Avg loss | -$111.73 |
| Avg PnL/candle | +$10.39 |
| Daily PnL (base) | $1,070/day |
| Daily PnL (30x scale) | $32,095/day |

### Live Paper Test (3 candles, 15 minutes, 2026-03-31)
| Candle | Dir | Entries | Cost | Payout | PnL | ROI |
|--------|-----|---------|------|--------|-----|-----|
| #1 | UP | 11 | $528 | $560 | +$31.69 | 6.0% |
| #2 | UP | 14 | $121 | $200 | +$78.53 | 64.7% |
| #3 | DOWN | 14 | $305 | $370 | +$64.71 | 21.2% |
| **Total** | | | **$955** | **$1,130** | **+$174.93** | **18.3%** |

### Files
| File | Purpose |
|------|---------|
| `galindrast_collector.py` | Live trade collector (running on Oracle VPS) |
| `galindrast_deep_analysis.py` | Full behavioral analysis (timing, sizing, direction) |
| `resolution_scalp_backtest.py` | Resolution scalp backtest by threshold |
| `resolution_scalp_v2.py` | Galindrast-style entry + scaling backtest |
| `resolution_scalp_v3.py` | DCA throughout candle backtest |
| `arb_bot/paper_test_dca.py` | Live paper test — DCA to resolution |
| `databases/galindrast_trades.db` | Local copy of collected trades (24k+) |

---

## Backtesting Rules (for any future backtests)
- **100% of candles** — never filter to decisive only
- **Winner = highest mid at last observed tick** (not >= 0.85 threshold)
- **Real fees** applied on every entry
- **Bid = 2×mid − ask** for exit prices (not mid)
- Entry at **ask price** (not mid)

---

## VPN / Geo Requirement
- **Polymarket blocks US IPs** — need a non-US IP to trade
- User has **NordVPN** (Netherlands exit) — only needed on PC to create/access Polymarket account
- **Bot VPS: Hetzner Falkenstein, Germany (~€4/mo)** — EU IP, no geo-block, direct Polymarket access
  - Polymarket: direct from Germany, no VPN needed, ~10ms
  - Kalshi: Germany → Ohio, ~100ms (acceptable, our gaps last seconds to minutes)
- **NEVER access Polymarket account from a US IP** — would flag the account
- **Kalshi is US-regulated, accessible from anywhere** — no VPN needed

## Polymarket Account Setup (COMPLETE — 2026-03-28)
- [x] MetaMask wallet connected to Polymarket (`0x4795e...` EOA, `0x6826c...` proxy)
- [x] ~$95 USDC deposited and available to trade
- [x] API credentials working (`019d323e...`)
- [x] Bot placing real orders on both platforms
- [ ] Deploy to Hetzner VPS (Monday)

## Next Steps
1. **Set up Hetzner VPS in Germany/Netherlands** (~€4/mo) — required because:
   - Binance.com blocks US IPs (Oracle VPS in Ashburn gets HTTP 451)
   - Polymarket blocks US IPs (needs non-US IP)
   - EU VPS solves both: Binance accessible, Polymarket accessible, no VPN needed
2. **Build production bot** combining both strategies:
   - **Latency arb**: Single-entry on BTC signal, exit on Poly reprice (fast, high frequency)
   - **DCA + resolution scalp (Galindrast-style)**: Enter both sides, DCA throughout, scale into winner at 0.90+
   - Both strategies are profitable independently — can run both or pick one
3. **Wire up Polymarket order execution:**
   - Already working from cross-platform arb work (proxy wallet `0x6826c...`, ~$95 USDC)
   - `MarketOrderArgs` + `FAK` order type confirmed working
   - `signature_type=2` + `funder=0x6826c...` required
4. **Deploy bot on Hetzner VPS** in DRY_RUN mode — verify latency and live signals
5. **Go live small** — $1-5/trade, scale gradually on evidence
6. **Add ETH 5m support** — Galindrast trades 31% ETH, backtest shows more signals than BTC
7. **Keep galindrast_collector running on Oracle VPS** — track their evolving strategy
8. **Keep arb_collector running on Oracle VPS** — cross-platform data for backup strategy
9. **Paper trade DCA strategy for 1+ week** — need 200+ candles to validate 71% WR holds live
