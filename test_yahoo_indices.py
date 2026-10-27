#!/usr/bin/env python3
"""Probe Yahoo Finance daily index history from the GitHub Actions runner."""
from datetime import datetime, timezone
import sys
import time
import requests

INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^DJI": "Dow Jones Industrial Average",
    "^HSI": "Hang Seng Index",
}
BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IndexDataProbe/1.0)"}


def check_symbol(session, symbol, name):
    url = BASE_URL.format(requests.utils.quote(symbol, safe=""))
    try:
        response = session.get(
            url, params={"range": "max", "interval": "1d", "events": "history"},
            headers=HEADERS, timeout=25
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return False, f"FAIL | {name} ({symbol}) | request/JSON error: {exc}"

    chart = payload.get("chart") or {}
    if chart.get("error"):
        return False, f"FAIL | {name} ({symbol}) | Yahoo error: {chart['error']}"
    results = chart.get("result") or []
    if not results:
        return False, f"FAIL | {name} ({symbol}) | no chart result"

    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])
    closes = quotes[0].get("close") or []
    valid = [(ts, close) for ts, close in zip(timestamps, closes)
             if isinstance(close, (int, float)) and close > 0]

    if meta.get("dataGranularity") != "1d":
        return False, f"FAIL | {name} ({symbol}) | expected daily data, got {meta.get('dataGranularity')!r}"
    if not valid:
        return False, f"FAIL | {name} ({symbol}) | no valid daily closes"

    first_ts, _ = valid[0]
    last_ts, last_close = valid[-1]
    first_date = datetime.fromtimestamp(first_ts, timezone.utc).date().isoformat()
    last_date_obj = datetime.fromtimestamp(last_ts, timezone.utc).date()
    last_date = last_date_obj.isoformat()
    max_close = max(close for _, close in valid)
    age_days = (datetime.now(timezone.utc).date() - last_date_obj).days
    if age_days > 7:
        return False, (f"FAIL | {name} ({symbol}) | {len(valid)} closes, {first_date} to {last_date}, "
                       f"latest={last_close:.4f}, max={max_close:.4f}; stale by {age_days} days")
    return True, (f"PASS | {name} ({symbol}) | {len(valid)} daily closes, {first_date} to {last_date}, "
                  f"latest={last_close:.4f}, max={max_close:.4f}; Yahoo symbol={meta.get('symbol', 'unknown')}")


def main():
    print("Yahoo Finance chart API — daily index history probe")
    print(f"Started UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("Request parameters: range=max, interval=1d")
    print("-" * 90)
    passed = 0
    with requests.Session() as session:
        for symbol, name in INDICES.items():
            ok, report = check_symbol(session, symbol, name)
            print(report, flush=True)
            passed += int(ok)
            time.sleep(0.4)
    print("-" * 90)
    print(f"Result: {passed}/{len(INDICES)} indices passed.")
    if passed != len(INDICES):
        return 1
    print("All configured index endpoints returned usable daily close history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
