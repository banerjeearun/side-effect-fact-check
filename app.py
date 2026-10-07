import json
import os
import uuid
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from signal_tools import TOOL_SPECS, run_tool

# --- Config ---

SYSTEM_PROMPT = """You are Side Effect Fact-Check. You help people see whether FDA data backs up a worry like
"does drug X really cause side effect Y?", using openFDA adverse event reports and official FDA labels.

Rules:
- For any drug-reaction question, call a tool. Never answer from memory.
- "Does X cause Y?" -> call assess_signal. "What do people report for X?" -> top_reactions.
  "What does the label warn about?" -> get_label_warnings.
- Tools take generic names. If the user gives a brand (Advil, Tylenol, Glucophage), convert it
  to the generic name and say so ("Advil is ibuprofen").
- Reactions must be medical terms. Turn plain words into one ("throwing up" -> "vomiting").
  If a tool says the reaction was not found, call top_reactions to find the right term.
- If the drug or reaction is unclear, ask one short clarifying question instead of guessing.
- Say "reported disproportionately", never "causes". Give the ratio with its 95% interval
  and the number of reports, then say whether the label mentions it.
- Always relay the caveats briefly: reports are voluntary and unverified, and a report is not proof.
- Never advise starting, stopping or changing a medicine. Point people to a doctor or pharmacist.
- In a follow-up, reuse the drug or reaction from earlier in the conversation.
- Keep answers short: a verdict line, the numbers, the label finding, one caveat line."""

# The starter's OpenAI-style tool format: wrap each declaration from signal_tools.
TOOLS = [{"type": "function", "function": spec} for spec in TOOL_SPECS]
MAX_TOOL_ROUNDS = 5

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            result = json.dumps(run_tool(call.function.name, args))
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
