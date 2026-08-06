# GA4 Reporting Automation (Python + GA4 Data API + GitHub Actions)

[![CI](https://github.com/damondrc/ga4-reporting-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/damondrc/ga4-reporting-automation/actions/workflows/ci.yml)

Automated traffic reporting for a GA4 property **I instrumented myself**: a Python
pipeline queries the [GA4 Data API](https://developers.google.com/analytics/devguides/reporting/data/v1),
builds a self-contained HTML report with charts, and a **GitHub Action re-runs it
every Monday** and commits the fresh report — zero manual steps.

> 📈 **Latest report (live):** **<https://damondrc.github.io/ga4-reporting-automation/>**
> — regenerated and republished every Monday by [this workflow](.github/workflows/weekly-report.yml).
> Source: [`report/report.html`](report/report.html) · raw extracts in [`report/data/`](report/data/).
> 🔌 **Published data contract:** [`data/public/`](data/public/) — stable CSVs over
> HTTPS for dashboards (Tableau, Power BI). See its [contract](data/public/README.md).

## Part of a three-repo system

Each repository covers one stage of the same pipeline. What they share on
purpose is the **GA4 event schema**.

| Stage | Repository | Dataset |
|---|---|---|
| **Collection** | [portfolio-analytics](https://github.com/damondrc/portfolio-analytics) — GTM + GA4 implementation, measurement plan, consent, tracking QA | Real · live · low volume |
| **Analysis** | [ecommerce-funnel-analysis](https://github.com/damondrc/ecommerce-funnel-analysis) — SQL funnel analysis and Tableau dashboard | Simulated · 80k events |
| **Activation** | **this repo** — GA4 Data API → automated weekly report | Real · live |

```
                        GA4 event schema
              ┌───────────────┴───────────────┐
      real · live · small              simulated · at scale
     instrumented demo site            80k generated events
              │                                │
      Data API ─▶ weekly report        SQL ─▶ Tableau dashboard
              ▲
          this repo
```

**Why two datasets.** A demo site cannot produce the volume an analysis needs
for its findings to mean anything, and a simulated dataset cannot prove that an
implementation works. So the live property carries the pipeline end to end, and
the simulated one carries the analysis. Both speak the same event schema, which
is what makes the split deliberate rather than convenient.

This repo queries the **real** property — the same one instrumented in the
collection repo. The numbers are small because it is a demo site; the point is
a pipeline that runs unattended and reports on what the instrumentation
actually collects. That is also how two collection defects were found
([Bug #6 and Bug #7](https://github.com/damondrc/portfolio-analytics/blob/main/Proyecto-3/README.md)):
the reporting layer surfaced them from the data, not the container.

**The contract between repos.** Event names are **append-only** upstream —
never renamed or reused — so the queries here only ever need additive changes
and historical series stay comparable.

## What the report includes

Daily sessions/users/pageviews trend, acquisition by source/medium, device breakdown
with engagement rate, top events, and the **signup funnel** — over a configurable
lookback window (default 28 days).

The funnel is the part a default GA4 property could not produce: sessions, channels and
devices exist everywhere without configuring anything, whereas
`view_item_list → select_item → begin_checkout → sign_up → purchase` is the
instrumentation designed in project 1. It is rendered in **journey order, never sorted by
volume** — a bar chart sorted by count reads like a funnel but isn't one — with the
step-to-step drop-off. Counts are events, not distinct users progressing; the report says
so next to the chart.

## How it works

```
GA4 property ──▶ GA4 Data API ──▶ ga4_report.py ──▶ report/report.html (+ CSV extracts)
                                        ▲
                    GitHub Actions (cron, weekly) + repo secrets
```

- **`ga4_report.py`** — 5 API reports → pandas → matplotlib charts embedded as base64
  into a single portable HTML file.
- **`--demo` mode** — generates sample data with the exact API response shape, so the
  pipeline can be tested with zero credentials: `python ga4_report.py --demo`
- **`data/public/`** — a **published data contract**: stable snake_case columns,
  append-only, raw values, versioned. Internal extracts (`report/data/`) are free
  to change with the queries; this layer is not, because dashboards outside this
  repo read from it and a renamed column breaks them silently.
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
