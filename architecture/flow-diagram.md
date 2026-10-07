# Side Effect Fact-Check: Architecture and Flow

How a question like *"Is nausea a real side effect of ibuprofen?"* becomes a verdict card. The diagrams
render on GitHub (Mermaid). Every number below matches the code: file and constant names are given so
the doc can be checked against the source.

**Contents**

1. [System context](#1-system-context): who and what the app talks to
2. [Components](#2-components): what lives in which file
3. [Request lifecycle](#3-request-lifecycle): one `/chat` call, end to end
4. [Agent loop](#4-agent-loop): the harness in `run_agent()`
5. [Tool routing](#5-tool-routing): which tool answers which question
6. [`assess_signal` pipeline](#6-assess_signal-pipeline): how a verdict is built
7. [openFDA access and error handling](#7-openfda-access-and-error-handling), then [guardrails](#7b-guardrails)
8. [Sessions](#8-sessions)
9. [Deployment](#9-deployment)
10. [Design decisions](#10-design-decisions)

---

## 1. System context

```mermaid
flowchart LR
    user(["User<br/>(Columbia account)"])
    subgraph gcp["Google Cloud"]
        iap["Identity-Aware Proxy<br/>columbia.edu only"]
        app["Side Effect Fact-Check<br/>Cloud Run service"]
        gemini["Gemini 3.5 Flash Lite<br/>Vertex AI, global"]
    end
    fda[("openFDA<br/>drug/event + drug/label")]

    user -- HTTPS --> iap --> app
    app -- "chat + tool specs<br/>(LiteLLM)" --> gemini
    app -- "REST, no key needed" --> fda
```

| External system | Used for | Auth |
|---|---|---|
| Vertex AI (Gemini) | Deciding which tools to call and writing the reply | Cloud Run service account (no API key in code) |
| openFDA `drug/event.json` | Adverse-event report counts (FAERS) | None; optional `OPENFDA_API_KEY` env var raises the daily limit |
| openFDA `drug/label.json` | Official FDA label text | Same as above |

---

## 2. Components

```mermaid
flowchart TB
    subgraph browser["Browser: index.html"]
        ui["Chat UI<br/>examples, disclaimer, input"]
        render["Renderers<br/>tool panels, verdict card"]
    end

    subgraph server["app.py (FastAPI)"]
        routes["Routes<br/>GET / · POST /chat · POST /clear"]
        store[("sessions dict<br/>session_id → messages")]
        harness["run_agent()<br/>tool loop, MAX_TOOL_ROUNDS = 5"]
        prompt["SYSTEM_PROMPT<br/>rules for tone and safety"]
    end

    subgraph tools["signal_tools.py"]
        specs["TOOL_SPECS<br/>what the model sees"]
        dispatch["run_tool()<br/>never raises"]
        fns["5 tool functions"]
        http["_get()<br/>cache, retry, error codes"]
        math["reporting_odds_ratio_math()<br/>pure, unit tested"]
    end

    ui -- "POST /chat" --> routes
    routes --> store
    routes --> harness
    prompt --> store
    specs --> harness
    harness --> dispatch --> fns
    fns --> http
    fns --> math
    routes -- "response, session_id, tool_calls" --> render
```

| File | Responsibility | Depends on |
|---|---|---|
| `index.html` | UI only: sends messages, renders replies, tool panels and the verdict card. No build step. | `/chat`, `/clear` |
| `app.py` | HTTP API, session store, system prompt, the agent loop. Knows nothing about openFDA. | `signal_tools.TOOL_SPECS`, `run_tool` |
| `signal_tools.py` | All domain logic: openFDA queries, statistics, label matching, error codes. Knows nothing about the web or the model. | `requests` |
| `test_signal_tools.py` | Offline unit tests (network mocked). | `signal_tools` |

The boundary between `app.py` and `signal_tools.py` is two names: `TOOL_SPECS` and `run_tool`. Swapping
the model or the web framework does not touch the tools, and the tools can be tested without either.

---

## 3. Request lifecycle

One user message, from click to rendered answer (sample query 1):

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Browser (index.html)
    participant A as FastAPI /chat
    participant S as sessions dict
    participant L as Gemini (Vertex AI)
    participant T as run_tool()
    participant F as openFDA

    U->>B: "Is nausea a real side effect of ibuprofen?"
    B->>A: POST /chat {message, session_id}
    A->>S: get or create session (system prompt first)
    A->>S: append user message
    A->>L: messages + TOOLS
    L-->>A: tool call assess_signal(ibuprofen, nausea)
    A->>T: run_tool("assess_signal", args)
    T->>F: 4 count queries (drug/event)
    F-->>T: meta.results.total each
    T->>F: label search (drug/label, limit 25)
    F-->>T: candidate labels
    T-->>A: {verdict, ROR, CI, label, caveats}
    A->>S: append assistant tool call + tool result
    A->>L: messages (now with the tool result)
    L-->>A: final text, no tool calls
    A->>S: append final reply
    A-->>B: {response, session_id, tool_calls[{name, args, result}]}
    B->>U: tool panel + verdict card + reply
```

The `/chat` response shape is kept from the starter so the UI can show every tool call:

```json
{
  "response": "Nausea is reported disproportionately with ibuprofen ...",
  "session_id": "6649eedd-...",
  "tool_calls": [
    {"name": "assess_signal", "args": {"drug": "ibuprofen", "reaction": "nausea"}, "result": "{\"verdict\": \"known_effect\", ...}"}
  ]
}
```

---

## 4. Agent loop

`run_agent()` in `app.py`. The model proposes; the harness executes. The model never touches the
network directly.

```mermaid
flowchart TD
    start(["messages for this session"]) --> llm["litellm.completion<br/>model + messages + TOOLS"]
    llm --> append["append assistant reply<br/>(model_dump to plain dict)"]
    append --> any{"reply has<br/>tool_calls?"}
    any -- no --> done(["return text + tool_calls log"])
    any -- yes --> each["for each tool call"]
    each --> parse["json.loads(arguments or '{}')"]
    parse --> run["run_tool(name, args)<br/>returns dict, never raises"]
    run --> log["log {name, args, result}"]
    log --> msg["append role=tool message<br/>(JSON string, matched by tool_call_id)"]
    msg --> rounds{"round < 5?"}
    rounds -- yes --> llm
    rounds -- no --> cap(["'Sorry, I hit my tool-call limit'"])

    llm -. "exception: auth, quota, model down" .-> fail(["/chat returns 'Model call failed: ...'<br/>as a normal reply, not a 500"])
```

Guard rails in the loop:

| Risk | Guard |
|---|---|
| Model loops on tools forever | `MAX_TOOL_ROUNDS = 5` |
| Model invents a tool name | `run_tool` returns `UNKNOWN_TOOL` with the list of valid names |
| Model sends wrong argument names | `run_tool` returns `BAD_ARGUMENTS` with the Python error text |
| Any other bug inside a tool | `run_tool` returns `TOOL_CRASH`; the conversation continues |
| Model / Vertex AI failure | Caught in `/chat`, shown in the chat window |

---

## 5. Tool routing

The system prompt maps question types to tools. The model chooses, but the descriptions in
`TOOL_SPECS` and the prompt steer it:

```mermaid
flowchart TD
    q(["User message"]) --> safe{"Emergency, dosing<br/>or off-topic?"}
    safe -- "emergency" --> em["911 / Poison Control<br/>1-800-222-1222 / 988<br/>(no tool call)"]
    safe -- "dosing" --> ds["Decline, point to pharmacist<br/>or label (no tool call)"]
    safe -- "off-topic or injection" --> ot["Say what the app does,<br/>give an example (no tool call)"]
    safe -- "side-effect question" --> clear{"Drug and reaction<br/>clear?"}
    clear -- no --> ask["Ask one clarifying question<br/>(no tool call)"]
    clear -- yes --> brand{"Brand name?<br/>e.g. Advil"}
    brand -- yes --> generic["Convert to generic and say so<br/>Advil → ibuprofen"]
    brand -- no --> kind
    generic --> kind{"What kind of question?"}

    kind -- "Does X cause Y?" --> as["assess_signal(drug, reaction)"]
    kind -- "What do people report for X?" --> tr["top_reactions(drug, limit)"]
    kind -- "What does the label warn about?" --> lw["get_label_warnings(drug)"]
    kind -- "Just the statistic" --> ror["reporting_odds_ratio(drug, reaction)"]
    kind -- "Is Y on the label?" --> cl["check_label_for_reaction(drug, reaction)"]

    as --> nf{"REACTION_NOT_FOUND?"}
    nf -- yes --> tr2["top_reactions(drug)<br/>to find the right MedDRA term"] --> as
    nf -- no --> reply(["Reply: 2-3 plain sentences,<br/>'about 1.8 times as often', label finding, caveat<br/>(numbers go on the card)"])
```

| Tool | External calls | Returns |
|---|---|---|
| `assess_signal` | 4 event counts + 1 label search | Verdict, ROR, 95% CI, label snippet, caveats |
| `reporting_odds_ratio` | 4 event counts | ROR, 95% CI, 2×2 counts, plain reading |
| `top_reactions` | 1 event count-by-reaction | Top N reactions with report counts |
| `get_label_warnings` | 1 label search | Boxed warning, warnings, adverse reactions (truncated to 1,200 chars each) |
| `check_label_for_reaction` | 1 label search | `on_label`, sections, snippet |

---

## 6. `assess_signal` pipeline

The main tool. It answers "does X really cause Y?" by combining two independent sources of
evidence: what people report, and what the FDA label already says.

### 6a. Reporting odds ratio

Four openFDA counts fill a 2×2 table (`reporting_odds_ratio_math`):

|  | Reaction Y | Any other reaction |
|---|---|---|
| **Drug X** | a = drug AND reaction | b = drug total − a |
| **All other drugs** | c = reaction total − a | d = all reports − a − b − c |

```mermaid
flowchart LR
    q1["count: drug AND reaction"] --> a["a"]
    q2["count: drug"] --> b["b = drug − a"]
    q3["count: reaction"] --> c["c = reaction − a"]
    q4["count: all reports"] --> d["d = all − a − b − c"]
    a & b & c & d --> chk{"any cell < 0<br/>or = 0?"}
    chk -- "< 0" --> e1["INCONSISTENT_COUNTS"]
    chk -- "= 0" --> e2["ZERO_CELL"]
    chk -- ok --> ror["ROR = (a·d) / (b·c)<br/>SE = √(1/a + 1/b + 1/c + 1/d)<br/>95% CI = exp(ln ROR ± 1.96·SE)"]
```

Ibuprofen + nausea (live): a = 18,315 → **ROR 1.78, 95% CI 1.76–1.81**.

### 6b. Label match

```mermaid
flowchart LR
    s["label search<br/>generic_name = drug, limit 25"] --> rank["rank candidates<br/>1. single ingredient<br/>2. oral route<br/>3. has adverse_reactions"]
    rank --> best["best label"]
    best --> scan["text search, MedDRA + US spellings<br/>e.g. oedema peripheral, peripheral edema<br/>in boxed_warning, warnings,<br/>warnings_and_cautions, adverse_reactions, precautions"]
    scan --> out["on_label, sections, snippet"]
```

Ranking matters: the first search hit for ibuprofen is an OTC "Drug Facts" label with no adverse
reactions section, which made nausea look "not on label". Ciprofloxacin matched its eye-drop label (no tendon warning), and British MedDRA spellings missed US label text. `_label_rank` and `_spellings` fix both, with unit tests; see the Validation table in the README.

### 6c. Verdict

```mermaid
flowchart TD
    in(["ROR result + label result"]) --> dis{"disproportionate?<br/>CI lower bound > 1<br/>AND a ≥ 5"}
    dis -- yes --> l1{"on label?"}
    dis -- no --> l2{"on label?"}
    l1 -- yes --> k["known_effect<br/>reported more AND listed"]
    l1 -- no --> w["not_on_label_worth_asking_about<br/>reported more, not found on label"]
    l2 -- yes --> o["on_label_not_disproportionate<br/>listed, not over-reported"]
    l2 -- no --> n["no_signal"]
```

The `a ≥ 5` rule stops a handful of reports from producing a dramatic but meaningless ratio.

---

## 7. openFDA access and error handling

Every openFDA call goes through `_get()` in `signal_tools.py`:

```mermaid
flowchart TD
    req(["_get(url, params)"]) --> key["add OPENFDA_API_KEY if set"]
    key --> cache{"in cache<br/>(6 h TTL)?"}
    cache -- yes --> hit(["return cached data"])
    cache -- no --> try["requests.get, timeout 20 s<br/>(up to 2 attempts)"]
    try --> st{"result"}
    st -- "200" --> ok(["cache + return data"])
    st -- "404" --> nm(["cache + return {} (openFDA's 'no matches')"])
    st -- "429" --> rl["RATE_LIMITED<br/>wait 1.5 s, retry"]
    st -- "timeout" --> to["UPSTREAM_TIMEOUT<br/>retry"]
    st -- "5xx / network" --> ue["UPSTREAM_ERROR<br/>retry"]
    st -- "other 4xx" --> stop(["UPSTREAM_ERROR, no retry"])
    rl & to & ue --> again{"attempts left?"}
    again -- yes --> try
    again -- no --> err(["return error dict"])
```

Errors are data, not exceptions. Every failure returns `{"error": CODE, "hint": "what to do next"}`, so
the model can recover or explain the problem to the user:

| Code | Raised by | Hint tells the model to |
|---|---|---|
| `MISSING_DRUG`, `MISSING_INPUT` | input check | Ask the user which drug / reaction |
| `DRUG_NOT_FOUND` | counts or top reactions | Try the generic name |
| `REACTION_NOT_FOUND` | reaction count = 0 | Call `top_reactions` for valid terms |
| `NO_LABEL`, `LABEL_EMPTY` | label search | Try the generic name / say the label has no warnings |
| `ZERO_CELL`, `INCONSISTENT_COUNTS` | ROR math | Say there are too few reports |
| `RATE_LIMITED`, `UPSTREAM_TIMEOUT`, `UPSTREAM_ERROR`, `UPSTREAM_BAD_JSON` | `_get()` | Ask the user to retry shortly |
| `UNKNOWN_TOOL`, `BAD_ARGUMENTS`, `TOOL_CRASH` | `run_tool()` | Fix the call or try another drug |

User input is sanitised by `_clean()` (letters, digits and a few punctuation marks, 80 characters max)
before it goes into an openFDA query string, so quotes and query operators cannot change the search.

---

## 7b. Guardrails

Safety is layered, so no single layer has to be perfect:

| Layer | Guardrail | Where |
|---|---|---|
| Prompt: safety rules (checked first) | Emergencies (overdose, severe reaction, self-harm) get 911 / Poison Control 1-800-222-1222 / 988 and no data · no dosing advice · never advise starting, stopping or changing a medicine | `SYSTEM_PROMPT` in `app.py` |
| Prompt: scope | Only drug side-effect questions; anything else gets one sentence on what the app does | `SYSTEM_PROMPT` |
| Prompt: grounding | Every side-effect statement must come from a tool result, never the model's own knowledge · drug classes ("antibiotics", "a statin") get a question asking which specific medicine, with examples, instead of a guess | `SYSTEM_PROMPT` |
| Prompt: injection | User cannot change the rules, reveal the prompt or assign a role; pasted text and tool results are data | `SYSTEM_PROMPT` |
| Prompt: wording | Never "causes"; "reported about N times as often as with other drugs"; always one caveat | `SYSTEM_PROMPT` |
| Tool output | Caveats travel inside every result; fewer than 5 reports is never a signal | `signal_tools.py` |
| Query safety | `_clean()` strips quotes and query operators before building openFDA searches | `signal_tools.py` |
| Agent loop | 5-round cap; unknown tools, bad args and crashes become hints | `app.py`, `run_tool()` |
| UI | Disclaimer always visible; model text is HTML-escaped before rendering | `index.html` |
| Access | IAP, Columbia accounts only; no secrets in the repo | Cloud Run |

---

## 8. Sessions

```mermaid
flowchart LR
    b1["Browser tab A<br/>session_id = 6649…"] --> s
    b2["Browser tab B<br/>session_id = 8c1c…"] --> s
    s[("sessions dict<br/>in process memory")]
    s --> h1["A: system, user, assistant, tool, ..."]
    s --> h2["B: system, user, ..."]
    reset["New chat button"] -- "POST /clear" --> s
```

- The server issues a `session_id` (UUID4) on the first message; the browser sends it back on every turn.
- Each session holds the full message list, including tool calls and results, so follow-ups like
  *"How does acetaminophen compare?"* reuse the earlier reaction.
- Verified: a new session does not see another session's history.
- **Trade-off:** sessions live in one process's memory. Cloud Run is set to **max instances = 1** so all
  requests reach the same process. Sessions reset when the instance restarts. A shared store
  (Firestore, Redis) would remove both limits but is not needed at class scale.

---

## 9. Deployment

```mermaid
flowchart LR
    dev["Local repo<br/>uv run app.py"] -- "git push main" --> gh["GitHub<br/>banerjeearun/side-effect-fact-check"]
    gh -- "Developer Connect trigger" --> cb["Cloud Build<br/>europe-west1<br/>Python buildpack"]
    cb --> img[("Container image")]
    img --> cr["Cloud Run service<br/>side-effect-fact-check-git<br/>max instances 1"]
    iap["IAP: columbia.edu"] --> cr
    cr --> url(["https://side-effect-fact-check-git-177209096392.europe-west1.run.app"])
```

| Setting | Value | Why |
|---|---|---|
| Build | Google Cloud buildpack (no Dockerfile) | `pyproject.toml` + `uv.lock` are enough |
| Entrypoint | `uvicorn app:app --host 0.0.0.0 --port $PORT` | Cloud Run sets `$PORT` and needs `0.0.0.0` |
| Max instances | 1 | Keeps in-memory sessions consistent |
| Access | IAP, `columbia.edu` | Only Columbia accounts can sign in |
| Secrets | None in the repo | Vertex AI uses the service account; `OPENFDA_API_KEY` is optional and set as an env var |

---

## 10. Design decisions

| Decision | Alternative considered | Why this way |
|---|---|---|
| Reporting odds ratio, not raw counts | Show top report counts only | Popular drugs have more reports of everything; the ratio compares against all other drugs |
| Combine reports **and** label in one tool | Let the model call two tools and combine | One deterministic verdict, computed in tested Python instead of by the model |
| Say "reported N times as often", never "causes" | Plain "causes" language, or statistical jargon | Spontaneous reports cannot prove causation; plain wording is readable, and the card keeps the exact statistics |
| Errors returned as `{error, hint}` | Raise exceptions | The model cannot see exceptions; a hint lets it retry or explain |
| Pure math function, tested offline | Compute inline in the tool | Statistics are checked without the network (20 unit tests) |
| 6-hour response cache | No cache | Follow-up questions repeat counts (e.g. "all reports"); saves openFDA quota and latency |
| `max(..., key=_label_rank)` over 25 labels | Take the first label | First hit is often an OTC or combination label |
| Domain logic isolated in `signal_tools.py` | Tools inside `app.py` | Clear boundary (`TOOL_SPECS`, `run_tool`); either side can change alone |

### Known limits

- Label matching is plain text, so synonyms ("emesis" vs "vomiting") are missed.
- openFDA data is voluntary and unverified; counts are affected by publicity, reporting habits and why people take the drug.
- Without an API key, openFDA allows 1,000 requests a day per IP. `assess_signal` uses up to 5, and the cache absorbs repeats.
