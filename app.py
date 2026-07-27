"""Skylark BI Agent — conversational interface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from agent import BIAgent, BRIEF_PROMPT  # noqa: E402
from data_source import load_from_local, load_from_monday  # noqa: E402
from monday_client import MondayError  # noqa: E402

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="centered")

STARTERS = [
    "How's our pipeline looking for the renewables sector?",
    "What's our revenue and collection status?",
    "Which sectors are performing best?",
    "How reliable is this data?",
]


def secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    import os

    return os.getenv(name, default)


@st.cache_resource(show_spinner=False)
def load_data(token: str, deals_id: str, wo_id: str, use_monday: bool):
    if use_monday:
        return load_from_monday(token, deals_id, wo_id)
    return load_from_local()


def render_history() -> None:
    for msg in st.session_state.display:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


def answer(agent: BIAgent, prompt: str) -> None:
    st.session_state.display.append({"role": "user", "content": prompt})
    st.session_state.convo.append({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        status = st.empty()
        text = ""
        failed = False

        for event in agent.run(st.session_state.convo):
            if event["type"] == "text":
                text += event["text"]
                placeholder.markdown(text)
            elif event["type"] == "tool":
                filters = {k: v for k, v in event["input"].items() if v not in (None, "")}
                shown = ", ".join(f"{k}={v}" for k, v in filters.items())
                status.caption(f"Querying `{event['name']}`" + (f" — {shown}" if shown else ""))
            elif event["type"] == "error":
                status.empty()
                st.error(event["text"])
                failed = True
            elif event["type"] == "done":
                st.session_state.convo = event["messages"]

        status.empty()
        if text:
            placeholder.markdown(text)
            st.session_state.display.append({"role": "assistant", "content": text})
        elif failed:
            # Drop the failed turn so the next question starts from clean state.
            st.session_state.convo = st.session_state.convo[:-1]
            st.session_state.display = st.session_state.display[:-1]


def main() -> None:
    st.title("Skylark BI Agent")
    st.caption("Ask business questions across the Deals and Work Orders boards.")

    token = secret("MONDAY_API_TOKEN")
    deals_id = secret("MONDAY_DEALS_BOARD_ID")
    wo_id = secret("MONDAY_WORK_ORDERS_BOARD_ID")
    api_key = secret("ANTHROPIC_API_KEY")
    use_monday = bool(token and deals_id and wo_id)

    try:
        loaded = load_data(token, deals_id, wo_id, use_monday)
    except (MondayError, FileNotFoundError) as exc:
        st.error(str(exc))
        st.info(
            "Set `MONDAY_API_TOKEN`, `MONDAY_DEALS_BOARD_ID` and "
            "`MONDAY_WORK_ORDERS_BOARD_ID` to read live boards. See the README."
        )
        st.stop()

    ds = loaded.dataset

    with st.sidebar:
        st.subheader("Data source")
        if loaded.source == "monday.com":
            st.success(loaded.detail)
        else:
            st.warning(loaded.detail)

        st.subheader("Loaded")
        c1, c2 = st.columns(2)
        c1.metric("Deals", len(ds.deals))
        c2.metric("Work orders", len(ds.work_orders))

        st.subheader("Data quality")
        dq, wq = ds.deals_quality, ds.wo_quality
        st.caption(
            f"Deals: {dq.duplicates_removed} duplicates removed · "
            f"deal value {dq.completeness('deal_value'):.0f}% complete"
        )
        st.caption(
            f"Work orders: {sum(wq.units_stripped.values())} values had units stripped · "
            f"{sum(wq.canonicalized.values())} labels normalized"
        )
        if not ds.join_info.get("joinable"):
            st.caption("Boards cannot be joined.")
        else:
            st.caption(
                f"Cross-board: {ds.join_info['matched_names']} of "
                f"{ds.join_info['work_order_names']} work order names match a deal."
            )

        if st.button("Clear conversation", use_container_width=True):
            st.session_state.convo = []
            st.session_state.display = []
            st.rerun()

        st.divider()
        st.caption("Read-only. All figures computed in pandas, not by the model.")

    if not api_key:
        st.error("No Anthropic API key found. Set `ANTHROPIC_API_KEY` to start.")
        st.stop()

    try:
        agent = BIAgent(ds, api_key=api_key)
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    st.session_state.setdefault("convo", [])
    st.session_state.setdefault("display", [])

    render_history()

    pending = None
    if not st.session_state.display:
        st.markdown("**Try asking:**")
        cols = st.columns(2)
        for i, starter in enumerate(STARTERS):
            if cols[i % 2].button(starter, key=f"s{i}", use_container_width=True):
                pending = starter
        if st.button("📋 Generate leadership brief", type="primary", use_container_width=True):
            pending = BRIEF_PROMPT

    typed = st.chat_input("Ask about pipeline, revenue, sectors, or data quality...")
    prompt = typed or pending

    if prompt:
        answer(agent, prompt)
        if pending:
            st.rerun()


if __name__ == "__main__":
    main()
