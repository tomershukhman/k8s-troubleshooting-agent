"""The evidence critic: a separate model call with no tools. It sees only the draft and the
tool outputs the draft cites, and returns a structured `CriticVerdict`."""

import json
from typing import cast

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable

from k8s_agent import prompts
from k8s_agent.agent import tool_output
from k8s_agent.agent.records import Step
from k8s_agent.schemas import CriticVerdict, Diagnosis

PROMPT = prompts.load("critic_system")
# The tag around each cited output. Inside untrusted text (a log line, the model's words) it is
# made inert, so that text can't close a block early and add evidence of its own.
TAG = "tool_output"

type Critic = Runnable[LanguageModelInput, CriticVerdict]


def inert(text: str) -> str:
    return tool_output.inert(text, TAG)


def with_verdict(model: BaseChatModel) -> Critic:
    """The critic model, answering with a `CriticVerdict`. `json_schema` = native structured
    output; the default (`function_calling`) forces a tool call, which Sonnet 5.5 rejects."""
    structured = model.with_structured_output(CriticVerdict, method="json_schema")
    # Given a Pydantic class, it answers with an instance of it; the signature says only "a
    # BaseModel or a dict", for any schema.
    return cast(Critic, structured)


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
            tool_output.quoted(TAG, step.output),
        ]
    return "\n".join(lines)


async def review(
    critic: Critic, question: str, draft: Diagnosis, steps: list[Step]
) -> CriticVerdict:
    messages = [SystemMessage(PROMPT), HumanMessage(critic_input(question, draft, steps))]
    return await critic.ainvoke(messages, config={"run_name": "critic"})  # the trace span's name
