
#!/usr/bin/env python3
"""Daily index drawdown alerts using Yahoo Finance and ntfy."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^DJI": "Dow Jones",
    "^HSI": "Hang Seng",
}

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 IndexAlert/1.0"}

STATE_PATH = Path("data/alert_state.json")
THRESHOLDS = (10, 20, 30, 40)
HISTORY_START_YEAR = 1950
CHUNK_YEARS = 5
LOOKBACK_DAYS = 5 * 366


def utc_today():
    return datetime.now(timezone.utc).date()


def fetch_chunk(session, symbol, start, end):
    """Fetch daily closing prices for [start, end)."""
    url = BASE_URL.format(requests.utils.quote(symbol, safe=""))

    response = session.get(
        url,
        params={
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1d",
            "events": "history",
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo error: {chart['error']}")

    results = chart.get("result") or []
    if not results:
        return []

    result = results[0]
    granularity = (result.get("meta") or {}).get("dataGranularity")
    if granularity != "1d":
        raise RuntimeError(
            f"Expected daily data for {symbol}; got {granularity!r}"
        )

    timestamps = result.get("timestamp") or []
    quotes = (
        (result.get("indicators") or {}).get("quote") or [{}]
    )
    closes = quotes[0].get("close") or []

    rows = []
    for timestamp, close in zip(timestamps, closes):
        if isinstance(close, (int, float)) and close > 0:
            day = datetime.fromtimestamp(
                timestamp, timezone.utc
            ).date().isoformat()
            rows.append((day, float(close)))

    return rows


def fetch_recent_history(session, symbol):
    """Fetch recent daily closes for rolling 52-week calculations."""
    end = datetime.now(timezone.utc)
    start = end.timestamp() - LOOKBACK_DAYS * 86400
    from datetime import timedelta
    start_dt = datetime.fromtimestamp(start, timezone.utc)

    rows = fetch_chunk(session, symbol, start_dt, end)
    rows = sorted(dict(rows).items())

    if len(rows) < 200:
        raise RuntimeError(
            f"Too few recent daily closes for {symbol}: {len(rows)}"
        )

    age = (utc_today() - datetime.fromisoformat(rows[-1][0]).date()).days
    if age > 7:
        raise RuntimeError(
            f"Latest close for {symbol} is stale: {rows[-1][0]}"
        )

    return rows


def fetch_all_available_history(session, symbol):
    """Initial historical scan in 5-year chunks to estimate the ATH."""
    now = datetime.now(timezone.utc)
    year = HISTORY_START_YEAR
    rows = {}
    found_data = False

    while year <= now.year:
        next_year = min(year + CHUNK_YEARS, now.year + 1)
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(next_year, 1, 1, tzinfo=timezone.utc)

        chunk = fetch_chunk(session, symbol, start, end)

        if chunk:
            found_data = True
            rows.update(chunk)
        elif found_data:
            # Do not silently accept a possible gap in the history.
            raise RuntimeError(
                f"Empty historical chunk for {symbol}: "
                f"{year}-{next_year - 1}. Retry later to verify data."
            )

        year = next_year
        time.sleep(0.25)

    rows = sorted(rows.items())
    if len(rows) < 200:
        raise RuntimeError(
            f"Not enough historical data for {symbol}: {len(rows)} closes"
        )

    return rows


def send_ntfy(session, topic, token, title, message, priority="default"):
    """Send a notification. Keep HTTP headers ASCII-safe."""
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": "chart_with_downwards_trend",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = session.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=25,
    )
    response.raise_for_status()
    print(f"NOTIFIED | {title} | HTTP {response.status_code}")


def load_state():
    if not STATE_PATH.exists():
        return {"indices": {}}

    with STATE_PATH.open("r", encoding="utf-8") as file:
        state = json.load(file)

    if not isinstance(state, dict):
        raise RuntimeError("Invalid alert state file.")
    state.setdefault("indices", {})
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(STATE_PATH)


def threshold_flags(drawdown):
    return {
        str(level): drawdown <= -level
        for level in THRESHOLDS
    }


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("NTFY_TOKEN", "").strip()

    if not topic:
        raise RuntimeError("Missing GitHub Secret: NTFY_TOPIC")

    state = load_state()
    all_ok = True
    notifications_sent = 0

    print("Index Alert starting")
    print(f"UTC time: {datetime.now(timezone.utc).isoformat()}")
    print("Thresholds: -10%, -20%, -30%, -40%")
    print("-" * 72)

    with requests.Session() as session:
        for symbol, name in INDICES.items():
            try:
                saved = state["indices"].get(symbol, {})
                initialized = saved.get("initialized", False)

                # First run: scan the available history to establish an ATH.
                # Later runs: keep the saved ATH and refresh recent data.
                if initialized:
                    rows = fetch_recent_history(session, symbol)
                    previous_ath = float(saved["ath_close"])
                    ath_date = saved.get("ath_date", "unknown")
                    ath_close = max(
                        previous_ath,
                        max(close for _, close in rows),
                    )
                    if ath_close > previous_ath:
                        ath_date = max(
                            rows, key=lambda item: item[1]
                        )[0]
                else:
                    rows = fetch_all_available_history(session, symbol)
                    ath_date, ath_close = max(
                        rows, key=lambda item: item[1]
                    )

                latest_date, latest_close = rows[-1]
                previous_date = saved.get("last_date")

                # Rolling 52-week high: up to 252 previous trading closes.
                recent_closes = [close for _, close in rows]
                prior_window = recent_closes[-253:-1]
                if not prior_window:
                    raise RuntimeError("Insufficient data for 52-week high")

                high_52w = max(prior_window)
                high_52w_date = max(
                    rows[-253:-1], key=lambda item: item[1]
                )[0]

                dd_ath = (latest_close / ath_close - 1) * 100
                dd_52w = (latest_close / high_52w - 1) * 100

                print(
                    f"PASS | {name} | {latest_date} close={latest_close:.2f} "
                    f"| ATH={ath_close:.2f} ({ath_date}) "
                    f"| 52w high={high_52w:.2f} ({high_52w_date}) "
                    f"| ATH drawdown={dd_ath:.2f}% "
                    f"| 52w drawdown={dd_52w:.2f}%"
                )

                old_ath_flags = saved.get("ath_alerts", {})
                old_52w_flags = saved.get("52w_alerts", {})
                new_ath_flags = threshold_flags(dd_ath)
                new_52w_flags = threshold_flags(dd_52w)

                # On first run, establish baseline without historical alerts.
                if initialized and latest_date != previous_date:
                    for level in THRESHOLDS:
                        key = str(level)

                        if new_ath_flags[key] and not old_ath_flags.get(key, False):
                            send_ntfy(
                                session,
                                topic,
                                token,
                                "Index Alert",
                                f"{name}: ราคาปิดลดลง {dd_ath:.2f}% "
                                f"จาก ATH ({ath_close:.2f})\n"
                                f"วันที่ข้อมูล: {latest_date}\n"
                                f"ถึงระดับแจ้งเตือน -{level}%",
                                priority="high",
                            )
                            notifications_sent += 1

                        if new_52w_flags[key] and not old_52w_flags.get(key, False):
                            send_ntfy(
                                session,
                                topic,
                                token,
                                "Index Alert",
                                f"{name}: ราคาปิดลดลง {dd_52w:.2f}% "
                                f"จากจุดสูงสุด 52 สัปดาห์ ({high_52w:.2f})\n"
                                f"วันที่ข้อมูล: {latest_date}\n"
                                f"ถึงระดับแจ้งเตือน -{level}%",
                                priority="high",
                            )
                            notifications_sent += 1

                    # New 52-week high: only compare a newly dated close.
                    if latest_close > high_52w:
                        send_ntfy(
                            session,
                            topic,
                            token,
                            "Index Alert",
                            f"{name}: ทำจุดสูงสุดใหม่ในรอบ 52 สัปดาห์\n"
                            f"ราคาปิด: {latest_close:.2f}\n"
                            f"วันที่ข้อมูล: {latest_date}",
                        )
                        notifications_sent += 1

                # Save current conditions to avoid duplicate alerts.
                # Recovery above a threshold resets it for a future re-alert.
                state["indices"][symbol] = {
                    "initialized": True,
                    "ath_close": ath_close,
                    "ath_date": ath_date,
                    "last_date": latest_date,
                    "last_close": latest_close,
                    "ath_alerts": new_ath_flags,
                    "52w_alerts": new_52w_flags,
                    "last_52w_high": max(
                        latest_close, high_52w
                    ),
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                }

            except Exception as exc:
                all_ok = False
                print(f"FAIL | {name} ({symbol}) | {exc}")

            time.sleep(1)

    save_state(state)

    print("-" * 72)
    print(f"Notifications sent: {notifications_sent}")
    print(f"State saved to {STATE_PATH}")

    if not all_ok:
        sys.exit("One or more indices failed. Check logs before relying on alerts.")

    print("All index checks completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
