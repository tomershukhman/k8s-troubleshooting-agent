"""Every prompt a model reads, one Markdown file each, named `<agent>_<what>.md`:
- investigator_system, investigator_feedback, investigator_verdict_unread, investigator_recheck
- critic_system
- remediator_system, remediator_fix_request
- judge_evidence (the evals' evidence judge; the advice judge takes each scenario's rubric)
A file with `{placeholders}` is a `str.format` template; judge_evidence is an MLflow one."""

from importlib import resources


def load(name: str) -> str:
    return resources.files(__package__).joinpath(f"{name}.md").read_text().rstrip("\n")
