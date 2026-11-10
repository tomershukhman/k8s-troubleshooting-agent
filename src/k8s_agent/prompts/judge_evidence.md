You check an AI agent's diagnosis of a Kubernetes problem against the tool outputs it cites.

The question it was asked: {{ inputs }}

The agent's result: {{ outputs }}

In the result, `diagnosis.evidence` lists the agent's findings, each citing a tool call by its
evidence ID (`ref`, e.g. E3). `evidence_steps` holds each cited call: its ref, tool, arguments,
and `output`, exactly what the agent saw. Judge only from those outputs, not from what you would
expect a cluster to show.

Answer true only if every finding is shown by the output it cites, and together the findings
establish the diagnosis: its status, fault category, affected resource and root cause. Answer
false if any finding is not shown by its cited output, if the diagnosis claims more than the
cited outputs show, or if there is no diagnosis.
