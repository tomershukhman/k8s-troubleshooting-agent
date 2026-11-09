"""What every tool result goes through before the model sees it (SPEC 3.3)."""

import yaml

from k8s_agent.agent.tool_output import strip_fields
from tests.fakes import DEPLOYMENT_YAML


def test_managed_fields_are_removed_and_spec_and_status_stay():
    obj = yaml.safe_load(strip_fields(DEPLOYMENT_YAML, ["managedFields"]))
    original = yaml.safe_load(DEPLOYMENT_YAML)
    assert "managedFields" not in obj["metadata"]
    assert obj["metadata"]["labels"] == original["metadata"]["labels"]
    assert (obj["spec"], obj["status"]) == (original["spec"], original["status"])


def test_spec_and_status_are_never_touched():
    text = strip_fields(DEPLOYMENT_YAML, ["managedFields", "spec", "status"])
    assert "status" in yaml.safe_load(text)  # never stripped, even when configured
    assert yaml.safe_load(text)["spec"]["template"]["metadata"] == {
        "managedFields": "kept, under spec"
    }


def test_fields_are_removed_in_lists_of_objects_such_as_events():
    events = yaml.safe_dump([{"reason": "BackOff", "managedFields": [1]}])
    assert yaml.safe_load(strip_fields(events, ["managedFields"])) == [{"reason": "BackOff"}]


def test_text_that_is_not_an_object_is_unchanged():
    # a log whose lines parse as a YAML mapping, even with the field as a key, is still a log
    log = "FATAL: DATABASE_URL not set\nmanagedFields: mentioned in a log line\n"
    table = "NAME   READY   STATUS\ncheckout-1   1/1   Running\n"
    for text in (log, table):
        assert strip_fields(text, ["managedFields"]) == text


def test_yaml_with_aliases_is_unchanged_so_an_alias_bomb_cannot_expand():
    # each level holds 9 aliases of the one before: 9**levels items once expanded
    lines = ["kind: Pod", "managedFields: x", "a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, 5)]
    bomb = "\n".join(lines) + "\n"
    assert strip_fields(bomb, ["managedFields"]) == bomb
