"""Normalization and data-quality profiling.

Turns raw monday.com rows into typed, canonical DataFrames and records every
repair it makes. The audit trail matters as much as the cleaning: the agent
cites it so a founder knows which numbers to trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

EXCEL_ERRORS = {"#VALUE!", "#REF!", "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!"}
NULL_TOKENS = {"", "-", "--", "n/a", "na", "none", "null", "nil", "tbd", "?", "#value!", "nan"}

DEALS_KEY_COLUMNS = [
    "Deal Name", "Client Code", "Deal Status", "Deal Stage",
    "Masked Deal value", "Sector/service", "Closure Probability",
    "Tentative Close Date", "Created Date",
]

WORK_ORDER_KEY_COLUMNS = [
    "Deal name masked", "Customer Name Code", "Execution Status", "Sector",
    "Order Value Excl GST", "Billed Value Excl GST", "Collected Amount",
    "Amount Receivable", "Invoice Status", "Date of PO/LOI",
]


class SchemaError(RuntimeError):
    """Raised when a board's columns don't match the expected import."""


DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
    "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%d-%b-%y", "%d/%m/%y",
    "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%b-%y", "%B %Y", "%b %Y",
]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Indian numbering shorthand seen in commercial trackers.
MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "l": 100_000, "lac": 100_000, "lacs": 100_000, "lakh": 100_000, "lakhs": 100_000,
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000,
}

SECTOR_CANON = {
    "renewables": "Renewables", "renewable": "Renewables", "renewable energy": "Renewables",
    "solar": "Renewables", "wind": "Renewables", "energy": "Renewables",
    "mining": "Mining", "mines": "Mining", "mineral": "Mining",
    "railways": "Railways", "railway": "Railways", "rail": "Railways",
    "powerline": "Powerline", "power line": "Powerline", "powerlines": "Powerline",
    "transmission": "Powerline", "power": "Powerline",
    "construction": "Construction", "infra": "Construction", "infrastructure": "Construction",
    "dsp": "DSP", "tender": "Tender", "tenders": "Tender",
    "manufacturing": "Manufacturing",
    "security and surveillance": "Security & Surveillance",
    "security & surveillance": "Security & Surveillance",
    "surveillance": "Security & Surveillance", "security": "Security & Surveillance",
    "aviation": "Aviation", "others": "Others", "other": "Others", "misc": "Others",
}

STATUS_CANON = {
    "won": "Won", "win": "Won", "closed won": "Won", "closed-won": "Won",
    "dead": "Dead", "lost": "Dead", "closed lost": "Dead", "closed-lost": "Dead",
    "open": "Open", "active": "Open", "in progress": "Open",
    "on hold": "On Hold", "onhold": "On Hold", "hold": "On Hold", "paused": "On Hold",
}

EXEC_STATUS_CANON = {
    "completed": "Completed", "complete": "Completed", "done": "Completed",
    "ongoing": "Ongoing", "in progress": "Ongoing", "executing": "Ongoing",
    "executed until current month": "Executed Until Current Month",
    "not started": "Not Started", "yet to start": "Not Started",
    "partial completed": "Partially Completed", "partially completed": "Partially Completed",
    "pause / struck": "Paused/Stuck", "pause/struck": "Paused/Stuck",
    "paused": "Paused/Stuck", "stuck": "Paused/Stuck", "struck": "Paused/Stuck",
    "details pending from client": "Details Pending From Client",
}

BILLING_CANON = {
    "fully billed": "Fully Billed", "billed": "Fully Billed",
    "partially billed": "Partially Billed", "partial": "Partially Billed",
    "not billed yet": "Not Billed", "not billed": "Not Billed",
    "not billable": "Not Billable", "update required": "Update Required",
    "stuck": "Stuck",
}

# Deal stages carry an ordering prefix (A..O) that encodes funnel position.
STAGE_ORDER = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "I": 9, "J": 10, "K": 11, "L": 12, "M": 13, "N": 14, "O": 15,
}
WON_STAGES = {"G. Project Won", "H. Work Order Received", "J. Invoice sent",
              "K. Amount Accrued", "Project Completed"}
LOST_STAGES = {"L. Project Lost", "N. Not relevant at the moment", "O. Not Relevant at all"}


@dataclass
class QualityReport:
    """Everything the cleaner had to repair, guess at, or give up on."""

    board: str
    total_rows: int = 0
    usable_rows: int = 0
    duplicates_removed: int = 0
    embedded_headers_removed: int = 0
    null_counts: dict[str, int] = field(default_factory=dict)
    unparsed_dates: dict[str, int] = field(default_factory=dict)
    unparsed_numbers: dict[str, int] = field(default_factory=dict)
    units_stripped: dict[str, int] = field(default_factory=dict)
    canonicalized: dict[str, int] = field(default_factory=dict)
    negative_values: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def completeness(self, column: str) -> float:
        if not self.usable_rows:
            return 0.0
        return 100.0 * (1 - self.null_counts.get(column, 0) / self.usable_rows)

    def caveats(self, columns: list[str] | None = None, threshold: float = 25.0) -> list[str]:
        """Human-readable warnings for the columns a given answer depends on."""
        out: list[str] = []
        for col in columns or list(self.null_counts):
            missing = 100.0 - self.completeness(col)
            if missing >= threshold:
                out.append(f"'{col}' is {missing:.0f}% empty in {self.board}")
        for col, n in self.negative_values.items():
            if columns is None or col in columns:
                out.append(f"'{col}' contains {n} negative value(s) in {self.board} (likely over-billing)")
        for col, n in self.unparsed_dates.items():
            if (columns is None or col in columns) and n:
                out.append(f"{n} unreadable date(s) in '{col}' were excluded")
        if self.duplicates_removed:
            out.append(f"{self.duplicates_removed} duplicate row(s) removed from {self.board}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "rows_received": self.total_rows,
            "rows_usable": self.usable_rows,
            "duplicates_removed": self.duplicates_removed,
            "embedded_headers_removed": self.embedded_headers_removed,
            "column_completeness_pct": {
                c: round(self.completeness(c), 1) for c in sorted(self.null_counts)
            },
            "unparsed_dates": {k: v for k, v in self.unparsed_dates.items() if v},
            "unparsed_numbers": {k: v for k, v in self.unparsed_numbers.items() if v},
            "units_stripped": self.units_stripped,
            "values_canonicalized": self.canonicalized,
            "negative_values": self.negative_values,
            "notes": self.notes,
        }


def is_null(value: Any) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True
    return str(value).strip().lower() in NULL_TOKENS


def parse_date(value: Any) -> pd.Timestamp | None:
    """Parse a date across the formats these trackers actually use."""
    if is_null(value):
        return None
    raw = str(value).strip()

    for fmt in DATE_FORMATS:
        try:
            return pd.Timestamp(datetime.strptime(raw, fmt))
        except ValueError:
            continue

    # Bare month names ("Dec", "November") appear in billing-month columns.
    key = re.sub(r"[^a-z]", "", raw.lower())
    if key in MONTHS:
        return None  # month-only: no year, ambiguous. Handled by parse_month.

    try:
        parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True)
        return None if pd.isna(parsed) else pd.Timestamp(parsed)
    except (ValueError, TypeError):
        return None


def parse_month(value: Any) -> int | None:
    """Extract a month number from free text like 'Dec' or 'November'."""
    if is_null(value):
        return None
    key = re.sub(r"[^a-z]", "", str(value).lower())
    return MONTHS.get(key)


def parse_number(value: Any) -> tuple[float | None, bool]:
    """Parse a numeric value. Returns (number, had_unit_or_symbol_stripped)."""
    if is_null(value):
        return None, False
    raw = str(value).strip()

    if raw.upper() in EXCEL_ERRORS:
        return None, False

    # monday's dropdown columns re-join comma-split values with a space
    # ("1,310.850" -> "1, 310.850"), which the trailing-unit rules below would
    # otherwise read as the number 1 followed by noise.
    degrouped = re.sub(r",\s*", "", raw)

    cleaned = degrouped.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    cleaned = cleaned.replace("INR", "").replace("$", "").strip()
    stripped = cleaned != degrouped

    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]

    # Indian shorthand: "4.5L", "2 Cr".
    match = re.match(r"^(-?\d*\.?\d+)\s*([a-zA-Z]+)$", cleaned)
    if match:
        num, suffix = match.groups()
        mult = MULTIPLIERS.get(suffix.lower())
        if mult:
            result = float(num) * mult
            return (-result if negative else result), True
        # A trailing unit that isn't a multiplier ("5360 HA") is just noise.
        try:
            result = float(num)
            return (-result if negative else result), True
        except ValueError:
            return None, True

    # Leading number with a trailing unit word: "5360 HA".
    match = re.match(r"^(-?[\d.]+)\s+\S+$", cleaned)
    if match:
        try:
            result = float(match.group(1))
            return (-result if negative else result), True
        except ValueError:
            return None, True

    try:
        result = float(cleaned)
        return (-result if negative else result), stripped
    except ValueError:
        return None, False


def canon(value: Any, mapping: dict[str, str]) -> tuple[str | None, bool]:
    """Map a messy label to its canonical form. Returns (value, was_changed)."""
    if is_null(value):
        return None, False
    raw = str(value).strip()
    key = re.sub(r"\s+", " ", raw.lower()).strip()

    if key in mapping:
        return mapping[key], mapping[key] != raw

    # Tolerate punctuation drift and stray suffixes.
    loose = re.sub(r"[^a-z0-9 &]", "", key).strip()
    if loose in mapping:
        return mapping[loose], True
    for known, canonical in mapping.items():
        if loose == re.sub(r"[^a-z0-9 &]", "", known).strip():
            return canonical, True

    return raw.title() if raw.islower() or raw.isupper() else raw, raw != raw.strip()


def _drop_embedded_headers(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0
    echoes = sum(
        (df[c].astype(str).str.strip() == str(c).strip()).astype(int) for c in df.columns
    )
    bad = echoes >= 3
    return df[~bad].copy(), int(bad.sum())


def _adopt_item_name(df: pd.DataFrame, name_col: str) -> pd.DataFrame:
    """Reconcile monday's item-title column with the CSV's name column.

    The API returns the item title as 'Name'; the source CSVs call it
    'Deal Name' / 'Deal name masked'. Downstream joins key on the CSV name, so
    without this the cross-board join silently matches nothing on live data.
    """
    if name_col in df.columns or "Name" not in df.columns:
        return df
    df = df.rename(columns={"Name": name_col})
    # monday titles a blank-named imported row 'Unnamed'; left as-is, two such
    # rows on opposite boards would join to each other.
    df[name_col] = df[name_col].where(
        df[name_col].astype(str).str.strip().str.lower() != "unnamed"
    )
    return df


def _require_columns(df: pd.DataFrame, board: str, expected: list[str]) -> None:
    """Fail loudly when a board's schema doesn't look like the expected import.

    A mis-imported board (wrong header row) yields columns named after data
    values. Normalization would then quietly drop every field it can't find and
    return a plausible-looking but wrong frame.
    """
    missing = [c for c in expected if c not in df.columns]
    if len(missing) > len(expected) // 2:
        raise SchemaError(
            f"The {board} board does not match the expected import. "
            f"Missing {len(missing)} of {len(expected)} key columns "
            f"(e.g. {', '.join(missing[:4])}). "
            "This usually means the CSV was imported with the wrong header row "
            "— re-import it and set the header to row 1."
        )


def _clean_frame(rows: list[dict], board: str) -> tuple[pd.DataFrame, QualityReport]:
    report = QualityReport(board=board, total_rows=len(rows))
    df = pd.DataFrame(rows)
    if df.empty:
        return df, report

    df, n_hdr = _drop_embedded_headers(df)
    report.embedded_headers_removed = n_hdr

    # Ignore monday's internal id when deciding what counts as a duplicate.
    business_cols = [c for c in df.columns if c != "_item_id"]
    before = len(df)
    df = df.drop_duplicates(subset=business_cols, keep="first")
    report.duplicates_removed = before - len(df)

    df = df.reset_index(drop=True)
    report.usable_rows = len(df)
    return df, report


def _norm_dates(df, report, cols):
    for col in cols:
        if col not in df.columns:
            continue
        parsed = df[col].apply(parse_date)
        attempted = df[col].apply(lambda v: not is_null(v))
        report.unparsed_dates[col] = int((attempted & parsed.isna()).sum())
        df[col] = parsed


def _norm_numbers(df, report, cols):
    for col in cols:
        if col not in df.columns:
            continue
        results = df[col].apply(parse_number)
        values = results.apply(lambda r: r[0])
        units = int(results.apply(lambda r: r[1]).sum())
        attempted = df[col].apply(lambda v: not is_null(v))
        report.unparsed_numbers[col] = int((attempted & values.isna()).sum())
        if units:
            report.units_stripped[col] = units
        neg = int((values < 0).sum())
        if neg:
            report.negative_values[col] = neg
        df[col] = values


def _norm_cats(df, report, mapping_by_col):
    for col, mapping in mapping_by_col.items():
        if col not in df.columns:
            continue
        results = df[col].apply(lambda v: canon(v, mapping))
        changed = int(results.apply(lambda r: r[1]).sum())
        if changed:
            report.canonicalized[col] = changed
        df[col] = results.apply(lambda r: r[0])


def _count_nulls(df, report):
    for col in df.columns:
        if col == "_item_id":
            continue
        report.null_counts[col] = int(df[col].isna().sum())


def normalize_deals(rows: list[dict]) -> tuple[pd.DataFrame, QualityReport]:
    df, report = _clean_frame(rows, "Deals")
    if df.empty:
        return df, report

    df = _adopt_item_name(df, "Deal Name")
    _require_columns(df, "Deals", DEALS_KEY_COLUMNS)

    _norm_dates(df, report, ["Close Date (A)", "Tentative Close Date", "Created Date"])
    _norm_numbers(df, report, ["Masked Deal value"])
    _norm_cats(df, report, {
        "Sector/service": SECTOR_CANON,
        "Deal Status": STATUS_CANON,
        "Closure Probability": {"high": "High", "medium": "Medium", "low": "Low"},
    })

    if "Deal Stage" in df.columns:
        df["Deal Stage"] = df["Deal Stage"].apply(lambda v: None if is_null(v) else str(v).strip())
        df["stage_order"] = df["Deal Stage"].apply(
            lambda s: STAGE_ORDER.get(str(s)[0].upper()) if s and re.match(r"^[A-O]\.", str(s)) else None
        )
        df["stage_bucket"] = df["Deal Stage"].apply(
            lambda s: "Won" if s in WON_STAGES else ("Lost" if s in LOST_STAGES else ("Active" if s else None))
        )
        unlettered = int(df["Deal Stage"].notna().sum() - df["stage_order"].notna().sum())
        if unlettered:
            report.notes.append(
                f"{unlettered} deal(s) use a stage label without the A-O ordering prefix "
                "(e.g. 'Project Completed'); these are bucketed by meaning, not position."
            )

    # A single effective close date simplifies every downstream time filter.
    if "Close Date (A)" in df.columns and "Tentative Close Date" in df.columns:
        filled = int(df["Close Date (A)"].isna().sum() - df["Tentative Close Date"].isna().sum())
        df["effective_close_date"] = df["Close Date (A)"].fillna(df["Tentative Close Date"])
        report.notes.append(
            "'effective_close_date' falls back to the tentative close date when the actual "
            f"one is missing ({max(filled, 0)} rows rely on the tentative date). Timing-based "
            "answers are therefore forecasts, not confirmed closes."
        )

    if "Masked Deal value" in df.columns:
        df = df.rename(columns={"Masked Deal value": "deal_value"})
        missing = int(df["deal_value"].isna().sum())
        if missing:
            report.notes.append(
                f"{missing} of {len(df)} deals have no value. Value-based totals cover only "
                "the remaining deals and understate the true pipeline."
            )

    _count_nulls(df, report)
    return df, report


def normalize_work_orders(rows: list[dict]) -> tuple[pd.DataFrame, QualityReport]:
    df, report = _clean_frame(rows, "Work Orders")
    if df.empty:
        return df, report

    df = _adopt_item_name(df, "Deal name masked")
    _require_columns(df, "Work Orders", WORK_ORDER_KEY_COLUMNS)

    _norm_dates(df, report, [
        "Data Delivery Date", "Date of PO/LOI", "Probable Start Date",
        "Probable End Date", "Last invoice date",
    ])
    _norm_numbers(df, report, [
        "Order Value Excl GST", "Order Value Incl GST", "Billed Value Excl GST",
        "Billed Value Incl GST", "Collected Amount", "To Be Billed Excl GST",
        "To Be Billed Incl GST", "Amount Receivable", "Quantity by Ops",
        "Quantities as per PO", "Quantity billed (till date)", "Balance in quantity",
    ])
    _norm_cats(df, report, {
        "Sector": SECTOR_CANON,
        "Execution Status": EXEC_STATUS_CANON,
        "Invoice Status": BILLING_CANON,
        "Billing Status": BILLING_CANON,
        "WO Status (billed)": {"open": "Open", "closed": "Closed"},
    })

    for col in ("Last executed month of recurring project", "Actual Billing Month"):
        if col in df.columns:
            df[f"{col} (month#)"] = df[col].apply(parse_month)

    if "Amount Receivable" in df.columns and report.negative_values.get("Amount Receivable"):
        report.notes.append(
            f"{report.negative_values['Amount Receivable']} work order(s) show a negative "
            "receivable, meaning billed value exceeds order value. Treated as real "
            "over-billing, not corrected."
        )

    _count_nulls(df, report)
    return df, report


def join_caveat(deals: pd.DataFrame, work_orders: pd.DataFrame) -> dict[str, Any]:
    """Describe how reliably the two boards can be joined.

    The boards use different client-code namespaces (COMPANY### vs
    WOCOMPANY_###), so deal name is the only available bridge -- and it is
    neither unique nor complete.
    """
    if deals.empty or work_orders.empty:
        return {"joinable": False, "reason": "one or both boards are empty"}

    d_names = set(deals.get("Deal Name", pd.Series(dtype=str)).dropna())
    w_names = set(work_orders.get("Deal name masked", pd.Series(dtype=str)).dropna())
    matched = d_names & w_names

    dupes = 0
    if "Deal Name" in deals.columns:
        counts = deals["Deal Name"].value_counts()
        dupes = int((counts > 1).sum())

    return {
        "joinable": bool(matched),
        "join_key": "Deal Name <-> Deal name masked",
        "work_order_names": len(w_names),
        "matched_names": len(matched),
        "unmatched_work_order_names": sorted(w_names - d_names),
        "warning": (
            "Client codes use different namespaces across boards (COMPANY### vs "
            "WOCOMPANY_###), so deal name is the only join key. "
            f"{len(w_names) - len(matched)} of {len(w_names)} work order names have no "
            f"matching deal, and {dupes} deal names appear on multiple rows, making this "
            "a many-to-many join. Cross-board figures are indicative, not exact."
        ),
    }
