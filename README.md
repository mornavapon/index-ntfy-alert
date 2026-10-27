# Index Alert — Yahoo Finance Data Probe

This is the first-stage data-access test for a daily index-alert system. It has no web page and does not send ntfy notifications yet.

## What it tests
- S&P 500 (`^GSPC`)
- Nasdaq Composite (`^IXIC`)
- Dow Jones Industrial Average (`^DJI`)
- Hang Seng Index (`^HSI`)

The GitHub Actions run checks the Yahoo Finance chart response, API errors, daily granularity, valid closing prices, and latest available close. Results appear in the Actions log.

## Setup
1. Create a new **Public** GitHub repository, e.g. `index-ntfy-alert`.
2. Upload the contents of this ZIP to the repository root. Preserve the `.github/workflows` folders.
3. Open **Actions** and enable workflows if prompted.
4. Choose **Test Yahoo Finance index data** → **Run workflow**.
5. Open the run and expand **Check index data** to view the report.

It also runs automatically at 01:30 UTC (08:30 Thailand time), Monday–Saturday. GitHub may delay scheduled workflows.

## Important
- This is only a data-access probe; it does not send notifications or calculate thresholds yet.
- Yahoo Finance is an unofficial data source and can change access or symbols.
- The later ATH calculation should use the longest available daily history, not just one year.
- Never put ntfy tokens in source files. Add them later through **Settings → Secrets and variables → Actions**.
