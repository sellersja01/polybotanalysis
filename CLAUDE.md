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
- **Use `MarketOrderArgs` with `amount = size * price` USDC** — fills immediately at AMM price
- **`signature_type=2` + `funder=0x6826c...`** — required or you get "balance: 0" error
- **`post_order(signed, "FOK")`** — FOK works with market orders
- `price=0.99` limit orders give wrong share count (e.g., 6.4 instead of 5) — don't use
- FOK limit orders at exact price fail: "order couldn't be fully filled" — don't use

#### Share matching between legs
- Polymarket AMM may fill fewer shares than requested (e.g., 3.5 instead of 5)
- **Executor reads `takingAmount` from Poly response and matches Kalshi to that exact number**
- Kalshi fires AFTER Poly confirms fill — no unhedged positions

#### Successful trades confirmed
- Kalshi: 201 responses, shares filled correctly
- Polymarket: 200 `"status": "matched", "success": true`
- One confirmed `fired=1 success=1 profit=$1.19` on 2026-03-28
- Both legs executing live as of 2026-03-29

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
1. ~~Fix Polymarket FOK fill issue~~ — DONE: using MarketOrderArgs + share matching
2. Spin up Hetzner CX22 (Falkenstein, Ubuntu 24.04)
3. Deploy bot on Hetzner — EU IP, no VPN needed for Polymarket, ~10ms latency
4. Run at higher share counts once execution is stable on Hetzner

## Environment Setup (each new terminal session)
```powershell
$env:DRY_RUN = "false"
$env:KALSHI_KEY_PATH = "C:\Users\James\kalshi_key.pem.txt"
$env:POLY_PRIVATE_KEY = "0x<metamask_private_key>"
cd C:\Users\James\polybotanalysis\arb_bot
python main.py
```
