"""Reading and validating scenario folders (SPEC §4.4). No cluster access here."""

from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from k8s_agent.policy import READ_ONLY_TOOLS
from k8s_agent.schemas import DiagnosisStatus, FaultCategory, ProposedFix

NAMESPACE_PREFIX = "scn-"
SCENARIO_FILE = "scenario.yaml"
MANIFESTS_DIR = "manifests"


class ScenarioError(Exception):
    """A scenario folder is missing, malformed or inconsistent."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- ready_when conditions: proof that the fault is live -------------------------------


class PodRestartsAtLeast(_Strict):
    """Some container in the namespace has restarted at least `value` times."""

    kind: Literal["pod_restarts_at_least"]
    value: PositiveInt


class PodsReady(_Strict):
    """At least `value` pods are Ready."""

    kind: Literal["pods_ready"]
    value: PositiveInt


class PodNotReady(_Strict):
    """At least `value` pods are Running but not Ready."""

    kind: Literal["pod_not_ready"]
    value: PositiveInt = 1


class EventsContain(_Strict):
    """One event's message contains every string in `value`."""

    kind: Literal["events_contain"]
    value: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class EndpointsReady(_Strict):
    """Service `service` has at least `value` ready endpoints."""

    kind: Literal["endpoints_ready"]
    service: str = Field(min_length=1)
    value: PositiveInt


Condition = Annotated[
    PodRestartsAtLeast | PodsReady | PodNotReady | EventsContain | EndpointsReady,
    Field(discriminator="kind"),
]


# --- expected answers and eval inputs --------------------------------------------------


class ResourceName(_Strict):
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Expected(_Strict):
    """Accepted answer sets (SPEC §2.2)."""

    status: Literal[DiagnosisStatus.fault_found, DiagnosisStatus.no_fault]
    fault_categories: list[FaultCategory] = Field(min_length=1)
    resources: list[ResourceName] = []

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status == DiagnosisStatus.no_fault:
            if self.fault_categories != [FaultCategory.none]:
                raise ValueError("no_fault expects fault_categories == [none]")
            if self.resources:
                raise ValueError("no_fault expects no resources")
        else:
            if FaultCategory.none in self.fault_categories:
                raise ValueError("fault_found cannot accept category 'none'")
            if not self.resources:
                raise ValueError("fault_found needs at least one accepted resource")
        return self


class RequiredTool(_Strict):
    tool: str
    args: dict[str, Any] = {}

    @field_validator("tool")
    @classmethod
    def _allowlisted(cls, v: str) -> str:
        if v not in READ_ONLY_TOOLS:
            raise ValueError(f"{v!r} is not an allowlisted tool ({sorted(READ_ONLY_TOOLS)})")
        return v


class ScenarioSpec(_Strict):
    name: str
    question: str = Field(min_length=1)
    namespace: str
    ready_when: list[Condition] = Field(min_length=1)
    expected: Expected
    required_tools: list[RequiredTool] = Field(min_length=1)
    judge_rubric: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    expected_fix: ProposedFix | None = None

    @field_validator("ready_when", mode="before")
    @classmethod
    def _single_condition_ok(cls, v: Any) -> Any:
        return [v] if isinstance(v, dict) else v

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.namespace != NAMESPACE_PREFIX + self.name:
            raise ValueError(f"namespace must be {NAMESPACE_PREFIX + self.name!r}")
        if self.expected_fix is not None:
            if self.expected.status != DiagnosisStatus.fault_found:
                raise ValueError("expected_fix only makes sense with status fault_found")
            if self.expected_fix.namespace != self.namespace:
                raise ValueError("expected_fix.namespace must be the scenario namespace")
        return self


class Scenario(BaseModel):
    """A validated scenario: its spec plus the manifests `up` applies."""

    model_config = ConfigDict(frozen=True)

    spec: ScenarioSpec
    manifests: tuple[dict[str, Any], ...]
    path: Path

    @property
    def name(self) -> str:
        return self.spec.name


# --- loading ---------------------------------------------------------------------------


def load_scenario(path: Path) -> Scenario:
    """Read and validate one scenario folder. Raises ScenarioError with the reason."""
    name = path.name
    spec_file = path / SCENARIO_FILE
    if not spec_file.is_file():
        raise ScenarioError(f"{name}: missing {SCENARIO_FILE}")
    raw = _read_yaml_one(spec_file)
    if not isinstance(raw, dict):
        raise ScenarioError(f"{name}: {SCENARIO_FILE} must be a mapping")
    if "name" in raw:
        raise ScenarioError(f"{name}: 'name' comes from the folder name; remove it")
    try:
        spec = ScenarioSpec.model_validate({**raw, "name": name})
    except ValidationError as e:
        raise ScenarioError(f"{name}: invalid {SCENARIO_FILE}:\n{e}") from e

    manifests = _load_manifests(path / MANIFESTS_DIR, spec.namespace, name)
    _check_references(spec, manifests)
    return Scenario(spec=spec, manifests=tuple(manifests), path=path)


def load_all(scenarios_dir: Path) -> dict[str, Scenario]:
    if not scenarios_dir.is_dir():
        raise ScenarioError(f"scenarios folder not found: {scenarios_dir.resolve()}")
    folders = sorted(p for p in scenarios_dir.iterdir() if (p / SCENARIO_FILE).exists())
    if not folders:
        raise ScenarioError(f"no scenarios in {scenarios_dir.resolve()}")
    return {p.name: load_scenario(p) for p in folders}


def get_scenario(scenarios_dir: Path, name: str) -> Scenario:
    scenarios = load_all(scenarios_dir)
    if name not in scenarios:
        raise ScenarioError(f"unknown scenario {name!r}; known: {', '.join(scenarios)}")
    return scenarios[name]


def _read_yaml_one(file: Path) -> Any:
    try:
        return yaml.safe_load(file.read_text())
    except yaml.YAMLError as e:
        raise ScenarioError(f"{file}: invalid YAML: {e}") from e


def _load_manifests(folder: Path, namespace: str, name: str) -> list[dict[str, Any]]:
    files = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    if not files:
        raise ScenarioError(f"{name}: no manifests in {folder}")
    docs: list[dict[str, Any]] = []
    for file in files:
        try:
            loaded = [d for d in yaml.safe_load_all(file.read_text()) if d is not None]
        except yaml.YAMLError as e:
            raise ScenarioError(f"{file}: invalid YAML: {e}") from e
        for doc in loaded:
            _check_manifest(doc, namespace, file)
            docs.append(doc)
    return docs


def _check_manifest(doc: Any, namespace: str, file: Path) -> None:
    if not isinstance(doc, dict) or not doc.get("apiVersion") or not doc.get("kind"):
        raise ScenarioError(f"{file}: every document needs apiVersion and kind")
    meta = doc.get("metadata") or {}
    ident = f"{file}: {doc['kind']}/{meta.get('name')}"
    if not meta.get("name"):
        raise ScenarioError(f"{file}: {doc['kind']} has no metadata.name")
    if doc["kind"] == "Namespace":
        raise ScenarioError(f"{ident}: don't declare the Namespace; `up` creates it")
    if meta.get("namespace") not in (None, namespace):
        raise ScenarioError(f"{ident}: namespace must be {namespace!r} or unset")
    for image in _images(doc):
        if "@sha256:" not in image:
            raise ScenarioError(f"{ident}: image {image!r} must be pinned by digest")


def _images(doc: dict[str, Any]) -> list[str]:
    spec = doc.get("spec") or {}
    pod_spec = (spec.get("template") or {}).get("spec") or spec  # workloads or bare Pod
    containers = (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])
    return [c.get("image", "") for c in containers if isinstance(c, dict)]


def _check_references(spec: ScenarioSpec, manifests: list[dict[str, Any]]) -> None:
    """Names in scenario.yaml must point at objects the manifests actually create."""
    declared = {(d["kind"], d["metadata"]["name"]) for d in manifests}

    def need(kind: str, obj_name: str, where: str) -> None:
        if (kind, obj_name) not in declared:
            raise ScenarioError(
                f"{spec.name}: {where} refers to {kind}/{obj_name}, not in manifests"
            )

    for r in spec.expected.resources:
        need(r.kind, r.name, "expected.resources")
    for c in spec.ready_when:
        if isinstance(c, EndpointsReady):
            need("Service", c.service, "ready_when")
    if spec.expected_fix is not None:
        need("Service", spec.expected_fix.service, "expected_fix")
