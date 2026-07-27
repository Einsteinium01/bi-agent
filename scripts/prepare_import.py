"""Produce monday.com-importable CSVs from the raw exports.

Deliberately minimal: this only repairs damage that would break the *import*
(structural noise, Excel error literals, all-empty columns). Semantic messiness
-- inconsistent casing, units glued into numbers, nulls, duplicate rows -- is
left intact so the agent's normalization layer handles it at query time, which
is what the brief actually asks for.
"""

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"

RAW_DEALS = ROOT / "Deal funnel Data.xlsx - Deal tracker.csv"
RAW_WOS = ROOT / "Work_Order_Tracker Data.xlsx - work order tracker.csv"

EXCEL_ERRORS = {"#VALUE!", "#REF!", "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!"}

# monday.com truncates and mangles very long column titles.
RENAME_WOS = {
    "Is any Skylark software platform part of the client deliverables in this deal?": "Software Platform Included",
    "Amount in Rupees (Excl of GST) (Masked)": "Order Value Excl GST",
    "Amount in Rupees (Incl of GST) (Masked)": "Order Value Incl GST",
    "Billed Value in Rupees (Excl of GST.) (Masked)": "Billed Value Excl GST",
    "Billed Value in Rupees (Incl of GST.) (Masked)": "Billed Value Incl GST",
    "Collected Amount in Rupees (Incl of GST.) (Masked)": "Collected Amount",
    "Amount to be billed in Rs. (Exl. of GST) (Masked)": "To Be Billed Excl GST",
    "Amount to be billed in Rs. (Incl. of GST) (Masked)": "To Be Billed Incl GST",
    "Amount Receivable (Masked)": "Amount Receivable",
}


def drop_embedded_headers(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Remove rows where the sheet's own header text was pasted back in as data."""
    matches = pd.Series(False, index=df.index)
    for col in df.columns:
        matches |= df[col].astype(str).str.strip() == str(col).strip()
    # A genuine data row won't echo three or more of its own column names.
    echo_count = sum(
        (df[col].astype(str).str.strip() == str(col).strip()).astype(int) for col in df.columns
    )
    bad = echo_count >= 3
    return df[~bad].copy(), int(bad.sum())


def scrub_excel_errors(df: pd.DataFrame) -> int:
    n = 0
    for col in df.columns:
        hits = df[col].isin(EXCEL_ERRORS)
        n += int(hits.sum())
        df.loc[hits, col] = pd.NA
    return n


def drop_empty_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    empty = [c for c in df.columns if df[c].isna().all()]
    return df.drop(columns=empty), empty


def clean(df: pd.DataFrame, label: str, rename: dict | None = None) -> pd.DataFrame:
    print(f"\n{label}: {df.shape[0]} rows x {df.shape[1]} cols")

    df, n_hdr = drop_embedded_headers(df)
    if n_hdr:
        print(f"  removed {n_hdr} embedded header row(s)")

    n_err = scrub_excel_errors(df)
    if n_err:
        print(f"  blanked {n_err} Excel error literal(s)")

    df, empties = drop_empty_columns(df)
    if empties:
        print(f"  dropped {len(empties)} all-empty column(s): {', '.join(empties)}")

    # Unnamed columns are spreadsheet padding artifacts.
    junk = [c for c in df.columns if str(c).startswith("Unnamed:")]
    if junk:
        df = df.drop(columns=junk)
        print(f"  dropped {len(junk)} unnamed padding column(s)")

    if rename:
        applied = {k: v for k, v in rename.items() if k in df.columns}
        df = df.rename(columns=applied)
        print(f"  renamed {len(applied)} column(s) for monday.com compatibility")

    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")

    dupes = int(df.duplicated().sum())
    if dupes:
        print(f"  NOTE: {dupes} exact duplicate row(s) retained - the agent flags these at query time")

    print(f"  -> {df.shape[0]} rows x {df.shape[1]} cols")
    return df


def main() -> int:
    for path in (RAW_DEALS, RAW_WOS):
        if not path.exists():
            print(f"ERROR: missing input file: {path.name}", file=sys.stderr)
            return 1

    OUT.mkdir(exist_ok=True)

    deals = clean(pd.read_csv(RAW_DEALS, dtype=str), "Deals")
    # The work order sheet has a blank spacer row above the real header.
    wos = clean(pd.read_csv(RAW_WOS, dtype=str, skiprows=1), "Work Orders", RENAME_WOS)

    deals_out = OUT / "monday_deals.csv"
    wos_out = OUT / "monday_work_orders.csv"
    deals.to_csv(deals_out, index=False)
    wos.to_csv(wos_out, index=False)

    print(f"\nWrote {deals_out.relative_to(ROOT)}")
    print(f"Wrote {wos_out.relative_to(ROOT)}")
    print("\nImport these two files into monday.com as separate boards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
