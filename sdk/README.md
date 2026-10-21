# promptguard-client

A Python SDK for PromptGuard's live guard API (`/guard/*`, see
`docs/plan/00_OVERVIEW.md` and `backend/CLAUDE.md`). Depends on nothing but
`httpx`.

## Install

```bash
pip install -e sdk/
```

## Example

```python
from promptguard_client import PromptGuard, ToolBlocked, ToolDenied

guard = PromptGuard(base_url="http://127.0.0.1:8000", api_key="demo-key")
session = guard.start(agent_id="assistant", goal="summarize today's emails")

@session.guarded_tool(actor="assistant", destructive=True)
def send_message(to: str, content: str) -> None:
    real_email_client.send(to, content)  # only runs if PromptGuard allows it

try:
    send_message(
        to="a@b.com", content="here's the summary",
        input_text=email_body, input_source="tool_output", input_provenance="external",
    )
except ToolBlocked as e:
    print("blocked:", e.detection.explanation)
except ToolDenied as e:
    print("denied or timed out:", e.detection.explanation)
```

## How `guarded_tool` decides

- `allow_logged` → runs the tool.
- `block` → raises `ToolBlocked(detection)`, the tool is **not** run.
- `approval_required` → polls `GET /guard/approval` every `poll_interval`
  seconds (default 2s) for up to `timeout` seconds (default 120s).
  `"approved"` runs the tool; `"denied"` or a timeout raises `ToolDenied`.
  Pass `wait=False` to raise `ApprovalPending(step_id)` immediately instead
  of blocking — resume later with `session.approval(step_id)`.
- `PromptGuard(..., enforce=False)` still calls the real `/guard/check` (so
  the decision is genuinely recorded) but always runs the tool regardless —
  the honest "guard OFF" comparison.

The tool's own arguments must be passed as keywords (they double as
`action_params`); every call also needs `input_text`, `input_source`, and
`input_provenance` — what triggered this call.

## Lower-level API

For full control instead of the decorator:

```python
result = session.check(actor, action, params, input_text, input_source, input_provenance)
# result.decision is "allow_logged" | "approval_required" | "block" — act on it yourself.

session.observe(actor, input_text, input_source, input_provenance)  # content arriving: an email, a web page, a handoff
```

## Testing

```bash
pip install -e "sdk/[dev]"
pytest sdk/tests/
```

Every test runs against `httpx.MockTransport` — no live backend, no live
model API calls.
