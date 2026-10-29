#!/usr/bin/env python3
"""Daily closing-price index alerts via ntfy."""
import json
import os
import sys
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "alert_state.json"
BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IndexAlert/1.0)"}
HISTORY_START = date(1970, 1, 1)
THRESHOLDS = (10, 20, 30, 40)
INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^DJI": "Dow Jones Industrial Average",
    "^HSI": "Hang Seng Index",
}


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "initialized": False, "indices": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_daily_closes(session, symbol):
    """Fetch history in 5-year chunks so Yahoo is less likely to downgrade interval."""
    url = BASE_URL.format(requests.utils.quote(symbol, safe=""))
    today = datetime.now(timezone.utc).date()
    cursor = HISTORY_START
    all_points = {}
    chunks = 0

    while cursor < today:
        end_year = min(cursor.year + 5, today.year)
        # Handle leap-day dates safely by stepping to the first of the same month.
        try:
            end = date(end_year, cursor.month, cursor.day)
        except ValueError:
            end = date(end_year, cursor.month, 28)
        if end > today:
            end = today
        if end <= cursor:
            end = today

        params = {
            "period1": int(datetime(cursor.year, cursor.month, cursor.day, tzinfo=timezone.utc).timestamp()),
            "period2": int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()),
            "interval": "1d",
        }
        response = session.get(url, params=params, headers=HEADERS, timeout=30)
        response.raise_for_status()
        payload = response.json()
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise RuntimeError(f"Yahoo error: {chart['error']}")
        results = chart.get("result") or []
        if results:
            result = results[0]
            meta = result.get("meta") or {}
            if meta.get("dataGranularity") != "1d":
                raise RuntimeError(
                    f"expected daily data in {cursor} to {end}, got {meta.get('dataGranularity')!r}"
                )
            timestamps = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            for ts, close in zip(timestamps, closes):
                if isinstance(close, (int, float)) and close > 0:
                    day = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                    all_points[day] = float(close)
        cursor = end
        chunks += 1
        time.sleep(0.25)

    if not all_points:
        raise RuntimeError("no daily closes returned")
    points = sorted(all_points.items())
    latest_day = date.fromisoformat(points[-1][0])
    age = (today - latest_day).days
    if age > 7:
        raise RuntimeError(f"latest close {latest_day} is stale by {age} days")
    return points, chunks


def send_ntfy(session, title, message):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if not topic:
        print("NTFY_TOPIC is not set; notification skipped.")
        return False
    headers = {"Title": title, "Priority": "default", "Tags": "chart_with_upwards_trend"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = session.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                            headers=headers, timeout=20)
    response.raise_for_status()
    print(f"ntfy notification sent: {title}")
    return True


def main():
    state = load_state()
    old_indices = state.get("indices", {})
    first_run = not state.get("initialized", False)
    new_indices, messages, errors = {}, [], []

    with requests.Session() as session:
        for symbol, name in INDICES.items():
            try:
                points, chunks = fetch_daily_closes(session, symbol)
                dates = [d for d, _ in points]
                values = [v for _, v in points]
                latest_date, latest_close = points[-1]
                ath = max(values)
                ath_date = dates[values.index(ath)]
                cutoff = date.fromisoformat(latest_date) - timedelta(days=365)
                rolling = [(d, v) for d, v in points if date.fromisoformat(d) >= cutoff]
                high52 = max(v for _, v in rolling)
                high52_date = next(d for d, v in rolling if v == high52)
                dd_ath = (latest_close / ath - 1) * 100
                dd_52 = (latest_close / high52 - 1) * 100

                prev = old_indices.get(symbol, {})
                sent = prev.get("sent", {"ath": [], "52w": []})
                sent = {"ath": list(sent.get("ath", [])), "52w": list(sent.get("52w", []))}
                if not first_run:
                    for kind, drawdown, peak in (("ath", dd_ath, ath), ("52w", dd_52, high52)):
                        for threshold in THRESHOLDS:
                            key = str(threshold)
                            if drawdown <= -threshold and key not in sent[kind]:
                                messages.append(
                                    f"{name}: close {latest_close:,.2f} on {latest_date} is "
                                    f"{drawdown:.1f}% below {('all-time high' if kind == 'ath' else '52-week high')} "
                                    f"{peak:,.2f}. Threshold -{threshold}%."
                                )
                                sent[kind].append(key)
                            elif drawdown > -threshold and key in sent[kind]:
                                sent[kind].remove(key)
                    old_high = prev.get("high52_value")
                    if old_high is not None and high52 > float(old_high) and latest_close >= high52:
                        messages.append(f"{name}: new 52-week closing high {high52:,.2f} on {latest_date}.")

                new_indices[symbol] = {
                    "name": name, "latest_date": latest_date, "latest_close": latest_close,
                    "ath_value": ath, "ath_date": ath_date,
                    "high52_value": high52, "high52_date": high52_date,
                    "drawdown_ath_pct": round(dd_ath, 4), "drawdown_52w_pct": round(dd_52, 4),
                    "sent": sent,
                }
                print(
                    f"OK | {name} ({symbol}) | closes={len(points)} | chunks={chunks} | "
                    f"latest={latest_date} {latest_close:.2f} | ATH={ath:.2f} ({ath_date}) "
                    f"| from ATH={dd_ath:.2f}% | 52w high={high52:.2f} ({high52_date}) "
                    f"| from 52w high={dd_52:.2f}%"
                )
            except Exception as exc:
                errors.append(f"{name} ({symbol}): {exc}")
                print(f"FAIL | {name} ({symbol}) | {exc}", flush=True)
            time.sleep(0.5)

        if errors:
            print("One or more index checks failed; refusing to save partial state or send alerts.")
            for error in errors:
                print("ERROR:", error)
            sys.exit(1)

        if first_run:
            send_ntfy(session, "Index Alert initialized",
                      "Index alert system initialized. No historical threshold alerts were sent on the first run.")
        else:
            for message in messages:
                send_ntfy(session, "Index alert", message)

    state["version"] = 1
    state["initialized"] = True
    state["indices"] = new_indices
    save_state(state)
    print("Saved alert state to data/alert_state.json")
    if not messages:
        print("No new alert conditions today.")


if __name__ == "__main__":
    main()
