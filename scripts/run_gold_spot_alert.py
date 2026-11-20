#!/usr/bin/env python3
"""Gold Spot (XAU/USD) alerts using Gold API live spot price."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api.gold-api.com/price/XAU"
STATE_PATH = Path("data/gold_spot_history.json")
TRADINGVIEW_URL = "https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD"
THRESHOLDS = (5, 10, 20, 30, 40)
MAX_PRICE_AGE_HOURS = 36
MAX_HISTORY_DAYS = 2200  # Keep enough data for long-term high + 52-week calculations.


def utc_now():
    return datetime.now(timezone.utc)


def fetch_spot(session):
    response = session.get(
        API_URL,
        headers={"User-Agent": "Mozilla/5.0 IndexAlert-GoldSpot/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("symbol") != "XAU" or payload.get("currency") != "USD":
        raise RuntimeError(f"Unexpected Gold API response: {payload}")

    price = payload.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        raise RuntimeError(f"Invalid Gold Spot price: {price!r}")

    updated_text = payload.get("updatedAt")
    if not updated_text:
        raise RuntimeError("Gold API response is missing updatedAt.")

    updated_at = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    age_hours = (utc_now() - updated_at.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours < -1 or age_hours > MAX_PRICE_AGE_HOURS:
        raise RuntimeError(
            f"Gold Spot quote is stale or has an invalid timestamp: "
            f"{updated_text} (age {age_hours:.1f} hours)"
        )

    return float(price), updated_at.astimezone(timezone.utc)


def load_data():
    if not STATE_PATH.exists():
        return {"history": [], "state": {}}

    with STATE_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError("Invalid Gold Spot history file.")
    data.setdefault("history", [])
    data.setdefault("state", {})
    return data


def save_data(data):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(STATE_PATH)


def send_ntfy(session, topic, token, title, message, priority="default"):
    headers = {
        "Title": title,
        "Tags": "chart_with_downwards_trend",
        "Priority": priority,
        "Actions": f"view, Open Gold Spot on TradingView, {TRADINGVIEW_URL}",
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


def flags(drawdown):
    return {str(level): drawdown <= -level for level in THRESHOLDS}


def process_gold(session, topic, token, data):
    price, updated_at = fetch_spot(session)
    day = updated_at.date().isoformat()
    history = data["history"]
    state = data["state"]

    # One observation per UTC date. If Actions is manually rerun, do not
    # generate duplicate alerts or overwrite the day's first observation.
    existing = next((row for row in history if row.get("date") == day), None)
    if existing:
        print(
            f"INFO | Gold Spot | already recorded for {day}; "
            f"saved price={float(existing['close']):.2f} USD/oz"
        )
        return

    if history:
        history.sort(key=lambda row: row["date"])
        previous_prices = [float(row["close"]) for row in history]
        tracking_high = max(previous_prices)
        tracking_high_date = max(history, key=lambda row: float(row["close"]))["date"]
        previous_ath_flags = state.get("tracking_high_alerts", {})
        previous_date = history[-1]["date"]
    else:
        previous_prices = []
        tracking_high = price
        tracking_high_date = day
        previous_ath_flags = {}
        previous_date = None

    row = {
        "date": day,
        "close": round(price, 6),
        "updated_at": updated_at.isoformat(),
    }
    history.append(row)
    history.sort(key=lambda item: item["date"])
    history[:] = history[-MAX_HISTORY_DAYS:]

    is_baseline = previous_date is None
    is_new_day = day != previous_date
    new_tracking_high = max(tracking_high, price)
    if price > tracking_high:
        tracking_high_date = day

    dd_tracking_high = (price / new_tracking_high - 1) * 100
    new_tracking_flags = flags(dd_tracking_high)

    print(
        f"PASS | Gold Spot XAU/USD | {price:.2f} USD/troy oz "
        f"| quote={updated_at.isoformat()} | "
        f"observations={len(history)} | "
        f"highest since tracking began={new_tracking_high:.2f} "
        f"({tracking_high_date}) | drawdown={dd_tracking_high:.2f}%"
    )

    if not is_baseline and is_new_day:
        for level in THRESHOLDS:
            key = str(level)
            if new_tracking_flags[key] and not previous_ath_flags.get(key, False):
                send_ntfy(
                    session,
                    topic,
                    token,
                    "Gold Spot Alert",
                    f"Gold Spot (XAU/USD): ราคาลดลง {dd_tracking_high:.2f}% "
                    f"จากจุดสูงสุดตั้งแต่เริ่มติดตาม ({new_tracking_high:.2f} USD/oz)\n"
                    f"ราคาล่าสุด: {price:.2f} USD/troy oz\n"
                    f"เวลาอัปเดตข้อมูล: {updated_at.isoformat()}\n"
                    f"เพิ่งข้ามระดับแจ้งเตือน -{level}%",
                    priority="high",
                )

    # 52-week calculations become valid after 252 prior daily observations.
    if len(history) >= 253:
        prior_window = history[-253:-1]
        prior_high_row = max(prior_window, key=lambda item: float(item["close"]))
        prior_low_row = min(prior_window, key=lambda item: float(item["close"]))
        high_52w = float(prior_high_row["close"])
        low_52w = float(prior_low_row["close"])
        dd_52w = (price / high_52w - 1) * 100
        new_52w_flags = flags(dd_52w)
        old_52w_flags = state.get("52w_alerts", {})

        print(
            f"INFO | Gold Spot | 52-week high={high_52w:.2f} "
            f"({prior_high_row['date']}) | low={low_52w:.2f} "
            f"({prior_low_row['date']}) | drawdown={dd_52w:.2f}%"
        )

        if not is_baseline and is_new_day:
            for level in THRESHOLDS:
                key = str(level)
                if new_52w_flags[key] and not old_52w_flags.get(key, False):
                    send_ntfy(
                        session,
                        topic,
                        token,
                        "Gold Spot Alert",
                        f"Gold Spot (XAU/USD): ราคาลดลง {dd_52w:.2f}% "
                        f"จากจุดสูงสุด 52 สัปดาห์ ({high_52w:.2f} USD/oz)\n"
                        f"ราคาล่าสุด: {price:.2f} USD/troy oz\n"
                        f"เวลาอัปเดตข้อมูล: {updated_at.isoformat()}\n"
                        f"เพิ่งข้ามระดับแจ้งเตือน -{level}%",
                        priority="high",
                    )

            if price > high_52w:
                send_ntfy(
                    session, topic, token, "Gold Spot Alert",
                    f"Gold Spot (XAU/USD): ทำจุดสูงสุดใหม่ในรอบ 52 สัปดาห์\n"
                    f"ราคา: {price:.2f} USD/troy oz\n"
                    f"เวลาอัปเดตข้อมูล: {updated_at.isoformat()}",
                )

            if price < low_52w:
                send_ntfy(
                    session, topic, token, "Gold Spot Alert",
                    f"Gold Spot (XAU/USD): ทำจุดต่ำสุดใหม่ในรอบ 52 สัปดาห์\n"
                    f"ราคา: {price:.2f} USD/troy oz\n"
                    f"ต่ำกว่าจุดต่ำสุดเดิม {low_52w:.2f} USD/oz "
                    f"({prior_low_row['date']})\n"
                    f"เวลาอัปเดตข้อมูล: {updated_at.isoformat()}",
                    priority="high",
                )

        state["52w_alerts"] = new_52w_flags
    else:
        print(
            f"INFO | Gold Spot | collecting daily history "
            f"({len(history)}/253 observations); 52-week alerts not active yet."
        )
        state["52w_alerts"] = {}

    state.update({
        "tracking_high": new_tracking_high,
        "tracking_high_date": tracking_high_date,
        "tracking_high_alerts": new_tracking_flags,
        "last_date": day,
        "last_price": price,
        "updated_utc": utc_now().isoformat(),
    })


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if not topic:
        raise RuntimeError("Missing GitHub Secret: NTFY_TOPIC")

    data = load_data()
    with requests.Session() as session:
        process_gold(session, topic, token, data)
    save_data(data)
    print(f"Gold Spot history saved to {STATE_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL GOLD SPOT ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
