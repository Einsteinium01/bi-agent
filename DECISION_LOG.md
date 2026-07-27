# Decision Log — Skylark BI Agent

## Key assumptions

**"Energy sector" means Renewables.** The sample query in the brief asks about energy, but no such sector exists in the data. Renewables (109 deals after deduplication, the largest sector) is the closest match. The agent resolves fuzzy sector names against actual values rather than returning an empty result, and calls `list_dimensions` when uncertain.

**Financial year runs April–March.** Standard Indian convention, consistent with the `SDPL/FY25-26/` invoice numbering in the work order data.

**Masked values are proportionally consistent.** Both boards state values are masked. I assume the masking preserves relative magnitude, so rankings and ratios are meaningful even if absolute figures are not real. All comparative analysis rests on this.

**Deal value is opportunity size; order value is contracted amount.** These are not directly comparable — different definitions, and one excludes GST. `cross_board_view` says so explicitly rather than presenting the gap as a conversion loss.

**Probability labels map to weights of High=75%, Medium=45%, Low=20%.** No probabilities are given numerically. These are conventional B2B sales values. The tool always reports the basis alongside the forecast (`"6 of 43 open deals have both a value and a probability label"`) so the user can judge it.

**A stage without the A–O prefix is bucketed by meaning.** 18 deals use `Project Completed` with no ordering letter. Rather than dropping them from funnel analysis, they are bucketed as Won and the discrepancy is noted in the quality report.

---

## Trade-offs

### The model never computes numbers

**Chosen:** the LLM selects tools and filters; pandas computes every figure.

The alternative — hand the model raw rows and let it analyze — is faster to build and more flexible. It is also how BI agents produce confidently wrong revenue numbers. With 344 deals and 176 work orders, asking a model to sum a column is asking for silent arithmetic errors that no user can detect.

The cost is rigidity: a question no tool covers cannot be answered. I judged that acceptable. An agent that says "I can't answer that" is more useful to a founder than one that guesses. The tools were scoped to cover the query categories the brief names — revenue, pipeline health, sectoral performance, operational metrics.

### Direct GraphQL API rather than MCP

**Chosen:** monday.com's GraphQL v2 API directly.

MCP would have been less code. But the read pattern here needs cursor pagination over `items_page`, retry logic distinguishing rate limits from complexity-budget exhaustion (monday returns these differently and only one is worth retrying), and a TTL cache so a five-question conversation doesn't trigger fifteen full board fetches. Direct API access made all three straightforward. The brief permits either.

### Clean at query time, not at import

**Chosen:** import near-raw data; normalize on every read.

I fix only what would break the import itself — structural noise, Excel error literals, empty columns. Everything else (casing drift, units in numeric fields, nulls, duplicates) stays in monday.com and is handled by the normalization layer.

Two reasons. First, the brief says the agent must handle messy data gracefully — pre-cleaning it would be answering a different question. Second, it is realistic: production boards are edited by humans continuously, so an agent that only works against a one-time-cleaned snapshot is not an agent you can deploy.

The cost is per-query normalization work. At this data size it is milliseconds, and the TTL cache absorbs repeated reads.

### Data quality as a first-class output

**Chosen:** the normalizer records every repair and returns a structured audit the agent can cite.

It would have been simpler to clean silently. But when half the deals have no value, a pipeline total is *materially misleading* without that context. A founder told "pipeline is Rs 23 Cr" makes different decisions than one told "Rs 23 Cr across the 165 deals that have a value; 167 more have none."

So the quality report is a tool the agent can call, and the system prompt requires stating the caveat next to the number it affects — once, in plain language, not as a disclaimer wall.

### Streamlit over a custom frontend

**Chosen:** Streamlit, hosted on Community Cloud.

Native chat components, streaming support, and free public hosting with secrets management. Within a six-hour budget, a React frontend plus a FastAPI backend plus deployment would have consumed the time that went into the normalization and analytics layers — which is where the assignment's actual difficulty lies. The trade-off is limited UI control.

### Claude Opus 4.8 with adaptive thinking

**Chosen:** the strongest available model.

Query interpretation here is genuinely hard: mapping "how's energy looking" onto a Renewables filter, deciding whether a question needs one board or both, and judging which caveats matter for a given answer. A weaker model produces worse tool selection, and bad tool selection means a wrong answer delivered confidently. Adaptive thinking lets the model reason more on multi-step questions without a fixed token budget.

---

## Interpreting "the agent should help prepare data for leadership updates"

I read this as: **a founder should be able to get a board-ready summary without composing the questions themselves.**

The natural failure mode of a chat BI tool is that it only answers what you think to ask. But someone preparing for a board call doesn't want to run twelve queries and assemble the results — they want the picture, including the parts they didn't think to ask about.

So the agent includes a **leadership brief** — one action that runs a coordinated multi-tool analysis and returns a structured executive summary:

- **Headline** — where the business stands, in two or three sentences
- **Pipeline** — open value, weighted forecast, win rate, sector concentration
- **Revenue & Collections** — booked vs billed vs collected, receivables
- **Execution** — work order status across the portfolio
- **Watch List** — three to five specific items needing attention, named and quantified
- **Data Caveats** — what in the brief is uncertain, and why

The last two sections are the ones that matter. The **watch list** is the agent doing analysis rather than reporting: surfacing overdue deals, concentration risk, and collection gaps that nobody queried for. The **caveats section** exists because a number that reaches a board deck without its uncertainty attached is worse than no number — someone will act on it.

---

## What I'd do differently with more time

**Trend analysis.** Everything is a point-in-time snapshot. "Is the pipeline improving?" needs period-over-period comparison, which needs either historical snapshots or a proper time-series query layer. This is the largest functional gap.

**Resolve the join properly.** Client codes don't match across boards, so cross-board analysis rests on deal names — lossy and many-to-many. With access to the source systems I would establish a real key. Failing that, fuzzy matching on client name plus value proximity would beat exact name matching.

**Charts.** Sector comparisons and funnel breakdowns are much easier to read visually. Streamlit supports this natively; it was cut for time.

**Evaluation suite.** Right now correctness is verified by inspection. A set of questions with known-correct answers, run against every prompt or tool change, would catch regressions in tool selection — the most likely place for silent quality decay.

**Scheduled briefs.** The leadership brief is on-demand. Emailing it every Monday morning is the version someone would actually use.

**Write-back.** Currently read-only, as specified. Flagging a stalled deal directly on the monday.com board would close the loop from insight to action.
