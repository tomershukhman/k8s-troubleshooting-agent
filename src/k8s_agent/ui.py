"""What `agent investigate` shows a person (Rich): which agent is working and what it does as
it happens, then the result as a readable summary. Scripts get the result JSON (cli.py)."""

import re
from types import TracebackType
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from k8s_agent.agent import (
    Drafted,
    ProgressEvent,
    Reasoning,
    Stage,
    ToolCalled,
    Verified,
    tool_output,
)
from k8s_agent.agent.records import RunResult, Step, VerifyRound

ACTIVITY = {
    "intake": "Starting…",
    "investigate": "Investigator working…",
    "verify_evidence": "Checking the cited evidence…",
    "result": "Writing up the result…",
}
# The label in front of each progress line: what kind of thing it is, at its depth.
LABELS = {
    "node": ("node", "bold blue", 0),
    "agent": ("agent", "bold cyan", 2),
    "tool": ("tool", "bold yellow", 4),
    "thinking": ("thinking", "italic dim", 4),
    "draft": ("draft", "bold", 4),
    "check": ("check", "bold magenta", 2),
    "verdict": ("", "", 4),
    "next": ("", "", 2),
}


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


def preview(step: Step) -> str:
    """One line on what a tool returned: the names in a table, the kind of a YAML object,
    the events counted, or a log's last line."""
    lines = [line for line in step.output.splitlines() if line.strip()]
    if step.error or not lines:
        return lines[0] if lines else "(empty)"
    header = lines[0]
    if column := re.search(r"(?<!\S)NAME(?!\S)", header):  # a table: list its NAME column
        at = column.start()
        names = [row[at:].split()[0] for row in lines[1:] if len(row) > at]
        return f"{', '.join(names)}  ({len(names)} row{'s' * (len(names) != 1)})"
    if header.startswith("# The following events"):
        warnings = sum("Type: Warning" in line for line in lines)
        return f"{sum(line.startswith('- ') for line in lines)} events, {warnings} warnings"
    if header.startswith("apiVersion:"):
        kind = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("kind:")), "")
        return f"{kind} ({len(lines)} lines of YAML)"
    return f"{lines[-1].strip()}  ({len(lines)} lines)"


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

    def line(self, kind: str, *parts: str | tuple[str, str]) -> None:
        label, style, depth = LABELS[kind]
        text = Text.assemble(" " * depth, (f"{label:<9}" if label else "", style), *parts)
        text.truncate(self.console.width, overflow="ellipsis")
        self.console.print(text)

    def __call__(self, event: ProgressEvent) -> None:
        match event:
            case Stage(name=name, round=n):
                self.status.update(ACTIVITY[name])
                self.line("node", (name, "bold"), (f" · round {n}" if n > 1 else "", "dim"))
                if name == "investigate":
                    self.line("agent", "investigator", (f" · {self.models['investigator']}", "dim"))
            case ToolCalled(step=s):
                call = f"({describe_call(s.args)})  "
                self.line("tool", (s.tool, "bold"), call, (f"{s.ref} · {s.latency_ms} ms", "dim"))
                if denied := refused(s):  # expected outside granted namespaces: not a failure
                    self.line("verdict", " " * 9, (denied, "yellow"))
                else:
                    self.line("verdict", " " * 9, (preview(s), "red" if s.error else "dim"))
            case Reasoning(text=text):
                self.line("thinking", (" ".join(text.split())[: self.summary_chars], "italic dim"))
            case Drafted(diagnosis=d):
                r = d.affected_resource
                what = f"{d.status} · {d.fault_category}" + (f" · {r.kind}/{r.name}" if r else "")
                self.line("draft", what, (f" · cites {' '.join(d.refs) or 'nothing'}", "dim"))
            case Verified(round=r):
                self.verdict(r)

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


def summary(result: RunResult) -> RenderableType:
    """The result for a person: what's wrong, the evidence for it, and what to do."""
    rounds = len(result.verification)
    footer = (
        f"{result.duration_ms / 1000:.1f} s · {rounds} verify round{'s' * (rounds != 1)} · "
        f"run {result.run_id[:8]}"
    )
    d = result.diagnosis
    if d is None:
        body = Text(result.error or "unknown error")
        return Panel(body, title="[bold red]Run failed", subtitle=footer, border_style="red")
    label, color = TITLES[d.status]
    title = f"[bold {color}]{label}[/]"
    if d.status == "fault_found":
        r = d.affected_resource
        title += f" · {d.fault_category} · {r.kind}/{r.name} in {r.namespace}"
    parts: list[RenderableType] = [Text(d.root_cause)]
    if d.evidence:
        steps = {s.ref: s for s in result.evidence_steps}
        table = Table(box=None, show_header=True, header_style="bold dim", pad_edge=False)
        table.add_column("Ref")
        table.add_column("Finding", ratio=3)
        table.add_column("From", style="dim", ratio=2)
        for e in d.evidence:
            s = steps.get(e.ref)
            table.add_row(e.ref, e.finding, f"{s.tool} {describe_call(s.args)}" if s else "")
        parts += [Text(), table]
    parts += [Text(), Text.assemble(("Next step: ", "bold"), d.recommended_action)]
    if fix := d.proposed_fix:
        selector = ", ".join(f"{k}={v}" for k, v in fix.selector_dict().items())
        parts.append(
            Text.assemble(
                ("Proposed fix: ", "bold"),
                f"set the selector of Service {fix.namespace}/{fix.service} to {selector}. ",
                (f"{fix.explanation} Nothing has been changed.", "dim"),
            )
        )
    return Panel(
        Group(*parts), title=title, title_align="left", subtitle=footer, border_style=color
    )
