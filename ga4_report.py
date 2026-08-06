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
import matplotlib.dates as mdates
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


# The signup funnel this property was instrumented to measure, in journey
# order. Source of truth: portfolio-analytics/docs/MEASUREMENT_PLAN.md §2 —
# the order is the journey, never the volume ranking. A bar chart sorted by
# count is a ranking; a funnel is only a funnel if the steps stay in sequence.
FUNNEL_STEPS = {
    "view_item_list": "1. Viewed plans",
    "select_item": "2. Selected a plan",
    "begin_checkout": "3. Started signup",
    "sign_up": "4. Created account",
    "purchase": "5. Converted",
}


def only_funnel_events():
    """Restrict the query to the funnel events (server-side)."""
    from google.analytics.data_v1beta.types import Filter, FilterExpression

    return FilterExpression(
        filter=Filter(
            field_name="eventName",
            in_list_filter=Filter.InListFilter(values=list(FUNNEL_STEPS)),
        )
    )


def only_signups():
    """sign_up is the only event carrying plan_id (Measurement Plan §3.4).

    Without this filter the query returns one `(not set)` row aggregating
    every other event, which would swamp the real plan split.
    """
    from google.analytics.data_v1beta.types import Filter, FilterExpression

    return FilterExpression(
        filter=Filter(
            field_name="eventName",
            string_filter=Filter.StringFilter(value="sign_up"),
        )
    )


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
    # The one report here that a default GA4 property could not produce:
    # sessions, channels and devices exist everywhere without configuring
    # anything, whereas this funnel is the instrumentation work itself.
    "funnel": (
        ["eventName"],
        ["eventCount"],
        only_funnel_events,
    ),
    # Event-scoped custom dimension registered in GA4 Admin. The API refers to
    # it as `customEvent:<parameter name>`; registration is not retroactive, so
    # it only returns data collected after the dimension was created.
    "by_plan": (
        ["customEvent:plan_id"],
        ["eventCount"],
        only_signups,
    ),
}

# Queries whose failure must not abort the run. `by_plan` depends on a custom
# dimension existing in the property: if it is missing or renamed the API
# raises, and losing the whole weekly report over one optional breakdown would
# be a worse outcome than shipping it without that section.
OPTIONAL_QUERIES = {"by_plan"}

ACCENT = "#3b6fd4"
ACCENT_SOFT = "#dce6f9"
INK = "#1f2933"
MUTED = "#7b8794"
GRID = "#e6eaef"
POSITIVE = "#0f9d58"
NEGATIVE = "#d93025"

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


def previous_window(start, end):
    """The window of equal length immediately before the reported one.

    A number without a reference point is not information: 448 page views is
    only meaningful next to what the same span produced before it.
    """
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=span - 1), prev_end


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


def order_funnel(df: pd.DataFrame) -> pd.DataFrame:
    """Put the funnel back in journey order and compute the step-to-step rates.

    The API returns the five events unordered (and omits any with no data), so
    left alone they render as a ranking sorted by volume — which reads as a
    funnel but is not one. Reindexing over FUNNEL_STEPS restores the sequence
    and makes a missing step visible as a zero instead of a silent absence.

    Caveat, stated in the report too: these are event counts, not users
    progressing through the journey. A GA4 Exploration funnel counts distinct
    users per step; this counts events, so a user who reloads the pricing page
    is counted twice. The step ratios show the shape of the drop-off, not a
    user-level conversion rate.
    """
    counts = dict(zip(df["eventName"], df["eventCount"])) if not df.empty else {}

    rows, previous, entry = [], None, None
    for event, label in FUNNEL_STEPS.items():
        count = float(counts.get(event, 0))
        if entry is None:
            entry = count
        rows.append({
            "step": label,
            "eventName": event,
            "eventCount": count,
            # First step is the baseline: 100% of itself.
            "pctOfPrevious": 1.0 if previous is None
                             else (count / previous if previous else 0.0),
            "pctOfEntry": count / entry if entry else 0.0,
        })
        previous = count
    return pd.DataFrame(rows)


PLAN_LABELS = {"plan_starter": "Starter", "plan_pro": "Pro",
               "plan_business": "Business"}


def tidy_plans(df: pd.DataFrame) -> pd.DataFrame:
    """Give the custom-dimension column a readable name and label the plans."""
    if df.empty:
        return pd.DataFrame(columns=["plan", "eventCount", "share"])
    d = df.rename(columns={"customEvent:plan_id": "plan_id"}).copy()
    d["plan"] = d["plan_id"].map(lambda v: PLAN_LABELS.get(v, v or "(not set)"))
    total = d["eventCount"].sum()
    d["share"] = d["eventCount"] / total if total else 0.0
    return (d[["plan", "eventCount", "share"]]
            .sort_values("eventCount", ascending=False)
            .reset_index(drop=True))


def data_quality_checks(data: dict) -> list:
    """Assertions about the data, rendered in the report itself.

    A report that only shows numbers asks the reader to trust them. Stating
    what was checked — and what failed — is what separates a dashboard from a
    measurement deliverable, and it is how Bug #6 was caught in the first place.
    """
    checks = []
    funnel = data.get("funnel", pd.DataFrame())

    if not funnel.empty and funnel["eventCount"].sum() > 0:
        # A later step cannot exceed the one before it: you cannot start
        # checkout without having selected a plan. If it happens, the earlier
        # step is under-firing — a collection defect, not user behaviour.
        inversions = [
            (funnel.iloc[i - 1], funnel.iloc[i])
            for i in range(1, len(funnel))
            if funnel.iloc[i]["eventCount"] > funnel.iloc[i - 1]["eventCount"]
        ]
        for prev_step, step in inversions:
            checks.append((
                "fail",
                f'Funnel inversion: "{step["step"]}" ({int(step["eventCount"])}) '
                f'exceeds "{prev_step["step"]}" ({int(prev_step["eventCount"])}). '
                f'The earlier step is under-firing — investigate '
                f'<code>{prev_step["eventName"]}</code>.'
            ))
        if not inversions:
            checks.append(("pass", "Funnel steps decrease monotonically."))
    else:
        checks.append(("warn", "No funnel events in this period."))

    events = data.get("top_events", pd.DataFrame())
    if not events.empty and PHANTOM_EVENT in set(events["eventName"]):
        checks.append(("fail", f'Phantom event "{PHANTOM_EVENT}" is still being '
                               f"collected — the exclusion only hides it here."))
    else:
        checks.append(("pass", f'No events outside the plan dictionary '
                               f'(phantom "{PHANTOM_EVENT}" excluded until '
                               f'{PHANTOM_EXPIRES}).'))

    daily = data.get("daily_overview", pd.DataFrame())
    if not daily.empty:
        silent = int((daily["sessions"] == 0).sum())
        if silent:
            checks.append(("warn", f"{silent} of {len(daily)} days recorded no "
                                   f"sessions (shown as zeros, not gaps)."))

    plans = data.get("by_plan", pd.DataFrame())
    if plans.empty:
        checks.append(("warn", "No plan breakdown: the <code>plan_id</code> "
                               "custom dimension returned no rows. Registration "
                               "is not retroactive."))
    elif "(not set)" in set(plans["plan"]):
        checks.append(("warn", "Some sign-ups report <code>(not set)</code> for "
                               "<code>plan_id</code> — collected before the "
                               "dimension was registered, or the parameter was "
                               "missing."))
    return checks


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
        try:
            data[name] = fetch_report(client, property_id, start, end,
                                      dims, mets, dim_filter, name=name)
        except Exception as exc:                      # noqa: BLE001
            if name not in OPTIONAL_QUERIES:
                raise
            print(f"  WARNING  {name}: query failed ({exc.__class__.__name__}); "
                  f"the report continues without that section.")
            data[name] = pd.DataFrame(columns=dims + mets)

    # Only the daily series is reindexed. The other reports are breakdowns,
    # not time series: an absent channel or device is genuinely absent, not a
    # gap to fill.
    data["daily_overview"] = fill_missing_days(
        data["daily_overview"], start, end, QUERIES["daily_overview"][1]
    )

    # Same metrics over the preceding window, for the KPI deltas.
    prev_start, prev_end = previous_window(start, end)
    print("  querying previous period ...")
    prev_dims, prev_mets, _ = QUERIES["daily_overview"]
    prev = fetch_report(client, property_id, prev_start, prev_end,
                        prev_dims, prev_mets, None, name="previous_period")
    data["previous_period"] = totals_row(prev, prev_mets, prev_start, prev_end)
    return data


def totals_row(df: pd.DataFrame, metrics, start, end) -> pd.DataFrame:
    """Collapse a daily frame into the one-row totals used for the deltas."""
    sums = {m: (float(df[m].sum()) if m in df.columns and not df.empty else 0.0)
            for m in metrics}
    return pd.DataFrame([{"start": start.isoformat(), "end": end.isoformat(),
                          **sums}])


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
    # Same raw shape the API returns for the funnel query (unordered event
    # rows) so --demo exercises order_funnel exactly like the live path.
    funnel = pd.DataFrame({
        "eventName": ["purchase", "view_item_list", "sign_up",
                      "begin_checkout", "select_item"],
        "eventCount": [18, 47, 21, 33, 29],
    })
    # Raw custom-dimension shape, exactly as the API returns it.
    plans = pd.DataFrame({
        "customEvent:plan_id": ["plan_pro", "plan_starter", "plan_business"],
        "eventCount": [11, 6, 4],
    })
    start, end = report_window(days)
    prev_start, prev_end = previous_window(start, end)
    previous = pd.DataFrame([{
        "start": prev_start.isoformat(), "end": prev_end.isoformat(),
        "sessions": float(daily.sessions.sum()) * 0.82,
        "totalUsers": float(daily.totalUsers.sum()) * 0.85,
        "screenPageViews": float(daily.screenPageViews.sum()) * 0.9,
        "eventCount": float(daily.eventCount.sum()) * 0.88,
    }])
    return {"daily_overview": daily, "by_channel": channels,
            "by_device": devices, "top_events": events, "funnel": funnel,
            "by_plan": plans, "previous_period": previous}


# ------------------------------------------------------------------ charts --
def fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight",
                transparent=True)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def style_axes(ax, xgrid=False, ygrid=False):
    """One look for every chart, applied in one place."""
    ax.set_facecolor("none")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
    if xgrid:
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    if ygrid:
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def label_bars(ax, values, positions, fmt=lambda v: f"{int(v):,}", pad=1.02):
    """Value labels next to horizontal bars, so no axis reading is needed."""
    for value, pos in zip(values, positions):
        ax.text(value * pad, pos, f" {fmt(value)}", va="center",
                fontsize=8.5, color=INK)


def make_charts(data: dict) -> dict:
    charts = {}

    daily = data["daily_overview"]
    if not daily.empty:
        fig, ax = plt.subplots(figsize=(9, 2.9))
        ax.plot(daily["date"], daily["sessions"], color=ACCENT, linewidth=2.2,
                zorder=3)
        ax.fill_between(daily["date"], daily["sessions"], alpha=0.16,
                        color=ACCENT, zorder=2)

        # The x-axis is the reason this chart was unreadable: with one tick per
        # day the labels overlapped into a smear. AutoDateLocator picks a
        # sensible number of ticks for the window length, and ConciseDateFormatter
        # drops the repeated month/year instead of repeating them on every label.
        locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.set_ylim(bottom=0)
        ax.margins(x=0.01)
        style_axes(ax, ygrid=True)
        charts["daily"] = fig_to_base64(fig)

    ch = data["by_channel"].copy()
    if not ch.empty:
        ch["channel"] = ch["sessionSource"] + " / " + ch["sessionMedium"]
        ch = ch.sort_values("sessions").tail(8)
        fig, ax = plt.subplots(figsize=(9, 0.46 * len(ch) + 1.1))
        ax.barh(ch["channel"], ch["sessions"], color=ACCENT, height=0.62)
        ax.set_xlim(0, max(ch["sessions"].max(), 1) * 1.18)
        label_bars(ax, ch["sessions"], range(len(ch)))
        ax.xaxis.set_visible(False)
        style_axes(ax)
        charts["channel"] = fig_to_base64(fig)

    dev = data["by_device"]
    if not dev.empty:
        fig, ax = plt.subplots(figsize=(4.6, 2.9))
        ax.bar(dev["deviceCategory"], dev["sessions"], color=ACCENT, width=0.55)
        for x, value in enumerate(dev["sessions"]):
            ax.text(x, value, f"{int(value):,}\n", ha="center", va="bottom",
                    fontsize=8.5, color=INK)
        ax.set_ylim(0, max(dev["sessions"].max(), 1) * 1.2)
        ax.yaxis.set_visible(False)
        style_axes(ax)
        charts["device"] = fig_to_base64(fig)

    fn = data["funnel"]
    if not fn.empty and fn["eventCount"].sum() > 0:
        fig, ax = plt.subplots(figsize=(9, 3.2))
        y = list(range(len(fn)))
        # Softer shade for intermediate steps so entry and conversion read as
        # the two anchors of the journey.
        colors = [ACCENT if i in (0, len(fn) - 1) else ACCENT_SOFT for i in y]
        ax.barh(y, fn["eventCount"], color=colors, height=0.6)
        ax.set_yticks(y)
        ax.set_yticklabels(fn["step"])
        ax.invert_yaxis()          # journey order reads top-to-bottom
        for i, (count, pct) in enumerate(zip(fn["eventCount"], fn["pctOfPrevious"])):
            suffix = "" if i == 0 else f"   {pct * 100:.0f}% of previous"
            ax.text(count, i, f"  {int(count)}{suffix}", va="center",
                    fontsize=8.5, color=INK)
        ax.set_xlim(0, max(fn["eventCount"].max(), 1) * 1.5)
        ax.xaxis.set_visible(False)
        style_axes(ax)
        charts["funnel"] = fig_to_base64(fig)

    plans = data.get("by_plan", pd.DataFrame())
    if not plans.empty and plans["eventCount"].sum() > 0:
        fig, ax = plt.subplots(figsize=(4.6, 2.9))
        ax.bar(plans["plan"], plans["eventCount"], color=ACCENT, width=0.55)
        for x, (value, share) in enumerate(zip(plans["eventCount"],
                                               plans["share"])):
            ax.text(x, value, f"{int(value)} · {share * 100:.0f}%\n",
                    ha="center", va="bottom", fontsize=8.5, color=INK)
        ax.set_ylim(0, max(plans["eventCount"].max(), 1) * 1.25)
        ax.yaxis.set_visible(False)
        style_axes(ax)
        charts["plans"] = fig_to_base64(fig)

    ev = data["top_events"].sort_values("eventCount").tail(10)
    if not ev.empty:
        fig, ax = plt.subplots(figsize=(9, 0.42 * len(ev) + 1.1))
        ax.barh(ev["eventName"], ev["eventCount"], color=ACCENT, height=0.6)
        ax.set_xlim(0, max(ev["eventCount"].max(), 1) * 1.18)
        label_bars(ax, ev["eventCount"], range(len(ev)))
        ax.xaxis.set_visible(False)
        style_axes(ax)
        charts["events"] = fig_to_base64(fig)

    return charts


# -------------------------------------------------------------------- html --
# GA4 returns rates as fractions (0.7741935483870968). Rendered raw they are
# unreadable; rounded like every other float they become "0.77", which is not
# how anyone reads an engagement rate either.
RATE_COLUMNS = {"engagementRate", "bounceRate", "conversionRate",
                "pctOfPrevious", "pctOfEntry"}


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


def kpi_card(label: str, value: float, previous: float, hint: str = "") -> str:
    """A KPI with its change against the preceding window.

    A bare total invites no judgement; the same total next to "+18% vs previous
    28 days" does. When there is no prior period to compare against the delta is
    omitted rather than shown as a fake 0%.
    """
    if previous:
        change = (value - previous) / previous
        arrow = "▲" if change >= 0 else "▼"
        cls = "up" if change >= 0 else "down"
        delta = (f'<span class="delta {cls}">{arrow} {abs(change) * 100:.1f}%</span>'
                 f'<span class="vs">vs previous period</span>')
    else:
        delta = '<span class="vs">no prior period to compare</span>'
    hint_html = f'<span class="hint">{hint}</span>' if hint else ""
    return (f'<div class="kpi"><span class="kpi-label">{label}</span>'
            f'<b>{int(value):,}</b>{delta}{hint_html}</div>')


def quality_panel(checks: list) -> str:
    if not checks:
        return ""
    icons = {"pass": "✓", "warn": "!", "fail": "✕"}
    items = "".join(
        f'<li class="chk {level}"><span class="badge">{icons[level]}</span>'
        f'<span>{message}</span></li>'
        for level, message in checks
    )
    failures = sum(1 for level, _ in checks if level == "fail")
    summary = (f"{failures} check(s) failed" if failures
               else "all checks passed")
    return (f'<section class="card"><h2>Data quality</h2>'
            f'<p class="note">Assertions run against this extract every time the '
            f'report is generated — {summary}.</p>'
            f'<ul class="checks">{items}</ul></section>')


# ---------------------------------------------------------- public data --
# Everything under data/public/ is a PUBLISHED INTERFACE, not a by-product.
#
# report/data/*.csv mirrors whatever the API returned this week: camelCase,
# raw metric names, shape free to change whenever a query changes. Dashboards
# must not read it.
#
# data/public/*.csv is consumed by things outside this repo — a Google Sheet
# feeding Tableau Public, a Power BI web query, anything else added later. Those
# consumers break silently when a column is renamed: a chart keeps rendering
# with the last cached values and nobody notices for weeks. So this layer has
# rules:
#
#   1. Column names are snake_case and stable, decoupled from GA4's naming.
#   2. Columns are append-only. Never renamed, never removed, never reordered.
#   3. Rates stay decimal (0-1) and dates ISO — formatting belongs to the
#      dashboard, not to the interface.
#   4. Counts are integers.
#   5. Any breaking change means a new SCHEMA_VERSION and a new directory,
#      leaving the old one in place until consumers have migrated.
#
# It is the same append-only discipline the upstream event names follow.
PUBLIC_SCHEMA_VERSION = "1.0"


def public_tables(data: dict, days: int) -> dict:
    """Map the internal frames onto the published schema."""
    out = {}

    daily = data.get("daily_overview", pd.DataFrame())
    if not daily.empty:
        d = daily.rename(columns={
            "totalUsers": "users",
            "screenPageViews": "page_views",
            "eventCount": "events",
        }).copy()
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        for c in ("sessions", "users", "page_views", "events"):
            d[c] = d[c].round(0).astype(int)
        out["daily_kpis"] = d[["date", "sessions", "users", "page_views", "events"]]

    funnel = data.get("funnel", pd.DataFrame())
    if not funnel.empty:
        f = funnel.copy()
        # "1. Viewed plans" -> step_number 1, step_name "Viewed plans", so a
        # dashboard can sort by the journey without parsing a label.
        parts = f["step"].str.split(".", n=1, expand=True)
        f["step_number"] = parts[0].astype(int)
        f["step_name"] = parts[1].str.strip()
        f["event_count"] = f["eventCount"].round(0).astype(int)
        out["funnel"] = f.rename(columns={
            "eventName": "event_name",
            "pctOfPrevious": "pct_of_previous",
            "pctOfEntry": "pct_of_entry",
        })[["step_number", "step_name", "event_name", "event_count",
            "pct_of_previous", "pct_of_entry"]]

    ch = data.get("by_channel", pd.DataFrame())
    if not ch.empty:
        c = ch.rename(columns={
            "sessionSource": "source",
            "sessionMedium": "medium",
            "totalUsers": "users",
        }).copy()
        for col in ("sessions", "users"):
            c[col] = c[col].round(0).astype(int)
        out["by_channel"] = c[["source", "medium", "sessions", "users"]]

    dev = data.get("by_device", pd.DataFrame())
    if not dev.empty:
        v = dev.rename(columns={
            "deviceCategory": "device",
            "engagementRate": "engagement_rate",
        }).copy()
        v["sessions"] = v["sessions"].round(0).astype(int)
        out["by_device"] = v[["device", "sessions", "engagement_rate"]]

    plans = data.get("by_plan", pd.DataFrame())
    if not plans.empty:
        p = plans.rename(columns={"eventCount": "signups"}).copy()
        p["signups"] = p["signups"].round(0).astype(int)
        out["by_plan"] = p[["plan", "signups", "share"]]

    # Every consumer needs to answer "how old is this?" without trusting a
    # file timestamp that git rewrites on checkout.
    start, end = report_window(days)
    out["meta"] = pd.DataFrame([{
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_days": days,
        "source": "GA4 Data API",
    }])
    return out


def write_public_contract(data: dict, public_dir: str, days: int) -> list:
    os.makedirs(public_dir, exist_ok=True)
    written = []
    for name, df in public_tables(data, days).items():
        path = os.path.join(public_dir, f"{name}.csv")
        df.to_csv(path, index=False)
        written.append(os.path.basename(path))
    return written


def build_html(data: dict, charts: dict, days: int, demo: bool) -> str:
    daily = data["daily_overview"]
    prev = data.get("previous_period", pd.DataFrame())
    prev_row = prev.iloc[0] if not prev.empty else {}

    def total(metric):
        return float(daily[metric].sum()) if not daily.empty else 0.0

    def prior(metric):
        try:
            return float(prev_row[metric])
        except (KeyError, TypeError, IndexError):
            return 0.0

    if not daily.empty:
        period = (f'{daily["date"].min():%d %b %Y} – '
                  f'{daily["date"].max():%d %b %Y}')
    else:
        period = f"last {days} days"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    sessions, users = total("sessions"), total("totalUsers")
    pages, events = total("screenPageViews"), total("eventCount")
    pages_per_session = f"{pages / sessions:.1f} pages/session" if sessions else ""

    funnel = data.get("funnel", pd.DataFrame())
    if not funnel.empty and funnel.iloc[0]["eventCount"]:
        cvr = funnel.iloc[-1]["eventCount"] / funnel.iloc[0]["eventCount"]
        funnel_kpi = (f'<div class="kpi"><span class="kpi-label">Funnel conversion</span>'
                      f'<b>{cvr * 100:.1f}%</b>'
                      f'<span class="vs">{int(funnel.iloc[-1]["eventCount"])} '
                      f'conversions from {int(funnel.iloc[0]["eventCount"])} '
                      f'plan views</span></div>')
    else:
        funnel_kpi = ""

    banner = ('<p class="demo"><b>Demo data.</b> Generated sample with the exact '
              'shape of the API response — not real analytics. Run without '
              '<code>--demo</code> for live data.</p>' if demo else "")

    def img(key, alt):
        return (f'<img src="data:image/png;base64,{charts[key]}" alt="{alt}">'
                if key in charts
                else '<p class="empty">No data for this period.</p>')

    def section(title, chart_key, alt, table_df, note=""):
        note_html = f'<p class="note">{note}</p>' if note else ""
        table = (f'<details><summary>View data table</summary>'
                 f'{df_to_html_table(table_df)}</details>'
                 if table_df is not None and not table_df.empty else "")
        return (f'<section class="card"><h2>{title}</h2>{note_html}'
                f'{img(chart_key, alt)}{table}</section>')

    plans = data.get("by_plan", pd.DataFrame())
    plan_section = section(
        "Sign-ups by plan", "plans", "Sign-ups by plan", plans,
        'From the event-scoped custom dimension <code>plan_id</code>, sent with '
        'every <code>sign_up</code>. Registration is not retroactive, so this '
        'only covers sign-ups collected after the dimension was created.'
    ) if not plans.empty else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GA4 Report — {period}</title>
<style>
  :root {{ --accent: {ACCENT}; --ink: {INK}; --muted: {MUTED};
           --grid: {GRID}; --up: {POSITIVE}; --down: {NEGATIVE}; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif; margin: 0; background: #f5f7fa;
          color: var(--ink); line-height: 1.55;
          -webkit-font-smoothing: antialiased; }}
  .wrap {{ max-width: 1000px; margin: 0 auto; padding: 2.5rem 1.25rem 3rem; }}
  header.top {{ margin-bottom: 1.75rem; }}
  .eyebrow {{ text-transform: uppercase; letter-spacing: .13em; font-size: .7rem;
              font-weight: 700; color: var(--accent); margin: 0 0 .4rem; }}
  h1 {{ font-size: 1.75rem; margin: 0 0 .35rem; letter-spacing: -.02em; }}
  .period {{ color: var(--muted); font-size: .92rem; margin: 0; }}
  .demo {{ background: #fff8e1; border: 1px solid #f5d98b; color: #6b5312;
           padding: .8rem 1rem; border-radius: 10px; margin: 1.25rem 0 0;
           font-size: .88rem; }}
  .kpis {{ display: grid; gap: .9rem; margin: 1.75rem 0;
           grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }}
  .kpi {{ background: #fff; border: 1px solid var(--grid); border-radius: 12px;
          padding: 1.05rem 1.15rem; }}
  .kpi-label {{ display: block; font-size: .78rem; text-transform: uppercase;
                letter-spacing: .07em; color: var(--muted); font-weight: 600; }}
  .kpi b {{ font-size: 1.95rem; display: block; line-height: 1.15;
            margin: .3rem 0 .25rem; letter-spacing: -.02em; }}
  .delta {{ font-size: .84rem; font-weight: 700; margin-right: .4rem; }}
  .delta.up {{ color: var(--up); }} .delta.down {{ color: var(--down); }}
  .vs, .hint {{ font-size: .76rem; color: var(--muted); }}
  .hint {{ display: block; }}
  .card {{ background: #fff; border: 1px solid var(--grid); border-radius: 12px;
           padding: 1.4rem 1.5rem; margin-bottom: 1.1rem; }}
  .card h2 {{ font-size: 1.05rem; margin: 0 0 .5rem; letter-spacing: -.01em; }}
  .grid-2 {{ display: grid; gap: 1.1rem;
             grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }}
  .grid-2 .card {{ margin-bottom: 0; }}
  img {{ max-width: 100%; height: auto; display: block; margin: .35rem 0 0; }}
  .note {{ color: var(--muted); font-size: .83rem; margin: 0 0 .7rem; }}
  .empty {{ color: var(--muted); font-size: .85rem; font-style: italic; }}
  details {{ margin-top: .9rem; }}
  summary {{ cursor: pointer; font-size: .83rem; color: var(--accent);
             font-weight: 600; }}
  .tbl {{ border-collapse: collapse; font-size: .82rem; margin-top: .7rem;
          width: 100%; }}
  .tbl th, .tbl td {{ padding: .45rem .7rem; text-align: left;
                      border-bottom: 1px solid var(--grid); }}
  .tbl th {{ background: #f7f9fc; font-weight: 600; color: var(--muted);
             text-transform: uppercase; font-size: .72rem;
             letter-spacing: .05em; }}
  .checks {{ list-style: none; padding: 0; margin: 0; }}
  .chk {{ display: flex; gap: .65rem; align-items: flex-start;
          padding: .5rem 0; border-top: 1px solid var(--grid);
          font-size: .87rem; }}
  .chk:first-child {{ border-top: 0; }}
  .badge {{ flex: none; width: 1.25rem; height: 1.25rem; border-radius: 50%;
            display: grid; place-items: center; font-size: .72rem;
            font-weight: 700; color: #fff; margin-top: .12rem; }}
  .pass .badge {{ background: var(--up); }}
  .warn .badge {{ background: #e8a317; }}
  .fail .badge {{ background: var(--down); }}
  code {{ background: #f0f3f8; padding: .08rem .3rem; border-radius: 4px;
          font-size: .88em; }}
  footer {{ color: var(--muted); font-size: .8rem; margin-top: 1.75rem;
            text-align: center; }}
  footer a {{ color: var(--accent); }}
</style></head><body>
<div class="wrap">
<header class="top">
  <p class="eyebrow">Automated weekly report</p>
  <h1>GA4 traffic &amp; signup funnel</h1>
  <p class="period">{period} · {days}-day window</p>
  {banner}
</header>

<div class="kpis">
  {kpi_card("Sessions", sessions, prior("sessions"))}
  {kpi_card("Users", users, prior("totalUsers"))}
  {kpi_card("Page views", pages, prior("screenPageViews"), pages_per_session)}
  {kpi_card("Events", events, prior("eventCount"))}
  {funnel_kpi}
</div>

{section("Sessions per day", "daily", "Sessions per day", None,
         "Days with no activity are reindexed to zero: the API omits them, "
         "which would otherwise draw a slope across a silent stretch.")}

{section("Signup funnel", "funnel", "Signup funnel", funnel,
         'Journey order, as defined in the '
         '<a href="https://github.com/damondrc/portfolio-analytics/blob/main/docs/MEASUREMENT_PLAN.md">measurement plan</a> '
         '— never sorted by volume, which would make it a ranking. These are '
         'event counts, not distinct users progressing, so the ratios describe '
         'the shape of the drop-off rather than a user-level conversion rate.')}

{plan_section}

<div class="grid-2">
{section("Acquisition", "channel", "Sessions by source and medium",
         data["by_channel"],
         "Synthetic traffic from the simulator is tagged "
         "<code>traffic-sim / synthetic</code>, so it stays separable.")}
{section("Devices", "device", "Sessions by device", data["by_device"])}
</div>

{section("Top events", "events", "Top events by count", data["top_events"],
         f'The phantom event <code>{PHANTOM_EVENT}</code> is excluded '
         f'server-side until {PHANTOM_EXPIRES} — see Bug #6.')}

{quality_panel(data_quality_checks(data))}

<footer>Generated {generated} · GA4 Data API ·
<a href="https://github.com/damondrc/ga4-reporting-automation">source</a> ·
<a href="https://github.com/damondrc/portfolio-analytics">instrumentation</a>
</footer>
</div>
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

    # Applied outside fetch_all so both paths run the same transform: --demo
    # (and therefore CI) exercises the ordering logic, not a shortcut around it.
    data["funnel"] = order_funnel(data["funnel"])
    data["by_plan"] = tidy_plans(data["by_plan"])

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    csv_dir = os.path.join(os.path.dirname(args.output), "data")
    os.makedirs(csv_dir, exist_ok=True)
    for name, df in data.items():
        df.to_csv(os.path.join(csv_dir, f"{name}.csv"), index=False)

    # The published contract lives next to the report's parent directory, so a
    # run with --output /tmp/... writes its public files to /tmp too. That is
    # deliberate: `--demo` must never be able to overwrite the real published
    # data with sample values, the same reason CI redirects the HTML output.
    public_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(args.output))),
        "data", "public")
    written = write_public_contract(data, public_dir, args.days)
    print(f"Public contract v{PUBLIC_SCHEMA_VERSION} written to {public_dir} "
          f"({', '.join(written)})")

    charts = make_charts(data)
    html = build_html(data, charts, args.days, args.demo)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report written to {args.output}")


if __name__ == "__main__":
    main()
