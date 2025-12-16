#!/usr/bin/env python3
"""
Simple script to base64 encode your Google Service Account JSON key.

Usage:
1. Save your JSON key as 'google_key.json' in the same folder as this script
2. Run: python encode_key.py
3. Copy the output and paste it as GOOGLE_SHEETS_CREDS in Railway
"""

import base64
import os

KEY_FILE = "google_key.json"

if not os.path.exists(KEY_FILE):
    print(f"❌ File '{KEY_FILE}' not found!")
    print(f"\nPlease save your Google Service Account JSON key as '{KEY_FILE}'")
    print("in the same folder as this script, then run again.")
    exit(1)

with open(KEY_FILE, "rb") as f:
    content = f.read()

encoded = base64.b64encode(content).decode("utf-8")

print("\n" + "="*60)
print("✅ BASE64 ENCODED KEY (copy everything below):")
print("="*60 + "\n")
print(encoded)
print("\n" + "="*60)
print("\nPaste this as GOOGLE_SHEETS_CREDS in Railway")
print("="*60)
