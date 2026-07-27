"""
GA4 Reporting Automation
========================
Pulls real data from a Google Analytics 4 property via the GA4 Data API
and generates a self-contained HTML report (charts embedded as base64).

This closes the loop of my portfolio: the GA4 property being queried here
is the one I instrumented myself with GTM (see: portfolio-analytics).

Usage:
    # Real data (requires service account credentials + property ID):
    export GOOGLE_APPLICATION_CREDENTIALS=path/to/sa-key.json
    export GA4_PROPERTY_ID=123456789
    python ga4_report.py

    # Demo mode (no credentials needed, generates sample data):
    python ga4_report.py --demo

Options:
    --days N          Lookback window in days (default: 28)
    --property-id ID  GA4 property ID (overrides GA4_PROPERTY_ID env var)
    --demo            Run with generated sample data instead of the API
    --output PATH     Output HTML path (default: report/report.html)

Auth setup: see docs/setup_google_cloud.md
"""

import argparse
import base64
import io
import os
import sys
from datetime import date, datetime, timedelta

import matplotlib
matplotlib.use("Agg")  # headless backend (works in CI)
import matplotlib.pyplot as plt
import pandas as pd

# ------------------------------------------------------------------ filters --
# Bug #6 (portfolio-analytics/Proyecto-3): a GA4 Event tag was left with its
# Event Name field holding the tag's own name, so every page load emitted an
# event literally called "Tag - GA4 Config". In the report published on
# 2026-07-20 it ranked second by volume, above scroll_50.
#
# The tag was disabled in the container on 2026-07-26 (GTM v2.2), but GA4
# cannot delete events it has already collected and its data filters are not
# retroactive. With a rolling 28-day window the phantom keeps surfacing until
# it falls out of range.
#
# THIS EXCLUSION IS TEMPORARY — delete it (and PHANTOM_EVENT, and the third
# element of the top_events query) on or after PHANTOM_EXPIRES.
PHANTOM_EVENT = "Tag - GA4 Config"
PHANTOM_EXPIRES = date(2026, 8, 17)


def exclude_phantom():
    """Server-side exclusion of the phantom event.

    Filtered in the query rather than dropped from the DataFrame afterwards:
    a `df[df.eventName != ...]` buried in the transform layer reads as a
    patch, whereas a named FilterExpression with this comment attached reads
    as a documented decision — and it is the correct use of the API.

    Built lazily so the google-analytics-data types are only imported on the
    live path; --demo must keep working without them.
    """
    from google.analytics.data_v1beta.types import Filter, FilterExpression

    return FilterExpression(
        not_expression=FilterExpression(
            filter=Filter(
                field_name="eventName",
                string_filter=Filter.StringFilter(value=PHANTOM_EVENT),
            )
        )
    )


# ----------------------------------------------------------------- queries --
# Each report we pull from the API:
#   name -> (dimensions, metrics, dimension_filter factory or None)
QUERIES = {
    "daily_overview": (
        ["date"],
        ["sessions", "totalUsers", "screenPageViews", "eventCount"],
        # Deliberately NOT filtered. Excluding the phantom here would change
        # eventCount retroactively and break comparability with the CSVs
        # already committed to this repo.
        None,
    ),
    "by_channel": (
        ["sessionSource", "sessionMedium"],
        ["sessions", "totalUsers"],
        None,
    ),
    "by_device": (
        ["deviceCategory"],
        ["sessions", "engagementRate"],
        None,
    ),
    "top_events": (
        ["eventName"],
        ["eventCount"],
        exclude_phantom,
    ),
}

ACCENT = "#4c78a8"

# Max rows requested per report. See the truncation check in fetch_report().
LIMIT = 1000


# --------------------------------------------------------------- extraction --
def report_window(days: int):
    """The date window every query in a run shares.

    Explicit dates instead of the API's relative "28daysAgo"/"yesterday"
    strings: the server resolves those, so the caller never knows which range
    it actually asked for, and the reindexing below has no range to fill.
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start, end


def fill_missing_days(df: pd.DataFrame, start, end, metrics) -> pd.DataFrame:
    """GA4 returns no row at all for a day with no activity.

    Left as-is, matplotlib draws a straight line between the two rows that
    surround the gap, which reads as gently declining traffic across days that
    actually had none — and the x-axis stops being a time scale, since eleven
    silent days occupy the same width as one. Reindexing over the full window
    makes the zeros explicit and the axis honest.
    """
    full = pd.date_range(start, end)
    if df.empty:
        return pd.DataFrame({"date": full, **{m: 0 for m in metrics}})
    df = df.set_index("date").reindex(full, fill_value=0)
    return df.rename_axis("date").reset_index()


def fetch_report(client, property_id: str, start, end, dims, mets,
                 dim_filter=None, name: str = "query") -> pd.DataFrame:
    """Run one GA4 Data API report and return it as a DataFrame."""
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start.isoformat(),
                               end_date=end.isoformat())],
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in mets],
        dimension_filter=dim_filter() if dim_filter else None,
        limit=LIMIT,
    )
    response = client.run_report(request)

    # A truncated result set is indistinguishable from a complete one: the
    # report would render with incomplete numbers and no sign anything is
    # missing. Pagination would be unnecessary complexity at this volume — a
    # visible warning is the proportionate answer to a silent failure.
    if response.row_count > len(response.rows):
        print(f"  WARNING  {name}: API reports {response.row_count} rows, only "
              f"{len(response.rows)} fetched (limit={LIMIT}). Results are "
              f"truncated — implement pagination if this persists.")

    rows = []
    for row in response.rows:
        record = {d: v.value for d, v in zip(dims, row.dimension_values)}
        record.update({m: float(v.value) for m, v in zip(mets, row.metric_values)})
        rows.append(record)
    df = pd.DataFrame(rows, columns=dims + mets)
    if "date" in df.columns and not df.empty:
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        df = df.sort_values("date")
    return df


def fetch_all(property_id: str, days: int) -> dict:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    client = BetaAnalyticsDataClient()
    start, end = report_window(days)

    if date.today() >= PHANTOM_EXPIRES:
        print(f"  note: {PHANTOM_EXPIRES} has passed — the '{PHANTOM_EVENT}' "
              f"exclusion is no longer needed and can be removed.")

    data = {}
    for name, (dims, mets, dim_filter) in QUERIES.items():
        print(f"  querying {name} ...")
        data[name] = fetch_report(client, property_id, start, end,
                                  dims, mets, dim_filter, name=name)

    # Only the daily series is reindexed. The other reports are breakdowns,
    # not time series: an absent channel or device is genuinely absent, not a
    # gap to fill.
    data["daily_overview"] = fill_missing_days(
        data["daily_overview"], start, end, QUERIES["daily_overview"][1]
    )
    return data


# -------------------------------------------------------------------- demo --
def demo_data(days: int) -> dict:
    """Sample data with the exact shape the API returns, so the whole
    pipeline can be tested without credentials (e.g. in CI or by reviewers)."""
    import numpy as np

    rng = np.random.default_rng(7)
    start, end = report_window(days)          # same window as the live path
    dates = pd.date_range(start, end)
    sessions = rng.integers(4, 28, days)
    daily = pd.DataFrame({
        "date": dates,
        "sessions": sessions,
        "totalUsers": (sessions * rng.uniform(0.7, 0.95, days)).astype(int),
        "screenPageViews": (sessions * rng.uniform(1.8, 3.2, days)).astype(int),
        "eventCount": (sessions * rng.uniform(6, 11, days)).astype(int),
    })
    channels = pd.DataFrame({
        "sessionSource": ["google", "(direct)", "linkedin.com", "github.com"],
        "sessionMedium": ["organic", "(none)", "referral", "referral"],
        "sessions": [int(daily.sessions.sum() * s) for s in (0.38, 0.31, 0.19, 0.12)],
        "totalUsers": [int(daily.totalUsers.sum() * s) for s in (0.38, 0.31, 0.19, 0.12)],
    })
    devices = pd.DataFrame({
        "deviceCategory": ["desktop", "mobile", "tablet"],
        "sessions": [int(daily.sessions.sum() * s) for s in (0.55, 0.4, 0.05)],
        "engagementRate": [0.71, 0.54, 0.62],
    })
    # Event names must match the real container. See the event dictionary in
    # portfolio-analytics/docs/MEASUREMENT_PLAN.md §3.1 — sample data that
    # invents names teaches the wrong ones to whoever reads this file first.
    events = pd.DataFrame({
        "eventName": ["page_view", "session_start", "scroll_50",
                      "click_cta", "form_submit", "first_visit"],
        "eventCount": sorted(rng.integers(20, 900, 6).tolist(), reverse=True),
    })
    return {"daily_overview": daily, "by_channel": channels,
            "by_device": devices, "top_events": events}


# ------------------------------------------------------------------ charts --
def fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def make_charts(data: dict) -> dict:
    charts = {}

    daily = data["daily_overview"]
    if not daily.empty:
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.plot(daily["date"], daily["sessions"], color=ACCENT, linewidth=2)
        ax.fill_between(daily["date"], daily["sessions"], alpha=0.15, color=ACCENT)
        ax.set_title("Sessions per day")
        ax.spines[["top", "right"]].set_visible(False)
        charts["daily"] = fig_to_base64(fig)

    ch = data["by_channel"].copy()
    if not ch.empty:
        ch["channel"] = ch["sessionSource"] + " / " + ch["sessionMedium"]
        ch = ch.sort_values("sessions").tail(8)
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.barh(ch["channel"], ch["sessions"], color=ACCENT)
        ax.set_title("Sessions by source / medium")
        ax.spines[["top", "right"]].set_visible(False)
        charts["channel"] = fig_to_base64(fig)

    dev = data["by_device"]
    if not dev.empty:
        fig, ax = plt.subplots(figsize=(5, 3))
        ax.bar(dev["deviceCategory"], dev["sessions"], color=ACCENT)
        ax.set_title("Sessions by device")
        ax.spines[["top", "right"]].set_visible(False)
        charts["device"] = fig_to_base64(fig)

    ev = data["top_events"].sort_values("eventCount").tail(10)
    if not ev.empty:
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.barh(ev["eventName"], ev["eventCount"], color=ACCENT)
        ax.set_title("Top events by count")
        ax.spines[["top", "right"]].set_visible(False)
        charts["events"] = fig_to_base64(fig)

    return charts


# -------------------------------------------------------------------- html --
# GA4 returns rates as fractions (0.7741935483870968). Rendered raw they are
# unreadable; rounded like every other float they become "0.77", which is not
# how anyone reads an engagement rate either.
RATE_COLUMNS = {"engagementRate", "bounceRate", "conversionRate"}


def df_to_html_table(df: pd.DataFrame) -> str:
    """Format for a human reader.

    Presentation layer only — this runs after the CSV extracts are written, so
    the CSVs keep the raw numeric values and stay machine-processable.
    """
    d = df.copy()
    if "date" in d.columns:
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
    for col in d.columns:
        if col in RATE_COLUMNS:
            d[col] = (d[col] * 100).round(1).astype(str) + "%"
        elif pd.api.types.is_float_dtype(d[col]):
            # fetch_report casts every metric with float(), so sessions and
            # eventCount arrive as 65.0 / 281.0. They are counts.
            d[col] = d[col].round(0).astype(int)
    return d.to_html(index=False, border=0, classes="tbl")


def build_html(data: dict, charts: dict, days: int, demo: bool) -> str:
    daily = data["daily_overview"]
    total_sessions = int(daily["sessions"].sum()) if not daily.empty else 0
    total_users = int(daily["totalUsers"].sum()) if not daily.empty else 0
    total_pv = int(daily["screenPageViews"].sum()) if not daily.empty else 0
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    banner = ('<p class="demo">⚠️ DEMO DATA — generated sample, not real '
              'analytics. Run without <code>--demo</code> for live data.</p>'
              if demo else "")

    def img(key):
        return (f'<img src="data:image/png;base64,{charts[key]}" alt="{key}">'
                if key in charts else "<p><em>No data for this period.</em></p>")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>GA4 Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ border-bottom: 3px solid {ACCENT}; padding-bottom: .3rem; }}
  .kpis {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 1.5rem 0; }}
  .kpi {{ background: #f4f6f8; border-radius: 8px; padding: 1rem 1.5rem; }}
  .kpi b {{ font-size: 1.8rem; display: block; }}
  .demo {{ background: #fff3cd; padding: .7rem 1rem; border-radius: 6px; }}
  img {{ max-width: 100%; }}
  .tbl {{ border-collapse: collapse; font-size: .85rem; }}
  .tbl th, .tbl td {{ padding: .35rem .8rem; text-align: left;
                      border-bottom: 1px solid #e3e6e8; }}
  .tbl th {{ background: #f4f6f8; }}
  footer {{ color: #888; font-size: .8rem; margin-top: 2rem; }}
  details {{ margin: .5rem 0 1.5rem; }}
</style></head><body>
<h1>GA4 Traffic Report — last {days} days</h1>
{banner}
<div class="kpis">
  <div class="kpi"><b>{total_sessions:,}</b>Sessions</div>
  <div class="kpi"><b>{total_users:,}</b>Users</div>
  <div class="kpi"><b>{total_pv:,}</b>Page views</div>
</div>
<h2>Daily trend</h2>{img("daily")}
<h2>Acquisition</h2>{img("channel")}
<details><summary>View table</summary>{df_to_html_table(data["by_channel"])}</details>
<h2>Devices</h2>{img("device")}
<details><summary>View table</summary>{df_to_html_table(data["by_device"])}</details>
<h2>Events</h2>{img("events")}
<details><summary>View table</summary>{df_to_html_table(data["top_events"])}</details>
<footer>Generated {generated} · GA4 Data API ·
<a href="https://github.com/damondrc/ga4-reporting-automation">source</a></footer>
</body></html>"""


# -------------------------------------------------------------------- main --
def main():
    p = argparse.ArgumentParser(description="Generate an HTML report from GA4.")
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--property-id", default=os.getenv("GA4_PROPERTY_ID"))
    p.add_argument("--demo", action="store_true")
    p.add_argument("--output", default="report/report.html")
    args = p.parse_args()

    if args.demo:
        print("Running in DEMO mode (sample data).")
        data = demo_data(args.days)
    else:
        if not args.property_id:
            sys.exit("Missing property ID: set GA4_PROPERTY_ID or use --property-id.")
        if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            sys.exit("Missing credentials: set GOOGLE_APPLICATION_CREDENTIALS "
                     "to your service account key path.")
        print(f"Querying GA4 property {args.property_id} "
              f"(last {args.days} days)...")
        data = fetch_all(args.property_id, args.days)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    csv_dir = os.path.join(os.path.dirname(args.output), "data")
    os.makedirs(csv_dir, exist_ok=True)
    for name, df in data.items():
        df.to_csv(os.path.join(csv_dir, f"{name}.csv"), index=False)

    charts = make_charts(data)
    html = build_html(data, charts, args.days, args.demo)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report written to {args.output}")


if __name__ == "__main__":
    main()
