# GA4 Reporting Automation (Python + GA4 Data API + GitHub Actions)

Automated traffic reporting for a GA4 property **I instrumented myself**: a Python
pipeline queries the [GA4 Data API](https://developers.google.com/analytics/devguides/reporting/data/v1),
builds a self-contained HTML report with charts, and a **GitHub Action re-runs it
every Monday** and commits the fresh report — zero manual steps.

> 📈 **Latest report:** [`report/report.html`](report/report.html) — updated weekly by
> [this workflow](.github/workflows/weekly-report.yml). Raw extracts in [`report/data/`](report/data/).

## Why this project

This is the third piece of my analytics portfolio, and it closes the loop with **real data**:

1. **[portfolio-analytics](https://damondrc.github.io/portfolio-analytics)** — I implement the tracking (GTM + GA4). *Collection.*
2. **[ecommerce-funnel-analysis](https://github.com/damondrc/ecommerce-funnel-analysis)** — I analyze event data with SQL + Tableau (simulated at scale). *Analysis.*
3. **This repo** — I extract what my own tracking collects, via API, and automate the reporting. *Activation.*

The property queried here is the same one instrumented in project 1, so the numbers are
small (it's a demo site) — the point is the working end-to-end pipeline, not the volume.

## What the report includes

Daily sessions/users/pageviews trend, acquisition by source/medium, device breakdown
with engagement rate, and top events — over a configurable lookback window (default 28 days).

## How it works

```
GA4 property ──▶ GA4 Data API ──▶ ga4_report.py ──▶ report/report.html (+ CSV extracts)
                                        ▲
                    GitHub Actions (cron, weekly) + repo secrets
```

- **`ga4_report.py`** — 4 API reports → pandas → matplotlib charts embedded as base64
  into a single portable HTML file.
- **`--demo` mode** — generates sample data with the exact API response shape, so the
  pipeline can be tested with zero credentials: `python ga4_report.py --demo`
- **`.github/workflows/weekly-report.yml`** — scheduled run; credentials live in GitHub
  Secrets, never in the repo.

## Run it yourself

Full setup guide (Google Cloud, service account, GA4 access, secrets):
[docs/setup_google_cloud.md](docs/setup_google_cloud.md). Quick demo without any setup:

```bash
pip install -r requirements.txt
python ga4_report.py --demo
```

## Security notes

The service account key is a credential and never touches the repo: `.gitignore` blocks
it locally, and in CI it exists only for the duration of the job (written from a secret,
deleted right after). The account has read-only (Viewer) access to a single property.
