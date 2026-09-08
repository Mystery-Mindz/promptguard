from app.provenance import get_provenance_flag
from app.schemas import Trace, TraceStep


def _step(step_id: int, actor: str, input_provenance: str, action: str = "noop") -> TraceStep:
    return TraceStep(
        step_id=step_id,
        actor=actor,
        input_text="text",
        input_source="user",
        input_provenance=input_provenance,
        action=action,
        action_params={},
        timestamp="2026-01-01T00:00:00Z",
    )


def test_no_handoff_external_step_reports_external():
    trace = Trace(
        trace_id="t1",
        agent_id="agent_A",
        original_goal="Read and summarize today's emails",
        steps=[
            _step(1, "agent_A", "internal"),
            _step(2, "agent_A", "external"),
        ],
    )

    assert get_provenance_flag(trace, 2) == "external"


def test_agent_handoff_from_external_upstream_reports_tainted():
    trace = Trace(
        trace_id="t2",
        agent_id="agent_A",
        original_goal="Read and summarize today's emails",
        steps=[
            _step(1, "agent_A", "internal"),
            _step(2, "agent_A", "external"),  # agent_A picked up external content
            _step(3, "agent_B", "internal"),  # handoff to agent_B, claims internal
        ],
    )

    assert get_provenance_flag(trace, 3) == "tainted"
