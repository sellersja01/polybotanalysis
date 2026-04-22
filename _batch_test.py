"""
Measure: does Polymarket's post_orders() batch endpoint parallelize
internally, or process orders sequentially?

Method:
  1. Get current 5m market tokens for BTC/ETH/SOL/XRP (Down side of each).
  2. Pre-sign 4 market orders, $1 each (tiny exposure, $4 total).
  3. Fire them via post_orders() in ONE HTTP call.
  4. Measure: total time from send to response, shares filled per order.

If total time ≈ baseline post_order latency (~400-500ms) → parallel ✅
If total time ≈ 4× baseline (~1600-2000ms) → sequential ❌
Anything in between tells us the level of amortization.
"""
import os, json, time, asyncio, aiohttp
from datetime import datetime, timezone

TRADE_USD = 1.0   # tiny for test
POLY_KEY  = os.environ.get("POLY_PRIVATE_KEY","")
POLY_FUNDER = "0x6826c3197fff281144b07fe6c3e72636854769ab"
POLY_CLOB = "https://clob.polymarket.com"
INTERVAL = 300
ASSETS = [
    ("BTC", "btc"), ("ETH", "eth"), ("SOL", "sol"), ("XRP", "xrp"),
]

def build_clob():
    from py_clob_client.client import ClobClient
    c = ClobClient(host=POLY_CLOB, key=POLY_KEY, chain_id=137,
                   signature_type=2, funder=POLY_FUNDER)
    creds = c.create_or_derive_api_creds()
    c.set_api_creds(creds)
    return c

async def get_market(slug):
    now=time.time()
    async with aiohttp.ClientSession() as s:
        for off in range(5):
            cs=int(now//INTERVAL)*INTERVAL-(off*INTERVAL)
            sl=f"{slug}-updown-5m-{cs}"
            try:
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={sl}",timeout=10) as r:
                    d=await r.json()
                if d:
                    m=d[0]["markets"][0]
                    t=json.loads(m.get("clobTokenIds","[]"))
                    o=json.loads(m.get("outcomes","[]"))
                    if len(t)>=2:
                        ui=0 if o[0]=="Up" else 1
                        return t[ui],t[1-ui],cs
            except: pass
    return None,None,None

async def main():
    print("="*66)
    print(f"  Batch endpoint test — {len(ASSETS)} orders at ${TRADE_USD} each")
    print("="*66)

    if not POLY_KEY:
        print("ERROR: POLY_PRIVATE_KEY env not set")
        return

    print("\nBuilding CLOB client...")
    clob = build_clob()
    print(f"  creds derived: {clob.creds.api_key[:8]}...")

    print("\nFetching market tokens (Down side of each asset)...")
    tokens = []
    for label, slug in ASSETS:
        up, dn, cs = await get_market(slug)
        if not dn:
            print(f"  {label}: FAILED to fetch market, skipping")
            continue
        print(f"  {label}: Down token {dn[:10]}... (candle start {cs})")
        tokens.append((label, dn))
    if len(tokens) < 2:
        print("ERROR: need at least 2 markets to test batch"); return

    print(f"\nPre-signing {len(tokens)} orders (Down side, ${TRADE_USD} each)...")
    from py_clob_client.clob_types import MarketOrderArgs, PostOrdersArgs
    signed_orders = []
    t_presign_start = time.time()
    for label, tok in tokens:
        t0 = time.time()
        args = MarketOrderArgs(token_id=tok, amount=round(TRADE_USD,2), side="BUY", price=0.99)
        signed = await asyncio.to_thread(clob.create_market_order, args)
        dt = (time.time()-t0)*1000
        signed_orders.append((label, signed))
        print(f"  {label}: signed in {dt:.0f}ms")
    t_presign_total = (time.time()-t_presign_start)*1000
    print(f"  total presign time: {t_presign_total:.0f}ms\n")

    print(f"--- Firing post_orders() with {len(signed_orders)} orders in ONE HTTP call ---")
    args_list = [PostOrdersArgs(order=s, orderType="FAK") for _,s in signed_orders]

    t_send = time.time()
    try:
        resp = await asyncio.to_thread(clob.post_orders, args_list)
        t_recv = time.time()
    except Exception as e:
        t_recv = time.time()
        print(f"ERROR: {e} after {(t_recv-t_send)*1000:.0f}ms")
        return

    total_ms = (t_recv - t_send)*1000
    print(f"\n  TOTAL post_orders() time: {total_ms:.0f} ms")
    print(f"  Response type: {type(resp).__name__}")
    print(f"  Response: {json.dumps(resp, default=str, indent=2)[:2000]}")

    # Interpret shares filled
    if isinstance(resp, list):
        print(f"\n  Per-order fills:")
        for (label, _), r in zip(signed_orders, resp):
            if isinstance(r, dict):
                shares = float(r.get("takingAmount", 0) or 0)
                spent  = float(r.get("makingAmount", TRADE_USD) or TRADE_USD)
                status = r.get("status","?")
                fill_price = spent / shares if shares > 0 else None
                print(f"    {label}: shares={shares:.3f} spent=${spent:.2f} fill=${fill_price:.3f} status={status}")

    # Compare to baseline
    print("\n" + "="*66)
    print(f"  INTERPRETATION:")
    print(f"    Baseline single post_order latency: ~400-500 ms")
    print(f"    Measured post_orders({len(signed_orders)}): {total_ms:.0f} ms")
    n = len(signed_orders)
    if total_ms < 700:
        print(f"    → PARALLEL: server processed all {n} orders concurrently. Batch dispatcher is a big win.")
    elif total_ms < 1200:
        print(f"    → PARTIAL AMORTIZATION: saves ~{1600-total_ms:.0f}ms vs serial but not fully parallel.")
    else:
        print(f"    → SEQUENTIAL: ~{total_ms/n:.0f}ms/order, no meaningful parallelism. Batching = marginal win only.")
    print("="*66)

if __name__ == "__main__":
    asyncio.run(main())
