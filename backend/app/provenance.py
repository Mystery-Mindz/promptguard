import networkx as nx

from app.schemas import ProvenanceFlag, Trace


def _build_graph(trace: Trace) -> nx.DiGraph:
    """Builds a simple chain graph representing the order steps happened in,
    so later code can ask "what happened before this step?".

    Parameters:
    - `trace`: the trace to build a graph from.

    Returns a NetworkX directed graph (`nx.DiGraph`) with one node per
    `step_id` (each node also stores that step's `actor` and
    `input_provenance`), and one edge from each step to the very next step
    in the trace.
    """
    graph = nx.DiGraph()

    previous_step_id = None
    for step in trace.steps:
        graph.add_node(step.step_id, actor=step.actor, input_provenance=step.input_provenance)
        if previous_step_id is not None:
            graph.add_edge(previous_step_id, step.step_id)
        previous_step_id = step.step_id

    return graph


def get_provenance_flag(trace: Trace, step_id: int) -> ProvenanceFlag:
    """The main entry point of the Cross-Agent Provenance Tracer: decides
    whether a given step's input should be trusted, distrusted, or treated
    as secretly distrusted despite looking trustworthy.

    Parameters:
    - `trace`: the full trace the step belongs to.
    - `step_id`: which step to check.

    Returns one of three flags:
    - `"external"` — the step's own `input_provenance` is already marked
      external (e.g. it came from an email or other untrusted source).
    - `"tainted"` — the step's `input_provenance` claims "internal", but
      it's the receiving end of an agent-to-agent handoff (its `actor`
      differs from the step(s) before it), and some earlier step upstream
      in the trace ever carried external content. This is the cross-agent
      poisoning case: a compromised agent can't launder external content
      into a handoff that looks trustworthy to another agent — the
      receiving step gets flagged anyway.
    - `"internal"` — genuinely trusted: declared internal, and either not a
      handoff or no external content anywhere upstream.
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
