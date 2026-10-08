import os
import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
from kiteconnect import KiteConnect

# Import pattern recognition logic
from patternsdetect.patterns import detect_all_patterns
# Import cross-platform notifier
from notifier import notifier

CONFIG_FILE = "config.json"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

def run_pattern_scanner():
    # Load dotenv from workspace root or parent directories
    load_dotenv(ENV_FILE, override=True)
    
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    
    if not api_key or not access_token:
        print("[ERROR] Credentials missing in .env file. From trading_alerts, run .venv/bin/python -m kite_runtime kite --login first.")
        sys.exit(1)
        
    # Read config.json for symbols
    if not os.path.exists(CONFIG_FILE):
        print(f"[ERROR] Configuration file '{CONFIG_FILE}' not found.")
        sys.exit(1)
        
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        
    symbols = config.get("symbols", [])
    if not symbols:
        print("[SYSTEM] No symbols configured in config.json. Exiting.")
        return
        
    # Initialize Kite Connect client
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    
    # 1. Resolve Instrument Tokens (Single batched call for efficiency)
    print(f"[SYSTEM] Resolving instrument tokens for {len(symbols)} symbols...")
    exchanges = set(sym.split(":")[0] for sym in symbols if ":" in sym)
    if not exchanges:
        exchanges = {"NSE"}
        
    all_instruments = []
    for ex in exchanges:
        try:
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[SYSTEM] Fetching instruments for exchange: {ex}...")
            insts = kite.instruments(ex)
            all_instruments.extend(insts)
        except Exception as e:
            print(f"[SYSTEM WARNING] Failed to fetch instruments for {ex}: {e}")
            
    if not all_instruments:
        try:
            if os.getenv("VERBOSE", "false").lower() == "true":
                print("[SYSTEM] Fetching all instruments from Kite Connect...")
            all_instruments = kite.instruments()
        except Exception as e:
            print(f"[SYSTEM ERROR] Could not fetch instruments: {e}")
            sys.exit(1)
            
    mapping = {}
    for inst in all_instruments:
        ex = inst.get("exchange")
        sym = inst.get("tradingsymbol")
        token = inst.get("instrument_token")
        if ex and sym and token:
            mapping[f"{ex}:{sym}"] = token
            mapping[sym] = token
            
    symbol_tokens = {}
    for sym in symbols:
        token = mapping.get(sym)
        if not token:
            clean_sym = sym.split(":")[-1] if ":" in sym else sym
            token = mapping.get(clean_sym)
        if token:
            symbol_tokens[sym] = token
        else:
            print(f"[SYSTEM WARNING] Could not resolve token for symbol: {sym}")
            
    if not symbol_tokens:
        print("[SYSTEM ERROR] No symbols resolved. Exiting.")
        sys.exit(1)
        
    # 2. Scan each symbol for chart patterns
    interval = os.getenv("PATTERN_INTERVAL", "day")
    days_to_fetch = int(os.getenv("PATTERN_DAYS", "180"))
    
    # Cap historical duration for intraday timeframes to prevent API range limits
    if interval != "day":
        days_to_fetch = min(days_to_fetch, 30)
        
    print(f"\n[SYSTEM] Starting Pattern Scanner (Interval: {interval}, History: {days_to_fetch} days)")
    print("=" * 80)
    
    for symbol, token in symbol_tokens.items():
        if os.getenv("VERBOSE", "false").lower() == "true":
            print(f"Scanning {symbol}...")
        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=days_to_fetch)
            
            # Fetch historical candlestick records
            records = kite.historical_data(token, from_date, to_date, interval)
            if not records:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"  └─ No historical data returned for {symbol}.")
                continue
                
            df = pd.DataFrame(records)
            df['date'] = pd.to_datetime(df['date']) # Ensure timezone-aware or standardized dates
            
            # Detect pattern configurations
            detected = detect_all_patterns(df)
            
            if not detected:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"  └─ No chart patterns detected.")
                continue
                
            for pattern_res in detected:
                pattern_name = pattern_res.get("pattern")
                
                # Determine bullishness of pattern
                is_bullish = True
                if "top" in pattern_name.lower() or "bearish" in pattern_name.lower() or "head" in pattern_name.lower():
                    is_bullish = False
                    
                status = pattern_res.get("status", "confirmed")
                detail_str = f"Chart Pattern: {pattern_name} ({status.upper()})"
                
                # Add specific pattern details
                if pattern_name == "Double Top":
                    left_val = pattern_res.get("left_peak_price")
                    right_val = pattern_res.get("right_peak_price")
                    left_date = pattern_res.get("left_peak_date")
                    right_date = pattern_res.get("right_peak_date")
                    detail_str += f" | Peak 1: {left_val:.2f} ({left_date}) | Peak 2: {right_val:.2f} ({right_date})"
                    if "neckline" in pattern_res:
                        detail_str += f" | Neckline: {pattern_res['neckline']:.2f}"
                elif pattern_name == "Double Bottom":
                    left_val = pattern_res.get("left_bottom_price")
                    right_val = pattern_res.get("right_bottom_price")
                    left_date = pattern_res.get("left_bottom_date")
                    right_date = pattern_res.get("right_bottom_date")
                    detail_str += f" | Bottom 1: {left_val:.2f} ({left_date}) | Bottom 2: {right_val:.2f} ({right_date})"
                    if "neckline" in pattern_res:
                        detail_str += f" | Neckline: {pattern_res['neckline']:.2f}"
                elif "neckline" in pattern_res:
                    detail_str += f" | Neckline: {pattern_res['neckline']:.2f}"
                elif "resistance" in pattern_res and "support" in pattern_res:
                    detail_str += f" | Range: {pattern_res['support']:.2f} - {pattern_res['resistance']:.2f}"
                    
                last_price = float(df.iloc[-1]['close'])
                
                # Trigger alert and notification
                notifier.send_alert(
                    symbol=symbol,
                    is_bullish=is_bullish,
                    condition_str=detail_str,
                    price=last_price,
                    cooldown_key=f"pattern_{pattern_name}"
                )
                
        except Exception as e:
            print(f"  └─ Error scanning {symbol}: {e}")
            
    print("\n" + "=" * 80)
    print("PATTERN SCAN COMPLETED")
    print("=" * 80)

if __name__ == "__main__":
    run_pattern_scanner()
