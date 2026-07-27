# Skylark BI Agent

A conversational business intelligence agent that answers founder-level questions across two monday.com boards — Deals (sales pipeline) and Work Orders (project execution and billing).

Ask *"How's our pipeline looking for renewables this quarter?"* and it interprets the question, queries both boards live, cleans the data, computes the numbers, and explains what they mean — including which figures you should not fully trust and why.

---

## Architecture

```
                      Streamlit chat UI  (app.py)
                               |
                      Agent loop  (src/agent.py)
                  Claude Opus 4.8 + 8 typed tools
                               |
                 Analytics engine  (src/analytics.py)
                  all arithmetic runs here, in pandas
                               |
          Normalization + quality audit  (src/normalize.py)
                               |
           monday.com GraphQL client  (src/monday_client.py)
                               |
              monday.com API v2   (read-only, paginated)
```

### The core design decision

**The language model never does arithmetic.** It decides *which* analysis to run and *with what filters*; every number is computed by pandas and handed back as a tool result. The model's job is interpretation and narrative, not calculation.

This is the difference between a BI agent you can trust and one that invents plausible-looking revenue figures. If the model had to sum a column of 344 deal values in its head, some of those sums would be wrong, and you would have no way to tell which.

### Layers

**`src/monday_client.py`** — Read-only GraphQL v2 client. Cursor pagination via `items_page`, exponential backoff on 429s and 5xx, special handling for monday's complexity-budget errors, and a 5-minute TTL cache so a multi-turn conversation doesn't refetch 500 rows per question.

**`src/normalize.py`** — Converts raw board rows into typed, canonical DataFrames while recording every repair it makes. Parses dates across 15 formats, coerces currency (including Indian shorthand like `4.5L` and `2 Cr`), strips units glued onto numbers (`"5360 HA"` → `5360.0`), canonicalizes labels (`BIlled` → `Fully Billed`), removes duplicate rows and header rows pasted into the data. The audit trail it produces is a first-class output: the agent cites it to warn users about weak figures.

**`src/analytics.py`** — Typed analysis functions over the cleaned frames: `pipeline_health`, `revenue_summary`, `aggregate`, `cross_board_view`, and the two raw query tools. Handles Indian financial years, resolves phrases like `this_quarter` into concrete date ranges, and attaches relevant caveats to every result.

**`src/agent.py`** — Tool schemas and the Anthropic tool-use loop. Streams text as it arrives, executes tool calls, feeds results back, up to 8 rounds. Tool errors are returned to the model as structured JSON so it can correct itself rather than crashing the turn.

**`app.py`** — Streamlit chat interface with streaming responses, live tool-call indicators, a data-quality sidebar, and the leadership brief generator.

---

## Setup

### 1. monday.com boards

Sign up at [monday.com](https://monday.com) (free trial is sufficient).

Generate the import-ready CSVs:

```bash
python scripts/prepare_import.py
```

This writes `data/monday_deals.csv` and `data/monday_work_orders.csv`. It fixes only what would break the *import* — the blank spacer row above the work order header, two header rows pasted into the deals data, an `#VALUE!` Excel error, and four entirely empty columns. Semantic messiness (inconsistent casing, units inside numeric fields, nulls, duplicate rows) is deliberately left intact so the agent's normalization layer handles it at query time.

Import each file as a separate board (**Add → Import data → Excel/CSV**).

**Column types.** monday's auto-detection is adequate; the agent normalizes text regardless. Setting these explicitly gives a better board experience:

| Board | Column | Type |
|---|---|---|
| Deals | `Masked Deal value` | Numbers |
| Deals | `Tentative Close Date`, `Created Date`, `Close Date (A)` | Date |
| Deals | `Deal Status`, `Deal Stage`, `Sector/service` | Status |
| Work Orders | All `Amount` / `Value` / `Billed` / `Collected` columns | Numbers |
| Work Orders | `Date of PO/LOI`, `Probable Start Date`, `Last invoice date` | Date |
| Work Orders | `Execution Status`, `Sector`, `Invoice Status` | Status |

Get each **board ID** from its URL: `monday.com/boards/1234567890` → `1234567890`.

### 2. API tokens

- **monday.com** — avatar (bottom-left) → *Developers* → *My Access Tokens* → copy
- **Anthropic** — [console.anthropic.com](https://console.anthropic.com) → *API Keys* → *Create Key*

### 3. Run locally

```bash
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# then edit secrets.toml with your keys and board IDs

streamlit run app.py
```

Opens at `http://localhost:8501`.

> Without the three `MONDAY_*` values, the app falls back to the local CSVs in `data/` and labels the source in the sidebar. Useful for development; the hosted version reads live boards.

### 4. Deploy

1. Push to GitHub (`.gitignore` excludes `secrets.toml` — verify before pushing).
2. [share.streamlit.io](https://share.streamlit.io) → *New app* → select the repo → main file `app.py`.
3. *Advanced settings* → *Secrets* → paste the contents of your `secrets.toml`.
4. Deploy. You get a public `*.streamlit.app` URL.

---

## What it can answer

| Category | Example |
|---|---|
| Pipeline health | *"How's our pipeline looking for energy this quarter?"* |
| Revenue & cash | *"What's outstanding in receivables?"* |
| Sector performance | *"Which sectors are performing best?"* |
| Operations | *"Which work orders are stalled?"* |
| Cross-board | *"Are our won deals converting into work orders?"* |
| Data quality | *"How reliable is this data?"* |
| Leadership brief | One-click structured executive summary |

---

## Data quality handling

Measured on the actual dataset:

| Issue | Found | Handling |
|---|---|---|
| Deals missing a value | 167 of 332 (50%) | Totals cover only valued deals; agent states this whenever quoting a pipeline figure |
| Duplicate deal rows | 12 | Removed, reported in the quality audit |
| Header rows inside data | 2 | Detected and dropped |
| Units inside numeric fields | 64 values | Stripped (`"5360 HA"` → `5360.0`) |
| Inconsistent labels | 31 values | Canonicalized (`BIlled` → `Fully Billed`) |
| Excel error literals | 1 | Blanked |
| Fully empty columns | 4 | Dropped |
| Negative receivables | 11 | Preserved — real over-billing, not corruption |
| Unparseable quantities | 5 | Rejected rather than coerced (`"Rate based on MW slabs"`) |

**The cross-board join is the significant analytical limitation.** The two boards use different client-code namespaces (`COMPANY089` vs `WOCOMPANY_002`), so deal name is the only available link — and only 52 of 58 work order names match a deal, while 45 deal names appear on multiple rows. It is a lossy, many-to-many join. Rather than silently producing wrong cross-board totals, `cross_board_view` returns an explicit warning that the agent relays to the user.

---

## Project structure

```
skylark/
├── app.py                      # Streamlit chat UI
├── requirements.txt
├── src/
│   ├── monday_client.py        # GraphQL client: pagination, retries, cache
│   ├── normalize.py            # Cleaning + data quality audit
│   ├── analytics.py            # Analysis functions (all arithmetic)
│   ├── agent.py                # Tool schemas + Anthropic tool-use loop
│   └── data_source.py          # monday.com / local CSV loader
├── scripts/
│   └── prepare_import.py       # Generates monday.com-ready CSVs
├── data/                       # Generated import files
├── DECISION_LOG.md
└── .streamlit/
    └── secrets.toml.example
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `monday.com rejected the API token (401)` | Token wrong or expired — regenerate |
| `Board {id} not found` | Wrong board ID, or the token's account lacks access |
| `A board came back empty` | CSV import didn't complete, or the ID points at an empty board |
| Sidebar says "local CSV" when you expect live | One of the three `MONDAY_*` values is missing |
| `No Anthropic API key found` | `ANTHROPIC_API_KEY` not set in secrets or environment |
