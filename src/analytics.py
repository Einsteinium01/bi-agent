"""Analytics engine.

Every figure the agent reports is computed here in pandas. The language model
chooses which function to call and with what filters; it never does arithmetic
itself. That boundary is what stops a BI agent from inventing revenue numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from normalize import (
    QualityReport,
    join_caveat,
    normalize_deals,
    normalize_work_orders,
)

CURRENT_FY_START_MONTH = 4  # Indian financial year runs April-March.


@dataclass
class Dataset:
    deals: pd.DataFrame
    work_orders: pd.DataFrame
    deals_quality: QualityReport
    wo_quality: QualityReport
    join_info: dict[str, Any]

    @classmethod
    def from_boards(cls, deal_rows: list[dict], wo_rows: list[dict]) -> "Dataset":
        deals, dq = normalize_deals(deal_rows)
        wos, wq = normalize_work_orders(wo_rows)
        return cls(deals, wos, dq, wq, join_caveat(deals, wos))


def _fy_bounds(label: str, ref: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    year = ref.year if ref.month >= CURRENT_FY_START_MONTH else ref.year - 1
    if label == "last_fy":
        year -= 1
    start = pd.Timestamp(year=year, month=CURRENT_FY_START_MONTH, day=1)
    return start, start + pd.DateOffset(years=1) - pd.Timedelta(days=1)


def resolve_period(period: str | None, ref: pd.Timestamp | None = None) -> tuple[pd.Timestamp | None, pd.Timestamp | None, str]:
    """Turn a founder's time phrase into a concrete date range."""
    ref = ref or pd.Timestamp.today().normalize()
    if not period or period.lower() in ("all", "all_time", "any"):
        return None, None, "all time"

    p = period.lower().strip().replace(" ", "_")

    if p in ("this_quarter", "current_quarter", "this_qtr"):
        start = pd.Timestamp(year=ref.year, month=3 * ((ref.month - 1) // 3) + 1, day=1)
        return start, start + pd.DateOffset(months=3) - pd.Timedelta(days=1), f"quarter starting {start:%b %Y}"
    if p in ("last_quarter", "previous_quarter"):
        cur = pd.Timestamp(year=ref.year, month=3 * ((ref.month - 1) // 3) + 1, day=1)
        start = cur - pd.DateOffset(months=3)
        return start, cur - pd.Timedelta(days=1), f"quarter starting {start:%b %Y}"
    if p in ("this_month", "current_month"):
        start = ref.replace(day=1)
        return start, start + pd.DateOffset(months=1) - pd.Timedelta(days=1), f"{start:%b %Y}"
    if p in ("last_month", "previous_month"):
        start = ref.replace(day=1) - pd.DateOffset(months=1)
        return start, ref.replace(day=1) - pd.Timedelta(days=1), f"{start:%b %Y}"
    if p in ("this_year", "current_year"):
        return pd.Timestamp(ref.year, 1, 1), pd.Timestamp(ref.year, 12, 31), str(ref.year)
    if p in ("this_fy", "current_fy", "fy", "this_financial_year"):
        s, e = _fy_bounds("this_fy", ref)
        return s, e, f"FY{s.year}-{str(e.year)[2:]}"
    if p in ("last_fy", "previous_fy", "last_financial_year"):
        s, e = _fy_bounds("last_fy", ref)
        return s, e, f"FY{s.year}-{str(e.year)[2:]}"
    if p in ("ytd", "year_to_date"):
        return pd.Timestamp(ref.year, 1, 1), ref, f"{ref.year} to date"

    match = re.match(r"^(?:last_|past_)(\d+)_(day|week|month|quarter|year)s?$", p)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        delta = {
            "day": pd.DateOffset(days=n), "week": pd.DateOffset(weeks=n),
            "month": pd.DateOffset(months=n), "quarter": pd.DateOffset(months=3 * n),
            "year": pd.DateOffset(years=n),
        }[unit]
        return ref - delta, ref, f"last {n} {unit}(s)"

    match = re.match(r"^(?:q)([1-4])[_-]?(\d{4})$", p)
    if match:
        q, yr = int(match.group(1)), int(match.group(2))
        start = pd.Timestamp(year=yr, month=3 * (q - 1) + 1, day=1)
        return start, start + pd.DateOffset(months=3) - pd.Timedelta(days=1), f"Q{q} {yr}"

    if re.match(r"^\d{4}$", p):
        yr = int(p)
        return pd.Timestamp(yr, 1, 1), pd.Timestamp(yr, 12, 31), p

    return None, None, f"all time (could not interpret '{period}')"


def _match(series: pd.Series, wanted: str) -> pd.Series:
    """Case- and substring-tolerant categorical match."""
    target = str(wanted).strip().lower()
    text = series.fillna("").astype(str).str.lower().str.strip()
    exact = text == target
    if exact.any():
        return exact
    return text.str.contains(re.escape(target), na=False)


def _apply_filters(df: pd.DataFrame, spec: dict[str, Any], date_col: str) -> tuple[pd.DataFrame, list[str]]:
    applied: list[str] = []
    out = df

    for field, column in spec.get("_map", {}).items():
        value = spec.get(field)
        if value and column in out.columns:
            out = out[_match(out[column], value)]
            applied.append(f"{column} ~ '{value}'")

    period = spec.get("period")
    if period and date_col in out.columns:
        start, end, label = resolve_period(period)
        if start is not None:
            in_range = out[date_col].between(start, end)
            dropped = int(out[date_col].isna().sum())
            out = out[in_range]
            applied.append(f"{date_col} within {label}")
            if dropped:
                applied.append(f"{dropped} row(s) excluded for having no {date_col}")

    return out, applied


def _money(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "unavailable"
    x = float(x)
    if abs(x) >= 1e7:
        return f"Rs {x/1e7:,.2f} Cr"
    if abs(x) >= 1e5:
        return f"Rs {x/1e5:,.2f} L"
    return f"Rs {x:,.0f}"


def _summarize(df: pd.DataFrame, value_col: str) -> dict[str, Any]:
    if value_col not in df.columns or df.empty:
        return {"count": len(df), "total_value": None, "value_coverage": "0 of 0 rows"}
    vals = df[value_col].dropna()
    return {
        "count": len(df),
        "rows_with_value": len(vals),
        "total_value": float(vals.sum()) if len(vals) else None,
        "total_value_readable": _money(vals.sum()) if len(vals) else "unavailable",
        "average_value": float(vals.mean()) if len(vals) else None,
        "median_value": float(vals.median()) if len(vals) else None,
        "largest_value": float(vals.max()) if len(vals) else None,
        "value_coverage": f"{len(vals)} of {len(df)} rows have a value",
    }


# --- Tool implementations -------------------------------------------------

def query_deals(ds: Dataset, sector=None, status=None, stage=None, owner=None,
                period=None, probability=None, limit=15) -> dict[str, Any]:
    spec = {
        "sector": sector, "status": status, "stage": stage, "owner": owner,
        "probability": probability, "period": period,
        "_map": {
            "sector": "Sector/service", "status": "Deal Status", "stage": "Deal Stage",
            "owner": "Owner code", "probability": "Closure Probability",
        },
    }
    df, applied = _apply_filters(ds.deals, spec, "effective_close_date")
    summary = _summarize(df, "deal_value")

    cols = [c for c in ["Deal Name", "Client Code", "Sector/service", "Deal Status",
                        "Deal Stage", "deal_value", "effective_close_date", "Owner code"]
            if c in df.columns]
    sample = df.sort_values("deal_value", ascending=False, na_position="last")[cols].head(limit)

    return {
        "filters_applied": applied or ["none"],
        "summary": summary,
        "deals": _records(sample),
        "caveats": ds.deals_quality.caveats(["deal_value", "effective_close_date"]),
    }


def query_work_orders(ds: Dataset, sector=None, execution_status=None, billing_status=None,
                      nature_of_work=None, owner=None, period=None, limit=15) -> dict[str, Any]:
    spec = {
        "sector": sector, "execution_status": execution_status,
        "billing_status": billing_status, "nature_of_work": nature_of_work,
        "owner": owner, "period": period,
        "_map": {
            "sector": "Sector", "execution_status": "Execution Status",
            "billing_status": "Invoice Status", "nature_of_work": "Nature of Work",
            "owner": "BD/KAM Personnel code",
        },
    }
    df, applied = _apply_filters(ds.work_orders, spec, "Date of PO/LOI")
    summary = _summarize(df, "Order Value Excl GST")

    for label, col in [("billed", "Billed Value Excl GST"), ("collected", "Collected Amount"),
                       ("receivable", "Amount Receivable")]:
        if col in df.columns:
            total = df[col].dropna().sum()
            summary[f"total_{label}"] = float(total)
            summary[f"total_{label}_readable"] = _money(total)

    cols = [c for c in ["Deal name masked", "Customer Name Code", "Serial #", "Sector",
                        "Execution Status", "Order Value Excl GST", "Billed Value Excl GST",
                        "Amount Receivable", "Date of PO/LOI"] if c in df.columns]
    sample = df.sort_values("Order Value Excl GST", ascending=False, na_position="last")[cols].head(limit)

    return {
        "filters_applied": applied or ["none"],
        "summary": summary,
        "work_orders": _records(sample),
        "caveats": ds.wo_quality.caveats(["Order Value Excl GST", "Amount Receivable", "Execution Status"]),
    }


def aggregate(ds: Dataset, board: str, group_by: str, metric: str = "total_value",
              period=None, sector=None, top_n: int = 12) -> dict[str, Any]:
    if board == "deals":
        df, value_col, date_col = ds.deals, "deal_value", "effective_close_date"
        sector_col, quality = "Sector/service", ds.deals_quality
    else:
        df, value_col, date_col = ds.work_orders, "Order Value Excl GST", "Date of PO/LOI"
        sector_col, quality = "Sector", ds.wo_quality

    applied: list[str] = []
    if sector and sector_col in df.columns:
        df = df[_match(df[sector_col], sector)]
        applied.append(f"{sector_col} ~ '{sector}'")

    if period and date_col in df.columns:
        start, end, label = resolve_period(period)
        if start is not None:
            df = df[df[date_col].between(start, end)]
            applied.append(f"{date_col} within {label}")

    if group_by not in df.columns:
        return {
            "error": f"'{group_by}' is not a column on the {board} board.",
            "available_columns": [c for c in df.columns if not c.startswith("_")],
        }

    grouped = df.groupby(df[group_by].fillna("(not set)"), dropna=False)
    if metric == "count":
        result = grouped.size().sort_values(ascending=False)
        rows = [{"group": k, "count": int(v)} for k, v in result.head(top_n).items()]
    else:
        agg_fn = {"total_value": "sum", "average_value": "mean", "median_value": "median"}.get(metric, "sum")
        result = grouped[value_col].agg(agg_fn).sort_values(ascending=False)
        counts = grouped.size()
        rows = [
            {
                "group": k,
                metric: None if pd.isna(v) else float(v),
                f"{metric}_readable": _money(v),
                "row_count": int(counts.get(k, 0)),
            }
            for k, v in result.head(top_n).items()
        ]

    return {
        "board": board,
        "grouped_by": group_by,
        "metric": metric,
        "filters_applied": applied or ["none"],
        "groups": rows,
        "total_groups": int(result.shape[0]),
        "caveats": quality.caveats([value_col, group_by]),
    }


def pipeline_health(ds: Dataset, sector=None, period=None) -> dict[str, Any]:
    """Funnel shape, weighted forecast and stall signals for open deals."""
    df = ds.deals
    applied: list[str] = []

    if sector and "Sector/service" in df.columns:
        df = df[_match(df["Sector/service"], sector)]
        applied.append(f"sector ~ '{sector}'")
    if period:
        start, end, label = resolve_period(period)
        if start is not None and "effective_close_date" in df.columns:
            df = df[df["effective_close_date"].between(start, end)]
            applied.append(f"expected close within {label}")

    active = df[df["stage_bucket"] == "Active"] if "stage_bucket" in df.columns else df
    won = df[df["stage_bucket"] == "Won"] if "stage_bucket" in df.columns else df.iloc[:0]
    lost = df[df["stage_bucket"] == "Lost"] if "stage_bucket" in df.columns else df.iloc[:0]

    # Probability labels are the only confidence signal present; map to weights.
    weights = {"High": 0.75, "Medium": 0.45, "Low": 0.2}
    weighted = 0.0
    weighted_rows = 0
    if "Closure Probability" in active.columns and "deal_value" in active.columns:
        for _, row in active.iterrows():
            val, prob = row.get("deal_value"), row.get("Closure Probability")
            if pd.notna(val) and prob in weights:
                weighted += float(val) * weights[prob]
                weighted_rows += 1

    decided = len(won) + len(lost)
    funnel = []
    if "Deal Stage" in active.columns:
        stage_group = active.groupby(active["Deal Stage"].fillna("(not set)"))
        for stage, sub in stage_group:
            vals = sub["deal_value"].dropna() if "deal_value" in sub.columns else pd.Series(dtype=float)
            funnel.append({
                "stage": stage,
                "deal_count": len(sub),
                "total_value": float(vals.sum()) if len(vals) else None,
                "total_value_readable": _money(vals.sum()) if len(vals) else "unavailable",
                "deals_missing_value": int(sub["deal_value"].isna().sum()) if "deal_value" in sub.columns else 0,
            })
        funnel.sort(key=lambda r: (
            active.loc[active["Deal Stage"] == r["stage"], "stage_order"].dropna().min()
            if (active["Deal Stage"] == r["stage"]).any() else 99
        ))

    stalled = []
    if "effective_close_date" in active.columns:
        today = pd.Timestamp.today().normalize()
        overdue = active[active["effective_close_date"] < today]
        for _, row in overdue.nlargest(8, "deal_value").iterrows() if "deal_value" in overdue.columns else []:
            stalled.append({
                "deal": row.get("Deal Name"),
                "client": row.get("Client Code"),
                "stage": row.get("Deal Stage"),
                "value_readable": _money(row.get("deal_value")),
                "expected_close": str(row.get("effective_close_date"))[:10],
                "days_overdue": int((today - row["effective_close_date"]).days),
            })

    open_vals = active["deal_value"].dropna() if "deal_value" in active.columns else pd.Series(dtype=float)

    return {
        "filters_applied": applied or ["none"],
        "open_deals": len(active),
        "open_pipeline_value": float(open_vals.sum()) if len(open_vals) else None,
        "open_pipeline_readable": _money(open_vals.sum()) if len(open_vals) else "unavailable",
        "open_deals_missing_value": int(active["deal_value"].isna().sum()) if "deal_value" in active.columns else 0,
        "weighted_forecast": round(weighted, 2) if weighted_rows else None,
        "weighted_forecast_readable": _money(weighted) if weighted_rows else "unavailable",
        "weighted_forecast_basis": (
            f"{weighted_rows} of {len(active)} open deals have both a value and a probability "
            "label (High=75%, Medium=45%, Low=20%)"
        ),
        "won_deals": len(won),
        "lost_deals": len(lost),
        "win_rate_pct": round(100 * len(won) / decided, 1) if decided else None,
        "win_rate_basis": f"{len(won)} won of {decided} decided deals",
        "funnel_by_stage": funnel,
        "overdue_deals": stalled,
        "caveats": ds.deals_quality.caveats(["deal_value", "Closure Probability", "effective_close_date"]),
    }


def revenue_summary(ds: Dataset, period=None, sector=None) -> dict[str, Any]:
    """Booked vs billed vs collected, from the work order board."""
    df = ds.work_orders
    applied: list[str] = []

    if sector and "Sector" in df.columns:
        df = df[_match(df["Sector"], sector)]
        applied.append(f"sector ~ '{sector}'")
    if period:
        start, end, label = resolve_period(period)
        if start is not None and "Date of PO/LOI" in df.columns:
            excluded = int(df["Date of PO/LOI"].isna().sum())
            df = df[df["Date of PO/LOI"].between(start, end)]
            applied.append(f"PO date within {label}")
            if excluded:
                applied.append(f"{excluded} work order(s) excluded for missing a PO date")

    def total(col: str) -> float:
        return float(df[col].dropna().sum()) if col in df.columns else 0.0

    booked, billed = total("Order Value Excl GST"), total("Billed Value Excl GST")
    collected, receivable = total("Collected Amount"), total("Amount Receivable")
    unbilled = total("To Be Billed Excl GST")

    return {
        "filters_applied": applied or ["none"],
        "work_order_count": len(df),
        "booked_value": booked, "booked_readable": _money(booked),
        "billed_value": billed, "billed_readable": _money(billed),
        "collected_value": collected, "collected_readable": _money(collected),
        "outstanding_receivable": receivable, "receivable_readable": _money(receivable),
        "yet_to_bill": unbilled, "yet_to_bill_readable": _money(unbilled),
        "billing_rate_pct": round(100 * billed / booked, 1) if booked else None,
        "collection_rate_pct": round(100 * collected / billed, 1) if billed else None,
        "caveats": ds.wo_quality.caveats(
            ["Order Value Excl GST", "Billed Value Excl GST", "Collected Amount", "Amount Receivable"]
        ) + [
            "Collected Amount is sparsely populated, so collection rate is a floor, not an exact figure."
        ],
    }


def cross_board_view(ds: Dataset, sector=None) -> dict[str, Any]:
    """Compare pipeline against execution, with explicit join warnings."""
    deals, wos = ds.deals, ds.work_orders
    applied: list[str] = []

    if sector:
        if "Sector/service" in deals.columns:
            deals = deals[_match(deals["Sector/service"], sector)]
        if "Sector" in wos.columns:
            wos = wos[_match(wos["Sector"], sector)]
        applied.append(f"sector ~ '{sector}'")

    won = deals[deals["stage_bucket"] == "Won"] if "stage_bucket" in deals.columns else deals.iloc[:0]
    won_names = set(won.get("Deal Name", pd.Series(dtype=str)).dropna())
    wo_names = set(wos.get("Deal name masked", pd.Series(dtype=str)).dropna())

    won_value = won["deal_value"].dropna().sum() if "deal_value" in won.columns else 0
    wo_value = wos["Order Value Excl GST"].dropna().sum() if "Order Value Excl GST" in wos.columns else 0

    return {
        "filters_applied": applied or ["none"],
        "won_deals": len(won),
        "won_deal_value_readable": _money(won_value),
        "work_orders": len(wos),
        "work_order_value_readable": _money(wo_value),
        "won_deals_with_work_order": len(won_names & wo_names),
        "won_deals_without_work_order": sorted(won_names - wo_names)[:15],
        "work_orders_without_matching_deal": sorted(wo_names - won_names)[:15],
        "join_warning": ds.join_info.get("warning"),
        "interpretation": (
            "Deal value and work order value are not directly comparable: deal value is "
            "the masked opportunity size, while order value is the contracted amount "
            "excluding GST. Treat the gap as directional."
        ),
    }


def data_quality_report(ds: Dataset) -> dict[str, Any]:
    return {
        "deals": ds.deals_quality.to_dict(),
        "work_orders": ds.wo_quality.to_dict(),
        "cross_board_join": ds.join_info,
    }


def list_dimensions(ds: Dataset) -> dict[str, Any]:
    """Show what can actually be filtered on, so the agent stops guessing."""
    def options(df: pd.DataFrame, col: str, cap: int = 25) -> list[str]:
        if col not in df.columns:
            return []
        return [str(v) for v in df[col].dropna().unique()[:cap]]

    return {
        "deals": {
            "columns": [c for c in ds.deals.columns if not c.startswith("_")],
            "sectors": options(ds.deals, "Sector/service"),
            "statuses": options(ds.deals, "Deal Status"),
            "stages": options(ds.deals, "Deal Stage"),
            "owners": options(ds.deals, "Owner code"),
            "row_count": len(ds.deals),
        },
        "work_orders": {
            "columns": [c for c in ds.work_orders.columns if not c.startswith("_")],
            "sectors": options(ds.work_orders, "Sector"),
            "execution_statuses": options(ds.work_orders, "Execution Status"),
            "nature_of_work": options(ds.work_orders, "Nature of Work"),
            "types_of_work": options(ds.work_orders, "Type of Work"),
            "row_count": len(ds.work_orders),
        },
        "supported_periods": [
            "this_quarter", "last_quarter", "this_month", "last_month", "this_fy",
            "last_fy", "ytd", "this_year", "last_6_months", "Q1_2026", "2025", "all",
        ],
    }


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """JSON-safe row dicts with money pre-formatted for the model."""
    out = []
    for _, row in df.iterrows():
        rec: dict[str, Any] = {}
        for col, val in row.items():
            if pd.isna(val):
                rec[col] = None
            elif isinstance(val, pd.Timestamp):
                rec[col] = val.strftime("%Y-%m-%d")
            elif isinstance(val, (int, float)):
                rec[col] = float(val)
                if any(k in col.lower() for k in ("value", "amount", "billed", "collected")):
                    rec[f"{col} (readable)"] = _money(val)
            else:
                rec[col] = str(val)
        out.append(rec)
    return out
