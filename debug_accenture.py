#!/usr/bin/env python3
"""
Debug-Script: Prüft, ob Parqet die Accenture-Position noch über API sendet
"""
import json
import sys
sys.path.insert(0, '.')

from sync import fetch_parqet_holdings, load_config

def debug_accenture():
    cfg = load_config()
    access_token = cfg.get("parqet_access_token", "")

    if not access_token:
        print("❌ Kein Access Token in config.json!")
        return

    print("🔍 Frage Parqet API ab...")
    raw_holdings = fetch_parqet_holdings(access_token)

    if not raw_holdings:
        print("❌ Parqet sendet keine Holdings!")
        return

    print(f"✓ {len(raw_holdings)} Holdings von Parqet")

    # Suche nach Accenture
    accenture = None
    for h in raw_holdings:
        if 'ACN' in (h.get('isin', '') or '') or 'Accenture' in (h.get('name', '') or ''):
            accenture = h
            break

    if accenture:
        print("\n✓ ACCENTURE GEFUNDEN in Parqet API:")
        print(f"  ISIN: {accenture.get('isin')}")
        print(f"  Name: {accenture.get('name')}")
        print(f"  Quantity: {accenture.get('quantity')}")
        print(f"  Current Price: {accenture.get('current_price')}")
    else:
        print("\n❌ ACCENTURE NICHT GEFUNDEN in Parqet API")
        print("\n📋 Alle Holdings:")
        for h in raw_holdings[:10]:
            print(f"  - {h.get('name')} ({h.get('isin')}): qty={h.get('quantity')}")

if __name__ == "__main__":
    debug_accenture()
