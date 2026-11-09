"""Tracing on the real cluster, model and tracking server. Every investigation
runs as the real command (`agent investigate`, a subprocess), so the trace link, the flush on exit
and the stored trace are what a user gets; traces are then read back from the server. Model
answers vary: a run that misses something the model decides (a reasoning summary, a refused
call) gets one retry."""

import json
import subprocess

import mlflow
import pytest
from mlflow.entities import SpanType

from k8s_agent.config import get_settings
from k8s_agent.policy import READ_ONLY_TOOLS

from .conftest import (
    ROOT,
    run_selector,
    selector_question,
    stored,
)

pytestmark = pytest.mark.usefixtures("cluster")
ATTEMPTS = 2


@pytest.fixture(scope="module")
def uri() -> str:
    uri = get_settings().mlflow_tracking_uri
    if not uri:
        pytest.fail("set MLFLOW_TRACKING_URI (see .env.example) to run the tracing tests")
    mlflow.set_tracking_uri(uri)
    return uri


def children(trace, span) -> list:
    kids = [s for s in trace.data.spans if s.parent_id == span.span_id]
    return sorted(kids, key=lambda s: s.start_time_ns)


def parent(trace, span):
    return (trace.search_spans(span_id=span.parent_id) or [None])[0] if span.parent_id else None


def root(trace):
    [span] = [s for s in trace.data.spans if s.parent_id is None]
    return span


def thinking(span) -> list[str]:
    """The reasoning summaries in a model call's stored output."""
    gens = (span.outputs or {}).get("generations") or [[]]
    content = gens[0][0]["message"]["content"] if gens[0] else []
    blocks = content if isinstance(content, list) else []
    return [b["thinking"] for b in blocks if b.get("type") == "thinking" and b.get("thinking")]


def model_calls(trace) -> list:
    return trace.search_spans(span_type=SpanType.CHAT_MODEL)


def inside(trace, span, name: str) -> bool:
    """Whether `span` is somewhere under a span called `name`."""
    while (span := parent(trace, span)) is not None:
        if span.name == name:
            return True
    return False


def tool_calls(trace) -> list:
    """The investigator's tool calls, in evidence-ID order (a fix's remediator calls: see
    test_remediate.py)."""
    calls = trace.search_spans(span_type=SpanType.TOOL)
    return sorted((s for s in calls if inside(trace, s, "investigator")), key=ref_number)


def ref_of(span) -> str | None:
    return (span.attributes.get("metadata") or {}).get("evidence_ref")


def ref_number(span) -> int:
    return int((ref_of(span) or "E0")[1:])


@pytest.fixture(scope="module")
def selector(uri, cluster, selector_run):
    """The selector scenario, investigated with a user (no namespace given): the run
    test_remediate.py shares. Run again if the model returned no reasoning summary at all, which
    happens at any effort."""
    run = selector_run
    for attempt in range(ATTEMPTS):
        if attempt:
            run = run_selector(cluster)
        trace = stored(run.result["trace_id"])
        if any(thinking(s) for s in model_calls(trace)) or attempt == ATTEMPTS - 1:
            return run.result, run.stderr, trace


# --- the brief's observability list, line by line, on a real stored trace (SPEC 3.7) --------


def test_the_flow_request_agent_model_tool_calls_response(selector):
    result, _, trace = selector
    top = root(trace)
    assert top.name == "investigation" and top.span_type == "AGENT"
    assert top.inputs["question"] == selector_question()  # the request
    assert top.outputs["diagnosis"] == result["diagnosis"]  # the response: the result itself
    # the graph's stages, in order; a second graph span resumes it after a fix's approval pause
    graphs = [s for s in children(trace, top) if s.name == "graph"]
    stages = [s.name for graph in graphs for s in children(trace, graph)]
    rounds = len(result["verification"])
    investigation = ["intake", *["investigate", "verify_evidence"] * rounds, "result"]
    assert stages[: len(investigation)] == investigation
    assert set(stages[len(investigation) :]) <= {"remediate", "investigate", "recheck"}  # a fix
    for stage in trace.search_spans(name="investigate"):
        # the agent, inside its stage (then `after_investigate`: where the graph goes next)
        assert children(trace, stage)[0].name == "investigator"
    assert model_calls(trace) and tool_calls(trace)
    every_call = trace.search_spans(span_type=SpanType.TOOL)  # each inside the agent that made it
    assert all(
        inside(trace, s, "investigator") or inside(trace, s, "remediator") for s in every_call
    )


def test_every_tool_call_is_named_with_its_evidence_id(selector):
    result, _, trace = selector
    calls = tool_calls(trace)
    assert {s.name for s in calls} <= READ_ONLY_TOOLS
    # every call carries its ID, E1..En as the run numbered them (a retried call: one span per try)
    refs = list(dict.fromkeys(ref_of(s) for s in calls))
    assert refs == [f"E{n}" for n in range(1, len(refs) + 1)]
    by_ref = {ref_of(s): s for s in calls}
    for step in result["evidence_steps"]:  # what the answer cites is in the trace
        assert by_ref[step["ref"]].name == step["tool"]


def test_each_tool_calls_arguments_and_output(selector):
    result, _, trace = selector
    by_ref = {ref_of(s): s for s in tool_calls(trace)}
    for step in result["evidence_steps"]:
        span = by_ref[step["ref"]]
        assert span.inputs == step["args"]
        # the server's raw output; empty when the call found nothing (e.g. no NetworkPolicies),
        # which is still evidence
        assert "text" in span.outputs["content"][0]
        # and what the model saw: cleaned, capped and tagged, on the enclosing tools step
        seen = parent(trace, span).outputs["messages"][0]["content"]
        assert seen.startswith(f"[{step['ref']}] ")
        assert step["output_excerpt"][:200] in seen


def test_timing_on_every_span_and_for_the_whole_run(selector):
    result, _, trace = selector
    for span in trace.data.spans:
        assert 0 < span.start_time_ns <= span.end_time_ns, span.name
        if up := parent(trace, span):  # inside its parent's time
            assert up.start_time_ns <= span.start_time_ns and span.end_time_ns <= up.end_time_ns
    top = root(trace)
    run_ms = (top.end_time_ns - top.start_time_ns) / 1e6
    assert trace.info.execution_duration == pytest.approx(run_ms, abs=5)
    assert run_ms == pytest.approx(result["duration_ms"], abs=250)  # the trace is the whole run


def test_how_the_conclusion_was_reached(selector):
    result, _, trace = selector
    summaries = [t for s in model_calls(trace) for t in thinking(s)]
    print("reasoning summaries:", summaries)
    assert summaries, "no model call in the trace has a reasoning summary"
    judged = [r for r in result["verification"] if r["findings"]]  # rounds the critic judged
    critics = trace.search_spans(name="critic")
    assert len(critics) == len(judged) and judged  # the critic's verdict, once per round
    for span, round_ in zip(critics, judged, strict=True):
        verdict = span.outputs
        assert [f["ref"] for f in verdict["findings"]] == [f["ref"] for f in round_["findings"]]
        assert all(f["reason"].strip() for f in verdict["findings"])
        assert verdict["founded"] == round_["founded"]


def test_the_user_session_and_run_settings_are_on_the_trace(selector):
    result, _, trace = selector
    meta, s = trace.info.trace_metadata, get_settings()
    assert (meta["mlflow.trace.user"], meta["mlflow.trace.session"]) == (
        "alice",
        result["thread_id"],
    )
    assert meta["run_id"] == result["run_id"]
    assert (meta["model"], meta["critic_model"], meta["effort"]) == (
        s.model,
        s.effective_critic_model,
        s.effort,
    )
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    assert meta["mlflow.source.git.commit"] == head.stdout.strip()
    usage = json.loads(meta["mlflow.trace.tokenUsage"])
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    print("tokens:", usage, "cost:", meta.get("mlflow.trace.cost"))


def test_the_trace_link_is_printed_and_the_result_has_the_trace_id(selector, uri):
    result, stderr, trace = selector
    assert trace.info.trace_id == result["trace_id"]
    experiment = trace.info.trace_location.mlflow_experiment.experiment_id
    link = f"{uri}/#/experiments/{experiment}/traces?selectedEvaluationId={result['trace_id']}"
    assert f"Trace: {link}" in stderr.splitlines()
