"""The evidence critic: a separate model call with no tools. It sees only the draft and the
tool outputs the draft cites, and returns a structured `CriticVerdict`."""

import json
import re
from importlib import resources
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from k8s_agent.agent.records import Step
from k8s_agent.schemas import CriticVerdict, Diagnosis

PROMPT = resources.files("k8s_agent.agent").joinpath("critic.md").read_text()
# The tag around each cited output. Inside untrusted text (a log line, the model's words) it is
# made inert, so that text can't close a block early and add evidence of its own.
TAG = re.compile(r"<(\s*/?\s*tool_output\b)", re.IGNORECASE)


def inert(text: str) -> str:
    return TAG.sub(r"&lt;\1", text)


def with_verdict(model: BaseChatModel) -> Any:
    """The critic model, answering with a `CriticVerdict`. `json_schema` = native structured
    output; the default (`function_calling`) forces a tool call, which Sonnet 5.5 rejects."""
    return model.with_structured_output(CriticVerdict, method="json_schema")


def critic_input(question: str, draft: Diagnosis, steps: list[Step]) -> str:
    by_ref = {s.ref: s for s in steps}
    lines = [
        f"Question: {inert(question)}",
        "",
        "Draft answer:",
        inert(draft.model_dump_json(exclude={"evidence"}, indent=2)),
        "",
        "Findings, each with the tool output it cites:",
    ]
    for i, e in enumerate(draft.evidence, 1):
        step = by_ref[e.ref]
        error = " (the tool returned an error)" if step.error else ""
        lines += [
            "",
            inert(f"{i}. [{e.ref}] {e.finding}"),
            inert(f"Cited: {step.tool} {json.dumps(step.args, sort_keys=True)}{error}"),
            f"<tool_output>\n{inert(step.output)}\n</tool_output>",
        ]
    return "\n".join(lines)


async def review(critic: Any, question: str, draft: Diagnosis, steps: list[Step]) -> CriticVerdict:
    messages = [SystemMessage(PROMPT), HumanMessage(critic_input(question, draft, steps))]
    return await critic.ainvoke(messages, config={"run_name": "critic"})  # the trace span's name
