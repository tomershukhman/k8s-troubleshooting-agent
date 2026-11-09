"""The investigator (DESIGN 3.1, 3.4): the agent that diagnoses, with the read-only MCP tools only.
A LangChain agent (`create_agent`): the system prompt, native structured output (`Diagnosis`),
and the middleware that tags each tool result as evidence, retries failed calls, and enforces
the per-round and run-wide limits. The graph (graph.py) adds it as its `investigate` node."""

import textwrap
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    InputAgentState,
    ModelCallLimitMiddleware,
    ModelRefusalMiddleware,
    OutputAgentState,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from k8s_agent import prompts
from k8s_agent.agent.middleware import (
    EvidenceMiddleware,
    RunContext,
    retries_exhausted,
    tool_error_text,
    worth_retrying,
)
from k8s_agent.config import Settings
from k8s_agent.schemas import CATEGORY_HELP, Diagnosis

type Investigator = CompiledStateGraph[
    AgentState[Diagnosis], RunContext, InputAgentState, OutputAgentState[Diagnosis]
]


def system_prompt(settings: Settings) -> str:
    return prompts.load("investigator_system").format(
        categories=textwrap.indent(CATEGORY_HELP, "  "),
        max_tool_calls=settings.max_tool_calls,
        remediation_namespaces=", ".join(settings.remediation_namespaces),
    )


def build_investigator(
    model: BaseChatModel, tools: list[BaseTool], settings: Settings
) -> Investigator:
    # run_limit: per investigate round; thread_limit: the whole run, one thread per run. A
    # thread reused across questions would need this budget reset per question.
    # Each middleware has its own state and context types, which create_agent merges; its
    # signature wants one of each for the whole list.
    middleware: list[AgentMiddleware[Any, Any]] = [
        EvidenceMiddleware(),
        # A failed call raises: it is retried with backoff unless a retry can't fix it,
        # then comes back to the model as a tool error with the server's text.
        ToolErrorMiddleware(on_error=tool_error_text),
        ToolRetryMiddleware(
            max_retries=settings.tool_max_retries,
            initial_delay=settings.tool_retry_delay_seconds,
            retry_on=worth_retrying,
            on_failure=retries_exhausted(settings.tool_max_retries + 1),
        ),
        ModelRefusalMiddleware(),
        ModelCallLimitMiddleware(  # "error": the run stops, and the core says which limit
            run_limit=settings.max_model_calls,
            thread_limit=settings.run_max_model_calls,
            exit_behavior="error",
        ),
        ToolCallLimitMiddleware(  # "continue": further calls refused, the model concludes
            run_limit=settings.max_tool_calls,
            thread_limit=settings.run_max_tool_calls,
            exit_behavior="continue",
        ),
    ]
    return create_agent(
        model,
        tools,
        system_prompt=system_prompt(settings),
        # Native structured output, set explicitly: a bare schema lets LangChain fall back to a
        # forced tool call, which Sonnet 5.5 rejects.
        response_format=ProviderStrategy(Diagnosis),
        middleware=middleware,
        context_schema=RunContext,
        checkpointer=True,
        name="investigator",
    )
