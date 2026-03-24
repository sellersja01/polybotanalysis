# Polymarket Trading Bot — Project Context

## What This Project Is
Automated analysis and paper trading system for Polymarket Up/Down binary markets.
Every 5 or 15 minutes, Polymarket opens a BTC/ETH market: Up token pays $1 if price went up, Down pays $0 (and vice versa). We collect live odds, backtest strategies, and paper trade them.

## Infrastructure
- **VPS**: Oracle Cloud Always Free — `132.145.168.14` (opc user)
- **SSH key (PC)**: `C:\Users\James\btc-oracle-key\oracle-btc-collector.key`
- **SSH key (Laptop)**: `C:\Users\selle\oracle.key`
- **SSH command (PC)**: `ssh -i "C:\Users\James\btc-oracle-key\oracle-btc-collector.key" opc@132.145.168.14`
- **SSH command (Laptop)**: `ssh -i "C:\Users\selle\oracle.key" opc@132.145.168.14`
- **NOTE**: User will specify PC or Laptop at the start of each session — use the correct key
- **Storage**: 30GB boot volume (11GB used), expandable to 200GB free

## Processes Running on VPS (all nohup)
| Process | Log | Purpose |
|---------|-----|---------|
| `collector_v2.py` | `collector.log` | Collects live BTC/ETH odds every few seconds (via auto-restart wrapper) |
| `paper_trader_v8.py` | `v8_single.log` | Paper trader — single entry at mid=0.25 |
| `paper_trader_v8_layered.py` | `v8_layered.log` | Paper trader — all 5 levels (0.45→0.25) |
| `paper_trader_contrarian.py` | `contrarian.log` | Paper trader — contrarian cheap-side DCA (launched 2026-03-24) |
| `wallet_collector.py` | `wallet_collector.log` | Polls 9 wallets every 60s for new trades |

**Collector runs via auto-restart wrapper** (`run_collector.sh`):
```bash
# Already running — survives crashes automatically
# To start manually:
nohup bash /home/opc/run_collector.sh > collector.log 2>&1 &
```

**Restart paper traders after VPS reboot:**
```bash
nohup python3 -u paper_trader_v8.py > v8_single.log 2>&1 &
nohup python3 -u paper_trader_v8_layered.py > v8_layered.log 2>&1 &
nohup python3 -u paper_trader_contrarian.py > contrarian.log 2>&1 &
nohup python3 -u wallet_collector.py > wallet_collector.log 2>&1 &
```

## Databases on VPS (`/home/opc/`)
| DB | Contents |
|----|----------|
| `market_btc_5m.db` | Live BTC 5m odds (primary analysis DB) |
| `market_btc_15m.db` | Live BTC 15m odds |
| `market_eth_5m.db` | Live ETH 5m odds |
| `market_eth_15m.db` | Live ETH 15m odds (avoid — negative ROI) |
| `paper_v8_single.db` | Paper trader results (single entry) |
| `paper_v8_layered.db` | Paper trader results (layered) |
| `paper_contrarian.db` | Paper trader results (contrarian DCA) |
| `wallet_trades.db` | All 9 wallet trade history |

**Local copies** (3-day snapshot used for backtesting):
`C:\Users\selle\git_repository\polybotanalysis\databases\market_btc_5m.db` etc.

## The Strategies (Backtested & Paper Trading)

### Strategy 1: Wait-for-Divergence (V8)
- Monitor BTC_5m, BTC_15m, ETH_5m (NOT ETH_15m)
- When either side's mid drops to threshold, buy BOTH sides at current ask
- If loser's mid drops to 0.20, early exit at bid = 2×mid − ask
- Hold winner to $1.00 resolution
- **Winner = whichever side has higher mid at last observed tick (100% of candles)**

#### V8 Backtest Results (100% candles, real fees, 3-day sample)
| Config | Market | n | WR% | ROI% | $/candle |
|--------|--------|---|-----|------|----------|
| Single 0.25 | BTC_5m | 742 | 80.5% | 3.60% | +$3.59 |
| Single 0.25 | BTC_15m | 248 | 79.8% | 1.63% | +$1.62 |
| Single 0.25 | ETH_5m | 702 | 80.3% | 1.56% | +$1.57 |
| Single 0.25 | ETH_15m | 248 | 74.6% | -2.15% | -$2.15 ❌ |
| All 5 levels | BTC_5m | 790 | 76.6% | 2.89% | +$13.07 |
| All 5 levels | BTC_15m | 258 | 77.1% | 0.39% | +$1.80 |
| All 5 levels | ETH_5m | 735 | 77.1% | 0.20% | +$0.93 |
| All 5 levels | ETH_15m | 256 | 71.1% | -3.13% | -$14.63 ❌ |

#### V8 Live Paper Trading Results (as of 2026-03-24, ~2 days)
| Config | n | WR% | ROI% | Note |
|--------|---|-----|------|------|
| V8 Single | 688 | 80.2% | -0.11% | Near-zero, likely weekend effect |
| V8 Layered | 701 | 78.9% | -0.57% | Near-zero, likely weekend effect |
WR matches backtest perfectly. ROI near-zero likely due to weekend low-volatility trading — need weekday data to confirm edge.

---

### Strategy 2: Contrarian Cheap-Side DCA (NEW — paper trading since 2026-03-24)

**Why we're running it:** Reverse-engineered from wallet_7 (a profitable on-chain trader with 67% WR, +$42k on $2M deployed). Analysis showed wallet_7 mechanically DCA-buys whichever side is falling cheapest, using the early hedge exit as the key edge. Backtested across 3,340 candles (BTC_5m + BTC_15m + ETH_5m) showing ~23% ROI — significantly stronger than V8. Triggers on 96%+ of all candles.

**How it works:**
1. Watch both sides of each candle
2. Whichever side first drops to mid=0.40 becomes the **cheap side**
3. DCA-buy the cheap side at each level as it keeps falling: `0.40 → 0.35 → 0.30 → 0.25 → 0.20 → 0.15 → 0.10`
4. When cheap side hits mid=0.25, also buy the **expensive side once** (hedge, 100 shares)
5. If the hedge side later drops to mid=0.20 (meaning cheap side has risen to ~0.80 and is winning), **early exit the hedge** at bid = 2×mid − ask to recoup capital
6. Hold the cheap side to $1.00 resolution

**Key insight:** The cheap side gets temporarily depressed by momentum traders, but mean-reverts to win ~36% of the time. The hedge on the expensive side is pure insurance — if cheap side wins, the hedge is exited early for partial recovery; if cheap side loses, the hedge pays $1.00.

**Config (paper trader):**
- `CHEAP_SHARES = 100` per level (7 levels = up to 700 shares per candle)
- `HEDGE_SHARES = 100` once at mid=0.25
- `EXIT_MID = 0.20` for hedge early exit

#### Contrarian Backtest Results (100% candles, real fees, 3,340 candles)
| Config | n | WR% | ROI% | $/candle | avg cost/candle |
|--------|---|-----|------|----------|-----------------|
| Cheap only 100sh + exit | 3340 | 36.1% | 23.6% | +$32 | $137 |
| 3:1 (300sh cheap / 100sh hedge) | 3340 | 38.3% | 21.6% | +$105 | $486 |
| 5:1 (500sh cheap / 100sh hedge) | 3340 | 37.8% | 22.4% | +$170 | $760 |
| 10:1 (1000sh cheap / 100sh hedge) | 3340 | 36.9% | 23.1% | +$334 | $1,447 |

**At $1,000 deployed per candle: ~$215–236/candle across all configs.**

**File:** `paper_trader/paper_trader_contrarian.py` → deployed at `/home/opc/paper_trader_contrarian.py`

### Fee Formula (Polymarket Crypto markets)
```python
fee = shares * price * 0.25 * (price * (1 - price)) ** 2
```
Peaks at 1.56% at p=0.50. Maker rebate = 20%.

### Key Decisions Made
- **ETH_15m excluded** — consistently negative ROI across all configs
- **Winner determination**: use highest mid at last tick (not 0.85/0.15 threshold) — includes 100% of candles
- **Exit price**: bid = 2×mid − ask (realistic taker sell)
- **Early exit at mid=0.20** is the optimal threshold

## Polymarket Market Structure
- Up/Down tokens always sum to ~1.00
- When one side hits mid=0.25, the other is at ~0.75
- Strategy triggers on EITHER side dropping — catches moves in both directions
- Flat candles (both sides stay near 0.50) → no entry, no loss
- ~84% of BTC_5m candles trigger at mid=0.25 threshold

## The 9 Tracked Wallets
```python
WALLETS = {
    'wallet_1': '0x61276aba49117fd9299707d5d573652949d5c977',
    'wallet_2': '0x5bde889dc26b097b5eaa2f1f027e01712ebccbb7',
    'wallet_3': '0xd111ced402bac802f74606deca83bbf6a1eaaf32',
    'wallet_4': '0x437bfe05a1e169b1443f16e718525a88b6f283b2',
    'wallet_5': '0x52f8784a81d967a3afb74d2e1608503ff5e261b9',
    'wallet_6': '0xa84edaf1a562eabb463dc6cf4c3e9c407a5edbeb',
    'wallet_7': '0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82',
    'wallet_8': '0xf539c942036cc7633a1e0015209a1343e9b2dda9',
    'wallet_9': '0x37c94ea1b44e01b18a1ce3ab6f8002bd6b9d7e6d',
}
```
Wallet trades stored in `/home/opc/wallet_trades.db` — ~3,300-3,500 trades per wallet backfilled, then continuous 60s polling.

## Key Files
| File | Purpose |
|------|---------|
| `paper_trader/paper_trader_v8.py` | Paper trader — single 0.25 entry (V8) |
| `paper_trader/paper_trader_v8_layered.py` | Paper trader — all 5 levels (V8) |
| `paper_trader/paper_trader_contrarian.py` | Paper trader — contrarian cheap-side DCA |
| `layered_entry_backtest.py` | Main backtest (100% candles, real fees) |
| `early_exit_backtest.py` | Early exit strategy backtest |
| `wallet_collector.py` | Continuous wallet trade collector |
| `wallet7_strategy_analysis.py` | Statistical analysis of wallet_7 strategy |
| `show_pnl.py` | View paper trader PnL |

## Backtesting Rules (ALWAYS follow these)
- **100% of candles** — never filter to decisive only
- **Winner = highest mid at last observed tick** (not >= 0.85 threshold)
- **Real Polymarket fees** applied on every entry
- **Bid = 2×mid − ask** for exit prices (not mid)
- Entry at **ask price** (not mid)

## Wallet_7 Analysis (2026-03-24)
- 315 candles analyzed, all resolved via Polymarket API
- **67.0% WR | +$42,154 net | 2.04% ROI | $133/candle avg**
- Deploys ~$6,500/candle on average
- Strategy identified as contrarian cheap-side DCA (mechanically buys cheapest side)
- Lower ROI than our backtest (~2% vs ~23%) likely because they don't aggressively early-exit the losing side

## Next Steps / Open Questions
- **Contrarian paper trader** (launched 2026-03-24) — need 1-2 weeks of data to validate ~23% backtest ROI
- **V8 paper traders** — 2 days of data showing near-zero ROI; likely weekend effect, check again after weekdays
- Consider going live with contrarian strategy once paper trading confirms edge
- Wallet collector running — analyze other wallets once more data accumulates
- VPS storage: expand boot volume to 200GB when approaching 25GB used
