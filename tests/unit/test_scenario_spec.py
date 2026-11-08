"""Reading and validating scenario folders. No cluster."""

import shutil
from pathlib import Path

import pytest
import yaml

from k8s_agent.scenarios.spec import (
    EndpointsReady,
    PodRestartsAtLeast,
    ScenarioError,
    get_scenario,
    load_all,
    load_scenario,
)
from k8s_agent.schemas import DiagnosisStatus, FaultCategory

REPO_SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


# --- the committed scenarios -----------------------------------------------------------


def test_all_four_scenarios_load():
    assert set(load_all(REPO_SCENARIOS)) == {"crashloop", "selector", "readiness", "healthy"}


@pytest.mark.parametrize(
    ("name", "status", "categories", "resources"),
    [  # SPEC §2.2 expected answers
        (
            "crashloop",
            DiagnosisStatus.fault_found,
            [FaultCategory.bad_config],
            {("ConfigMap", "orders-config"), ("Deployment", "orders")},
        ),
        (
            "selector",
            DiagnosisStatus.fault_found,
            [FaultCategory.selector_mismatch],
            {("Service", "checkout")},
        ),
        (
            "readiness",
            DiagnosisStatus.fault_found,
            [FaultCategory.probe_failure],
            {("Deployment", "catalog")},
        ),
        ("healthy", DiagnosisStatus.no_fault, [FaultCategory.none], set()),
    ],
)
def test_expected_answers_match_spec(name, status, categories, resources):
    exp = get_scenario(REPO_SCENARIOS, name).spec.expected
    assert exp.status == status
    assert exp.fault_categories == categories
    assert {(r.kind, r.name) for r in exp.resources} == resources


def test_only_selector_has_expected_fix():
    scenarios = load_all(REPO_SCENARIOS)
    assert [n for n, s in scenarios.items() if s.spec.expected_fix] == ["selector"]
    fix = scenarios["selector"].spec.expected_fix
    assert fix.model_dump() == {
        "type": "patch_service_selector",
        "namespace": "scn-selector",
        "service": "checkout",
        "selector": {"app": "checkout"},
    }


def test_single_ready_when_mapping_becomes_a_list():
    rw = get_scenario(REPO_SCENARIOS, "crashloop").spec.ready_when
    assert rw == [PodRestartsAtLeast(kind="pod_restarts_at_least", value=2)]


def test_unknown_scenario_name():
    with pytest.raises(ScenarioError, match="unknown scenario 'nope'"):
        get_scenario(REPO_SCENARIOS, "nope")


# --- validation failures, on a copy of a good scenario ---------------------------------


@pytest.fixture
def scn(tmp_path: Path) -> Path:
    """A writable copy of the selector scenario."""
    dst = tmp_path / "selector"
    shutil.copytree(REPO_SCENARIOS / "selector", dst)
    return dst


def _edit(folder: Path, **changes) -> None:
    file = folder / "scenario.yaml"
    data = yaml.safe_load(file.read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    file.write_text(yaml.safe_dump(data))


def _edit_manifest(folder: Path, edit) -> None:
    file = folder / "manifests" / "checkout.yaml"
    docs = list(yaml.safe_load_all(file.read_text()))
    edit(docs)
    file.write_text(yaml.safe_dump_all(docs))


def test_copy_is_valid(scn):
    assert load_scenario(scn).name == "selector"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"surprise": 1}, "Extra inputs are not permitted"),
        ({"name": "x"}, "comes from the folder name"),
        ({"question": None}, "question"),
        ({"namespace": "default"}, "namespace must be 'scn-selector'"),
        ({"ready_when": []}, "ready_when"),
        ({"ready_when": {"kind": "pods_happy", "value": 1}}, "does not match any of the expected"),
        ({"ready_when": {"kind": "pods_ready", "value": 0}}, "greater than 0"),
        (
            {"expected": {"status": "inconclusive", "fault_categories": ["none"]}},
            "status",
        ),
        (
            {
                "expected": {
                    "status": "fault_found",
                    "fault_categories": ["typo"],
                    "resources": [{"kind": "Service", "name": "checkout"}],
                }
            },
            "fault_categories",
        ),
        (
            {
                "expected": {
                    "status": "fault_found",
                    "fault_categories": ["none"],
                    "resources": [{"kind": "Service", "name": "checkout"}],
                }
            },
            "cannot accept category 'none'",
        ),
        (
            {"expected": {"status": "fault_found", "fault_categories": ["selector_mismatch"]}},
            "at least one accepted resource",
        ),
        (
            {"expected": {"status": "no_fault", "fault_categories": ["oom"]}, "expected_fix": None},
            "no_fault expects fault_categories == \\[none\\]",
        ),
        ({"required_tools": [{"tool": "pods_exec"}]}, "not an allowlisted tool"),
        ({"required_tools": []}, "required_tools"),
        ({"judge_rubric": []}, "judge_rubric"),
        (
            {
                "expected_fix": {
                    "type": "delete_pod",
                    "namespace": "scn-selector",
                    "service": "checkout",
                    "selector": {"app": "checkout"},
                }
            },
            "expected_fix.type",
        ),
        (
            {
                "expected_fix": {
                    "type": "patch_service_selector",
                    "namespace": "scn-other",
                    "service": "checkout",
                    "selector": {"app": "checkout"},
                }
            },
            "expected_fix.namespace must be the scenario namespace",
        ),
    ],
)
def test_invalid_scenario_yaml_is_rejected(scn, changes, message):
    _edit(scn, **changes)
    with pytest.raises(ScenarioError, match=message):
        load_scenario(scn)


def test_expected_resource_must_exist_in_manifests(scn):
    _edit(
        scn,
        expected={
            "status": "fault_found",
            "fault_categories": ["selector_mismatch"],
            "resources": [{"kind": "Service", "name": "chekout"}],
        },
    )
    with pytest.raises(ScenarioError, match="Service/chekout, not in manifests"):
        load_scenario(scn)


def test_endpoints_ready_service_must_exist_in_manifests(scn):
    _edit(scn, ready_when=[{"kind": "endpoints_ready", "service": "nope", "value": 1}])
    with pytest.raises(ScenarioError, match="Service/nope, not in manifests"):
        load_scenario(scn)


def test_endpoints_ready_condition_parses(scn):
    _edit(scn, ready_when=[{"kind": "endpoints_ready", "service": "checkout", "value": 2}])
    assert load_scenario(scn).spec.ready_when == [
        EndpointsReady(kind="endpoints_ready", service="checkout", value=2)
    ]


def test_missing_scenario_yaml(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(ScenarioError, match="missing scenario.yaml"):
        load_scenario(tmp_path / "x")


def test_invalid_yaml(scn):
    (scn / "scenario.yaml").write_text("question: [unclosed\n")
    with pytest.raises(ScenarioError, match="invalid YAML"):
        load_scenario(scn)


def test_no_manifests(scn):
    shutil.rmtree(scn / "manifests")
    with pytest.raises(ScenarioError, match="no manifests"):
        load_scenario(scn)


def _set_image(docs, image):
    docs[0]["spec"]["template"]["spec"]["containers"][0]["image"] = image


def _set_namespace(docs, ns):
    docs[1]["metadata"]["namespace"] = ns


def _add_namespace_doc(docs):
    docs.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "scn-selector"}})


def _drop_name(docs):
    del docs[1]["metadata"]["name"]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: _set_image(d, "nginx:1.30-alpine"), "must be pinned by digest"),
        (lambda d: _set_namespace(d, "default"), "namespace must be 'scn-selector' or unset"),
        (_add_namespace_doc, "don't declare the Namespace"),
        (_drop_name, "has no metadata.name"),
        (lambda d: d.append({"kind": "Service"}), "needs apiVersion and kind"),
    ],
)
def test_invalid_manifests_are_rejected(scn, edit, message):
    _edit_manifest(scn, edit)
    with pytest.raises(ScenarioError, match=message):
        load_scenario(scn)


def test_manifest_with_explicit_matching_namespace_is_ok(scn):
    _edit_manifest(scn, lambda d: _set_namespace(d, "scn-selector"))
    load_scenario(scn)


def test_scenarios_dir_missing(tmp_path):
    with pytest.raises(ScenarioError, match="scenarios folder not found"):
        load_all(tmp_path / "nope")
