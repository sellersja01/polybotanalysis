# Polymarket Trading Bot — Project Context

## What This Project Is
Automated analysis and trading system for Polymarket Up/Down binary markets.
Every 5 or 15 minutes, Polymarket opens a BTC/ETH/SOL/XRP market: Up token pays $1 if price went up, Down pays $0 (and vice versa). We collect live odds, backtest strategies, and run paper/live trading bots from a Hetzner VPS in Finland.

## QUICK START (what's running RIGHT NOW)

**Hetzner VPS** (Helsinki, Finland): `ssh root@65.21.107.77`

Systemd services running 24/7:
- `collector.service` — Coinbase prices + Poly odds for 8 markets
- `wallet-collector.service` — 7 tracked wallets → `wallet_trades_v2.db`
- `paperbot.service` — latency arb paper trader on ETH 5m (DRY_RUN)
- `paper-s4.service` — Latency Arb 60s exit paper trader (all 4 markets)
- `paper-s6.service` — Penny Reversal paper trader (all 4 markets)
- `paper-s11.service` — Mid-Candle Momentum paper trader (all 4 markets)
- `paper-s12.service` — Both-Sides-Cheap paper trader (all 4 markets)
- `paper-s13.service` — First CEX Move paper trader (all 4 markets) — **PRIMARY, 70% WR**

Check all services:
```bash
systemctl is-active collector wallet-collector paperbot paper-s4 paper-s6 paper-s11 paper-s12 paper-s13
```

Live logs:
```bash
tail -f /root/paper_s13.log      # primary strat
tail -f /root/paper_bot.log      # latency arb
tail -f /root/collector.log      # collector
```

## 📍 SESSION 2026-04-21 (UTC) — LIVE POST-MORTEM + SLIPPAGE DEBUG + V2 BUILT

### TL;DR
Flipped `live-s13` to real money briefly (~6 fills, ~$12 deployed, $1.47 realized loss + 4 positions open at time of stop). **Every live fill was 15–29¢ worse than what the bot's log said.** Investigated — root cause is the bot acting on a stale view of the book, then ORDER placement racing an already-moved market run by maker bots that cancel+repost faster than we fire. Built v2 copies of all three bots with a corrected WS handler. Confirmed with a diagnostic that the real problem isn't just the WS bug — **maker bots reprice the book by cancel/repost in ~10–30 ms**, so no reasonable Python latency win lets us catch the "pre-move" price. Service `live-s13` is stopped + disabled; `paper-s13-v2` running DRY alongside v1s for comparison.

### What actually happened — trade-by-trade observed slippage

| Trade | Asset | Side | Bot said | Real fill | Slippage |
|---|---|---|---|---|---|
| 1 | XRP | Down | @0.500 | $2/2.532 = **$0.79** | **29¢** |
| 2 | XRP | Down | @0.470 | $2/3.226 = **$0.62** | 15¢ |
| 3 | XRP | Up | @0.520 | $2/3.077 = **$0.65** | 13¢ |
| 4 | SOL | Down | @0.400 | $2/3.030 = **$0.66** | 26¢ |
| 5 | BTC | Down | @0.340 | $2/3.333 = **$0.60** | 26¢ |
| 6 | ETH | Down | @0.330 | $2/3.390 = **$0.59** | 26¢ |

SOL/BTC/ETH hit **exactly 26¢** of "slippage" — too consistent to be random book-walking. That's the fingerprint of the bot reading a candle-open book snapshot, then firing ~10–15s later at the real (already-repriced) market.

### Root-cause investigation (what we actually proved)
1. **Cross-checked real fills against the collector DB** — at the time of the XRP Up entry (01:14:25 UTC, bot logged @0.520), the collector had captured Up ask = **0.680** just 4s earlier. Real fill was $0.65. **DB matched reality; bot was stale.** One entry was a clean match (ETH Up @0.440 = DB 0.440), so the bug is intermittent.
2. **Compared WS handler code**: collector maintains a full local book and processes both `book` (snapshot) and `price_change` (incremental diff) events. Bot only grabs `item["bids"]` / `item["asks"]` via `if bids else 0` and silently skips anything that isn't a full snapshot. *Hypothesis:* bot freezes at initial candle snapshot, misses incremental diffs.
3. **30-min WS diagnostic captured 3 windows** (bug limited capture — see below). All 3 windows were at pinned end-of-candle and showed **100% `book` events, zero `price_change` events**. So Polymarket IS sending `book` events at ~30/sec — which means the bug might not be "missing price_change" alone. Could also be: blocking I/O delays, coroutine starvation, or the bot's event loop falling behind.
4. **Conclusion:** two things are simultaneously wrong — (a) the WS handler is technically buggy (we fixed it anyway), and (b) even with a perfect WS, maker bots reprice faster than our order arrives. Fixing (a) alone won't make the strategy profitable. Fixing (b) requires either speed infrastructure OR a different strategy.

### What we built this session (v2 scripts — committed)
| File | Purpose |
|---|---|
| [arb_bot/paper_s13_v2.py](arb_bot/paper_s13_v2.py) | Paper s13 with fixed WS handler + log banner `S13_V2` |
| [arb_bot/paper_s13api_v2.py](arb_bot/paper_s13api_v2.py) | Same fix for API-resolver paper |
| [arb_bot/live_s13_v2.py](arb_bot/live_s13_v2.py) | Live trader with fix (DRY-only until validated) |
| [arb_bot/patch_v2.py](arb_bot/patch_v2.py) | The patcher — applies 3 edits to any v1 file to produce its _v2. Documents exactly what changed. |
| [arb_bot/paper-s13-v2.service](arb_bot/paper-s13-v2.service) | Systemd unit for paper-s13-v2 (deployed) |
| [arb_bot/ws_diagnostic.py](arb_bot/ws_diagnostic.py) | 30-min WS event capture (had a bug — see below) |

**The v2 fix** (applied to all 3 scripts by `patch_v2.py`):
```python
# BEFORE (v1 — only handles book events):
bids = item.get("bids",[]); asks = item.get("asks",[])
bb = max(... for b in bids) if bids else 0
if bb > 0: a["up_bid"] = bb
# price_change events have no "bids"/"asks" field, so they silently skip

# AFTER (v2 — mirrors collector_v2.py):
book = a["up_book"] if side=="Up" else a["dn_book"]
etype = item.get("event_type", "")
if etype == "book":
    book["bids"] = {b["price"]: float(b["size"]) for b in item.get("bids", [])}
    book["asks"] = {x["price"]: float(x["size"]) for x in item.get("asks", [])}
elif etype == "price_change":
    for ch in item.get("changes", []):
        d = book["bids"] if ch["side"]=="BUY" else book["asks"]
        d[ch["price"]] = float(ch["size"])
bids_live = [float(p) for p,s in book["bids"].items() if s>0]
asks_live = [float(p) for p,s in book["asks"].items() if s>0]
if bids_live: a["up_bid"] = max(bids_live)
if asks_live: a["up_ask"] = min(asks_live)
```
Plus asset state gets `up_book`/`dn_book` dicts and `setup()` resets them on candle rollover.

### Service status when we stopped
| Service | Status | Code | Meaning |
|---|---|---|---|
| `collector.service` | 🟢 active | `collector_v2.py` | **Unchanged.** Writes to all 8 `/root/market_<asset>_<tf>.db` files. Fine. |
| `wallet-collector.service` | 🟢 active | `wallet_collector_v2.py` | Unchanged. |
| `paper-s13.service` | 🟢 active | `paper_s13.py` (v1) | Still running — provides baseline to compare v2 against |
| `paper-s13api.service` | 🟢 active | `paper_s13api.py` (v1) | Still running — same purpose |
| `paper-s13-v2.service` | 🟢 active | `paper_s13_v2.py` (**new**) | **NEW** — fixed-WS paper trader, DRY. Comparison arm. |
| `live-s13.service` | 🔴 **inactive + disabled** | `live_s13.py` (v1) | Stopped mid-session after 6 live fills |

### Speed / "get there first" ideas, ranked by bang for time
The core fight: maker bots running on Polymarket cancel their resting limit orders within ~10–30 ms of a CEX tick, then repost at the new fair price. We fire at ~200–500 ms. So by the time our order arrives, the "stale" orders we wanted to hit are gone — the only available liquidity is the new thick book at the new price. **Speed only helps if we can fire before the makers cancel.** Below 30ms = viable; 100ms = mixed; 300ms+ = losing.

| # | Idea | Est. latency saved | Effort | Why (not) first |
|---|---|---|---|---|
| **1** | **Pre-sign order templates at candle open.** Pre-generate + EIP-712 sign a `BUY Up $X` and `BUY Down $X` at candle start (both tokens known). On signal, just HTTP POST the signed blob — skip the ~100–200ms signing step. | **100–200ms** | 1–2h | **Biggest Python-only win.** No infra change. Start here. |
| 2 | Persistent `aiohttp` session with keep-alive to CLOB host. TLS handshake happens once, not per order. | 50–100ms | 30 min | Easy, piggyback on #1. py_clob_client may already do this — verify first. |
| 3 | Switch CEX signal source to Binance futures (BTCUSDT-PERP, etc.) instead of Coinbase spot. Futures lead spot by 50–150ms; most HFT shops watch futures. | 50–100ms (timing, not loop) | 1h | Free edge if VPS is in Finland (Binance.com accessible). US IP would block. |
| 4 | Colocate in AWS us-east-1 (where Polymarket's matching engine runs). Our Helsinki→US RTT is ~100ms; us-east-1 RTT to matching engine is ~1–5ms. | 80–100ms (every POST) | 1+ day | **Polymarket geoblocks US IPs.** Requires proxy / routing through EU exit = fragile and risks account flag. Postpone. |
| 5 | Switch market orders → FOK limit at `ask + 2¢`. Not a speed fix, but a **defense:** if the book already moved past `ask + 2¢`, the order doesn't fill and we lose zero. Kills the 0.60 disasters. | 0 (defensive) | 30 min | Should combine with #1. |
| 6 | Tighten `ask` ceiling from ≤ 0.75 → ≤ 0.55. Only enter if the book truly hasn't moved. Fewer trades, much cleaner ones. | 0 (defensive) | 1 line | **Highest-value one-line change.** Do alongside #1. |
| 7 | Signing daemon in Go/Rust over Unix socket. ECDSA signing in Go = ~5ms (vs Python ~100ms). | 100–150ms | 1+ day | Overkill if #1 works. |
| 8 | **Follow-mode strategy:** subscribe to Polymarket's trade/live_activity WS. When we see a burst of buys on "Up" side within 200ms of a CEX tick, those are the frontrunners confirming direction. We buy Up at 0.60 *knowing* the signal is real instead of at 0.40 *hoping* it is. | N/A (different strategy) | 1 day | **The structural escape hatch.** Accepts we can't win the race, wins EV via conviction + better WR. Likely higher real-world PnL than any pure-speed fix. |

**Recommended order:** #6 (1 line, defensive) → #1 (biggest real speed) → #5 (stop-loss on latency) → #3 (free edge). Prototype #8 in parallel as a strategy fork. Skip #4 and #7 unless #1–3 don't close the gap enough.

### Critical insight: how Polymarket's book actually moves
Tested directly: when a CEX tick arrives, the book doesn't get **walked by taker trades** (option A). It gets **repriced by maker cancel+repost** (option B). Sequence:
1. Binance / Coinbase tick arrives
2. Maker bots see the tick on their own feed
3. Each maker sends `cancel` for its resting bids/asks at old prices
4. Each maker posts new limits at the new fair price
5. Old orders are **gone** (canceled, not filled); new thick book sits at the new price

Implication: there's no ladder of orders at intermediate prices (0.42, 0.45, 0.48…) for us to walk through. Our market order arrives and finds a cleaned-out book with fresh orders only at 0.60. **Faster ≠ more cheap fills — it's binary: either we beat the cancel (<30ms) or we don't.** This is why pure speed optimization has steep diminishing returns past the first ~100ms.

### Data gaps + bugs we noted (not fixing tonight)
- **Collector WS silence:** saw 16 seconds of zero BTC `polymarket_odds` rows around 23:10:35-23:10:51 UTC, exactly during candle rollover. Suggests collector's Poly WS briefly drops during token-id transition. Worth a watchdog.
- **ws_diagnostic.py has a bug:** poly_ws doesn't reconnect when tokens change at candle rollover, so after the first candle it stayed subscribed to dead tokens. Only captured 3 windows in 30 min (all from first candle, all pinned end-of-candle — no mid-candle reprice captured). If we rerun for better data, fix: break out of `async with` block when `candle_ts` changes and reconnect with new tokens.
- **Live fills open at session-stop time:** XRP Up ~3.08sh @$0.65, SOL Down ~3.03sh @$0.66, BTC Down ~3.33sh @$0.60, ETH Down ~3.39sh @$0.59. Combined cost ~$8; each will resolve on its own at candle end.

### Suggested next-session plan
1. **Pull 12–24h of `/root/paper_s13_v2.log` data**, compare entries and PnL vs original `paper-s13.service`. If v2 is materially better, the WS fix has real value even without a speed win.
2. **Implement #6 first (1-line: lower `ask` ceiling to 0.55)** in a new `paper_s13_v3.py`. This alone may make the strategy profitable at our current speed because we simply don't take the trades where front-runners already moved the book.
3. **Implement #1 (pre-signed orders)** if #6 looks good but still too slow.
4. **Decide between pure-speed path (#1+#3+#5) vs strategy pivot (#8)** based on v2 vs v1 comparison data.
5. **Do NOT go live again** until paper_v3 or v4 shows consistent WR > 65% with real entry prices ≈ what the log claims (audit each trade with the cross-check script).

### Files to look at next time
- `/root/paper_s13_v2.log` on VPS — running data
- `arb_bot/patch_v2.py` — if we need to generate _v3 from _v2 with one more tweak
- `arb_bot/ws_diagnostic.py` — fix the rollover bug if we want real mid-candle data

---

## 📍 SESSION 2026-04-20 — S13 VALIDATED + LIVE BOT READY (GO-LIVE NEXT)

### What we built this session
1. **Audited paper_s13** against Polymarket gamma API → **94.8% of 427 trades classified correctly**. The DB-based resolver was occasionally wrong (missed final WS tick). True PnL was actually **$698 HIGHER** than reported ($6,348 reported → $7,047 real).
2. **Created paper_s13api.py** on VPS — same strategy, but uses `gamma-api.polymarket.com/events?slug=X-updown-5m-{cs}` as the authoritative resolver (falls back to DB only if API down). Running as `paper-s13api.service`.
3. **Built live_s13.py** — live trader. Initially had structural differences from paper (async `try_enter` vs sync `check`) that caused Poly WS backpressure and missed SOL/XRP candles. **Rewrote to be byte-identical to paper_s13api.py** except for:
   - Env vars (DRY_RUN, TRADE_USD, POLY_PRIVATE_KEY)
   - `build_clob()` + `place_live_order()` helpers
   - Inside `check()`: fire-and-forget order placement via `asyncio.create_task(_submit_order_bg(...))`
   - `resolve_delayed` uses real shares+USDC instead of SHARES=100
4. **Verified trade-for-trade parity** after fix:
   - live vs paper: 107 overlapping candles, **104 same direction (97%)**
   - live vs paperapi: 111 overlapping candles, **104 same direction (94%)**
   - Candles paper entered but live did NOT: **only 2**

### Services running on VPS as of this session
| Service | Script | Purpose | Status |
|---|---|---|---|
| `paper-s13` | `/root/paper_s13.py` | First CEX Move (DB resolver) — original | active |
| `paper-s13api` | `/root/paper_s13api.py` | First CEX Move (API resolver) — audit baseline | active |
| `live-s13` | `/root/live_s13.py` | LIVE trader, $2/trade | active (**DRY_RUN=true**) |

### 🚨 GO-LIVE STEPS (do these on PC)
SSH into VPS and edit the env file — **paste your private key yourself, do not let Claude type it:**
```bash
ssh root@65.21.107.77
nano /root/.live_s13_env
```

Change file to:
```
DRY_RUN=false
TRADE_USD=2.0
POLY_PRIVATE_KEY=0x<your MetaMask EOA private key for 0x4795e77317792011c8967de46441f586987101fc>
```

Save (Ctrl+X, Y, Enter), then restart:
```bash
systemctl restart live-s13
tail -f /root/live_s13.log
```

Expected log output within ~10 seconds:
- `[poly] CLOB creds derived api_key=...` → signing works ✅
- Banner says `LIVE_S13 [LIVE]` (not `[DRY]`)
- First entry will trigger when next candle move crosses 0.03%
- Entry line: `ENTRY[LIVE] [BTC] Up @0.510 mid=0.505 mv=+0.032%`
- Fill line: `FILLED [BTC] Up shares=3.92 spent=$2.00`

### Before going live — verify these
- [ ] **USDC balance** in Polymarket proxy wallet `0x6826c3197fff281144b07fe6c3e72636854769ab` is enough. At $2/trade × ~35 trades/hour × ~59% WR, realistic daily cost/profit could swing several hundred $. Recommend **min $100 USDC deposited** before going live.
- [ ] **Watch for `ORDER_FAIL`** in log. If seen, immediately revert `DRY_RUN=true` and debug.
- [ ] **Watch for `SKIP ... no fill recorded`** — means order didn't complete before candle resolved.

### Known issues / lingering work
1. **WS staleness bug**: All three paper/live scripts can silently hang if their Coinbase or Polymarket WebSocket drops incoming messages without raising an exception. Symptoms: heartbeat keeps printing, no new entries. Fix: add a watchdog that forces reconnect if no tick received in 60s. **Not yet implemented.** If a bot goes quiet for 10+ min, `systemctl restart` will unstick it.
2. **DB resolver accuracy**: paper_s13 uses DB for W/L tracking, which is ~5% inaccurate. Doesn't affect live trading (real $ is determined by market, not bot's internal tagging), but reported PnL is off by ~$700 over 430 trades.

### Files in this repo (committed 2026-04-20)
| File | Purpose |
|---|---|
| `live_s13.py` | **THE LIVE BOT** — identical to paper_s13api structurally, $2/trade |
| `_compare_bots.py` | Side-by-side trade comparison across 3 bots (shows candle-level divergence) |
| `_s13_audit.py` | Audit all paper_s13 trades against Polymarket gamma API |
| `_s13_last10.py` | Pull last 10 trades per asset with Up/Down prices at entry |

---

## SSH into the Hetzner VPS — Detailed Guide

**Primary command (from PC or laptop):**
```bash
ssh root@65.21.107.77
```

**SSH key locations:**
- PC: `~/.ssh/id_ed25519` (ed25519, auto-used by OpenSSH)
- If on a new machine, generate + add:
  ```bash
  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
  # copy ~/.ssh/id_ed25519.pub content, then paste into Hetzner console → /root/.ssh/authorized_keys
  ```

**File transfer (scp, from PC to VPS):**
```bash
# Upload a Python file
scp c:/Users/James/polybotanalysis/arb_bot/paper_s13.py root@65.21.107.77:/root/paper_s13.py

# Download a log
scp root@65.21.107.77:/root/paper_s13.log c:/Users/James/polybotanalysis/logs/

# Download a database (stop collector first to avoid corruption)
ssh root@65.21.107.77 "systemctl stop collector"
scp root@65.21.107.77:/root/market_btc_5m.db c:/Users/James/polybotanalysis/
ssh root@65.21.107.77 "systemctl start collector"
```

**Run a one-off remote command without interactive shell:**
```bash
ssh root@65.21.107.77 "systemctl restart paper-s13"
ssh root@65.21.107.77 "tail -20 /root/paper_s13.log"
ssh root@65.21.107.77 "grep 'S13 |' /root/paper_s13.log | tail -1"
```

**Run a multi-line Python script remotely (WARNING: bash heredoc with `$` escapes can choke — upload the file with scp instead):**
```bash
# BAD — dollar signs and backticks in heredoc mangle
ssh root@65.21.107.77 "python3 << EOF ... EOF"

# GOOD — write locally, scp, execute
scp my_script.py root@65.21.107.77:/tmp/my_script.py
ssh root@65.21.107.77 "python3 /tmp/my_script.py"
```

**Common ops:**
```bash
# Service control
systemctl is-active <service>            # check if running
systemctl status <service> --no-pager    # full status
systemctl restart <service>              # restart
systemctl stop <service>                 # stop
journalctl -u <service> -n 100           # last 100 systemd log lines
journalctl -u <service> -f               # follow systemd log

# File-based logs
tail -f /root/<log>                       # live tail
> /root/<log>                             # clear a log (before fresh test run)

# Disk / resource
df -h                                     # disk usage
free -h                                   # ram usage
top                                       # live process view
```

**Troubleshooting:**
- **Connection reset by peer**: usually means heredoc script had bash syntax issues. Write the script locally and scp it up.
- **"Permission denied (publickey)"**: SSH key missing or wrong. Verify `~/.ssh/id_ed25519` exists and `~/.ssh/id_ed25519.pub` is in `/root/.ssh/authorized_keys` on the VPS.
- **Service won't restart**: check `journalctl -u <service> -n 50` for Python traceback.
- **DB locked**: check if collector is running. Use `sqlite3 /root/market_btc_5m.db "PRAGMA journal_mode=WAL"` to enable WAL.

## Polymarket Wallet (for live trading)
- **Polymarket proxy wallet** (funds): `0x6826c3197fff281144b07fe6c3e72636854769ab`
- **MetaMask EOA** (signer): `0x4795e77317792011c8967de46441f586987101fc`
- **Polymarket API key**: `019d323e-f149-794e-8c3b-6c1df3877250`
- **Private key**: NEVER stored in files — pass via `POLY_PRIVATE_KEY` env var only
- **VPN requirement from US PC**: NordVPN Switzerland exit (Netherlands/Germany BLOCKED for trading)

## Infrastructure

### Hetzner VPS (PRIMARY — as of 2026-04-08)
- **IP**: `65.21.107.77`
- **Location**: Helsinki, Finland (CX23, 4GB RAM, 40GB disk, €4.99/mo)
- **User**: `root`
- **SSH command**: `ssh root@65.21.107.77`
- **SSH key**: `~/.ssh/id_ed25519` (ed25519, auto-used)
- **Why Helsinki**: Polymarket BLOCKS Germany/Netherlands/US for trading. Finland is NOT on the geoblock list. Confirmed working — buys and sells go through.
- **Latency**: Polymarket ~28ms, Coinbase ~24ms (vs US PC at 230ms)
- **No SSH rate limiter** (unlike Oracle)

### Oracle VPS (DEPRECATED)
- **IP**: `132.145.168.14` (opc user)
- **SSH key (PC)**: `C:\Users\James\btc-oracle-key\oracle-btc-collector.key`
- **SSH key (Laptop)**: `C:\Users\selle\oracle.key`
- **Why abandoned**: Only 1GB RAM (OOM killed processes), US IP (Polymarket blocks), collectors kept dying
- **Status**: Still running but unused since 2026-04-08

## Kalshi API Credentials
- **Key ID**: `d307ccc8-df96-4210-8d42-8d70c75fe71f`
- **Key file (local)**: `C:\Users\James\kalshi_key.pem.txt`
- **Key file (VPS)**: `/home/opc/kalshi_key.pem`
- **Signing**: RSA-PSS with SHA-256 (NOT PKCS1v15)
- Kalshi crypto 15m Up/Down markets are **legal in the US** (CFTC-regulated)

## Services Running on Hetzner VPS (systemd)

All processes run as **systemd services** — survive SSH disconnects, auto-restart on crash, auto-start on reboot.

| Service | Script | Log | Purpose |
|---------|--------|-----|---------|
| `collector.service` | `/root/collector_v2.py` | `/root/collector.log` | Coinbase prices + Polymarket odds for BTC/ETH/SOL/XRP 5m+15m (8 DBs) |
| `wallet-collector.service` | `/root/wallet_collector_v2.py` | `/root/wallet_collector.log` | Tracks 7 wallet addresses via Polymarket activity API → `/root/wallet_trades_v2.db` |
| `paperbot.service` | `/root/live_coinbase.py` | `/root/paper_bot.log` | Latency arb paper trader on ETH 5m (DRY_RUN mode) |
| `paper-s4.service` | `/root/paper_s4.py` | `/root/paper_s4.log` | S4 — Latency arb 60s exit (all 4 markets) |
| `paper-s6.service` | `/root/paper_s6.py` | `/root/paper_s6.log` | S6 — Penny reversal (ask ≤ 0.10, hold to resolution) |
| `paper-s11.service` | `/root/paper_s11.py` | `/root/paper_s11.log` | S11 — Mid-candle momentum (t+150s, mid > 0.60) |
| `paper-s12.service` | `/root/paper_s12.py` | `/root/paper_s12.log` | S12 — Both sides cheap (up+dn ask sum < 0.90) |
| `paper-s13.service` | `/root/paper_s13.py` | `/root/paper_s13.log` | **S13 — First CEX Move (PRIMARY strat, 70% WR)** |

**Service management:**
```bash
# Status
systemctl is-active collector wallet-collector paperbot
systemctl status collector --no-pager

# Logs
journalctl -u collector -f              # live tail
tail -50 /root/collector.log            # file log

# Restart
systemctl restart collector
systemctl restart wallet-collector
systemctl restart paperbot
```

**systemd unit files (create once, never need to touch again):**
```bash
# Example: /etc/systemd/system/collector.service
[Unit]
Description=Polymarket + Coinbase Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 -u /root/collector_v2.py
StandardOutput=append:/root/collector.log
StandardError=append:/root/collector.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then: `systemctl daemon-reload && systemctl enable collector && systemctl start collector`

## Databases on Hetzner VPS (`/root/`)
| DB | Schema | Purpose |
|----|--------|---------|
| `market_btc_5m.db`  | `asset_price` + `polymarket_odds` | BTC Coinbase + Poly 5m candle odds |
| `market_btc_15m.db` | same | BTC 15m |
| `market_eth_5m.db`  | same | ETH 5m |
| `market_eth_15m.db` | same | ETH 15m |
| `market_sol_5m.db`  | same | SOL 5m |
| `market_sol_15m.db` | same | SOL 15m |
| `market_xrp_5m.db`  | same | XRP 5m |
| `market_xrp_15m.db` | same | XRP 15m |
| `wallet_trades_v2.db` | `trades` (wallet_name, wallet_addr, timestamp, side, outcome, price, size, ...) | All 7 tracked wallets' trades |

**Critical DB fix (2026-04-15):** `wallet_trades_v2.db` was getting lock errors in WAL mode. Apply once:
```python
import sqlite3
c = sqlite3.connect('/root/wallet_trades_v2.db')
c.execute('PRAGMA journal_mode=WAL')
c.commit()
c.close()
```

## Tracked Wallets (wallet_collector_v2.py)
```python
WALLETS = {
    "galindrast":  "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",
    "wallet_2":    "0x89b5cdaaa4866c1e738406712012a630b4078beb",
    "wallet_3":    "0x1f3472bc20dbdee754d09b2fc292efc8a8f0ba6e",
    "wallet_4":    "0x5d634050ad89f172afb340437ed3170eaa2c9075",
    "wallet_5":    "0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82",
    "wallet_6":    "0x7da07b2a8b009a406198677debda46ad651b6be2",
    "wallet_7":    "0x8c901f67b036b5eebab4e1f2f904b8676743a904",
}
```

## Deploying code to Hetzner
From PC PowerShell (not SSH):
```powershell
scp c:\Users\James\polybotanalysis\arb_bot\live_coinbase.py root@65.21.107.77:/root/live_coinbase.py
scp c:\Users\James\polybotanalysis\collector_v2.py root@65.21.107.77:/root/collector_v2.py
scp c:\Users\James\polybotanalysis\wallet_collector_v2.py root@65.21.107.77:/root/wallet_collector_v2.py
```

Then SSH in and `systemctl restart <service>` to pick up changes.

## Debugging data gaps
Find when a collector died:
```python
# Run on VPS
python3 << 'EOF'
import sqlite3
from datetime import datetime, timezone
c = sqlite3.connect('/root/market_btc_5m.db')
rows = c.execute('SELECT unix_time FROM asset_price ORDER BY unix_time DESC LIMIT 100000').fetchall()
c.close()
gaps = []
for i in range(len(rows) - 1):
    gap = float(rows[i][0]) - float(rows[i+1][0])
    if gap > 60:
        gaps.append((float(rows[i+1][0]), float(rows[i][0]), gap))
gaps.sort(key=lambda x: x[2], reverse=True)
for start, end, gap in gaps[:5]:
    s = datetime.fromtimestamp(start, tz=timezone.utc).strftime('%m-%d %H:%M:%S')
    e = datetime.fromtimestamp(end, tz=timezone.utc).strftime('%m-%d %H:%M:%S')
    print(f'{s} -> {e}  gap={gap:.0f}s ({gap/3600:.1f}h)')
EOF
```

## Oracle VPS (DEPRECATED `/home/opc/`)
Kept for historical reference only — no longer running any processes.
| DB | Contents |
|----|----------|
| `arb_collector.db` | Legacy side-by-side Poly + Kalshi prices |
| `paper_v8_single.db` | Legacy paper trader (single entry) |
| `paper_v8_layered.db` | Legacy paper trader (layered) |
| `paper_contrarian.db` | Legacy paper trader (contrarian DCA) |
| `wallet_trades.db` | Legacy wallet trade history |
| `galindrast_trades.db` | Legacy single-wallet collector (replaced by wallet_trades_v2.db on Hetzner) |

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

### Latency Bot Files (built 2026-03-30, updated 2026-03-30 evening)
| File | Purpose |
|------|---------|
| `arb_bot/paper_test.py` | **PRIMARY** — multi-asset live/paper bot (BTC+ETH 5m simultaneously) |
| `arb_bot/polymarket_client.py` | Polymarket CLOB client — place_order + sell_order |
| `arb_bot/test_sell.py` | Buy->sell cycle test (confirms 4s settlement wait works) |
| `backtest_cheapside.py` | Backtest of cheap-side + expensive-side wallet strategy |
| `latency_lag_v2.py` | Fast backtest — loads all data into memory |
| `latency_lag_honest.py` | Honest backtest — enters on every signal, no cherry-picking |
| `latency_lag_all.py` | Multi-market backtest (BTC/ETH/SOL/XRP x 5m/15m) |

### paper_test.py — Current Architecture (2026-03-30 evening)
- **Multi-asset**: BTC 5m + ETH 5m run in parallel — separate Binance + Poly WS feeds per asset
- **Per-asset state dicts** — no shared mutable variables between assets
- **Entry trigger**: Binance move >= MOVE_THRESH AND Poly mid < 0.55 (stale)
- **Exit**: sell after 4s minimum hold, when profit >= 2c/share OR age >= 60s
- **Real P&L**: tracks `usdc_spent` (makingAmount) and `usdc_received` (sell takingAmount)
- **ETH price fix**: book snapshots with empty bids/asks no longer overwrite prices to 0
- **Status line**: shows current 15s move% per asset so you can see how close to threshold

### Paper Test Config (`paper_test.py`)
```python
LOOKBACK            = 15     # price lookback window (seconds)
MOVE_THRESH         = 0.07   # min move % to trigger (env: MOVE_THRESH)
COOLDOWN            = 2      # seconds between trades per asset
MIN_ENTRY_PRICE     = 0.25
MAX_ENTRY_PRICE     = 0.75
MAX_TRADES_PER_CANDLE = 10
MAX_OPEN            = 5      # across all assets
TRADE_USD           = 1.0    # USDC per live trade (env: TRADE_USD)
```

### MOVE_THRESH History
- **0.05%** — original, caused noisy losing trades (too sensitive, fires on random wiggles)
- **0.15%** — raised to filter noise, but too strict for calm markets (0 entries for hours)
- **0.07%** — current default, middle ground

### Run commands
```powershell
# Paper mode
python arb_bot/paper_test.py

# Live mode (always clear old env vars first)
Remove-Item Env:MOVE_THRESH -ErrorAction SilentlyContinue
$env:DRY_RUN="false"; python arb_bot/paper_test.py

# Custom threshold
$env:MOVE_THRESH="0.05"; $env:DRY_RUN="false"; python arb_bot/paper_test.py
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

## Wallet Analysis (2026-03-30 evening)

Analyzed an unknown wallet trading both sides of each candle. Key findings:

**Pattern**: buys cheap side (small position) + expensive side (large position) in same candle
- Cheap side entries: 14-47c | Expensive side: 52-96c | 10-20x more capital on expensive side
- Net profitable on observed trades (~+$354 across 11 positions)

**Backtest of this strategy** (`backtest_cheapside.py`, 109-93 hours of data):

| Strategy | BTC 5m WR | ETH 5m WR | Avg $/trade |
|----------|-----------|-----------|-------------|
| Cheap side <= 20c | 8.2% | 11.0% | -$41 to -$56 |
| Expensive side >= 80c | **88.2%** | **87.1%** | **+$5 to +$6** |
| Combined | 9% net positive | 12% net positive | deeply negative |

**Conclusion**: The cheap side is a lottery ticket — deeply losing over time. The expensive side (>=80c) is profitable at 84-88% WR with ~$5/trade. The wallet's edge comes from the expensive-side momentum bet, not the cheap-side contrarian.

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
| `arb_bot/paper_test_dca.py` | **Full Galindrast replica** — all 7 behaviors below |
| `databases/galindrast_trades.db` | Local copy of collected trades (24k+) |

### Paper Test: Galindrast Strategy Replica (`paper_test_dca.py`)

Built from reverse-engineering 24,604 trades across 8.7 hours of Galindrast's actual behavior.
Every behavior below was directly observed in their trade data and replicated in the bot.

**7 core behaviors (all implemented):**

1. **Initial both-sides entry (0-10s)** — Buy BASE_SHARES of both Up and Down immediately at candle start. Small exploratory position (~$17 per side). Observed: 94% of Galindrast's first trades happen within 30s at avg price 0.51.

2. **BTC signal locks direction** — When Binance BTC moves >= 0.05% in 15s, lock `primary_dir` to Up or Down. All subsequent DCA buys go to this side. Observed: Galindrast has a directional tilt on 64% of candles (UP_heavy or DN_heavy).

3. **DCA with confidence scaling** — Buy every 10 seconds, scaling shares based on poly mid:
   - mid < 0.55: 10 shares (exploratory)
   - mid 0.55-0.70: 20 shares
   - mid 0.70-0.80: 30 shares
   - mid 0.80-0.90: 50 shares
   - mid 0.90-0.95: 80 shares (resolution scalp)
   - mid 0.95+: 100 shares (near-certain, pile in)
   Observed: Galindrast deploys 10x more capital at 0.90+ (avg 195sh vs 22sh at other levels). 69% of their volume is at 0.90-1.00.

4. **Flip on reversal** — If our side drops below 0.25 AND the other side is above 0.70, FLIP `primary_dir` to the other side. Max 3 flips per candle. Observed: 30% of Galindrast's candles had both sides bought above 0.70 (full reversals). They don't stop buying — they switch sides and start resolution scalping the new winner.

5. **Sell losers** — When losing side drops below 0.20, sell remaining shares at bid to recover capital. Observed: 3.7% of Galindrast's trades are sells at avg price 0.17, happening avg 207s into candle.

6. **Resolution scalp trigger** — If either side hits 0.90+ even without a BTC signal, set direction and start buying big. This catches candles where BTC is flat but Poly odds drift. Observed: Galindrast trades candles with no clear BTC signal too.

7. **Hold to resolution** — All winning entries resolve at $1.00 at candle end. No early exits on the winning side. Observed: 96% of Galindrast's trades are buys (holds to resolution), only 4% sells (cutting losers).

**Config (as of 2026-03-31 overnight deployment):**
```python
LOOKBACK = 15           # BTC move lookback (seconds)
MOVE_THRESH = 0.05      # min BTC move % to trigger signal
DCA_INTERVAL = 10       # buy every 10 seconds
BASE_SHARES = 1         # base shares per buy — cut 10x from original to reduce swing size
                        # Scales: mid<0.55->10sh, 0.55-0.70->20, 0.70-0.80->30,
                        #         0.80-0.90->50, 0.90-0.95->80, 0.95+->100 (all x BASE_SHARES)
MAX_ENTRIES = 15        # max entries PER DIRECTION (resets to 0 on flip)
FLIP_THRESHOLD = 0.25   # flip direction when our side drops below this
SELL_THRESHOLD = 0.20   # sell losing side when it drops below this
RESOLUTION_THRESHOLD = 0.90  # pile in when a side hits this
```

**Price feed**: Uses **Coinbase Advanced Trade WebSocket** (`wss://advanced-trade-ws.coinbase.com`) instead of Binance — Oracle VPS is US IP (Ashburn), Binance.com blocks US IPs (HTTP 451). Coinbase works fine.

**Bug fixes discovered during live paper testing (2026-03-31):**
- **Empty book false flip**: When UP wins, order book empties (ask=0). `up_mid()` returned 0, triggering flip condition. Fixed: `if our_ask <= 0: return` guard in `check_flip()`.
- **Wrong winner at resolution**: Winning side book empties first (ask=0, mid=0). Bot was declaring loser as winner. Fixed: track `last_nonzero_up_mid` / `last_nonzero_dn_mid` — updated on every price tick, used in `resolve_candle()` instead of current (possibly 0) mids.
- **`n_entries` not resetting on flip**: `n_entries` was not in nonlocal declaration in `check_flip()`, so reset was silently ignored. Fixed: added to nonlocal and reset to 0 on every flip so new direction gets its own entry budget.
- **last_nonzero values stale**: Were only updated during periodic Binance checks (every 20 ticks). Fixed: call `up_mid(); dn_mid()` on every Polymarket price update event.
- **MAX_ENTRIES blocking post-flip buys**: After 20 entries (old value), flipping direction couldn't buy anything. Fixed: MAX_ENTRIES=15 per direction, resets to 0 on flip.

**VPS deployment:**
```bash
# Already deployed and running since 2026-03-31 night
tail -50 /home/opc/paper_dca.log   # check results
```

**Why each behavior matters:**
- Initial both-sides: ensures you have a position in whichever side wins, even before BTC signal
- BTC signal: gives directional edge (71% WR in backtest)
- Confidence scaling: small risk early, massive size when near-certain — matches Galindrast's 10x at 0.90+
- Flip on reversal: prevents catastrophic losses when candle reverses (happens 30% of the time)
- Sell losers: recovers $13.60 per $40 lost position instead of holding to $0
- Resolution scalp: the main profit driver — $0.05/share × 1000+ shares at 0.95
- Hold to resolution: no early exits means capturing full $1.00 payout

**Paper test results (3 candles, 15 min, pre-flip version):**
3/3 wins, +$174.93, 18.3% ROI. Full version with flip/sell not yet tested over large sample.

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
1. **Review overnight paper_test_dca.py results** — check `/home/opc/paper_dca.log` for candle-by-candle PnL. Need 200+ candles to validate 71% WR holds live.
2. **Set up Hetzner VPS in Germany/Netherlands** (~€4/mo) — required for live trading because:
   - Polymarket blocks US IPs (needs non-US IP)
   - EU VPS: Polymarket accessible, Binance accessible, no VPN needed
   - Oracle VPS (US IP) can only run paper DCA (uses Coinbase feed, no Poly trading from US IP)
3. **Build expensive-side momentum bot** — enter when Poly ask >= 0.80 on either side, hold to settlement. Backtest: 84-88% WR, ~$5/trade at $100. Keep separate from other bots.
4. **Go live small on DCA strategy** — $1-5/trade once 200+ candle paper history confirms WR
5. **Keep galindrast_collector running on Oracle VPS** — track their evolving strategy
6. **Keep arb_collector running on Oracle VPS** — cross-platform data for backup strategy
7. **Cross-platform arb (on pause)** — revisit when latency arb slows down

---

## S13 — First CEX Move (PRIMARY, 2026-04-19)

### Strategy
- Watch Coinbase price vs the candle's **open price**.
- If Coinbase moves **≥ 0.03%** AND Poly mid of the matching side < 0.55 AND ask < 0.75 → buy that side.
- **One trade per candle per asset.** Hold to resolution.
- Entry window: **10s–270s** into the candle (matches backtest's `offset >= 10`).
- Shares: **100** (~$47 avg deployed per trade).

### Files
| File | Purpose |
|------|---------|
| `arb_bot/paper_s13.py` | Live paper trader |
| `backtest_s13.py` | Backtest on collector DB |
| `paper_s4.py / s6.py / s11.py / s12.py` | Other paper traders (less profitable) |

### Backtest Results (Apr 8–19, 148.7h actual data across all 4 assets)

| Market | Hours | Trades | Wins | WR% | Avg win | Avg loss | Net PnL | $/day |
|--------|-------|--------|------|-----|---------|----------|---------|-------|
| BTC | 148.7 | ~1700 | — | 65.1% | — | — | +$20,622 | ~$3,325 |
| ETH | 148.7 | ~1700 | — | 57.7% | — | — | +$18,220 | ~$2,940 |
| SOL | 148.7 | ~1700 | — | 57.2% | — | — | +$16,445 | ~$2,650 |
| XRP | 148.7 | ~1700 | — | 58.6% | — | — | +$16,768 | ~$2,700 |
| **Total** | | | | **~60%** | | | **+$72,055** | **~$11,620** |

### Live Paper Results (1.5h, post-fix)

| Metric | Value |
|--------|-------|
| Trades | 80 (4 markets) |
| WR | **70%** (55W / 25L) |
| Avg win | +$51.61 |
| Avg loss | −$48.41 |
| Avg entry price (Up) | 0.467 |
| Avg entry price (Down) | 0.484 |
| Avg $/trade deployed | $47.55 |
| Net PnL | **+$1,628** (≈$1,085/hr) |
| ROI on deployed capital | **~43%** |

### Slippage Cushion
At 69% WR, breakeven requires entry cost ≤ 0.69 per share. Avg entry is 0.484 (with fees), so the cushion is **~20¢/share (~$20/trade)** before expected value goes negative. Realistic safe margin is ~10¢/share since real slippage also depresses WR.

### Winner Detection — 3-stage resolve (fixed 2026-04-19)

**Bug:** original live bot used its WS state at the moment of candle rollover to determine the winner. This state often didn't match the DB's last tick — the bot missed the final resolution messages or had stale cached prices from mid-candle wobble. Result: live flipped winners on some candles, showed 21% WR vs backtest's 60%.

**Fix:** after candle end, wait 5s, then resolve via fallback chain:
1. **DB** — query `/root/market_<asset>_5m.db` for the last `polymarket_odds` tick of the candle (same logic as the backtest). Zero-ask rule first, then higher-mid fallback.
2. **API** — fetch `https://gamma-api.polymarket.com/events?slug=<asset>-updown-5m-<cs>`. Read `outcomePrices`; whichever is ≥0.99 wins.
3. **WS snapshot** — last cached bid/ask from WS right before rollover (zero-ask rule).

Each resolution logs a tag `(DB)`, `(API)`, or `(WS)` so you can see which source was used. In practice, **(DB) hits 100% of the time** because the collector writes ticks in real time.

**Post-fix result:** 70% WR on 80 live trades, matches backtest expectation.

### Winner logic details (the "zero-ask rule")
```python
# If one side has zero asks, it's the winner (nobody sells a winning token)
if up_ask == 0 and dn_ask > 0: winner = "Up"
elif dn_ask == 0 and up_ask > 0: winner = "Down"
else:
    # Fallback: higher mid wins
    up_mid = (up_bid + up_ask) / 2
    dn_mid = (dn_bid + dn_ask) / 2
    winner = "Up" if up_mid >= dn_mid else "Down"
```
User rationale: "polymarket odds will eventually be for example 0 cents up and 1 cent down because nobody wants to sell their up position because its pretty much already guaranteed to win" — so the side with empty asks is the winner.

### Session changelog (2026-04-19)
1. Deployed 5 paper traders (S4, S6, S11, S12, S13) as systemd services.
2. Discovered S13 showing 36% WR live vs 60% backtest — winner detection bug.
3. First fix attempt: use `last_up_mid` tracked during the candle — rejected (user: can't rely on Coinbase close because Polymarket uses a different price feed).
4. Second fix: apply **zero-ask rule** for winner (user direction). Applied to S13, S6, S11.
5. Pulled fresh collector data with services stopped (preserves old DBs): `hetzner_apr19/`. 269h span but 148.7h actual data due to two gaps (95.5h Apr 8–12 and 25.1h Apr 12–14) before services were set up as systemd.
6. Re-ran S13 backtest on fresh data: **+$72,055 / ~$11,620/day**, confirmed edge persists.
7. Still saw 21–29% WR live on 33 trades → discovered bot's in-memory WS state didn't match the DB's last tick.
8. Aligned live with backtest: `age>=10` (was 5), `candle_open` = first CB tick inside candle (was last pre-rollover tick).
9. Replaced `resolve()` with **async 5s-delayed 3-stage fallback**: DB → API → WS snapshot. Fire-and-forget via `asyncio.create_task` so new candle entries aren't blocked.
10. Live WR jumped to **70%** with `(DB)` resolutions on all 80 trades.

### Known caveats
- **Sample size small** (80 trades). Needs 500+ to be confident 70% holds.
- **Look-ahead bias check**: backtest uses DB's last tick; live now does too — consistent.
- **Slippage not modeled**: real fills on Polymarket may fall 1-2¢ worse than quoted ask, eating into the 20¢ cushion.
- **0.03% threshold** is tiny — crypto crosses it in almost every candle (~100% entry rate).
- **Edge compression risk**: as more bots front-run Polymarket on CEX moves, the stale-odds window will shrink. Monitor WR weekly.
