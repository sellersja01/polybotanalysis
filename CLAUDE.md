# Polymarket Trading Bot — Project Context

## What This Project Is
Automated analysis and paper trading system for Polymarket Up/Down binary markets.
Every 5 or 15 minutes, Polymarket opens a BTC/ETH market: Up token pays $1 if price went up, Down pays $0 (and vice versa). We collect live odds, backtest strategies, and paper trade them.

## Infrastructure
- **VPS**: Oracle Cloud Always Free — `132.145.168.14` (opc user)
- **SSH key**: `C:\Users\James\btc-oracle-key\oracle-btc-collector.key`
- **SSH command**: `ssh -i "C:\Users\James\btc-oracle-key\oracle-btc-collector.key" opc@132.145.168.14`
- **Storage**: 30GB boot volume (11GB used), expandable to 200GB free

## Processes Running on VPS (all nohup)
| Process | Log | Purpose |
|---------|-----|---------|
| `collector_v2.py` | `collector.log` | Collects live BTC/ETH odds every few seconds |
| `paper_trader_v8.py` | `v8_single.log` | Paper trader — single entry at mid=0.25 |
| `paper_trader_v8_layered.py` | `v8_layered.log` | Paper trader — all 5 levels (0.45→0.25) |
| `wallet_collector.py` | `wallet_collector.log` | Polls 9 wallets every 60s for new trades |

**Restart all after VPS reboot:**
```bash
nohup python3 -u collector_v2.py > collector.log 2>&1 &
nohup python3 -u paper_trader_v8.py > v8_single.log 2>&1 &
nohup python3 -u paper_trader_v8_layered.py > v8_layered.log 2>&1 &
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
| `wallet_trades.db` | All 9 wallet trade history |

**Local copies** (3-day snapshot used for backtesting):
`C:\Users\James\polybotanalysis\market_btc_5m.db` etc.

## The Strategy (Backtested & Paper Trading)

### Wait-for-Divergence
- Monitor BTC_5m, BTC_15m, ETH_5m (NOT ETH_15m)
- When either side's mid drops to threshold, buy BOTH sides at current ask
- If loser's mid drops to 0.20, early exit at bid = 2×mid − ask
- Hold winner to $1.00 resolution
- **Winner = whichever side has higher mid at last observed tick (100% of candles)**

### Backtest Results (100% candles, real fees, 3-day sample)
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
| `paper_trader_v8.py` | Paper trader — single 0.25 entry |
| `paper_trader_v8_layered.py` | Paper trader — all 5 levels |
| `layered_entry_backtest.py` | Main backtest (100% candles, real fees) |
| `early_exit_backtest.py` | Early exit strategy backtest |
| `wallet_collector.py` | Continuous wallet trade collector |
| `show_pnl.py` | View paper trader PnL |

## Backtesting Rules (ALWAYS follow these)
- **100% of candles** — never filter to decisive only
- **Winner = highest mid at last observed tick** (not >= 0.85 threshold)
- **Real Polymarket fees** applied on every entry
- **Bid = 2×mid − ask** for exit prices (not mid)
- Entry at **ask price** (not mid)

## Next Steps / Open Questions
- Paper traders launched 2026-03-22 — need 1-2 weeks of data to validate edge
- Consider going live once paper trading confirms ~3% ROI holds
- Wallet collector running — analyze patterns once more data accumulates
- VPS storage: expand boot volume to 200GB when approaching 25GB used
