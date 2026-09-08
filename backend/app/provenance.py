import networkx as nx

from app.schemas import ProvenanceFlag, Trace


def _build_graph(trace: Trace) -> nx.DiGraph:
    """Builds a directed graph of the trace's steps, in order, one node per step_id."""
    graph = nx.DiGraph()

    previous_step_id = None
    for step in trace.steps:
        graph.add_node(step.step_id, actor=step.actor, input_provenance=step.input_provenance)
        if previous_step_id is not None:
            graph.add_edge(previous_step_id, step.step_id)
        previous_step_id = step.step_id

    return graph


def get_provenance_flag(trace: Trace, step_id: int) -> ProvenanceFlag:
    """Returns "internal", "external", or "tainted" for the given step of the trace.

    A step's own declared input_provenance is trusted as-is, unless it's the
    receiving end of an agent-to-agent handoff (its actor differs from a
    predecessor's) and some upstream step in the trace ever carried external
    content — in that case it's "tainted" even though it claims internal.
    This is the cross-agent poisoning case: a compromised agent can't launder
    external content into a trusted-looking handoff to another agent.
    """
    graph = _build_graph(trace)
    steps_by_id = {step.step_id: step for step in trace.steps}
    step = steps_by_id[step_id]

    if step.input_provenance == "external":
        return "external"

    # step.input_provenance == "internal": this step looks trusted on its
    # face. Check whether it's the receiving end of an agent-to-agent
    # handoff — if so, and any upstream step ever carried external content,
    # this node inherits that taint even though it claims to be internal.
    predecessors = list(graph.predecessors(step_id))
    is_handoff = bool(predecessors) and any(
        steps_by_id[predecessor_id].actor != step.actor for predecessor_id in predecessors
    )

    if is_handoff:
        upstream_step_ids = nx.ancestors(graph, step_id)
        if any(steps_by_id[upstream_id].input_provenance == "external" for upstream_id in upstream_step_ids):
            return "tainted"

    return "internal"
