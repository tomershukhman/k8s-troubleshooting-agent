"""The prompts folder: one Markdown file per prompt, named for the agent that reads it."""

from importlib import resources

from k8s_agent import prompts

AGENTS = ("investigator", "critic", "remediator", "judge")


def test_every_prompt_file_is_named_for_its_agent_and_loads():
    files = [
        f.name for f in resources.files("k8s_agent.prompts").iterdir() if f.name.endswith(".md")
    ]
    assert len(files) == 8
    for name in files:
        assert name.split("_")[0] in AGENTS, name
        assert prompts.load(name.removesuffix(".md")).strip()
