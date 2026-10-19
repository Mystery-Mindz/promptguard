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
      the step's actor received external content via an agent-to-agent
      handoff — either this step IS that handoff (its `actor` differs from
      the step(s) before it, and external content exists upstream), or the
      actor was tainted by an earlier handoff and hasn't had its taint
      reset since. This is the cross-agent poisoning case: a compromised
      agent can't launder external content into a handoff that looks
      trustworthy to another agent, and that agent's actions on the back
      of it keep getting flagged for as long as it keeps acting on that
      laundered content — not just on the first step after the handoff.
    - `"internal"` — genuinely trusted: declared internal, and this actor
      has never been tainted (no handoff from external-carrying upstream),
      or a fresh, directly user-sourced input (`input_source == "user"`)
      has since reset this actor's taint.

    Taint is tracked per-actor across the whole trace in one forward pass
    (steps are processed in their existing order) rather than only looking
    one step back, so it correctly spans every later step the receiving
    agent takes, not just the first one right after the handoff.
    """
    graph = _build_graph(trace)
    steps_by_id = {step.step_id: step for step in trace.steps}

    tainted_actors: set[str] = set()
    seen_external = False
    flags: dict[int, ProvenanceFlag] = {}

    for node_id in graph.nodes:
        step = steps_by_id[node_id]
        predecessors = list(graph.predecessors(node_id))
        is_handoff = bool(predecessors) and any(
            steps_by_id[predecessor_id].actor != step.actor for predecessor_id in predecessors
        )

        if step.input_provenance == "external":
            flags[node_id] = "external"
            seen_external = True
            continue

        if is_handoff:
            # The receiving end of a handoff: tainted if external content
            # exists anywhere upstream, or if this same actor was already
            # tainted (a laundering agent handing off to itself/another
            # tainted actor shouldn't get a clean slate).
            if seen_external or step.actor in tainted_actors:
                tainted_actors.add(step.actor)
                flags[node_id] = "tainted"
            else:
                flags[node_id] = "internal"
        elif step.actor in tainted_actors:
            # Not a handoff, but this actor is still carrying taint from an
            # earlier one. A genuinely fresh, directly user-sourced input
            # resets it; anything else (another tool output, another
            # handoff-adjacent step, etc.) keeps the taint flowing forward.
            if step.input_source == "user":
                tainted_actors.discard(step.actor)
                flags[node_id] = "internal"
            else:
                flags[node_id] = "tainted"
        else:
            flags[node_id] = "internal"

    return flags[step_id]
