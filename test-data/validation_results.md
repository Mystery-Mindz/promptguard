# Validation Results

**Final result: 18/18 traces produced the correct decision.**

- 11/11 clean traces correctly resulted in `allow_logged`
- 6/6 injected traces correctly resulted in `approval_required`
- 1/1 cross-agent handoff trace correctly flagged at its code-guaranteed step (step 3, `tainted`)

Full breakdown and reproduction: `python run_validation.py --test-data-dir ../test-data/traces` from `backend/`.

## Known limitations

1. **The cross-agent handoff's earlier step is a probabilistic detection, not a guaranteed one.** In `trace_01`, step 2 (the initial external read, before the tainted handoff) depends on the live judge model's `injection_confidence` for that specific borderline text crossing the approval threshold — it scored `risk_score` ~54-57 (flagged) in early runs and 22 (not flagged) in a later run with a different judge model/prompt. Only step 3 (the tainted handoff itself) has a code-level guarantee: `gate.py`'s tainted floor forces `approval_required` regardless of `risk_score`. `expected_flagged_steps` for `trace_01` is `[3]` for exactly this reason — step 2 is documented, expected variance, not something to assert as a hard pass/fail expectation.

2. **Severity ranking within suspicious content is coarse.** Across the six injected traces (six distinct injection styles — calendar invite, admin impersonation, footer note, classic urgent/authority, bureaucratic authority, reply hijack), risk_score landed within a 6-point band (50-56). Binary suspicious-vs-clean detection is reliable — clean traces scored 20-24 and injected traces scored 50-56, a stable ~30-point separation — but the system cannot currently distinguish a subtler injection attempt from a more blatant one; that's a direction for future work (e.g. a richer judge prompt, a different judge model, or a differently-weighted scoring scheme), not a current capability.

## Bugs found and fixed during validation

1. **Tool-output capture gap.** `agent.py` recorded a `read_email` call's action and params, but never recorded what the tool actually *returned* into any `TraceStep`. If the agent didn't take a further action based on that content, the content — including any injected instruction inside it — was silently dropped from the trace, and `drift_engine` never saw it during a live agent run. Confirmed by tracing the trace-recording logic and reproducing a live run start to finish. Fixed by having `agent.py` record an additional step (`action="tool_output_received"`) whenever `read_email` returns content, with `input_source="tool_output"`, `input_provenance="external"`, and `input_text` set to the actual returned content.

2. **Drift-embedding blindness.** `drift_engine.py`'s `_describe_action` produced a generic label (`"tool output received (tool read_email)"`) for `tool_output_received` steps, built from `action_params` alone — but `action_params` for that step type is always the same generic `{"tool": "read_email"}` regardless of what was actually read. As a result, `drift_score` for these steps was structurally blind to the real content and only varied with each trace's goal text, not its injected content (confirmed via raw-output inspection: two traces with different injected emails but the same goal produced bit-identical `drift_score` values before the fix). Fixed by branching in `_text_to_embed_for_drift`: `tool_output_received` steps now embed the step's actual `input_text`; all other steps still embed the action description as before.

Both bugs were caught by inspecting raw, unparsed model output and intermediate values directly (not just final risk_score/decision), before any fix was applied.
