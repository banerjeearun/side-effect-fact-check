# Side Effect Fact-Check

*Project 1 for IEOR 4570: Agentic AI (Columbia University, Fall 2026).*

**Does drug X really cause side effect Y?** Side Effect Fact-Check is a chat agent that answers that worry with
FDA data instead of forum anecdotes. For a drug and a reaction, it measures whether the reaction is
*reported disproportionately* in the FDA Adverse Event Reporting System (via openFDA) compared with
all other drugs, and checks whether the official FDA label already lists it.

**Live app:** https://side-effect-fact-check-git-177209096392.europe-west1.run.app (Columbia sign-in required)

> Reports are voluntary and unverified, and a report does not prove the drug caused the reaction.
> Not medical advice.

## How it works

Full diagrams (system context, request lifecycle, agent loop, tool routing, error handling, deployment): [architecture/flow-diagram.md](architecture/flow-diagram.md).

The starter harness (FastAPI + LiteLLM + Gemini `gemini-3.5-flash-lite`) runs a tool loop. Sessions are
kept in memory, so the agent follows the conversation ("how does acetaminophen compare?"), and
different browser sessions stay separate. `/chat` returns `response`, `session_id` and `tool_calls`
(`name`, `args`, `result`), and the page shows every tool call in a collapsible panel plus a verdict card.

### Tools (`signal_tools.py`)

All tools call openFDA live (no key needed) and return `{"error": CODE, "hint": ...}` instead of raising,
so the model can recover, e.g. by calling `top_reactions` to find a valid reaction term.

| Tool | What it does |
|---|---|
| `assess_signal` | The main tool. Combines the reporting odds ratio with the label check and returns a verdict: `known_effect`, `not_on_label_worth_asking_about`, `on_label_not_disproportionate` or `no_signal`. |
| `reporting_odds_ratio` | Builds the 2×2 table from four openFDA counts (drug+reaction, drug, reaction, all reports) and computes the ROR with a 95% confidence interval, the standard pharmacovigilance disproportionality measure. |
| `top_reactions` | Most-reported reactions for a drug (also gives the model valid MedDRA terms). |
| `get_label_warnings` | Boxed warning, warnings and adverse reactions from the FDA label (truncated). |
| `check_label_for_reaction` | Whether the label text mentions a reaction, with a snippet. Picks a single-ingredient, oral label with an adverse reactions section (over OTC, combination or eye-drop labels), and matches US spellings and word order of MedDRA terms ("oedema peripheral" → "peripheral edema"). |

### Guardrails

The system prompt handles safety before anything else: emergencies (overdose, severe reactions,
self-harm) get 911 / Poison Control 1-800-222-1222 / 988 and no data; no dosing advice; never advise
starting or stopping a medicine; off-topic requests and prompt-injection attempts are declined.
Every side-effect statement must come from a tool result: for a drug class ("antibiotics", "a statin")
the agent asks which specific medicine instead of answering from memory or guessing.
Replies are 2-3 plain sentences ("reported about 1.8 times as often as with other drugs"), and the card
carries the exact statistics.

## Sample queries

1. **"Is nausea a real side effect of ibuprofen?"** → calls `assess_signal`: *known side effect*, reported
   about 1.8× as often as with other drugs (ROR 1.78, 95% CI 1.76–1.81, ~18,300 reports), and listed on the label.
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

## Validation

`assess_signal` was run live on 12 textbook side effects (positive controls) and 4 pairs with no known
link (negative controls):

| Group | Drug + reaction | Reports | ROR (95% CI) | On label | Verdict |
|---|---|---:|---|---|---|
| Known | lisinopril + cough | 9,441 | 2.05 (2.01–2.09) | ✔ | known_effect |
| Known | atorvastatin + myalgia | 10,035 | 5.39 (5.28–5.50) | ✔ | known_effect |
| Known | simvastatin + rhabdomyolysis | 5,808 | 13.05 (12.69–13.42) | ✔ | known_effect |
| Known | ciprofloxacin + tendon rupture | 1,309 | 25.84 (24.39–27.37) | ✔ | known_effect |
| Known | amoxicillin + rash | 8,386 | 2.80 (2.73–2.86) | ✔ | known_effect |
| Known | warfarin + haemorrhage | 18,737 | 5.66 (5.58–5.75) | ✔ | known_effect |
| Known | amlodipine + oedema peripheral | 8,316 | 3.53 (3.45–3.61) | ✔ | known_effect |
| Known | metformin + diarrhoea | 28,450 | 2.21 (2.18–2.24) | ✔ | known_effect |
| Known | gabapentin + dizziness | 16,893 | 1.99 (1.96–2.03) | ✔ | known_effect |
| Known | metoprolol + bradycardia | 4,879 | 4.82 (4.68–4.97) | ✔ | known_effect |
| Known | isotretinoin + depression | 5,916 | 11.36 (11.05–11.67) | ✔ | known_effect |
| Known | sertraline + insomnia | 7,914 | 2.66 (2.60–2.72) | ✔ | known_effect |
| No link | metformin + tendon rupture | 281 | 1.09 (0.96–1.22) | | no_signal |
| No link | amlodipine + acne | 803 | 0.48 (0.45–0.52) | | no_signal |
| No link | levothyroxine + tendon rupture | 297 | 1.69 (1.51–1.90) | | **false alarm** |
| No link | amoxicillin + rhabdomyolysis | 400 | 2.00 (1.81–2.20) | | **false alarm** |

- **12/12 known side effects** are flagged and found on the label. The first run found only 8/12 on the
  label: ciprofloxacin matched its eye-drop label, and British MedDRA spellings ("haemorrhage",
  "diarrhoea") missed the US label text. Both are fixed and unit tested.
- **2/4 unrelated pairs are false alarms.** This is a known weakness of disproportionality analysis: a report lists every drug a patient was taking, so a common drug can pick up another
  drug's reactions (older patients on levothyroxine are also the ones prescribed fluoroquinolones,
  which cause tendon rupture). The false alarms are weak (about 2×, a few hundred reports); real effects
  are mostly far stronger (5–26×, thousands of reports). This is why the app says "worth asking a
  pharmacist", never "causes".

## Caveats

- A high ratio means a reaction is *reported* more often with this drug than with others, not that it is
  likely to happen to you. Widely used drugs, co-prescribed drugs and the reasons people take them all
  skew reports (see the false alarms above).
- The label check is a text match (with US spellings), so a true synonym on the label ("emesis" vs
  "vomiting") can still be missed.
- Sessions live in memory and reset when the instance restarts.
