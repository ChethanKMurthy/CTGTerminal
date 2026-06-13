#!/usr/bin/env python3
"""Ping the running dashboard and print a compact health summary.

Usage:  python scripts/healthcheck.py [host:port]   (default 127.0.0.1:8799)
Exits non-zero if the service is unreachable.
"""
import sys
import urllib.request
import json

addr = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8799"
try:
    with urllib.request.urlopen(f"http://{addr}/api/health", timeout=10) as r:
        h = json.load(r)
except Exception as exc:  # noqa: BLE001
    print(f"DOWN — {exc}")
    sys.exit(1)

caps = h.get("capabilities", {})
on = [k for k, v in caps.items() if v] or ["data-only"]
print(f"UP  v{h.get('version', '?')}  capabilities: {', '.join(on)}")
for t, n in (h.get("row_counts") or {}).items():
    print(f"  {t:<16} {n:>8}")
