
#!/usr/bin/env python3

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 IndexAlertDataProbe/1.0"
}


def check_symbol(session, symbol, name):
    url = BASE_URL.format(requests.utils.quote(symbol, safe=""))

    try:
        response = session.get(
            url,
            params={
                "range": "5y",
                "interval": "1d",
            },
            headers=HEADERS,
            timeout=25,
        )

        response.raise_for_status()
        payload = response.json()

    except Exception as exc:
        return False, (
            f"FAIL | {name} ({symbol}) | "
            f"Request error: {exc}"
        )

    chart = payload.get("chart") or {}

    if chart.get("error"):
        return False, (
            f"FAIL | {name} ({symbol}) | "
            f"Yahoo error: {chart['error']}"
        )

    results = chart.get("result") or []

    if not results:
        return False, (
            f"FAIL | {name} ({symbol}) | No data returned"
        )

    result = results[0]
    meta = result.get("meta") or {}

    timestamps = result.get("timestamp") or []
    quote_data = (
        (result.get("indicators") or {}).get("quote") or [{}]
    )
    closes = quote_data[0].get("close") or []

    valid = [
        (ts, close)
        for ts, close in zip(timestamps, closes)
        if isinstance(close, (int, float)) and close > 0
    ]

    granularity = meta.get("dataGranularity")

    if granularity != "1d":
        return False, (
            f"FAIL | {name} ({symbol}) | "
            f"Expected daily data, got {granularity!r}"
        )

    if len(valid) < 100:
        return False, (
            f"FAIL | {name} ({symbol}) | "
            f"Too few daily closing prices: {len(valid)}"
        )

    first_ts, _ = valid[0]
    last_ts, last_close = valid[-1]

    first_date = datetime.fromtimestamp(
        first_ts, timezone.utc
    ).date()

    last_date = datetime.fromtimestamp(
        last_ts, timezone.utc
    ).date()

    age_days = (
        datetime.now(timezone.utc).date() - last_date
    ).days

    if age_days > 7:
        return False, (
            f"FAIL | {name} ({symbol}) | "
            f"Latest close is stale: {last_date}, "
            f"{age_days} days ago"
        )

    max_close = max(close for _, close in valid)

    return True, (
        f"PASS | {name} ({symbol}) | "
        f"{len(valid)} daily closes | "
        f"{first_date} to {last_date} | "
        f"Latest close={last_close:.2f} | "
        f"5-year max close={max_close:.2f}"
    )


def main():
    print("Yahoo Finance daily data test")
    print(f"Started UTC: {datetime.now(timezone.utc).isoformat()}")
    print("Requested history: 5 years, interval=1d")
    print("-" * 80)

    passed = 0

    with requests.Session() as session:
        for symbol, name in INDICES.items():
            ok, message = check_symbol(session, symbol, name)
            print(message, flush=True)

            if ok:
                passed += 1

            time.sleep(1)

    print("-" * 80)
    print(f"Result: {passed}/{len(INDICES)} indices passed.")

    if passed != len(INDICES):
        sys.exit(1)

    print("All four indices passed the daily data test.")


if __name__ == "__main__":
    main()
