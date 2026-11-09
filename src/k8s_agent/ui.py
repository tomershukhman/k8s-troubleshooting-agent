"""What the CLI shows a person (Rich): `agent investigate`'s live progress (which agent is
working and what it does), the approval prompt's panels and the result summary; and
`agent eval`'s results table. Scripts get the result JSON (cli.py)."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from k8s_agent.agent import (
    ApprovalRequest,
    Decided,
    Drafted,
    FixCalled,
    ProgressEvent,
    Reasoning,
    Rechecked,
    Remediated,
    Stage,
    ToolCalled,
    Verified,
    tool_output,
)
from k8s_agent.agent.records import (
    EvidenceStep,
    FixArgs,
    FixStep,
    Remediation,
    RunResult,
    Step,
    VerifyRound,
)
from k8s_agent.kube import labels
from k8s_agent.policy import FIX_TOOL
from k8s_agent.schemas import Diagnosis, PostFixCheck

if TYPE_CHECKING:  # the eval extra's full mlflow: imported only when `agent eval` runs
    from k8s_agent.evals import ScenarioResult

ACTIVITY = {
    "intake": "Starting…",
    "investigate": "Investigator working…",
    "verify_evidence": "Checking the cited evidence…",
    "result": "Writing up the result…",
    "remediate": "Remediator checking the fix…",
    "recheck": "Checking the recheck's evidence…",
}
# The label in front of each progress line: what kind of thing it is, at its depth.
LABEL_WIDTH = 9  # the longest label and a space
LABELS = {
    "node": ("node", "bold blue", 0),
    "agent": ("agent", "bold cyan", 2),
    "tool": ("tool", "bold yellow", 4),
    "thinking": ("thinking", "italic dim", 4),
    "draft": ("draft", "bold", 4),
    "check": ("check", "bold magenta", 2),
    "approval": ("approval", "bold magenta", 2),
    "verdict": ("", "", 4),
    "output": ("", "", 4 + LABEL_WIDTH),  # a tool call's result, under the call
    "next": ("", "", 2),
}
# ANSI escape sequences, such as the colours in a pod's log.
ANSI = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI: ESC [ (or 8-bit), parameters, a final byte
    r"|\x1b\][^\x00-\x1f\x7f-\x9f]*(?:\x07|\x1b\\|\x9c)"  # OSC: ESC ], text, BEL or ST
)
# C0 and C1 control characters but tab and newline. From the cluster or a model they could
# move the cursor or rewrite lines on the terminal; Rich keeps ESC.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(text: str) -> str:
    """`text` fit for the terminal: ANSI escape sequences removed, then each other control
    character but tab and newline replaced by U+FFFD."""
    return CONTROL.sub("\ufffd", ANSI.sub("", text))


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return safe(value)
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def scrubbed[M: BaseModel](record: M) -> M:
    """A copy of `record` with every string in it made safe(), for showing it whole."""
    return type(record).model_validate(_scrub(record.model_dump(mode="json")))


def describe_call(args: dict[str, Any]) -> str:
    """Tool arguments the way kubectl would say them: `Service/checkout -n shop-checkout`."""
    args = {k: v for k, v in args.items() if v not in (None, "", False) and k != "apiVersion"}
    target = "/".join(str(args.pop(k)) for k in ("kind", "name") if k in args)
    namespace = f"-n {args.pop('namespace')}" if "namespace" in args else ""
    rest = " ".join(f"{k}={v}" for k, v in args.items())
    return " ".join(part for part in (target, namespace, rest) if part)


def refused(step: Step) -> str | None:
    """A refused call in plain words: `no access: can't list pods in namespace default`."""
    if not (step.error and (m := tool_output.REFUSED.search(step.output))):
        return None
    verb, resource, namespace = m.groups()
    return f"no access: can't {verb} {resource} " + (
        f"in namespace {namespace}" if namespace else "cluster-wide"
    )


def describe_fix_call(step: FixStep) -> str:
    """A remediator read, like describe_call, with a selector as `kubectl` writes it."""
    a = step.args
    if isinstance(a.get("selector"), dict):
        a = {**a, "selector": labels(a["selector"])}
    return describe_call(a)


def preview(step: Step) -> str:
    """One line on what a tool returned: the names in a table, the kind of a YAML object,
    the events counted, or a log's last line."""
    lines = [line for line in step.output.splitlines() if line.strip()]
    if step.error or not lines:
        return lines[0] if lines else "(empty)"
    header = lines[0]
    if column := re.search(r"(?<!\S)NAME(?!\S)", header):  # a table: list its NAME column
        at = column.start()
        names = [(row[at:].split() or ["?"])[0] for row in lines[1:] if len(row) > at]
        return f"{', '.join(names)}  ({len(names)} row{'s' * (len(names) != 1)})"
    if header.startswith("# The following events"):
        warnings = sum("Type: Warning" in line for line in lines)
        return f"{sum(line.startswith('- ') for line in lines)} events, {warnings} warnings"
    if header.startswith("apiVersion:"):
        kind = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("kind:")), "")
        return f"{kind} ({len(lines)} lines of YAML)"
    return f"{lines[-1].strip()}  ({len(lines)} lines)"


def _which_time(node: str, n: int, recheck: bool) -> str:
    """A node that runs again: which time it is."""
    if recheck:
        return "recheck after the fix"
    if node == "remediate":  # it starts again only to resume after the approval
        return "resumed"
    return f"round {n}"


class Progress:
    """Live progress on stderr: a spinner for what's happening now and, as it happens, the
    graph's nodes and, inside them, each agent's work: the investigator's tool calls,
    reasoning and draft; the code checks and the critic's verdict on each finding."""

    def __init__(self, console: Console, models: dict[str, str], summary_chars: int) -> None:
        self.console, self.summary_chars = console, summary_chars
        self.models = {agent: model.split(":", 1)[-1] for agent, model in models.items()}
        self.status = console.status("Starting…")

    def __enter__(self) -> Progress:
        self.status.start()
        return self

    def __exit__(self, *_: type[BaseException] | BaseException | TracebackType | None) -> None:
        self.status.stop()

    @contextmanager
    def paused(self) -> Iterator[None]:
        """No spinner while a prompt is up."""
        self.status.stop()
        try:
            yield
        finally:
            self.status.start()

    def line(self, kind: str, *parts: str | tuple[str, str]) -> None:
        label, style, depth = LABELS[kind]
        shown = [(safe(p[0]), p[1]) if isinstance(p, tuple) else safe(p) for p in parts]
        text = Text.assemble(
            " " * depth, (label.ljust(LABEL_WIDTH) if label else "", style), *shown
        )
        text.truncate(self.console.width, overflow="ellipsis")
        self.console.print(text)

    def __call__(self, event: ProgressEvent) -> None:
        match event:
            case Stage(name=name, round=n, recheck=again):
                self.status.update(
                    "Investigator checking that the fix worked…" if again else ACTIVITY[name]
                )
                which = f" · {_which_time(name, n, again)}" if n > 1 else ""
                self.line("node", (name, "bold"), (which, "dim"))
                if name == "investigate" or (name == "remediate" and n == 1):
                    agent = "remediator" if name == "remediate" else "investigator"
                    self.line("agent", agent, (f" · {self.models[agent]}", "dim"))
            case ToolCalled(step=s):
                call = f"({describe_call(s.args)})  "
                self.line("tool", (s.tool, "bold"), call, (f"{s.ref} · {s.latency_ms} ms", "dim"))
                if denied := refused(s):  # expected outside granted namespaces: not a failure
                    self.line("output", (denied, "yellow"))
                else:
                    self.line("output", (preview(s), "red" if s.error else "dim"))
            case Reasoning(text=text):
                self.line("thinking", (" ".join(text.split())[: self.summary_chars], "italic dim"))
            case Drafted(diagnosis=d):
                r = d.affected_resource
                what = f"{d.status} · {d.fault_category}" + (f" · {r.kind}/{r.name}" if r else "")
                self.line("draft", what, (f" · cites {' '.join(d.refs) or 'nothing'}", "dim"))
            case Verified(round=r):
                self.verdict(r)
            case FixCalled(step=s) if s.tool != FIX_TOOL:  # the fix: the approval lines below
                self.line("tool", (s.tool, "bold"), f"({describe_fix_call(s)})")
                first = (s.output.strip().splitlines() or ["(empty)"])[0]
                self.line("output", (first, "red" if s.error else "dim"))
            case Decided(request=q, decision=d):
                a, now = q.args, q.facts.current_selector
                change = f"{labels(now)} → " if now is not None else ""
                wanted = a.get("selector") if isinstance(a.get("selector"), dict) else None
                self.line("approval", f"Service/{a.get('service')} -n {a.get('namespace')}: ",
                          (f"{change}{labels(wanted)}", "bold"))  # fmt: skip
                mark = ("✓ ", "green") if d.approved else ("✗ ", "red")
                verdict = ("approved", "bold green") if d.approved else ("rejected", "bold red")
                self.line("verdict", mark, verdict, f": {d.reason}", (f" (by {d.by})", "dim"))
            case Remediated(remediation=r):
                style = "bold green" if r.outcome == "applied" else "bold yellow"
                self.line("next", (f"→ {r.outcome}: ", style), r.detail)
            case Rechecked(remediation=r):
                if r.verified:
                    self.line("next", ("→ verified: ", "bold green"),
                              "ready endpoints, and the recheck says resolved")  # fmt: skip
                else:
                    self.line("next", ("→ not verified: ", "bold yellow"), r.recheck_note or "")

    def verdict(self, r: VerifyRound) -> None:
        for problem in r.problems:
            self.line("check", ("✗ ", "red"), problem)
        if not r.problems:
            self.line("check", "code checks passed")
        if r.findings or r.proposed_fix_backed is not None:
            self.line("agent", "critic", (f" · {self.models['critic']}", "dim"))
            for f in r.findings:
                mark = ("✓ ", "green") if f.supported else ("✗ ", "red")
                self.line("verdict", mark, (f"{f.ref}  ", "bold"), f.reason)
            if r.proposed_fix_backed is not None:
                mark = ("✓ ", "green") if r.proposed_fix_backed else ("✗ ", "red")
                backed = "is" if r.proposed_fix_backed else "isn't"
                self.line("verdict", mark, f"the proposed fix {backed} backed by the evidence")
        if r.founded:
            self.line("next", ("→ founded", "bold green"))
        else:
            self.line("next", ("→ not founded: ", "bold yellow"), r.feedback or "")


TITLES = {
    "fault_found": ("Fault found", "red"),
    "no_fault": ("No fault", "green"),
    "inconclusive": ("Inconclusive", "yellow"),
}


def summary(result: RunResult, diagnosis_shown: bool = False) -> RenderableType:
    """The result for a person: what's wrong, the evidence for it, and what to do, then what
    happened to the fix. `diagnosis_shown`: the person saw the diagnosis at the approval prompt
    (diagnosis()), so a run with a fix shows only what happened to it."""
    result = scrubbed(result)
    rounds = len(result.verification)
    footer = (
        f"{result.duration_ms / 1000:.1f} s · {rounds} verify round{'s' * (rounds != 1)} · "
        f"run {result.run_id[:8]}"
    )
    d = result.diagnosis
    if d is None:
        body = Text(result.error or "unknown error")
        return Panel(body, title="[bold red]Run failed", subtitle=footer, border_style="red")
    rem = result.remediation
    if rem and diagnosis_shown:
        return _panel(d, [fix_summary(rem, d.post_fix_check)], footer)
    parts = _findings(d, result.evidence_steps)
    if fix := d.proposed_fix:
        unchanged = "" if rem else " Nothing has been changed."  # else: the fix line says
        parts.append(
            Text.assemble(
                ("Proposed fix: ", "bold"),
                f"set the selector of Service {fix.namespace}/{fix.service} to "
                f"{labels(fix.selector_dict())}. ",
                (f"{fix.explanation}{unchanged}", "dim"),
            )
        )
    if rem:
        parts.append(fix_summary(rem, d.post_fix_check))
    return _panel(d, parts, footer)


def diagnosis(d: Diagnosis, evidence_steps: list[EvidenceStep]) -> RenderableType:
    """The verified diagnosis as the summary shows it, for a person about to approve the fix
    it proposes (the change itself: approval())."""
    d = scrubbed(d)
    return _panel(d, _findings(d, [scrubbed(s) for s in evidence_steps]))


def _findings(d: Diagnosis, evidence_steps: list[EvidenceStep]) -> list[RenderableType]:
    """What's wrong, each finding with the tool call it came from, and what to do."""
    parts: list[RenderableType] = [Text(d.root_cause)]
    if d.evidence:
        steps = {s.ref: s for s in evidence_steps}
        table = Table(box=None, show_header=True, header_style="bold dim", pad_edge=False)
        table.add_column("Ref")
        table.add_column("Finding", ratio=3)
        table.add_column("From", style="dim", ratio=2)
        for e in d.evidence:
            s = steps.get(e.ref)
            source = f"{s.tool} {describe_call(s.args)}" if s else ""
            table.add_row(Text(e.ref), Text(e.finding), Text(source))
        parts += [Text(), table]
    return [*parts, Text(), Text.assemble(("Next step: ", "bold"), d.recommended_action)]


def _panel(d: Diagnosis, parts: list[RenderableType], footer: str | None = None) -> Panel:
    """`parts` in a panel titled with what was found."""
    label, color = TITLES[d.status]
    # Text, not markup, wherever the model's words go: they may hold "[...]".
    title = Text(label, f"bold {color}")
    r = d.affected_resource
    if d.status == "fault_found" and r is not None:  # verified: a fault names its resource
        title.append(f" · {d.fault_category} · {r.kind}/{r.name} in {r.namespace}")
    return Panel(
        Group(*parts), title=title, title_align="left", subtitle=footer, border_style=color
    )


def fix_summary(r: Remediation, check: PostFixCheck | None) -> Text:
    """What happened to the fix: the decision, the outcome, and whether a recheck verified it."""
    text = Text.assemble(("Fix: ", "bold"))
    if d := r.decision:
        verdict = ("approved", "green") if d.approved else ("rejected", "red")
        text.append_text(Text.assemble(verdict, f": {d.reason} (by {d.by}, {d.at:%H:%M:%S} UTC). "))
    if r.outcome == "applied":
        text.append(f"Applied: {r.detail}. ")
        if r.verified and check:  # verified implies the recheck's own evidence
            refs = ", ".join(e.ref for e in check.evidence)
            text.append(f"Verified by a recheck with fresh evidence ({refs}).", "green")
        else:
            text.append(f"Not verified: {r.recheck_note}.", "yellow")
    elif r.outcome == "not_applied":
        text.append("Nothing was changed." if d else f"The remediator declined: {r.detail}.")
    else:
        text.append(f"{r.outcome.capitalize()}: {r.detail}.")
    return text


def approval(request: ApprovalRequest) -> RenderableType:
    """The fix a person is asked to approve: the change, the remediator's explanation, and
    facts code just read as the remediator."""
    request = scrubbed(request)
    a, f = FixArgs.model_validate(request.args), request.facts  # the gate checked it
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold")
    rows.add_column()
    now = labels(f.current_selector) if f.error is None else "?"
    # Text, not markup: these come from the cluster and the model, and may hold "[...]".
    rows.add_row("Service", Text(f"{a.namespace}/{a.service}"))
    rows.add_row("Selector", Text(f"{now}  →  {labels(a.selector)}  (replaces the whole selector)"))
    rows.add_row("Remediator", Text(a.explanation))
    if f.error is None:
        # Each pod with its controller: `checkout-a (ReplicaSet/checkout-69db948c9f)`.
        pods = ", ".join(f"{p} ({c or 'no controller'})" for p, c in f.owners.items()) or "none"
        match = f"{len(f.owners)} ready pods match {labels(a.selector)}: {pods}"
        rows.add_row("Checked now", Text(match))
    rows.add_row("Applies only if", Text(f"the Service is still at version {a.resource_version}"))
    warnings = []
    if f.error is not None:
        warnings.append(f"Couldn't check the Service as the remediator: {f.error}")
    elif f.resource_version != a.resource_version:
        warnings.append(
            f"The Service changed since the remediator read it (version {f.resource_version}): "
            "approving will be refused as stale."
        )
    elif not f.owners:
        warnings.append("No ready pod matches the new selector: approving will be refused.")
    unmanaged = [p for p, c in f.owners.items() if c is None]
    if f.error is None and unmanaged:  # a planted standalone pod, say
        warnings.append(
            f"A pod no controller manages would receive traffic: {', '.join(unmanaged)}."
        )
    body = Group(rows, *(Text(w, "bold yellow") for w in warnings))
    return Panel(body, title="[bold]Approve this fix?", title_align="left", border_style="yellow")


# The eval checks, in the results table's order: (MLflow scorer, column header).
EVAL_COLUMNS = (
    ("diagnosis_correct", "Diagnosis"),
    ("evidence_supports", "Evidence\nsupports"),
    ("advice_ok", "Advice"),
    ("evidence_exists", "Evidence\nexists"),
    ("right_tools", "Right\ntools"),
    ("actions_safe", "Safe"),
)


def eval_table(results: list[ScenarioResult]) -> Table:
    """`agent eval`'s results: runs passed per check, the fix run, then speed and cost per
    investigation (from the traces; the total includes the fix runs) and the judges' cost."""
    table = Table(title="Eval results", header_style="bold")
    table.add_column("Scenario")
    table.add_column("Result")
    for _, header in EVAL_COLUMNS:
        table.add_column(header, justify="right")
    table.add_column("Fix\nrun", justify="right")
    for header in ("Avg time", "Avg tokens", "Avg cost", "Judges"):
        table.add_column(header, justify="right")
    every = [run for r in results for run in r.runs + (r.fix.runs if r.fix else [])]
    for r in results:
        passed = [f"{r.passes.get(name, 0)}/{r.planned}" for name, _ in EVAL_COLUMNS]
        fix = "" if r.fix is None else "[green]pass[/]" if r.fix.passed else "[red]FAIL[/]"
        table.add_row(
            r.name,
            "[green]pass[/]" if r.passed else "[red]FAIL[/]",
            *passed,
            fix,
            *_costs(r.runs),
        )
    table.add_section()
    table.add_row("All", f"{sum(r.passed for r in results)}/{len(results)} pass",
                  *[""] * (len(EVAL_COLUMNS) + 1), *_costs(every, total=True))  # fmt: skip
    return table


def _costs(runs: list[dict[str, Any]], total: bool = False) -> list[str]:
    n = len(runs) or 1
    agent_cost = sum(r["cost_usd"] for r in runs)
    judges = sum(r["judge_cost_usd"] for r in runs)
    cost = f"${agent_cost / n:.3f}" + (f" (${agent_cost:.2f} total)" if total else "")
    return [
        f"{sum(r['duration_ms'] for r in runs) / n / 1000:.1f} s",
        f"{sum(r['tokens'] for r in runs) / n / 1000:.1f}k",
        cost,
        f"${judges:.2f}",
    ]
