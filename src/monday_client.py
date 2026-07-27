"""monday.com GraphQL API v2 client.

Read-only. Fetches every item from a board via cursor pagination and flattens
column values into plain dicts. Retries on rate limits and transient server
errors; surfaces anything else as MondayError for the UI to render.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.monday.com/v2"
API_VERSION = "2024-10"
PAGE_SIZE = 100
MAX_RETRIES = 4
TIMEOUT = 45


class MondayError(RuntimeError):
    """Raised when monday.com cannot serve a request."""


ITEMS_QUERY = """
query ($boardId: [ID!], $limit: Int!, $cursor: String) {
  boards(ids: $boardId) {
    id
    name
    items_page(limit: $limit, cursor: $cursor) {
      cursor
      items {
        id
        name
        column_values { id text type ... on BoardRelationValue { display_value } }
      }
    }
  }
}
"""

COLUMNS_QUERY = """
query ($boardId: [ID!]) {
  boards(ids: $boardId) { id name columns { id title type } }
}
"""


@dataclass
class BoardData:
    board_id: str
    board_name: str
    rows: list[dict[str, Any]]
    columns: list[dict[str, str]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


class MondayClient:
    """Read-only monday.com client with pagination, retries and a TTL cache."""

    def __init__(self, api_token: str | None = None, cache_ttl: int = 300):
        self.api_token = api_token or os.getenv("MONDAY_API_TOKEN", "")
        if not self.api_token:
            raise MondayError(
                "No monday.com API token. Set MONDAY_API_TOKEN in your environment "
                "or Streamlit secrets."
            )
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, BoardData]] = {}
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": self.api_token,
                "Content-Type": "application/json",
                "API-Version": API_VERSION,
            }
        )

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        last_error: str | None = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = self._session.post(API_URL, json=payload, timeout=TIMEOUT)
            except requests.Timeout:
                last_error = "request timed out"
                time.sleep(2**attempt)
                continue
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                # monday.com returns Retry-After on complexity/rate limits.
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                log.warning("rate limited, sleeping %ss", wait)
                time.sleep(min(wait, 30))
                last_error = "rate limited"
                continue

            if resp.status_code in (500, 502, 503, 504):
                last_error = f"server error {resp.status_code}"
                time.sleep(2**attempt)
                continue

            if resp.status_code == 401:
                raise MondayError("monday.com rejected the API token (401). Check MONDAY_API_TOKEN.")

            if resp.status_code != 200:
                raise MondayError(f"monday.com returned HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                body = resp.json()
            except json.JSONDecodeError:
                raise MondayError("monday.com returned a non-JSON response.") from None

            if "errors" in body and body["errors"]:
                msg = "; ".join(e.get("message", str(e)) for e in body["errors"])
                # Complexity budget exhaustion is retryable; other GraphQL errors are not.
                if "complexity" in msg.lower():
                    last_error = msg
                    time.sleep(2**attempt + 2)
                    continue
                raise MondayError(f"monday.com GraphQL error: {msg}")

            return body.get("data") or {}

        raise MondayError(f"monday.com unreachable after {MAX_RETRIES} attempts ({last_error}).")

    def fetch_board(self, board_id: str, use_cache: bool = True) -> BoardData:
        """Fetch all items from a board, following the pagination cursor."""
        board_id = str(board_id).strip()
        now = time.monotonic()

        if use_cache and board_id in self._cache:
            cached_at, data = self._cache[board_id]
            if now - cached_at < self.cache_ttl:
                log.info("cache hit for board %s (%d rows)", board_id, len(data))
                return data

        meta = self._post(COLUMNS_QUERY, {"boardId": [board_id]})
        boards = meta.get("boards") or []
        if not boards:
            raise MondayError(
                f"Board {board_id} not found, or the token lacks access to it. "
                "Confirm the board ID from its URL."
            )
        board_name = boards[0].get("name", f"Board {board_id}")
        columns = [
            {"id": c["id"], "title": c["title"], "type": c["type"]}
            for c in boards[0].get("columns", [])
        ]
        title_by_id = {c["id"]: c["title"] for c in columns}

        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0

        while True:
            data = self._post(
                ITEMS_QUERY, {"boardId": [board_id], "limit": PAGE_SIZE, "cursor": cursor}
            )
            page_boards = data.get("boards") or []
            if not page_boards:
                break
            page = page_boards[0].get("items_page") or {}

            for item in page.get("items") or []:
                row: dict[str, Any] = {"_item_id": item.get("id"), "Name": item.get("name")}
                for cv in item.get("column_values") or []:
                    title = title_by_id.get(cv["id"], cv["id"])
                    value = cv.get("display_value") or cv.get("text")
                    row[title] = value if value not in ("", None) else None
                rows.append(row)

            pages += 1
            cursor = page.get("cursor")
            if not cursor:
                break
            if pages > 200:
                log.warning("stopping pagination at %d pages for board %s", pages, board_id)
                break

        result = BoardData(board_id=board_id, board_name=board_name, rows=rows, columns=columns)
        self._cache[board_id] = (now, result)
        log.info("fetched board %s (%s): %d rows over %d page(s)", board_id, board_name, len(rows), pages)
        return result

    def invalidate_cache(self) -> None:
        self._cache.clear()

    def verify_connection(self) -> str:
        """Return the authenticated account name, or raise MondayError."""
        data = self._post("query { me { name email } }", {})
        me = data.get("me") or {}
        return me.get("name") or me.get("email") or "unknown account"
