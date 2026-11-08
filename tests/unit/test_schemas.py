"""The answer schemas (SPEC 4.2). The consistency rules are code checks (test_agent.py)."""

import pytest
from pydantic import ValidationError

from k8s_agent.schemas import CATEGORY_DEFINITIONS, Diagnosis, ProposedFix
from tests.fakes import diagnosis, selector_fix


def test_an_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(diagnosis("E1") | {"confidence": 0.9})


def test_enum_values_in_another_capitalisation_are_accepted():
    # Anthropic's docs: structured output may return an enum value in another capitalisation
    d = Diagnosis.model_validate(diagnosis("E1", status="Fault_Found", fault_category="Bad_Config"))
    assert (d.status, d.fault_category) == ("fault_found", "bad_config")


@pytest.mark.parametrize(
    "fix",
    [
        selector_fix(selector=[]),
        selector_fix(selector={"app": "checkout"}),  # a map can't be expressed: labels
        selector_fix(selector=[{"key": "app"}]),
        {k: v for k, v in selector_fix().items() if k != "explanation"},
    ],
)
def test_proposed_fix_validates_its_shape(fix):
    with pytest.raises(ValidationError):
        ProposedFix.model_validate(fix)


def test_every_category_definition_is_in_the_schema():
    description = Diagnosis.model_json_schema()["$defs"]["FaultCategory"]["description"]
    assert all(f"- {c}: {d}" in description for c, d in CATEGORY_DEFINITIONS.items())
