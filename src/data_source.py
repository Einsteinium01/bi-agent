"""Loads board data for the agent.

monday.com is the real source. The local CSV path exists so the app can be
developed and demonstrated without live board access; it reads the same files
that were imported into monday.com, so the normalization path is identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from analytics import Dataset
from monday_client import MondayClient, MondayError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DEALS = ROOT / "data" / "monday_deals.csv"
LOCAL_WORK_ORDERS = ROOT / "data" / "monday_work_orders.csv"


@dataclass
class LoadResult:
    dataset: Dataset
    source: str
    detail: str


def load_from_monday(token: str, deals_board_id: str, wo_board_id: str) -> LoadResult:
    client = MondayClient(api_token=token)
    account = client.verify_connection()
    deals = client.fetch_board(deals_board_id)
    work_orders = client.fetch_board(wo_board_id)

    if not deals.rows or not work_orders.rows:
        raise MondayError(
            f"A board came back empty ('{deals.board_name}': {len(deals.rows)} rows, "
            f"'{work_orders.board_name}': {len(work_orders.rows)} rows). "
            "Check that the CSVs were imported and the board IDs are correct."
        )

    return LoadResult(
        dataset=Dataset.from_boards(deals.rows, work_orders.rows),
        source="monday.com",
        detail=(
            f"Connected as {account}. Loaded '{deals.board_name}' "
            f"({len(deals.rows)} rows) and '{work_orders.board_name}' "
            f"({len(work_orders.rows)} rows)."
        ),
    )


def load_from_local() -> LoadResult:
    if not LOCAL_DEALS.exists() or not LOCAL_WORK_ORDERS.exists():
        raise FileNotFoundError(
            "Local CSVs not found. Run `python scripts/prepare_import.py` first."
        )

    deals = pd.read_csv(LOCAL_DEALS, dtype=str).to_dict("records")
    work_orders = pd.read_csv(LOCAL_WORK_ORDERS, dtype=str).to_dict("records")

    return LoadResult(
        dataset=Dataset.from_boards(deals, work_orders),
        source="local CSV",
        detail=(
            f"Reading local CSVs ({len(deals)} deals, {len(work_orders)} work orders). "
            "These are the same files imported into monday.com."
        ),
    )
