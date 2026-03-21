import requests
import csv
from datetime import datetime, timezone

WALLETS = {
    "wallet_1": "0x61276aba49117fd9299707d5d573652949d5c977",
    "wallet_2": "0x5bde889dc26b097b5eaa2f1f027e01712ebccbb7",
    "wallet_3": "0xd111ced402bac802f74606deca83bbf6a1eaaf32",
    "wallet_4": "0x437bfe05a1e169b1443f16e718525a88b6f283b2",
    "wallet_5": "0x52f8784a81d967a3afb74d2e1608503ff5e261b9",
    "wallet_6": "0xa84edaf1a562eabb463dc6cf4c3e9c407a5edbeb",
    "wallet_7": "0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82",
    "wallet_8": "0xf539c942036cc7633a1e0015209a1343e9b2dda9",
    "wallet_9": "0x37c94ea1b44e01b18a1ce3ab6f8002bd6b9d7e6d",
}

def fetch_last40(wallet):
    all_trades = []
    for offset in range(0, 3000, 500):
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": wallet, "limit": 500, "offset": offset},
                timeout=15
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            all_trades.extend(data)
            if len(all_trades) >= 1000:
                break
            if len(data) < 500:
                break
        except Exception as e:
            print(f"  Error: {e}")
            break
    return all_trades[:1000]

def analyze(name, wallet, trades):
    print(f"\n{'='*70}")
    print(f"  {name} | {wallet}")
    print(f"{'='*70}")
    
    if not trades:
        print("  No trades found.")
        return

    print(f"  {len(trades)} trades fetched")
    print(f"\n  {'#':<4} {'Time':<14} {'Side':<5} {'Outcome':<7} {'Price':<8} {'Size':<10} {'USDC':<10} Market")
    print(f"  {'-'*90}")

    pairs = {}  # track Up+Down pairs per market per second
    
    for i, t in enumerate(trades):
        ts = t.get("timestamp", 0)
        # handle ms timestamps
        if ts > 9999999999:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        side = t.get("side", "")
        outcome = t.get("outcome", "")
        price = float(t.get("price", 0))
        size = float(t.get("size", 0))
        usdc = float(t.get("usdcAmt", price * size))
        market = t.get("market", t.get("title", ""))[-45:]

        print(f"  {i+1:<4} {dt:<14} {side:<5} {outcome:<7} {price:<8.3f} {size:<10.1f} {usdc:<10.2f} {market}")

        # track pairs
        bucket = round(ts)
        key = (t.get("market", ""), bucket)
        if key not in pairs:
            pairs[key] = {}
        if outcome in ("Up", "Down"):
            pairs[key][outcome] = price

    # Check for arb pairs
    arb_count = 0
    print(f"\n  --- Arb pair analysis ---")
    for key, sides in pairs.items():
        if "Up" in sides and "Down" in sides:
            combined = sides["Up"] + sides["Down"]
            gap = 1.0 - combined
            market = key[0][-45:]
            status = "ARB ✓" if combined < 1.0 else "no arb"
            print(f"  {status} | Up={sides['Up']:.3f} + Down={sides['Down']:.3f} = {combined:.3f} (gap={gap:+.3f}) | {market}")
            if combined < 1.0:
                arb_count += 1

    if arb_count == 0:
        print("  No arb pairs detected in last 40 trades")
    
    print(f"\n  Total arb opportunities: {arb_count}")

    # Save to CSV
    filename = f"{name}_trades.csv"
    with open(filename, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "side", "outcome", "price", "size", "usdc", "market"])
        for t in trades:
            ts = t.get("timestamp", 0)
            if ts > 9999999999:
                ts = ts / 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            w.writerow([
                dt,
                t.get("side", ""),
                t.get("outcome", ""),
                t.get("price", 0),
                t.get("size", 0),
                t.get("usdcAmt", 0),
                t.get("market", t.get("title", ""))
            ])
    print(f"  Saved to {filename}")

if __name__ == "__main__":
    for name, wallet in WALLETS.items():
        print(f"\nFetching {name}...")
        trades = fetch_last40(wallet)
        analyze(name, wallet, trades)
