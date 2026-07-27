"""The BI agent: tool definitions and the Anthropic tool-use loop.

The model chooses which analysis to run and with what filters; every number it
reports comes back from analytics.py. It is never asked to do arithmetic on raw
rows, which is what keeps reported revenue figures trustworthy.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Iterator

import anthropic

import analytics as an
from analytics import Dataset

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 8000
MAX_TOOL_ROUNDS = 8

PERIOD_DESC = (
    "Time period. One of: this_quarter, last_quarter, this_month, last_month, "
    "this_fy, last_fy, ytd, this_year, last_N_months, Q1_2026, 2025, all. "
    "Omit for all time."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_dimensions",
        "description": (
            "List the columns and the actual filter values present on both boards "
            "(sectors, statuses, stages, owners) plus supported time periods. Call "
            "this first when unsure whether a sector or status the user mentioned "
            "exists, so you filter on real values instead of guessing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_deals",
        "description": (
            "Query the Deals board (sales pipeline). Returns a summary with totals "
            "and the largest matching deals. Use for questions about specific deals, "
            "sectors, owners, or deal stages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "e.g. Renewables, Mining, Railways, Powerline"},
                "status": {"type": "string", "description": "Won, Dead, Open, or On Hold"},
                "stage": {"type": "string", "description": "Funnel stage, e.g. 'E. Proposal' or 'Negotiations'"},
                "owner": {"type": "string", "description": "Owner code, e.g. OWNER_003"},
                "probability": {"type": "string", "description": "High, Medium, or Low"},
                "period": {"type": "string", "description": PERIOD_DESC},
                "limit": {"type": "integer", "description": "Max deals to list (default 15)"},
            },
        },
    },
    {
        "name": "query_work_orders",
        "description": (
            "Query the Work Orders board (project execution and billing). Returns "
            "order value, billed value, collections and receivables. Use for "
            "questions about delivery, invoicing, or cash collection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string"},
                "execution_status": {"type": "string", "description": "Completed, Ongoing, Not Started, Paused/Stuck"},
                "billing_status": {"type": "string", "description": "Fully Billed, Partially Billed, Not Billed"},
                "nature_of_work": {"type": "string", "description": "One time Project, Monthly Contract, Annual Rate Contract, Proof of Concept"},
                "owner": {"type": "string"},
                "period": {"type": "string", "description": PERIOD_DESC},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "aggregate",
        "description": (
            "Group either board by any column and compute a metric. Use for "
            "ranking questions: revenue by sector, deal count by owner, order "
            "value by type of work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "enum": ["deals", "work_orders"]},
                "group_by": {"type": "string", "description": "Column name to group by. Check list_dimensions if unsure."},
                "metric": {"type": "string", "enum": ["total_value", "average_value", "median_value", "count"]},
                "period": {"type": "string", "description": PERIOD_DESC},
                "sector": {"type": "string", "description": "Optional sector filter before grouping"},
                "top_n": {"type": "integer"},
            },
            "required": ["board", "group_by"],
        },
    },
    {
        "name": "pipeline_health",
        "description": (
            "Assess pipeline health: open pipeline value, a probability-weighted "
            "forecast, win rate, the funnel broken down by stage, and deals whose "
            "expected close date has already passed. Use for 'how's the pipeline "
            "looking' style questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string"},
                "period": {"type": "string", "description": PERIOD_DESC},
            },
        },
    },
    {
        "name": "revenue_summary",
        "description": (
            "Revenue funnel from the work order board: booked vs billed vs "
            "collected, outstanding receivables, and billing/collection rates. "
            "Use for revenue, cash flow, and receivables questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string"},
                "period": {"type": "string", "description": PERIOD_DESC},
            },
        },
    },
    {
        "name": "cross_board_view",
        "description": (
            "Compare won deals against actual work orders to spot conversion gaps. "
            "Always returns a join warning describing how reliable the linkage is; "
            "relay that caveat to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sector": {"type": "string"}},
        },
    },
    {
        "name": "data_quality_report",
        "description": (
            "Full data quality audit of both boards: row counts, per-column "
            "completeness, values that could not be parsed, duplicates removed, "
            "and cross-board join reliability. Use when the user asks how "
            "trustworthy the data is, or to justify a caveat."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

SYSTEM_PROMPT = """You are a business intelligence analyst for Skylark Drones, a drone services company working across mining, renewables, railways, powerline, and construction sectors. You answer questions from founders and executives using live data from two monday.com boards: Deals (sales pipeline) and Work Orders (project execution and billing).

## How to work

Call tools to get numbers. Never estimate, extrapolate, or compute figures yourself — every number in your answer must come from a tool result. If a tool did not give you a figure, say you don't have it rather than inferring one.

Prefer the purpose-built tools over raw queries: `pipeline_health` for pipeline questions, `revenue_summary` for revenue and cash questions, `aggregate` for rankings. Call several tools when a question spans both boards.

When a user names a sector or status you are unsure about, call `list_dimensions` first rather than guessing a filter value that may not exist.

## Data caveats are part of the answer, not a footnote

This data is genuinely messy and you must be honest about it:

- About half the deals have no deal value. Any value-based total covers only the deals that have one and therefore understates the true figure. Say so whenever you quote a pipeline total.
- Client codes use different namespaces across the two boards, so they can only be joined on deal name — an imperfect, many-to-many link. Cross-board numbers are indicative, not exact.
- Some work orders show negative receivables. That is real over-billing, not a data error.
- Where a tool returns a `caveats` list, work the relevant ones into your answer naturally.

State the caveat once, in plain language, near the number it affects. Do not bury it or repeat it three times.

## How to answer

Lead with the direct answer to what was asked. Then give the two or three numbers that support it, and what they imply — a founder wants to know what to do, not just what the total is. Point out anything genuinely notable: a concentration risk, a stalled deal, a collection gap.

Use Indian numbering (Rs X Cr / Rs X L) as the tools return it. Keep it tight — a few short paragraphs or a small table, not an essay. Use markdown.

If a question is genuinely ambiguous in a way that changes the answer — an unspecified time period when trends matter, or a sector name matching nothing in the data — ask one clarifying question. Otherwise make a sensible assumption, state it in one line, and answer."""

BRIEF_PROMPT = """Produce a leadership brief for this week. Gather the data first: overall pipeline health, revenue and collections, sector performance for both boards, and the data quality report.

Structure it as:

## Headline
Two or three sentences: where the business stands right now.

## Pipeline
Open pipeline value, weighted forecast, win rate. Which sectors are carrying it.

## Revenue & Collections
Booked vs billed vs collected. Outstanding receivables and what they imply.

## Execution
Work order status — what's completed, ongoing, stalled.

## Watch List
Three to five specific items needing attention: overdue deals, concentration risk, collection gaps. Be concrete — name the sector or deal and the number.

## Data Caveats
What in this brief is uncertain and why. Brief and factual.

Write it for a founder skimming before a board call: specific numbers, no filler."""


def build_tool_registry(ds: Dataset) -> dict[str, Callable[..., Any]]:
    return {
        "list_dimensions": lambda **kw: an.list_dimensions(ds),
        "query_deals": lambda **kw: an.query_deals(ds, **kw),
        "query_work_orders": lambda **kw: an.query_work_orders(ds, **kw),
        "aggregate": lambda **kw: an.aggregate(ds, **kw),
        "pipeline_health": lambda **kw: an.pipeline_health(ds, **kw),
        "revenue_summary": lambda **kw: an.revenue_summary(ds, **kw),
        "cross_board_view": lambda **kw: an.cross_board_view(ds, **kw),
        "data_quality_report": lambda **kw: an.data_quality_report(ds),
    }


class BIAgent:
    def __init__(self, dataset: Dataset, api_key: str | None = None):
        key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY in your environment "
                "or Streamlit secrets."
            )
        self.client = anthropic.Anthropic(api_key=key)
        self.dataset = dataset
        self.tools = build_tool_registry(dataset)

    def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        fn = self.tools.get(name)
        if fn is None:
            return json.dumps({"error": f"Unknown tool '{name}'."})
        try:
            return json.dumps(fn(**args), default=str)
        except TypeError as exc:
            # Bad argument from the model: tell it what went wrong so it can retry.
            return json.dumps({"error": f"Invalid arguments for {name}: {exc}"})
        except Exception as exc:
            log.exception("tool %s failed", name)
            return json.dumps({"error": f"{name} failed: {exc}"})

    def run(self, messages: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """Run the tool-use loop, yielding events for the UI to render.

        Events: {"type": "tool", "name": ..., "input": ...} and
        {"type": "text", "text": ...} and {"type": "done", "messages": ...}.
        """
        convo = list(messages)

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                with self.client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    thinking={"type": "adaptive"},
                    tools=TOOLS,
                    messages=convo,
                ) as stream:
                    for text in stream.text_stream:
                        yield {"type": "text", "text": text}
                    response = stream.get_final_message()
            except anthropic.RateLimitError:
                yield {"type": "error", "text": "Rate limited by the Anthropic API. Wait a moment and try again."}
                return
            except anthropic.AuthenticationError:
                yield {"type": "error", "text": "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY."}
                return
            except anthropic.APIError as exc:
                yield {"type": "error", "text": f"Anthropic API error: {exc}"}
                return

            convo.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                yield {"type": "done", "messages": convo}
                return

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield {"type": "tool", "name": block.name, "input": block.input}
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": self._run_tool(block.name, dict(block.input)),
                    }
                )
            convo.append({"role": "user", "content": results})

        yield {
            "type": "error",
            "text": f"Stopped after {MAX_TOOL_ROUNDS} rounds of tool calls without a final answer.",
        }
