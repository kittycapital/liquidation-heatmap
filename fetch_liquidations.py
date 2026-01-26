"""
Hyperliquid Liquidation Heatmap Fetcher
Fetches top 100 traders' positions and extracts liquidation prices.
"""

import json
import requests
from datetime import datetime
from collections import defaultdict
import time

DATA_FILE = 'data.json'

HEADERS = {
    'Content-Type': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

API_URL = 'https://api.hyperliquid.xyz/info'

# Top 10 coins to track
TOP_COINS = ['BTC', 'ETH', 'SOL', 'HYPE', 'XRP', 'DOGE', 'SUI', 'LINK', 'AVAX', 'PEPE']

# Leverage buckets for color coding
LEVERAGE_BUCKETS = [
    {'min': 1, 'max': 10, 'label': '10x', 'color': '#3b82f6'},    # Blue
    {'min': 11, 'max': 25, 'label': '25x', 'color': '#eab308'},   # Yellow
    {'min': 26, 'max': 50, 'label': '50x', 'color': '#f97316'},   # Orange
    {'min': 51, 'max': 100, 'label': '100x', 'color': '#ef4444'}  # Red
]


def get_leverage_bucket(leverage):
    """Get leverage bucket label"""
    for bucket in LEVERAGE_BUCKETS:
        if bucket['min'] <= leverage <= bucket['max']:
            return bucket['label']
    return '100x'


def fetch_leaderboard():
    """Fetch top traders from leaderboard"""
    try:
        # Hyperliquid leaderboard endpoint
        url = 'https://stats-data.hyperliquid.xyz/Mainnet/leaderboard'
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        wallets = []
        
        # Handle different response formats
        if isinstance(data, dict) and 'leaderboardRows' in data:
            for entry in data['leaderboardRows'][:200]:
                wallet = entry.get('ethAddress') or entry.get('user')
                if wallet:
                    wallets.append(wallet)
        elif isinstance(data, list):
            for entry in data[:200]:
                if isinstance(entry, dict):
                    wallet = entry.get('ethAddress') or entry.get('user') or entry.get('address')
                    if wallet:
                        wallets.append(wallet)
                elif isinstance(entry, str):
                    wallets.append(entry)
        
        print(f"  Leaderboard: {len(wallets)} traders found")
        return wallets
    except Exception as e:
        print(f"  Leaderboard error: {e}")
        # Fallback: try alternative endpoint
        try:
            payload = {"type": "leaderboard", "timeWindow": "day"}
            response = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            wallets = []
            if isinstance(data, list):
                for entry in data[:200]:
                    if isinstance(entry, dict):
                        wallet = entry.get('ethAddress') or entry.get('user')
                        if wallet:
                            wallets.append(wallet)
            print(f"  Leaderboard (fallback): {len(wallets)} traders found")
            return wallets
        except Exception as e2:
            print(f"  Leaderboard fallback error: {e2}")
            return []


def fetch_clearinghouse_state(wallet):
    """Fetch user's positions and liquidation prices"""
    try:
        payload = {"type": "clearinghouseState", "user": wallet}
        response = requests.post(API_URL, json=payload, headers=HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        positions = []
        asset_positions = data.get('assetPositions', [])
        
        for ap in asset_positions:
            pos = ap.get('position', {})
            coin = pos.get('coin', '')
            
            if coin not in TOP_COINS:
                continue
            
            liq_price = pos.get('liquidationPx')
            entry_price = pos.get('entryPx')
            size = pos.get('szi', '0')
            leverage_info = pos.get('leverage', {})
            
            if liq_price and entry_price:
                # Determine if long or short
                size_float = float(size) if size else 0
                is_long = size_float > 0
                
                # Get leverage value
                leverage = leverage_info.get('value', 1) if isinstance(leverage_info, dict) else 1
                
                # Calculate position value
                position_value = abs(size_float) * float(entry_price)
                
                positions.append({
                    'coin': coin,
                    'liquidationPx': float(liq_price),
                    'entryPx': float(entry_price),
                    'size': abs(size_float),
                    'positionValue': position_value,
                    'leverage': leverage,
                    'leverageBucket': get_leverage_bucket(leverage),
                    'side': 'long' if is_long else 'short'
                })
        
        return positions
    except Exception as e:
        return []


def fetch_current_prices():
    """Fetch current mark prices for all coins"""
    try:
        payload = {"type": "metaAndAssetCtxs"}
        response = requests.post(API_URL, json=payload, headers=HEADERS, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        prices = {}
        if len(data) >= 2:
            meta = data[0]
            contexts = data[1]
            
            for i, asset in enumerate(meta.get('universe', [])):
                coin = asset.get('name', '')
                if coin in TOP_COINS and i < len(contexts):
                    mark_price = float(contexts[i].get('markPx', 0) or 0)
                    prices[coin] = mark_price
        
        return prices
    except Exception as e:
        print(f"  Price fetch error: {e}")
        return {}


def aggregate_liquidations(all_positions, current_prices):
    """Aggregate liquidation data by price levels"""
    result = {}
    
    for coin in TOP_COINS:
        current_price = current_prices.get(coin, 0)
        if current_price == 0:
            continue
        
        # Filter positions for this coin
        coin_positions = [p for p in all_positions if p['coin'] == coin]
        
        if not coin_positions:
            continue
        
        # Determine price range (±30% from current price)
        price_min = current_price * 0.7
        price_max = current_price * 1.3
        
        # Create price buckets (50 buckets)
        num_buckets = 50
        bucket_size = (price_max - price_min) / num_buckets
        
        # Initialize buckets
        long_liquidations = defaultdict(lambda: {'total': 0, '10x': 0, '25x': 0, '50x': 0, '100x': 0})
        short_liquidations = defaultdict(lambda: {'total': 0, '10x': 0, '25x': 0, '50x': 0, '100x': 0})
        
        cumulative_long = 0
        cumulative_short = 0
        
        for pos in coin_positions:
            liq_price = pos['liquidationPx']
            value = pos['positionValue']
            leverage_bucket = pos['leverageBucket']
            
            # Find bucket index
            if price_min <= liq_price <= price_max:
                bucket_idx = int((liq_price - price_min) / bucket_size)
                bucket_idx = min(bucket_idx, num_buckets - 1)
                bucket_price = price_min + (bucket_idx + 0.5) * bucket_size
                
                if pos['side'] == 'long':
                    # Long positions get liquidated below entry
                    long_liquidations[bucket_price]['total'] += value
                    long_liquidations[bucket_price][leverage_bucket] += value
                    cumulative_long += value
                else:
                    # Short positions get liquidated above entry
                    short_liquidations[bucket_price]['total'] += value
                    short_liquidations[bucket_price][leverage_bucket] += value
                    cumulative_short += value
        
        # Convert to sorted lists
        long_data = []
        short_data = []
        
        # Calculate cumulative values
        sorted_long_prices = sorted(long_liquidations.keys(), reverse=True)
        sorted_short_prices = sorted(short_liquidations.keys())
        
        cum_long = 0
        for price in sorted_long_prices:
            cum_long += long_liquidations[price]['total']
            long_data.append({
                'price': round(price, 2),
                'value': long_liquidations[price]['total'],
                'cumulative': cum_long,
                '10x': long_liquidations[price]['10x'],
                '25x': long_liquidations[price]['25x'],
                '50x': long_liquidations[price]['50x'],
                '100x': long_liquidations[price]['100x']
            })
        
        cum_short = 0
        for price in sorted_short_prices:
            cum_short += short_liquidations[price]['total']
            short_data.append({
                'price': round(price, 2),
                'value': short_liquidations[price]['total'],
                'cumulative': cum_short,
                '10x': short_liquidations[price]['10x'],
                '25x': short_liquidations[price]['25x'],
                '50x': short_liquidations[price]['50x'],
                '100x': short_liquidations[price]['100x']
            })
        
        # Sort by price for display
        long_data.sort(key=lambda x: x['price'])
        short_data.sort(key=lambda x: x['price'])
        
        result[coin] = {
            'currentPrice': current_price,
            'longLiquidations': long_data,
            'shortLiquidations': short_data,
            'totalLongValue': cumulative_long,
            'totalShortValue': cumulative_short,
            'positionCount': len(coin_positions)
        }
    
    return result


def main():
    print("=" * 50)
    print("Hyperliquid Liquidation Heatmap Fetcher")
    print("=" * 50)
    
    # Fetch current prices
    print("\n1. Fetching current prices...")
    current_prices = fetch_current_prices()
    print(f"  Got prices for {len(current_prices)} coins")
    
    # Fetch leaderboard
    print("\n2. Fetching leaderboard...")
    wallets = fetch_leaderboard()
    
    if not wallets:
        print("  Failed to fetch leaderboard, using alternative method...")
        # Alternative: fetch from known active traders or use a fallback
        payload = {"type": "allMids"}
        # For now, we'll create sample data if leaderboard fails
        wallets = []
    
    # Fetch positions for each wallet
    print(f"\n3. Fetching positions for {len(wallets)} traders...")
    all_positions = []
    
    for i, wallet in enumerate(wallets):
        positions = fetch_clearinghouse_state(wallet)
        all_positions.extend(positions)
        
        if (i + 1) % 20 == 0:
            print(f"  Processed {i + 1}/{len(wallets)} wallets ({len(all_positions)} positions found)")
        
        # Rate limiting
        time.sleep(0.1)
    
    print(f"  Total positions: {len(all_positions)}")
    
    # Aggregate data
    print("\n4. Aggregating liquidation data...")
    liquidation_data = aggregate_liquidations(all_positions, current_prices)
    
    # Build output
    output = {
        'coins': list(liquidation_data.keys()),
        'data': liquidation_data,
        'leverageBuckets': LEVERAGE_BUCKETS,
        'tradersCount': len(wallets),
        'lastUpdated': datetime.utcnow().isoformat() + 'Z'
    }
    
    # Print summary
    print("\nSummary:")
    for coin, data in liquidation_data.items():
        print(f"  {coin}: {data['positionCount']} positions, "
              f"Long ${data['totalLongValue']:,.0f}, Short ${data['totalShortValue']:,.0f}")
    
    # Save to JSON
    with open(DATA_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\nSaved to {DATA_FILE}")


if __name__ == '__main__':
    main()
