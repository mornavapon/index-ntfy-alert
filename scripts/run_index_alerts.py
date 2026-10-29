
#!/usr/bin/env python3
"""Index alerts using Yahoo Finance daily closes and ntfy."""

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
    "^HSI": "Hang Seng Index",
}

HISTORY_START_YEARS = {
    "^GSPC": 1970,
    "^IXIC": 1970,
    "^DJI": 1985,
    "^HSI": 1986,
}

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 IndexAlert/2.2"}
STATE_PATH = Path("data/alert_state.json")
CHUNK_YEARS = 5
THRESHOLDS = (10, 20, 30, 40)
RECENT_DAYS = 5 * 366


def now_utc():
    return datetime.now(timezone.utc)


def fetch_chunk(session, symbol, start, end):
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
    quote = (
        (result.get("indicators") or {}).get("quote") or [{}]
    )[0]
    closes = quote.get("close") or []

    rows = []
    for timestamp, close in zip(timestamps, closes):
        if isinstance(close, (int, float)) and close > 0:
            day = datetime.fromtimestamp(
                timestamp, timezone.utc
            ).date().isoformat()
            rows.append((day, float(close)))

    return rows


def fetch_recent_history(session, symbol):
    end = now_utc()
    start = datetime.fromtimestamp(
        end.timestamp() - RECENT_DAYS * 86400,
        timezone.utc,
    )

    rows = sorted(dict(fetch_chunk(session, symbol, start, end)).items())

    if len(rows) < 253:
        raise RuntimeError(
            f"Too few recent daily closes for {symbol}: {len(rows)}"
        )

    latest_day = datetime.fromisoformat(rows[-1][0]).date()
    age_days = (now_utc().date() - latest_day).days

    if age_days > 7:
        raise RuntimeError(
            f"Latest close is stale for {symbol}: {latest_day}"
        )

    return rows


def fetch_historical_data(session, symbol, start_year):
    """Skip HTTP 400 ranges only before the first usable data."""
    current_year = now_utc().year
    year = start_year
    prices = {}
    found_data = False

    while year <= current_year:
        next_year = min(year + CHUNK_YEARS, current_year + 1)
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(next_year, 1, 1, tzinfo=timezone.utc)

        try:
            chunk = fetch_chunk(session, symbol, start, end)
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else None
            )

            if status == 400 and not found_data:
                print(
                    f"INFO | {symbol} | Yahoo HTTP 400 for "
                    f"{year}-{next_year - 1}; trying next range."
                )
                year = next_year
                time.sleep(0.3)
                continue

            raise

        if chunk:
            found_data = True
            prices.update(chunk)
        elif found_data:
            raise RuntimeError(
                f"Missing historical data for {symbol}: "
                f"{year}-{next_year - 1}. "
                "Stopping to avoid an unreliable historical maximum."
            )
        else:
            print(
                f"INFO | {symbol} | No data for "
                f"{year}-{next_year - 1}; trying next range."
            )

        year = next_year
        time.sleep(0.3)

    rows = sorted(prices.items())

    if len(rows) < 253:
        raise RuntimeError(
            f"Not enough historical closes for {symbol}: {len(rows)}"
        )

    return rows


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
    temp_path = STATE_PATH.with_suffix(".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temp_path.replace(STATE_PATH)


def send_ntfy(session, topic, token, title, message, priority="default"):
    headers = {
        "Title": title,
        "Tags": "chart_with_downwards_trend",
        "Priority": priority,
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


def threshold_flags(drawdown):
    return {
        str(level): drawdown <= -level
        for level in THRESHOLDS
    }


def process_index(session, topic, token, state, symbol, name):
    start_year = HISTORY_START_YEARS[symbol]
    saved = state["indices"].get(symbol, {})

    needs_baseline = (
        not saved.get("initialized", False)
        or saved.get("ath_start_year") != start_year
    )

    if needs_baseline:
        print(
            f"INFO | {name} | Building historical baseline "
            f"from {start_year}..."
        )
        rows = fetch_historical_data(session, symbol, start_year)
        ath_date, ath_close = max(rows, key=lambda item: item[1])
        is_baseline_run = True
    else:
        rows = fetch_recent_history(session, symbol)
        old_ath = float(saved["ath_close"])
        ath_close = max(old_ath, max(close for _, close in rows))
        ath_date = saved.get("ath_date", "unknown")

        if ath_close > old_ath:
            ath_date = max(rows, key=lambda item: item[1])[0]

        is_baseline_run = False

    latest_date, latest_close = rows[-1]

    previous_window = rows[-253:-1]
    if len(previous_window) < 252:
        raise RuntimeError(f"Insufficient 52-week history for {symbol}")

    high_52w_date, high_52w = max(
        previous_window, key=lambda item: item[1]
    )

    dd_ath = (latest_close / ath_close - 1) * 100
    dd_52w = (latest_close / high_52w - 1) * 100

    print(
        f"PASS | {name} | close date={latest_date} "
        f"| close={latest_close:.2f} "
        f"| highest close since {start_year}="
        f"{ath_close:.2f} ({ath_date}) "
        f"| previous 52-week high={high_52w:.2f} "
        f"({high_52w_date}) "
        f"| drawdown={dd_ath:.2f}% / {dd_52w:.2f}%"
    )

    new_ath_flags = threshold_flags(dd_ath)
    new_52w_flags = threshold_flags(dd_52w)
    previous_date = saved.get("last_date")

    if not is_baseline_run and latest_date != previous_date:
        old_ath_flags = saved.get("ath_alerts", {})
        old_52w_flags = saved.get("52w_alerts", {})

        for level in THRESHOLDS:
            key = str(level)

            if (
                new_ath_flags[key]
                and not old_ath_flags.get(key, False)
            ):
                send_ntfy(
                    session,
                    topic,
                    token,
                    "Index Alert",
                    f"{name}: ราคาปิดลดลง {dd_ath:.2f}% "
                    f"จากจุดสูงสุดที่พบตั้งแต่ {start_year} "
                    f"({ath_close:.2f})\n"
                    f"วันที่ข้อมูล: {latest_date}\n"
                    f"ถึงระดับแจ้งเตือน -{level}%",
                    priority="high",
                )

            if (
                new_52w_flags[key]
                and not old_52w_flags.get(key, False)
            ):
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

    state["indices"][symbol] = {
        "initialized": True,
        "ath_start_year": start_year,
        "ath_close": ath_close,
        "ath_date": ath_date,
        "last_date": latest_date,
        "last_close": latest_close,
        "ath_alerts": new_ath_flags,
        "52w_alerts": new_52w_flags,
        "updated_utc": now_utc().isoformat(),
    }


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("NTFY_TOKEN", "").strip()

    if not topic:
        raise RuntimeError("Missing GitHub Secret: NTFY_TOPIC")

    state = load_state()
    failures = 0

    print("Index Alert starting")
    print(f"UTC time: {now_utc().isoformat()}")
    print("Historical baseline years:")
    for symbol, name in INDICES.items():
        print(f"  {name}: from {HISTORY_START_YEARS[symbol]}")
    print(
        "Historical maximum means the highest close found since "
        "the configured start year, not a guaranteed all-time high."
    )
    print("-" * 72)

    with requests.Session() as session:
        for symbol, name in INDICES.items():
            try:
                process_index(
                    session, topic, token, state, symbol, name
                )
            except Exception as exc:
                failures += 1
                print(f"FAIL | {name} ({symbol}) | {exc}")

            time.sleep(1)

    save_state(state)
    print("-" * 72)
    print(f"State saved to {STATE_PATH}")

    if failures:
        sys.exit(
            f"{failures} index check(s) failed. "
            "Review the logs before relying on alerts."
        )

    print("All index checks completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
