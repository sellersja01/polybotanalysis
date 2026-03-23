"""
Paper Trader v7 — Fixed-Shares Price-Cap Strategy

Connects directly to Polymarket CLOB websocket (same feed as the live market).
Every BUY_INTERVAL seconds: if best_ask <= PRICE_CAP, log a simulated buy of SHARES.
At candle end, resolve and calculate PnL.

Run:       python3 paper_trader_v7.py
View PnL:  python3 show_pnl.py
"""

import asyncio
import websockets
import requests
import sqlite3
import json
import time
import sys
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────────
PRICE_CAP    = 0.35   # only buy when best_ask <= this
BUY_INTERVAL = 10     # seconds between buys per (market, candle, outcome)
SHARES       = 100    # shares per simulated buy
PAPER_DB     = '/home/opc/paper_trades.db'

ASSETS     = ['btc', 'eth']
TIMEFRAMES = {'5m': 300, '15m': 900}

# ── DB ─────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(PAPER_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL    NOT NULL,
            market       TEXT    NOT NULL,
            candle_start INTEGER NOT NULL,
            market_id    TEXT,
            outcome      TEXT    NOT NULL,
            ask          REAL    NOT NULL,
            shares       INTEGER NOT NULL,
            cost         REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resolved (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            resolved_at  REAL,
            market       TEXT    NOT NULL,
            candle_start INTEGER NOT NULL,
            market_id    TEXT,
            winner       TEXT,
            n_up         INTEGER DEFAULT 0,
            n_dn         INTEGER DEFAULT 0,
            up_shares    REAL    DEFAULT 0,
            dn_shares    REAL    DEFAULT 0,
            avg_up       REAL    DEFAULT 0,
            avg_dn       REAL    DEFAULT 0,
            combined_avg REAL    DEFAULT 0,
            up_cost      REAL    DEFAULT 0,
            dn_cost      REAL    DEFAULT 0,
            pnl          REAL    DEFAULT 0,
            win          INTEGER DEFAULT 0,
            UNIQUE(market, candle_start)
        );
        CREATE INDEX IF NOT EXISTS fills_candle ON fills(market, candle_start);
    """)
    conn.commit()
    conn.close()


def log_fill(market, candle_start, market_id, outcome, ask):
    ts   = time.time()
    cost = ask * SHARES
    conn = sqlite3.connect(PAPER_DB)
    conn.execute("""
        INSERT INTO fills (ts, market, candle_start, market_id, outcome, ask, shares, cost)
        VALUES (?,?,?,?,?,?,?,?)
    """, (ts, market, candle_start, market_id, outcome, ask, SHARES, cost))
    conn.commit()
    conn.close()


def get_fills(market, candle_start):
    conn = sqlite3.connect(PAPER_DB)
    rows = conn.execute("""
        SELECT outcome, ask, shares FROM fills WHERE market=? AND candle_start=?
    """, (market, candle_start)).fetchall()
    conn.close()
    up = [(float(a), int(s)) for o, a, s in rows if o == 'Up']
    dn = [(float(a), int(s)) for o, a, s in rows if o == 'Down']
    return up, dn


def save_resolved(market, candle_start, market_id, winner, up_fills, dn_fills):
    up_sh  = sum(s for _, s in up_fills)
    dn_sh  = sum(s for _, s in dn_fills)
    up_c   = sum(p * s for p, s in up_fills)
    dn_c   = sum(p * s for p, s in dn_fills)
    avg_up = (up_c / up_sh) if up_sh else 0.0
    avg_dn = (dn_c / dn_sh) if dn_sh else 0.0
    comb   = (avg_up + avg_dn) if (up_sh and dn_sh) else 0.0

    if winner == 'Up':
        pnl = (1.0 - avg_up) * up_sh + (0.0 - avg_dn) * dn_sh
    else:
        pnl = (0.0 - avg_up) * up_sh + (1.0 - avg_dn) * dn_sh

    win = 1 if pnl > 0 else 0
    conn = sqlite3.connect(PAPER_DB)
    conn.execute("""
        INSERT OR IGNORE INTO resolved
          (resolved_at, market, candle_start, market_id, winner,
           n_up, n_dn, up_shares, dn_shares, avg_up, avg_dn, combined_avg,
           up_cost, dn_cost, pnl, win)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (time.time(), market, candle_start, market_id, winner,
          len(up_fills), len(dn_fills), up_sh, dn_sh,
          avg_up, avg_dn, comb, up_c, dn_c, pnl, win))
    conn.commit()
    conn.close()

    icon  = "WIN " if win else "LOSS"
    ts_cs = datetime.fromtimestamp(candle_start, tz=timezone.utc).strftime('%H:%M')
    print(f"  [{icon}] {market:<8} {ts_cs} | winner={winner:4s} | "
          f"up={len(up_fills):2d}f/{up_sh:.0f}sh avg={avg_up:.3f}  "
          f"dn={len(dn_fills):2d}f/{dn_sh:.0f}sh avg={avg_dn:.3f} | "
          f"comb={comb:.3f} | pnl=${pnl:+.2f}")
    return pnl


# ── Polymarket token discovery ─────────────────────────────────────────────────
DB_PATHS = {
    ('btc', '5m'):  '/home/opc/market_btc_5m.db',
    ('btc', '15m'): '/home/opc/market_btc_15m.db',
    ('eth', '5m'):  '/home/opc/market_eth_5m.db',
    ('eth', '15m'): '/home/opc/market_eth_15m.db',
}

def get_final_mid_from_db(db_path, candle_start, interval):
    """Look up the final Up mid from the collector DB for a completed candle."""
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        row = conn.execute("""
            SELECT mid FROM polymarket_odds
            WHERE unix_time >= ? AND unix_time < ?
              AND outcome = 'Up' AND mid > 0
            ORDER BY unix_time DESC LIMIT 1
        """, (candle_start, candle_start + interval + 120)).fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def fetch_tokens(asset, tf):
    """
    Fetch Up/Down token IDs for the current candle.
    Strategy 1: events API with slug (same as paper_trader_v4, known working).
    Strategy 2: fallback — read market_id from collector DB, look up via markets API.
    Returns (token_up, token_dn, question, market_id) or (None,None,None,None).
    """
    interval = TIMEFRAMES[tf]
    now      = int(time.time())
    cs       = (now // interval) * interval
    slug     = f"{asset}-updown-{tf}-{cs}"

    # ── Strategy 1: events endpoint with slug ─────────────────────────────────
    try:
        url  = f"https://gamma-api.polymarket.com/events?slug={slug}"
        data = requests.get(url, timeout=10).json()
        if data and data[0].get('markets'):
            mkt      = data[0]['markets'][0]
            tokens   = json.loads(mkt.get('clobTokenIds', '[]'))
            outcomes = json.loads(mkt.get('outcomes', '["Up","Down"]'))
            if len(tokens) >= 2:
                try:
                    up_idx = outcomes.index('Up')
                    dn_idx = outcomes.index('Down')
                except ValueError:
                    up_idx, dn_idx = 0, 1
                return tokens[up_idx], tokens[dn_idx], mkt.get('question', ''), str(mkt.get('id', ''))
    except Exception:
        pass

    # ── Strategy 2: read market_id from collector DB, look up via markets API ─
    try:
        db_path = DB_PATHS[(asset, tf)]
        conn    = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        row     = conn.execute("""
            SELECT market_id, question FROM polymarket_odds
            WHERE unix_time >= ? ORDER BY unix_time DESC LIMIT 1
        """, (cs,)).fetchone()
        conn.close()

        if not row:
            return None, None, None, None

        market_id, question = row
        url  = f"https://gamma-api.polymarket.com/markets/{market_id}"
        resp = requests.get(url, timeout=10).json()
        tokens   = json.loads(resp.get('clobTokenIds', '[]'))
        outcomes = json.loads(resp.get('outcomes', '["Up","Down"]'))
        if len(tokens) >= 2:
            try:
                up_idx = outcomes.index('Up')
                dn_idx = outcomes.index('Down')
            except ValueError:
                up_idx, dn_idx = 0, 1
            return tokens[up_idx], tokens[dn_idx], question, market_id
    except Exception:
        pass

    return None, None, None, None


# ── Per-candle shared state ────────────────────────────────────────────────────
# state[(asset, tf)] = {
#   candle_start, market_id, token_up, token_dn,
#   last_buy_up, last_buy_dn,   <- unix_ts of last fill per side
#   last_mid_up,                <- most recent Up mid seen (for resolution)
# }
state: dict = {}
state_lock = asyncio.Lock()


# ── CLOB websocket stream ──────────────────────────────────────────────────────
async def stream_market(asset, tf, stop_evt):
    """
    Streams one candle's websocket feed.
    Fires simulated buys based on best_ask + rate limiter.
    Stops when stop_evt is set (candle ended).
    """
    interval    = TIMEFRAMES[tf]
    market_name = f"{asset.upper()}_{tf}"
    ws_url      = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    async with state_lock:
        s = state[(asset, tf)]
        token_up   = s['token_up']
        token_dn   = s['token_dn']
        candle_start = s['candle_start']
        market_id  = s['market_id']

    books = {
        token_up: {'bids': {}, 'asks': {}},
        token_dn: {'bids': {}, 'asks': {}},
    }

    while not stop_evt.is_set():
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=15,
                open_timeout=15
            ) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": [token_up, token_dn],
                    "markets": []
                }))

                while not stop_evt.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue

                    events = json.loads(raw)
                    if not isinstance(events, list):
                        events = [events]

                    for ev in events:
                        etype    = ev.get('event_type', '')
                        asset_id = ev.get('asset_id', '')
                        if asset_id not in books:
                            continue
                        book = books[asset_id]

                        if etype == 'book':
                            book['bids'] = {b['price']: float(b['size']) for b in ev.get('bids', [])}
                            book['asks'] = {a['price']: float(a['size']) for a in ev.get('asks', [])}
                        elif etype == 'price_change':
                            for ch in ev.get('changes', []):
                                side, p, sz = ch['side'], ch['price'], float(ch['size'])
                                if side == 'BUY':
                                    if sz == 0: book['bids'].pop(p, None)
                                    else:       book['bids'][p] = sz
                                elif side == 'SELL':
                                    if sz == 0: book['asks'].pop(p, None)
                                    else:       book['asks'][p] = sz
                        else:
                            continue

                        bids = [float(p) for p, sz in book['bids'].items() if sz > 0]
                        asks = [float(p) for p, sz in book['asks'].items() if sz > 0]
                        if not bids or not asks:
                            continue

                        best_ask = min(asks)
                        best_bid = max(bids)
                        mid      = (best_ask + best_bid) / 2
                        outcome  = 'Up' if asset_id == token_up else 'Down'
                        now      = time.time()

                        async with state_lock:
                            s = state.get((asset, tf))
                            if s is None or s['candle_start'] != candle_start:
                                return  # candle changed, let manager restart

                            # Track last mid for resolution
                            if outcome == 'Up':
                                s['last_mid_up'] = mid

                            # Skip buying if price too high or near resolution
                            if best_ask > PRICE_CAP:
                                continue
                            if mid >= 0.88 or mid <= 0.12:
                                continue

                            # Rate limiter
                            lb_key = f'last_buy_{outcome.lower()}'
                            if now - s.get(lb_key, 0) < BUY_INTERVAL:
                                continue

                            # Simulate buy
                            s[lb_key] = now

                        log_fill(market_name, candle_start, market_id, outcome, best_ask)
                        ts_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime('%H:%M:%S')
                        print(f"  BUY  {market_name:<8} {outcome:4s} @ {best_ask:.3f} | "
                              f"shares={SHARES} cost=${best_ask*SHARES:.2f} | {ts_str}")

        except Exception as e:
            if not stop_evt.is_set():
                await asyncio.sleep(3)


# ── Market manager ─────────────────────────────────────────────────────────────
async def manage_market(asset, tf):
    interval    = TIMEFRAMES[tf]
    market_name = f"{asset.upper()}_{tf}"
    stop_evt    = None
    ws_task     = None
    prev_candle_start = None

    while True:
        now          = time.time()
        candle_start = (int(now) // interval) * interval

        # New candle?
        if candle_start != prev_candle_start:

            # Resolve the previous candle
            if prev_candle_start is not None:
                async with state_lock:
                    s = state.get((asset, tf), {})
                    last_mid = s.get('last_mid_up')
                    mid_id   = s.get('market_id', 'unknown')

                up_fills, dn_fills = get_fills(market_name, prev_candle_start)
                if up_fills or dn_fills:
                    # Determine winner — prefer websocket last_mid, fall back to DB
                    winner = None
                    if last_mid is not None:
                        if last_mid >= 0.85:
                            winner = 'Up'
                        elif last_mid <= 0.15:
                            winner = 'Down'

                    if winner is None:
                        # Look up final mid from collector DB
                        db_path = DB_PATHS[(asset, tf)]
                        final_mid = await asyncio.to_thread(
                            get_final_mid_from_db, db_path,
                            prev_candle_start, interval
                        )
                        if final_mid is not None:
                            if final_mid >= 0.85:
                                winner = 'Up'
                            elif final_mid <= 0.15:
                                winner = 'Down'

                    if winner is None:
                        winner = 'UNRESOLVED'

                    save_resolved(market_name, prev_candle_start, mid_id,
                                  winner, up_fills, dn_fills)

            # Stop old stream
            if stop_evt:
                stop_evt.set()
            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:
                    await ws_task
                except (Exception, asyncio.CancelledError):
                    pass

            # Discover tokens for new candle
            print(f"\n[{market_name}] New candle @ "
                  f"{datetime.fromtimestamp(candle_start, tz=timezone.utc).strftime('%H:%M')} UTC — "
                  f"fetching tokens...")
            token_up, token_dn, question, market_id = await asyncio.to_thread(
                fetch_tokens, asset, tf
            )

            if token_up and token_dn:
                print(f"[{market_name}] {question}")

                # Warm rate limiter from DB (prevents duplicate fills after restart)
                conn = sqlite3.connect(PAPER_DB)
                lb_rows = conn.execute("""
                    SELECT outcome, MAX(ts) FROM fills
                    WHERE market=? AND candle_start=? GROUP BY outcome
                """, (market_name, candle_start)).fetchall()
                conn.close()
                last_buy_up   = 0.0
                last_buy_down = 0.0
                for out, ts in lb_rows:
                    if out == 'Up':   last_buy_up   = ts or 0.0
                    if out == 'Down': last_buy_down = ts or 0.0

                async with state_lock:
                    state[(asset, tf)] = {
                        'candle_start':  candle_start,
                        'market_id':     market_id,
                        'token_up':      token_up,
                        'token_dn':      token_dn,
                        'last_mid_up':   None,
                        'last_buy_up':   last_buy_up,
                        'last_buy_down': last_buy_down,
                    }
                stop_evt = asyncio.Event()
                ws_task  = asyncio.create_task(
                    stream_market(asset, tf, stop_evt)
                )
            else:
                print(f"[{market_name}] Could not find tokens — will retry next candle")

            prev_candle_start = candle_start

        await asyncio.sleep(5)


# ── Stats printer ──────────────────────────────────────────────────────────────
async def print_stats():
    while True:
        await asyncio.sleep(300)
        try:
            conn = sqlite3.connect(PAPER_DB)
            row = conn.execute("""
                SELECT COUNT(*), SUM(pnl), SUM(up_cost+dn_cost),
                       SUM(CASE WHEN win=1 THEN 1 ELSE 0 END)
                FROM resolved WHERE winner NOT IN ('','SKIP','UNRESOLVED')
                  AND (n_up > 0 OR n_dn > 0)
            """).fetchone()
            conn.close()
            n, pnl, dep, wins = row
            n = n or 0; pnl = pnl or 0; dep = dep or 0; wins = wins or 0
            wr  = 100 * wins / n if n else 0
            roi = 100 * pnl / dep if dep else 0
            print(f"\n  === {datetime.now(timezone.utc).strftime('%H:%M UTC')} | "
                  f"Resolved: {n} | WR: {wr:.1f}% | "
                  f"PnL: ${pnl:+.2f} | ROI: {roi:.1f}% ===\n")
        except Exception:
            pass


# ── Main ───────────────────────────────────────────────────────────────────────
async def managed_market_loop(asset, tf):
    """Wraps manage_market so a crash in one market never kills the others."""
    while True:
        try:
            await manage_market(asset, tf)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{asset.upper()}_{tf}] crashed: {e} — restarting in 5s")
            await asyncio.sleep(5)


async def main():
    print(f"\n{'='*65}")
    print(f"  PAPER TRADER v7 — Fixed-Shares Price-Cap")
    print(f"  Cap={PRICE_CAP}  Interval={BUY_INTERVAL}s  Shares={SHARES}/buy")
    print(f"  Markets: BTC+ETH × 5m+15m")
    print(f"  DB: {PAPER_DB}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*65}\n")

    init_db()

    tasks = [managed_market_loop(asset, tf)
             for asset in ASSETS for tf in TIMEFRAMES]
    tasks.append(print_stats())
    await asyncio.gather(*tasks)


if __name__ == '__main__':
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
        except BaseException as e:
            print(f"[CRASH] {type(e).__name__}: {e} — restarting in 5s...")
            time.sleep(5)
