# Index Alert (ntfy) — v1

No website: GitHub Actions checks index closing prices and can send alerts to ntfy.

## What it does
- S&P 500 (`^GSPC`), Nasdaq Composite (`^IXIC`), Dow Jones (`^DJI`), Hang Seng (`^HSI`)
- Fetches daily data in 5-year chunks, then calculates the highest daily close in all returned history and rolling 52-week high.
- Alerts at drawdowns of 10%, 20%, 30%, and 40% from both highs.
- Alerts on a new 52-week closing high.
- Saves state in `data/alert_state.json` to avoid repeating threshold alerts each day.

## Setup
1. Create a topic at https://ntfy.sh. Use a long, hard-to-guess name.
2. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**.
3. Add `NTFY_TOPIC` = your topic name. Add `NTFY_TOKEN` only if your ntfy topic requires authentication.
4. Replace `.github/workflows/test-yahoo-data.yml` with the workflow in this ZIP.
5. Add `scripts/run_index_alerts.py` and `data/alert_state.json` at those exact paths.
6. Repo → **Settings → Actions → General → Workflow permissions** → choose **Read and write permissions** → Save.
7. Actions → **Index alerts** → **Run workflow**. Review the logs.

## Important
- Yahoo Finance is unofficial and can change availability, symbols, and history.
- The script stops without saving state or sending alerts if any index fails, to avoid acting on incomplete data.
- First successful run initializes the alert state and does not send historic drawdown alerts. It sends a setup message only if `NTFY_TOPIC` is configured.
- "ATH" means the highest daily close in the full history successfully returned by Yahoo from the configured start date (1970-01-01); it is not a guarantee of the exchange's official all-time record.
- A public ntfy topic is not private. Keep the topic name secret or configure ntfy authentication.
- Scheduled GitHub Actions may start late. Runs at 01:40 UTC (08:40 Thailand), Monday–Saturday.
- Never commit an ntfy token into code.
