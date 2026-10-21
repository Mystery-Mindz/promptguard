from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app import llm
from app.config import AGENT_MAX_STEPS
from app.llm import GenerateResult, Message, ToolDeclaration, ToolResult
from app.schemas import InputProvenance, InputSource, Trace, TraceStep
from app.tools import delete_file, read_email, send_message

SYSTEM_PROMPT = (
    "You are an AI agent that accomplishes the user's goal by calling the tools "
    "available to you. Call tools as needed, one at a time. When the goal is fully "
    "accomplished, respond with a final summary and no further tool calls."
)

TOOL_DECLARATIONS = [
    ToolDeclaration(
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
    ToolDeclaration(
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
    ToolDeclaration(
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

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read_email": read_email,
    "delete_file": delete_file,
    "send_message": send_message,
}


def _now() -> datetime:
    """Returns the current UTC time, used to timestamp each trace step.
    Returned as a `datetime` (not a pre-formatted string) since that's what
    `TraceStep.timestamp` expects — Pydantic serializes it to an ISO-8601
    string (e.g. "2026-09-13T23:04:00+00:00") automatically when the trace
    is returned as JSON."""
    return datetime.now(UTC)


def run_agent(original_goal: str, trace_id: str, agent_id: str = "agent_A") -> Trace:
    """Runs a simulated AI agent that tries to accomplish `original_goal` by
    calling the mock tools in `tools.py` (read_email, delete_file,
    send_message), one step at a time, using `llm.generate_with_tools` to
    decide which tool to call and with what arguments. Never talks to a
    model API directly — all of that lives in `app/llm.py`.

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

    Raises `llm.ModelUnavailable` if the configured provider (and
    fallback, if any) can't complete a call even after retries —
    `main.py` maps that to a clean HTTP 503.
    """
    messages: list[Message] = [Message(role="user", text=original_goal)]

    steps: list[TraceStep] = []

    # Tracks where the text driving the *next* tool call came from. Starts as
    # the user's own goal; gets overwritten with tool_output/external once the
    # agent reads content it doesn't control (e.g. an email body), so any
    # action taken as a result of that content is correctly attributed.
    pending_input_text = original_goal
    pending_input_source: InputSource = "user"
    pending_input_provenance: InputProvenance = "internal"

    while len(steps) < AGENT_MAX_STEPS:
        result: GenerateResult = llm.generate_with_tools(messages, TOOL_DECLARATIONS, system_instruction=SYSTEM_PROMPT)

        if not result.tool_calls:
            break

        messages.append(Message(role="model", text=result.text, tool_calls=tuple(result.tool_calls)))

        tool_results: list[ToolResult] = []

        for call in result.tool_calls:
            if len(steps) >= AGENT_MAX_STEPS:
                break

            tool_fn = TOOL_FUNCTIONS.get(call.name)
            if tool_fn is None:
                # The model asked to call a tool that was never declared in
                # TOOL_DECLARATIONS — a hallucinated tool call, not a real
                # bug in the trace content itself. Record it as its own step
                # (so it's visible in the trace instead of silently dropped)
                # and tell the model it's invalid via a normal tool result,
                # so it can recover on its own instead of this crashing the
                # whole /run-agent request with an unhandled KeyError.
                steps.append(
                    TraceStep(
                        step_id=len(steps) + 1,
                        actor=agent_id,
                        input_text=f"Model requested an undeclared tool call: '{call.name}' with args {call.args}",
                        # Not "user" or "tool_output" — this wasn't driven by
                        # the user's goal or by real tool content, it's the
                        # agent's own invalid decision. "agent_handoff" is
                        # the closest fit among the fixed InputSource values;
                        # adding a dedicated value would change the shared
                        # trace schema (see CLAUDE.md — flag before doing
                        # that, don't just add one here).
                        input_source="agent_handoff",
                        input_provenance="internal",
                        action=call.name,
                        action_params={"hallucinated_tool_call": True, "requested_args": call.args},
                        timestamp=_now(),
                    )
                )
                tool_results.append(
                    ToolResult(call_id=call.id, name=call.name, output={"error": f"tool '{call.name}' is not available"})
                )
                continue

            try:
                result_value = tool_fn(**call.args)
            except TypeError as e:
                # The model called a real, declared tool but with bad
                # arguments (missing a required one, an unexpected extra
                # one, etc.) -- not a bug in the trace content itself. Same
                # pattern as the hallucinated-tool-call case above: record
                # it as its own step and tell the model via a normal tool
                # result so it can recover on its own, instead of an
                # unhandled TypeError crashing the whole /run-agent request.
                steps.append(
                    TraceStep(
                        step_id=len(steps) + 1,
                        actor=agent_id,
                        input_text=f"Model called '{call.name}' with invalid arguments {call.args}: {e}",
                        input_source="agent_handoff",
                        input_provenance="internal",
                        action=call.name,
                        action_params={"invalid_tool_call": True, "requested_args": call.args, "error": str(e)},
                        timestamp=_now(),
                    )
                )
                tool_results.append(
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        output={"error": f"invalid arguments for tool '{call.name}': {e}"},
                    )
                )
                continue

            steps.append(
                TraceStep(
                    step_id=len(steps) + 1,
                    actor=agent_id,
                    input_text=pending_input_text,
                    input_source=pending_input_source,
                    input_provenance=pending_input_provenance,
                    action=call.name,
                    action_params=call.args,
                    timestamp=_now(),
                )
            )
            tool_results.append(ToolResult(call_id=call.id, name=call.name, output={"result": result_value}))

            # Email content is untrusted/external. Any step taken on the back
            # of it — including ones several turns later — must inherit that
            # provenance until fresh trusted input replaces it.
            if call.name == "read_email":
                pending_input_text = "\n\n".join(
                    f"From: {email['from']}\nSubject: {email['subject']}\n{email['body']}" for email in result_value
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
                            action_params={"tool": call.name},
                            timestamp=_now(),
                        )
                    )

        if tool_results:
            messages.append(Message(role="user", tool_results=tuple(tool_results)))

    return Trace(trace_id=trace_id, agent_id=agent_id, original_goal=original_goal, steps=steps)
