#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
from sync import run_sync

print("\n" + "="*50)
print("STARTE SYNC - DEBUG MODE")
print("="*50 + "\n")

try:
    run_sync()
    print("\n✓ Sync abgeschlossen!")
except Exception as e:
    print(f"\n✗ Fehler beim Sync: {type(e).__name__}")
    print(f"  {e}")
    import traceback
    traceback.print_exc()
