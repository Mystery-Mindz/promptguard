import time
from datetime import datetime, timezone
from typing import Any, Callable

from google import genai
from google.genai import types

from app.config import AGENT_MAX_STEPS, AGENT_MODEL, GEMINI_API_KEY, GEMINI_CLIENT_TIMEOUT_MS, MIN_SECONDS_BETWEEN_GEMINI_CALLS
from app.schemas import InputProvenance, InputSource, Trace, TraceStep
from app.tools import delete_file, read_email, send_message

# Gemini's free tier caps generate_content at 5 requests/minute per model.
# Without pacing, a chain of calls fails into 429s that each trigger the
# SDK's own internal retry-with-backoff *and* still leave the caller to
# retry again — compounding into multi-minute stalls. Enforcing a floor on
# the gap between calls keeps normal runs under quota in the first place.
_last_call_at: float = 0.0

SYSTEM_PROMPT = (
    "You are an AI agent that accomplishes the user's goal by calling the tools "
    "available to you. Call tools as needed, one at a time. When the goal is fully "
    "accomplished, respond with a final summary and no further tool calls."
)

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="read_email",
        description="Read emails from a folder in the user's inbox.",
        parameters={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "The folder to read, e.g. 'inbox'."},
            },
            "required": ["folder"],
        },
    ),
    types.FunctionDeclaration(
        name="delete_file",
        description="Delete a file at the given path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path of the file to delete."},
            },
            "required": ["path"],
        },
    ),
    types.FunctionDeclaration(
        name="send_message",
        description="Send a message to a recipient.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "The recipient."},
                "content": {"type": "string", "description": "The message content."},
            },
            "required": ["to", "content"],
        },
    ),
]

GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
)

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read_email": read_email,
    "delete_file": delete_file,
    "send_message": send_message,
}

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Returns a shared Gemini API client, creating it the first time this is
    called and reusing it on every later call (so the app doesn't open a new
    connection for every request).

    Takes no parameters. Returns a `genai.Client` configured with the API
    key from `.env` and a connection timeout (`GEMINI_CLIENT_TIMEOUT_MS`)
    so a stalled network call can't hang forever.
    """
    global _client
    if _client is None:
        # Explicit timeout so a stalled connection can't hang a call
        # indefinitely — bounds worst-case latency per request.
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=GEMINI_CLIENT_TIMEOUT_MS))
    return _client


def _now() -> datetime:
    """Returns the current UTC time, used to timestamp each trace step.
    Returned as a `datetime` (not a pre-formatted string) since that's what
    `TraceStep.timestamp` expects — Pydantic serializes it to an ISO-8601
    string (e.g. "2026-09-13T23:04:00+00:00") automatically when the trace
    is returned as JSON."""
    return datetime.now(timezone.utc)


def _pace_calls() -> None:
    """Pauses execution (if needed) so that consecutive calls to Gemini's
    `generate_content` are always at least `MIN_SECONDS_BETWEEN_GEMINI_CALLS`
    seconds apart. This keeps the agent loop under Gemini's free-tier rate
    limit instead of hitting 429 "too many requests" errors.

    Takes no parameters and returns nothing — it just sleeps for the
    remaining wait time, if any, before the caller proceeds with its next
    Gemini call.
    """
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_SECONDS_BETWEEN_GEMINI_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_GEMINI_CALLS - elapsed)
    _last_call_at = time.monotonic()


def run_agent(original_goal: str, trace_id: str, agent_id: str = "agent_A") -> Trace:
    """Runs a simulated AI agent that tries to accomplish `original_goal` by
    calling the mock tools in `tools.py` (read_email, delete_file,
    send_message), one step at a time, using Gemini's function-calling to
    decide which tool to call and with what arguments.

    Parameters:
    - `original_goal`: the plain-English task given to the agent (e.g.
      "Read my inbox and summarize anything urgent").
    - `trace_id`: the identifier to stamp on the resulting Trace, so it can
      be looked up later (e.g. via GET /trace/{trace_id}).
    - `agent_id`: which agent is "acting" in this run (defaults to
      "agent_A"); recorded on every step so multi-agent handoffs can be
      told apart.

    Returns a `Trace` — a record of every step the agent took, in order.
    Each step's input_source/input_provenance reflects what drove that
    action: it starts as the user's own goal (internal), and flips to
    tool_output/external the moment the agent reads content it doesn't
    control (e.g. an email body) — every action taken afterward inherits
    that provenance until fresh trusted input replaces it. This is what
    lets a later step be caught as "acting on untrusted content" even if
    the untrusted content was read several steps earlier. The loop stops
    after `AGENT_MAX_STEPS` steps or as soon as the model stops requesting
    tool calls, whichever comes first.
    """
    client = _get_client()

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=original_goal)]),
    ]

    steps: list[TraceStep] = []

    # Tracks where the text driving the *next* tool call came from. Starts as
    # the user's own goal; gets overwritten with tool_output/external once the
    # agent reads content it doesn't control (e.g. an email body), so any
    # action taken as a result of that content is correctly attributed.
    pending_input_text = original_goal
    pending_input_source: InputSource = "user"
    pending_input_provenance: InputProvenance = "internal"

    while len(steps) < AGENT_MAX_STEPS:
        _pace_calls()
        response = client.models.generate_content(
            model=AGENT_MODEL,
            contents=contents,
            config=GENERATE_CONFIG,
        )

        function_calls = response.function_calls
        if not function_calls:
            break

        # Preserve the model's turn (text + function call parts) in the conversation.
        assert response.candidates, "generate_content returned function_calls but no candidates"
        model_turn = response.candidates[0].content
        assert model_turn, "the model's candidate had no content"
        contents.append(model_turn)

        for call in function_calls:
            if len(steps) >= AGENT_MAX_STEPS:
                break

            assert call.name, "a function call from Gemini had no tool name"
            name = call.name
            args: dict[str, Any] = dict(call.args or {})

            tool_fn = TOOL_FUNCTIONS.get(name)
            if tool_fn is None:
                # Gemini asked to call a tool that was never declared in
                # TOOL_DECLARATIONS — a hallucinated tool call, not a real
                # bug in the trace content itself. Record it as its own step
                # (so it's visible in the trace instead of silently dropped)
                # and tell the model it's invalid via a normal
                # function_response, so it can recover on its own instead of
                # this crashing the whole /run-agent request with an
                # unhandled KeyError.
                steps.append(
                    TraceStep(
                        step_id=len(steps) + 1,
                        actor=agent_id,
                        input_text=f"Model requested an undeclared tool call: '{name}' with args {args}",
                        # Not "user" or "tool_output" — this wasn't driven by
                        # the user's goal or by real tool content, it's the
                        # agent's own invalid decision. "agent_handoff" is
                        # the closest fit among the fixed InputSource values;
                        # adding a dedicated value would change the shared
                        # trace schema (see CLAUDE.md — flag before doing
                        # that, don't just add one here).
                        input_source="agent_handoff",
                        input_provenance="internal",
                        action=name,
                        action_params={"hallucinated_tool_call": True, "requested_args": args},
                        timestamp=_now(),
                    )
                )
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=call.id,
                                    name=name,
                                    response={"error": f"tool '{name}' is not available"},
                                )
                            )
                        ],
                    )
                )
                continue

            result = tool_fn(**args)

            steps.append(
                TraceStep(
                    step_id=len(steps) + 1,
                    actor=agent_id,
                    input_text=pending_input_text,
                    input_source=pending_input_source,
                    input_provenance=pending_input_provenance,
                    action=name,
                    action_params=args,
                    timestamp=_now(),
                )
            )

            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=call.id,
                                name=name,
                                response={"result": result},
                            )
                        )
                    ],
                )
            )

            # Email content is untrusted/external. Any step taken on the back
            # of it — including ones several turns later — must inherit that
            # provenance until fresh trusted input replaces it.
            if name == "read_email":
                pending_input_text = "\n\n".join(
                    f"From: {email['from']}\nSubject: {email['subject']}\n{email['body']}"
                    for email in result
                )
                pending_input_source = "tool_output"
                pending_input_provenance = "external"

                # Record the content itself as its own step. Without this,
                # a tool's return value only ever surfaces as the *next*
                # step's input_text — if the agent never takes a next step,
                # what it actually read (including any injected instruction
                # in it) is silently dropped from the trace entirely.
                if len(steps) < AGENT_MAX_STEPS:
                    steps.append(
                        TraceStep(
                            step_id=len(steps) + 1,
                            actor=agent_id,
                            input_text=pending_input_text,
                            input_source=pending_input_source,
                            input_provenance=pending_input_provenance,
                            action="tool_output_received",
                            action_params={"tool": name},
                            timestamp=_now(),
                        )
                    )

    return Trace(trace_id=trace_id, agent_id=agent_id, original_goal=original_goal, steps=steps)
