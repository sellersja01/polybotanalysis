"""
Paper Trader — Contrarian Cheap-Side DCA

Strategy:
  - Watch which side gets cheap first (mid drops to 0.40)
  - Buy that (cheap) side at each level: 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10
  - At mid=0.25: also buy the expensive side once (hedge)
  - If hedge side drops to mid=0.20, early exit hedge at bid = 2*mid - ask
  - Hold cheap side to $1.00 resolution
  - Winner = whichever side has higher mid at last observed tick

Backtest (BTC_5m + BTC_15m + ETH_5m, 3340 candles, 100% trigger rate):
  100sh cheap / 100sh hedge:  ~23% ROI  ~$32/candle
  (Equivalent to ~$230/candle at $1,000 deployed)
"""

import asyncio
import websockets
import requests
import sqlite3
import json
import time
import sys
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────────
CHEAP_LEVELS  = [0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10]
HEDGE_LEVEL   = 0.25   # buy expensive side once when cheap mid hits this
EXIT_MID      = 0.20   # early exit hedge when hedge mid drops to this
CHEAP_SHARES  = 100    # shares per level of cheap side
HEDGE_SHARES  = 100    # shares for hedge (expensive side, bought once)
PAPER_DB      = '/home/opc/paper_contrarian.db'

MARKETS    = [('btc', '5m'), ('btc', '15m'), ('eth', '5m')]
TIMEFRAMES = {'5m': 300, '15m': 900}
DB_PATHS   = {
    ('btc', '5m'):  '/home/opc/market_btc_5m.db',
    ('btc', '15m'): '/home/opc/market_btc_15m.db',
    ('eth', '5m'):  '/home/opc/market_eth_5m.db',
}

FEE_RATE = 0.25
FEE_EXP  = 2

def calc_fee(shares, price):
    return shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXP


# ── DB ──────────────────────────────────────────────────────────────────────────
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
            role         TEXT    NOT NULL,
            level        REAL    NOT NULL,
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
            cheap_side   TEXT,
            levels_hit   INTEGER DEFAULT 0,
            up_shares    REAL    DEFAULT 0,
            dn_shares    REAL    DEFAULT 0,
            up_cost      REAL    DEFAULT 0,
            dn_cost      REAL    DEFAULT 0,
            avg_up       REAL    DEFAULT 0,
            avg_dn       REAL    DEFAULT 0,
            up_exit_bid  REAL,
            dn_exit_bid  REAL,
            pnl          REAL    DEFAULT 0,
            win          INTEGER DEFAULT 0,
            UNIQUE(market, candle_start)
        );
        CREATE INDEX IF NOT EXISTS fills_candle ON fills(market, candle_start);
    """)
    conn.commit()
    conn.close()


def log_fill(market, candle_start, market_id, outcome, role, level, ask, shares):
    conn = sqlite3.connect(PAPER_DB)
    conn.execute("""
        INSERT INTO fills (ts, market, candle_start, market_id, outcome, role, level, ask, shares, cost)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (time.time(), market, candle_start, market_id, outcome, role, level, ask, shares, ask * shares))
    conn.commit()
    conn.close()


def get_fills(market, candle_start):
    conn = sqlite3.connect(PAPER_DB)
    rows = conn.execute(
        'SELECT outcome, role, ask, shares FROM fills WHERE market=? AND candle_start=?',
        (market, candle_start)
    ).fetchall()
    conn.close()
    up = [(role, float(a), int(s)) for o, role, a, s in rows if o == 'Up']
    dn = [(role, float(a), int(s)) for o, role, a, s in rows if o == 'Down']
    return up, dn


def save_resolved(market, candle_start, market_id, winner, cheap_side,
                  up_fills, dn_fills, up_exit_bid, dn_exit_bid, levels_hit):
    up_sh  = sum(s for _, _, s in up_fills)
    dn_sh  = sum(s for _, _, s in dn_fills)
    up_c   = sum(p * s for _, p, s in up_fills)
    dn_c   = sum(p * s for _, p, s in dn_fills)
    avg_up = up_c / up_sh if up_sh else 0.0
    avg_dn = dn_c / dn_sh if dn_sh else 0.0

    up_fee = sum(calc_fee(s, p) for _, p, s in up_fills)
    dn_fee = sum(calc_fee(s, p) for _, p, s in dn_fills)

    # Winner pays $1.00; loser pays exit_bid if early-exited, else $0
    if winner == 'Up':
        up_revenue = up_sh * 1.0
        dn_revenue = dn_sh * (dn_exit_bid if dn_exit_bid is not None else 0.0)
    else:
        dn_revenue = dn_sh * 1.0
        up_revenue = up_sh * (up_exit_bid if up_exit_bid is not None else 0.0)

    pnl = (up_revenue - up_c - up_fee) + (dn_revenue - dn_c - dn_fee)
    win = 1 if pnl > 0 else 0

    conn = sqlite3.connect(PAPER_DB)
    conn.execute("""
        INSERT OR IGNORE INTO resolved
          (resolved_at, market, candle_start, market_id, winner, cheap_side, levels_hit,
           up_shares, dn_shares, up_cost, dn_cost, avg_up, avg_dn,
           up_exit_bid, dn_exit_bid, pnl, win)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (time.time(), market, candle_start, market_id, winner, cheap_side, levels_hit,
          up_sh, dn_sh, up_c, dn_c, avg_up, avg_dn,
          up_exit_bid, dn_exit_bid, pnl, win))
    conn.commit()
    conn.close()

    icon     = "WIN " if win else "LOSS"
    ts_str   = datetime.fromtimestamp(candle_start, tz=timezone.utc).strftime('%H:%M')
    exit_str = ''
    if dn_exit_bid is not None: exit_str += f' dn_exit={dn_exit_bid:.3f}'
    if up_exit_bid is not None: exit_str += f' up_exit={up_exit_bid:.3f}'
    cheap_str = f'cheap={cheap_side}' if cheap_side else 'no-entry'
    print(f"  [{icon}] {market:<10} {ts_str} | winner={winner} | {cheap_str} | lvls={levels_hit} | "
          f"up={up_sh:.0f}sh@{avg_up:.3f}  dn={dn_sh:.0f}sh@{avg_dn:.3f}"
          f"{exit_str} | pnl=${pnl:+.2f}")
    return pnl


# ── Token discovery ──────────────────────────────────────────────────────────────
def fetch_tokens(asset, tf):
    interval = TIMEFRAMES[tf]
    cs       = (int(time.time()) // interval) * interval
    slug     = f"{asset}-updown-{tf}-{cs}"

    try:
        data = requests.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10
        ).json()
        if data and data[0].get('markets'):
            mkt      = data[0]['markets'][0]
            tokens   = json.loads(mkt.get('clobTokenIds', '[]'))
            outcomes = json.loads(mkt.get('outcomes', '["Up","Down"]'))
            if len(tokens) >= 2:
                up_idx = outcomes.index('Up') if 'Up' in outcomes else 0
                dn_idx = outcomes.index('Down') if 'Down' in outcomes else 1
                return tokens[up_idx], tokens[dn_idx], mkt.get('question', ''), str(mkt.get('id', ''))
    except Exception:
        pass

    try:
        conn = sqlite3.connect(f'file:{DB_PATHS[(asset, tf)]}?mode=ro', uri=True)
        row  = conn.execute(
            'SELECT market_id, question FROM polymarket_odds WHERE unix_time >= ? ORDER BY unix_time DESC LIMIT 1',
            (cs,)
        ).fetchone()
        conn.close()
        if row:
            resp     = requests.get(f"https://gamma-api.polymarket.com/markets/{row[0]}", timeout=10).json()
            tokens   = json.loads(resp.get('clobTokenIds', '[]'))
            outcomes = json.loads(resp.get('outcomes', '["Up","Down"]'))
            if len(tokens) >= 2:
                up_idx = outcomes.index('Up') if 'Up' in outcomes else 0
                dn_idx = outcomes.index('Down') if 'Down' in outcomes else 1
                return tokens[up_idx], tokens[dn_idx], row[1], row[0]
    except Exception:
        pass

    return None, None, None, None


# ── Shared state ─────────────────────────────────────────────────────────────────
state: dict = {}
state_lock  = asyncio.Lock()


# ── CLOB websocket stream ────────────────────────────────────────────────────────
async def stream_market(asset, tf, stop_evt):
    market_name = f"{asset.upper()}_{tf}"
    ws_url      = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    async with state_lock:
        s            = state[(asset, tf)]
        token_up     = s['token_up']
        token_dn     = s['token_dn']
        candle_start = s['candle_start']
        market_id    = s['market_id']

    books = {token_up: {'bids': {}, 'asks': {}}, token_dn: {'bids': {}, 'asks': {}}}

    while not stop_evt.is_set():
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=15, open_timeout=15
            ) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": [token_up, token_dn], "markets": []
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
                                p, sz = ch['price'], float(ch['size'])
                                d = book['bids'] if ch['side'] == 'BUY' else book['asks']
                                if sz == 0: d.pop(p, None)
                                else:       d[p] = sz
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

                        new_fills = []   # list of (outcome, role, level, ask, shares)
                        do_exit   = None

                        async with state_lock:
                            s = state.get((asset, tf))
                            if s is None or s['candle_start'] != candle_start:
                                return

                            # Update last seen prices for this side
                            if outcome == 'Up':
                                s['last_mid_up'] = mid
                                s['last_ask_up'] = best_ask
                            else:
                                s['last_mid_dn'] = mid
                                s['last_ask_dn'] = best_ask

                            # ── Determine cheap side on first trigger ─────────
                            if s['cheap_side'] is None and mid <= CHEAP_LEVELS[0]:
                                s['cheap_side'] = outcome

                            cheap_side = s['cheap_side']

                            # ── Entry: buy cheap side at each level ───────────
                            if cheap_side == outcome:
                                for lvl in CHEAP_LEVELS:
                                    if lvl not in s['cheap_levels_triggered'] and mid <= lvl:
                                        s['cheap_levels_triggered'].add(lvl)
                                        new_fills.append((outcome, 'cheap', lvl, best_ask, CHEAP_SHARES))

                                        # At mid=0.25: also buy hedge (expensive side once)
                                        if lvl == HEDGE_LEVEL and not s['hedge_bought']:
                                            hedge_out = 'Down' if outcome == 'Up' else 'Up'
                                            hedge_ask = s.get('last_ask_dn') if outcome == 'Up' else s.get('last_ask_up')
                                            if hedge_ask:
                                                new_fills.append((hedge_out, 'hedge', lvl, hedge_ask, HEDGE_SHARES))
                                                s['hedge_bought'] = True

                            # ── Early exit: hedge drops to EXIT_MID ──────────
                            if cheap_side and s['hedge_bought']:
                                hedge_side = 'Down' if cheap_side == 'Up' else 'Up'
                                if outcome == hedge_side and mid <= EXIT_MID:
                                    exit_bid = max(0.0, 2 * mid - best_ask)
                                    if outcome == 'Up' and s['up_exit_bid'] is None:
                                        s['up_exit_bid'] = exit_bid
                                        do_exit = ('Up', exit_bid)
                                    elif outcome == 'Down' and s['dn_exit_bid'] is None:
                                        s['dn_exit_bid'] = exit_bid
                                        do_exit = ('Down', exit_bid)

                        ts_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime('%H:%M:%S')

                        for out, role, lvl, ask, shares in new_fills:
                            log_fill(market_name, candle_start, market_id, out, role, lvl, ask, shares)
                            role_str = 'CHEAP' if role == 'cheap' else 'HEDGE'
                            print(f"  {role_str} [{lvl:.2f}] {market_name:<10} {out}@{ask:.3f} "
                                  f"| mid={mid:.3f} | {ts_str}")

                        if do_exit:
                            side, exit_bid = do_exit
                            print(f"  EXIT  hedge {market_name:<10} {side} @ bid={exit_bid:.3f} "
                                  f"| mid={mid:.3f} | {ts_str}")

        except Exception:
            if not stop_evt.is_set():
                await asyncio.sleep(3)


# ── Market manager ───────────────────────────────────────────────────────────────
async def manage_market(asset, tf):
    interval          = TIMEFRAMES[tf]
    market_name       = f"{asset.upper()}_{tf}"
    stop_evt          = None
    ws_task           = None
    prev_candle_start = None

    while True:
        now          = time.time()
        candle_start = (int(now) // interval) * interval

        if candle_start != prev_candle_start:

            # ── Resolve previous candle ────────────────────────────────────
            if prev_candle_start is not None:
                async with state_lock:
                    s          = state.get((asset, tf), {})
                    mid_up     = s.get('last_mid_up')
                    up_exit    = s.get('up_exit_bid')
                    dn_exit    = s.get('dn_exit_bid')
                    mid_id     = s.get('market_id', '')
                    lvls_hit   = len(s.get('cheap_levels_triggered', set()))
                    cheap_side = s.get('cheap_side')

                up_fills, dn_fills = get_fills(market_name, prev_candle_start)
                if up_fills or dn_fills:
                    winner = ('Up' if mid_up >= 0.5 else 'Down') if mid_up is not None else 'UNRESOLVED'
                    save_resolved(market_name, prev_candle_start, mid_id, winner, cheap_side,
                                  up_fills, dn_fills, up_exit, dn_exit, lvls_hit)

            # ── Stop old stream ────────────────────────────────────────────
            if stop_evt: stop_evt.set()
            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:    await ws_task
                except: pass

            # ── Discover tokens for new candle ─────────────────────────────
            print(f"\n[{market_name}] New candle @ "
                  f"{datetime.fromtimestamp(candle_start, tz=timezone.utc).strftime('%H:%M')} UTC")
            token_up, token_dn, question, market_id = await asyncio.to_thread(
                fetch_tokens, asset, tf
            )

            if token_up and token_dn:
                print(f"[{market_name}] {question}")
                async with state_lock:
                    state[(asset, tf)] = {
                        'candle_start':           candle_start,
                        'market_id':              market_id,
                        'token_up':               token_up,
                        'token_dn':               token_dn,
                        'last_mid_up':            None,
                        'last_ask_up':            None,
                        'last_mid_dn':            None,
                        'last_ask_dn':            None,
                        'cheap_side':             None,
                        'cheap_levels_triggered': set(),
                        'hedge_bought':           False,
                        'up_exit_bid':            None,
                        'dn_exit_bid':            None,
                    }
                stop_evt = asyncio.Event()
                ws_task  = asyncio.create_task(stream_market(asset, tf, stop_evt))
            else:
                print(f"[{market_name}] Could not find tokens — skipping candle")

            prev_candle_start = candle_start

        await asyncio.sleep(5)


# ── Stats printer ────────────────────────────────────────────────────────────────
async def print_stats():
    while True:
        await asyncio.sleep(300)
        try:
            conn = sqlite3.connect(PAPER_DB)
            rows = conn.execute("""
                SELECT market,
                       COUNT(*),
                       SUM(pnl),
                       SUM(up_cost + dn_cost),
                       SUM(CASE WHEN win=1 THEN 1 ELSE 0 END),
                       AVG(levels_hit)
                FROM resolved
                WHERE winner NOT IN ('', 'UNRESOLVED')
                  AND (up_shares > 0 OR dn_shares > 0)
                GROUP BY market
            """).fetchall()
            totals = conn.execute("""
                SELECT COUNT(*), SUM(pnl), SUM(up_cost+dn_cost),
                       SUM(CASE WHEN win=1 THEN 1 ELSE 0 END), AVG(levels_hit)
                FROM resolved
                WHERE winner NOT IN ('','UNRESOLVED')
                  AND (up_shares > 0 OR dn_shares > 0)
            """).fetchone()
            conn.close()

            print(f"\n  === {datetime.now(timezone.utc).strftime('%H:%M UTC')} ===")
            for mkt, n, pnl, dep, wins, avg_lvls in rows:
                n = n or 0; pnl = pnl or 0; dep = dep or 0; wins = wins or 0
                wr  = 100 * wins / n if n else 0
                roi = 100 * pnl / dep if dep else 0
                print(f"  {mkt:<12} n={n:3d} | WR={wr:.1f}% | PnL=${pnl:+.2f} | ROI={roi:.2f}% | AvgLvls={avg_lvls:.1f}")
            if totals and totals[0]:
                n, pnl, dep, wins, avg_lvls = totals
                n = n or 0; pnl = pnl or 0; dep = dep or 0; wins = wins or 0
                wr  = 100 * wins / n if n else 0
                roi = 100 * pnl / dep if dep else 0
                print(f"  {'TOTAL':<12} n={n:3d} | WR={wr:.1f}% | PnL=${pnl:+.2f} | ROI={roi:.2f}%")
            print()
        except Exception:
            pass


# ── Main ─────────────────────────────────────────────────────────────────────────
async def managed_loop(asset, tf):
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
    print(f"  PAPER TRADER — Contrarian Cheap-Side DCA")
    print(f"  Cheap levels : {CHEAP_LEVELS}")
    print(f"  Cheap shares : {CHEAP_SHARES}/level")
    print(f"  Hedge        : {HEDGE_SHARES}sh expensive side at mid={HEDGE_LEVEL}")
    print(f"  Hedge exit   : mid <= {EXIT_MID} -> bid = 2*mid - ask")
    print(f"  Markets      : BTC_5m, BTC_15m, ETH_5m")
    print(f"  DB           : {PAPER_DB}")
    print(f"  Started      : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*65}\n")
    init_db()
    tasks = [managed_loop(asset, tf) for asset, tf in MARKETS] + [print_stats()]
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
