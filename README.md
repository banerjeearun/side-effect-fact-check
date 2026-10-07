# Signal Check

**Does drug X really cause side effect Y?** Signal Check is a chat agent that answers that worry with
FDA data instead of forum anecdotes. For a drug and a reaction, it measures whether the reaction is
*reported disproportionately* in the FDA Adverse Event Reporting System (via openFDA) compared with
all other drugs, and checks whether the official FDA label already lists it.

**Live app:** DEPLOY_URL (Columbia sign-in required)

> Reports are voluntary and unverified, and a report does not prove the drug caused the reaction.
> Not medical advice.

## How it works

The starter harness (FastAPI + LiteLLM + Gemini `gemini-3.5-flash-lite`) runs a tool loop. Sessions are
kept in memory, so the agent follows the conversation ("how does acetaminophen compare?"), and
different browser sessions stay separate. `/chat` returns `response`, `session_id` and `tool_calls`
(`name`, `args`, `result`), and the page shows every tool call in a collapsible panel plus a verdict card.

### Tools (`signal_tools.py`)

All tools call openFDA live (no key needed) and return `{"error": CODE, "hint": ...}` instead of raising,
so the model can recover, e.g. by calling `top_reactions` to find a valid reaction term.

| Tool | What it does |
|---|---|
| `assess_signal` *(original)* | The main tool. Combines the reporting odds ratio with the label check and returns a verdict: `known_effect`, `not_on_label_worth_asking_about`, `on_label_not_disproportionate` or `no_signal`. |
| `reporting_odds_ratio` *(original)* | Builds the 2×2 table from four openFDA counts (drug+reaction, drug, reaction, all reports) and computes the ROR with a 95% confidence interval, the standard pharmacovigilance disproportionality measure. |
| `top_reactions` | Most-reported reactions for a drug (also gives the model valid MedDRA terms). |
| `get_label_warnings` | Boxed warning, warnings and adverse reactions from the FDA label (truncated). |
| `check_label_for_reaction` | Whether the label text mentions a reaction, with a snippet. Picks a single-ingredient label with an adverse reactions section over OTC or combination labels. |

## Sample queries

1. **"Is nausea a real side effect of ibuprofen?"** → calls `assess_signal`: *known effect*, ROR ≈ 1.78
   (95% CI 1.76–1.81, ~18,300 reports), and listed on the label.
2. **"What do people report most for metformin, and what does its label warn about?"** → calls
   `top_reactions` and `get_label_warnings`: nausea and diarrhoea top the reports, and the label has a
   boxed warning for lactic acidosis.
3. **In the same chat as (1): "How does acetaminophen compare for nausea?"** → uses the conversation to
   call `assess_signal(acetaminophen, nausea)`: ROR ≈ 1.79, but no matching text on the label (its label
   is OTC Drug Facts), so the verdict is *worth asking a pharmacist about*.

## Run locally

```bash
gcloud auth application-default login   # Vertex AI credentials, as in the course setup guide
uv run app.py                            # http://localhost:8000
uv run python -m unittest test_signal_tools
```

Optional: set `OPENFDA_API_KEY` (free from open.fda.gov) to raise openFDA's daily limit from 1,000 to 120,000 requests.

## Deployment

Cloud Run with continuous deploy from GitHub (Developer Connect, Python buildpack), entrypoint
`uvicorn app:app --host 0.0.0.0 --port $PORT`, max instances 1 so the in-memory sessions live in one process.

## Caveats

- A high ratio means a reaction is *reported* more often with this drug than with others, not that it is
  likely to happen to you. Widely used drugs and the reasons people take them both skew reports.
- The label check is a text match, so a synonym on the label ("emesis" vs "vomiting") can be missed.
- Sessions live in memory and reset when the instance restarts.
