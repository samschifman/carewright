"""L3 structural rules for the BPMN subset emitted by the compiler."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
import re

from lxml import etree

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.validators.results import Finding, Severity


BPMN_MODEL_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DROOLS_NS = "http://www.jboss.org/drools"
_BPMN = f"{{{BPMN_MODEL_NS}}}"
_DROOLS_TASK_NAME = f"{{{DROOLS_NS}}}taskName"
_FLOW_NODE_TYPES = frozenset(
    {
        "startEvent",
        "endEvent",
        "task",
        "userTask",
        "businessRuleTask",
        "exclusiveGateway",
        "intermediateCatchEvent",
        "boundaryEvent",
        "scriptTask",
        "subProcess",
        "serviceTask",
        "sendTask",
        "receiveTask",
        "manualTask",
        "callActivity",
    }
)
_ACTIVITY_TYPES = frozenset(
    {
        "task",
        "userTask",
        "businessRuleTask",
        "scriptTask",
        "subProcess",
        "serviceTask",
        "sendTask",
        "receiveTask",
        "manualTask",
        "callActivity",
    }
)
_TIMER_DURATION_RE = re.compile(r"^P(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$")
_TIMER_PARAMETER_RE = re.compile(r"^#\{[a-z][a-z0-9_]*\}$")
_TIMER_CYCLE_RE = re.compile(r"^R\d*/(.+)$")
_RESERVED_TASK_NAMES = frozenset({"Rest", "Service Task"})

Rule = Callable[[etree._Element], list[Finding]]


def _finding(
    code: str,
    message: str,
    *,
    element_id: str | None = None,
    severity: Severity = "ERROR",
) -> Finding:
    return Finding(
        rung="L3",
        severity=severity,
        code=code,
        message=message,
        element_id=element_id,
    )


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _scopes(root: etree._Element) -> list[etree._Element]:
    return [
        element
        for element in root.iter()
        if isinstance(element.tag, str)
        and element.tag.startswith(_BPMN)
        and _local(element) in {"process", "subProcess"}
    ]


def _nodes(scope: etree._Element) -> list[etree._Element]:
    return [
        child
        for child in scope
        if isinstance(child.tag, str)
        and child.tag.startswith(_BPMN)
        and _local(child) in _FLOW_NODE_TYPES
    ]


def _flows(scope: etree._Element) -> list[etree._Element]:
    return [
        child
        for child in scope
        if child.tag == _BPMN + "sequenceFlow"
    ]


def _single_start(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        starts = [node for node in _nodes(scope) if _local(node) == "startEvent"]
        if len(starts) != 1:
            scope_id = scope.get("id")
            findings.append(
                _finding(
                    "single-start",
                    f"{_local(scope)} {scope_id!r} has {len(starts)} start events; expected exactly one",
                    element_id=scope_id,
                )
            )
    return findings


def _end_required(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        ends = [node for node in _nodes(scope) if _local(node) == "endEvent"]
        if not ends:
            scope_id = scope.get("id")
            findings.append(
                _finding(
                    "end-required",
                    f"{_local(scope)} {scope_id!r} has no end event",
                    element_id=scope_id,
                )
            )
    return findings


def _no_disconnected(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        nodes = _nodes(scope)
        starts = [node for node in nodes if _local(node) == "startEvent"]
        if not starts:
            continue

        node_ids = {node.get("id") for node in nodes if node.get("id")}
        outgoing: dict[str, list[str]] = defaultdict(list)
        attached: dict[str, list[str]] = defaultdict(list)
        for flow in _flows(scope):
            source = flow.get("sourceRef")
            target = flow.get("targetRef")
            if source and target and source in node_ids:
                outgoing[source].append(target)
        for node in nodes:
            if _local(node) == "boundaryEvent":
                host = node.get("attachedToRef")
                identifier = node.get("id")
                if host and identifier:
                    attached[host].append(identifier)

        reachable: set[str] = set()
        pending = deque([starts[0].get("id")])
        while pending:
            identifier = pending.popleft()
            if not identifier or identifier in reachable:
                continue
            reachable.add(identifier)
            pending.extend(outgoing.get(identifier, ()))
            pending.extend(attached.get(identifier, ()))

        for node in nodes:
            identifier = node.get("id")
            if identifier and identifier not in reachable:
                findings.append(
                    _finding(
                        "no-disconnected",
                        f"flow element {identifier!r} is not reachable from the scope's start event",
                        element_id=identifier,
                    )
                )
    return findings


def _no_implicit_split(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        flows_by_source: dict[str, list[etree._Element]] = defaultdict(list)
        for flow in _flows(scope):
            source = flow.get("sourceRef")
            if source:
                flows_by_source[source].append(flow)
        for node in _nodes(scope):
            identifier = node.get("id")
            outgoing = flows_by_source.get(identifier or "", [])
            if len(outgoing) > 1 and not (
                _local(node) == "exclusiveGateway"
                and node.get("gatewayDirection") == "Diverging"
            ):
                findings.append(
                    _finding(
                        "no-implicit-split",
                        f"flow element {identifier!r} has {len(outgoing)} outgoing flows without a diverging exclusive gateway",
                        element_id=identifier,
                    )
                )
    return findings


def _no_implicit_join(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        flows_by_target: dict[str, list[etree._Element]] = defaultdict(list)
        for flow in _flows(scope):
            target = flow.get("targetRef")
            if target:
                flows_by_target[target].append(flow)
        for node in _nodes(scope):
            identifier = node.get("id")
            incoming = flows_by_target.get(identifier or "", [])
            if len(incoming) > 1 and not (
                _local(node) == "exclusiveGateway"
                and node.get("gatewayDirection") == "Converging"
            ):
                findings.append(
                    _finding(
                        "no-implicit-join",
                        f"flow element {identifier!r} has {len(incoming)} incoming flows without a converging exclusive gateway",
                        element_id=identifier,
                    )
                )
    return findings


def _no_duplicate_flows(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        signatures: dict[tuple[str | None, str | None, str | None, str | None], str] = {}
        for flow in _flows(scope):
            signature = (
                flow.get("sourceRef"),
                flow.get("targetRef"),
                flow.get("name"),
                next(
                    (child.text or "" for child in flow if child.tag == _BPMN + "conditionExpression"),
                    None,
                ),
            )
            previous_id = signatures.get(signature)
            identifier = flow.get("id")
            if previous_id is not None:
                findings.append(
                    _finding(
                        "no-duplicate-flows",
                        f"flow {identifier!r} duplicates flow {previous_id!r}",
                        element_id=identifier,
                    )
                )
            else:
                signatures[signature] = identifier or "(missing id)"
    return findings


def _conditional_flows(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        nodes = {node.get("id"): node for node in _nodes(scope) if node.get("id")}
        flows_by_source: dict[str, list[etree._Element]] = defaultdict(list)
        for flow in _flows(scope):
            source = flow.get("sourceRef") or ""
            flows_by_source[source].append(flow)
            condition = next(
                (child for child in flow if child.tag == _BPMN + "conditionExpression"),
                None,
            )
            source_node = nodes.get(source)
            if condition is not None and not (
                source_node is not None
                and _local(source_node) == "exclusiveGateway"
                and source_node.get("gatewayDirection") == "Diverging"
            ):
                findings.append(
                    _finding(
                        "conditional-flows",
                        f"conditional flow {flow.get('id')!r} does not leave a diverging exclusive gateway",
                        element_id=flow.get("id"),
                    )
                )

        for node in nodes.values():
            if _local(node) != "exclusiveGateway":
                continue
            identifier = node.get("id") or ""
            outgoing = flows_by_source.get(identifier, [])
            if node.get("gatewayDirection") != "Diverging":
                if node.get("default") is not None:
                    findings.append(
                        _finding(
                            "conditional-flows",
                            f"non-diverging gateway {identifier!r} must not declare a default flow",
                            element_id=identifier,
                        )
                    )
                continue

            default_id = node.get("default")
            defaults = [flow for flow in outgoing if flow.get("id") == default_id]
            if not default_id or len(defaults) != 1:
                findings.append(
                    _finding(
                        "conditional-flows",
                        f"diverging gateway {identifier!r} must declare exactly one outgoing default flow",
                        element_id=identifier,
                    )
                )
            elif any(
                child.tag == _BPMN + "conditionExpression" for child in defaults[0]
            ):
                findings.append(
                    _finding(
                        "conditional-flows",
                        f"default flow {default_id!r} must not have a conditionExpression",
                        element_id=default_id,
                    )
                )

            for flow in outgoing:
                if flow.get("id") == default_id:
                    continue
                if not any(child.tag == _BPMN + "conditionExpression" for child in flow):
                    findings.append(
                        _finding(
                            "conditional-flows",
                            f"non-default flow {flow.get('id')!r} from gateway {identifier!r} requires a conditionExpression",
                            element_id=flow.get("id"),
                        )
                    )
    return findings


def _boundary_attached(root: etree._Element) -> list[Finding]:
    findings = []
    nodes_by_id: dict[str, etree._Element] = {}
    scope_by_node: dict[str, etree._Element] = {}
    for scope in _scopes(root):
        for node in _nodes(scope):
            identifier = node.get("id")
            if identifier:
                nodes_by_id[identifier] = node
                scope_by_node[identifier] = scope
    for scope in _scopes(root):
        for node in _nodes(scope):
            if _local(node) != "boundaryEvent":
                continue
            identifier = node.get("id")
            host_id = node.get("attachedToRef")
            host = nodes_by_id.get(host_id or "")
            if (
                host is None
                or _local(host) not in _ACTIVITY_TYPES
                or scope_by_node.get(host_id or "") is not scope
            ):
                findings.append(
                    _finding(
                        "boundary-attached",
                        f"boundaryEvent {identifier!r} must reference an activity in the same scope",
                        element_id=identifier,
                    )
                )
    return findings


def _unique_ids(root: etree._Element) -> list[Finding]:
    id_elements: dict[str, list[etree._Element]] = defaultdict(list)
    for element in root.iter():
        if isinstance(element.tag, str) and element.get("id"):
            id_elements[element.get("id", "")].append(element)
    return [
        _finding(
            "unique-ids",
            f"XML id {identifier!r} appears {len(elements)} times",
            element_id=identifier,
        )
        for identifier, elements in id_elements.items()
        if len(elements) > 1
    ]


def _duplicate_boundary_names(root: etree._Element) -> list[Finding]:
    findings = []
    for process in (scope for scope in _scopes(root) if _local(scope) == "process"):
        names: dict[str, list[etree._Element]] = defaultdict(list)
        for node in _nodes(process):
            if _local(node) == "boundaryEvent" and (node.get("name") or "").strip():
                names[node.get("name", "")].append(node)
        for name, boundaries in names.items():
            if len(boundaries) > 1:
                findings.append(
                    _finding(
                        "duplicate-boundary-names",
                        f"{len(boundaries)} top-level boundary events share a name and Kogito will expose one signal route",
                        element_id=boundaries[-1].get("id"),
                        severity="WARNING",
                    )
                )
    return findings


def _label_required(root: etree._Element) -> list[Finding]:
    findings = []
    for scope in _scopes(root):
        for node in _nodes(scope):
            identifier = node.get("id")
            # The compiler's main-scope entry is intentionally unlabeled.
            if identifier == "main_start":
                continue
            if not (node.get("name") or "").strip():
                findings.append(
                    _finding(
                        "label-required",
                        f"{_local(node)} {identifier!r} requires a non-empty name",
                        element_id=identifier,
                    )
                )
    return findings


def _timer_format(root: etree._Element) -> list[Finding]:
    findings = []
    for definition in root.iter(_BPMN + "timerEventDefinition"):
        timer = next(
            (
                child
                for child in definition
                if child.tag in {_BPMN + "timeDuration", _BPMN + "timeCycle"}
            ),
            None,
        )
        if timer is None:
            findings.append(
                _finding("timer-format", "timerEventDefinition has no timer expression")
            )
            continue
        value = (timer.text or "").strip()
        valid = bool(_TIMER_PARAMETER_RE.fullmatch(value))
        if _local(timer) == "timeDuration":
            valid = valid or bool(_TIMER_DURATION_RE.fullmatch(value))
        else:
            cycle = _TIMER_CYCLE_RE.fullmatch(value)
            if cycle is not None:
                interval = cycle.group(1)
                valid = bool(
                    _TIMER_DURATION_RE.fullmatch(interval)
                    or _TIMER_PARAMETER_RE.fullmatch(interval)
                )
        if not valid:
            owner = definition.getparent()
            identifier = owner.get("id") if owner is not None else None
            findings.append(
                _finding(
                    "timer-format",
                    f"timer expression {value!r} must be a day/hour ISO duration, a parameter, or a supported repeat cycle",
                    element_id=identifier,
                )
            )
    return findings


def _reserved_task_names(root: etree._Element) -> list[Finding]:
    return [
        _finding(
            "reserved-task-names",
            f"task {task.get('id')!r} uses a Kogito-reserved work-item name",
            element_id=task.get("id"),
        )
        for task in root.iter(_BPMN + "task")
        if task.get(_DROOLS_TASK_NAME) in _RESERVED_TASK_NAMES
    ]


_RULES: tuple[tuple[str, Rule], ...] = (
    ("single-start", _single_start),
    ("end-required", _end_required),
    ("no-disconnected", _no_disconnected),
    ("no-implicit-split", _no_implicit_split),
    ("no-implicit-join", _no_implicit_join),
    ("no-duplicate-flows", _no_duplicate_flows),
    ("conditional-flows", _conditional_flows),
    ("boundary-attached", _boundary_attached),
    ("unique-ids", _unique_ids),
    ("duplicate-boundary-names", _duplicate_boundary_names),
    ("label-required", _label_required),
    ("timer-format", _timer_format),
    ("reserved-task-names", _reserved_task_names),
)


@trace(name="bpmn.l3.structure")
def validate_structure(xml: str) -> list[Finding]:
    """Run the registered L3 structural rules on one BPMN document."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(xml.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        return [_finding("xml-parse", f"BPMN XML could not be parsed: {exc}")]
    if root.tag != _BPMN + "definitions":
        return [_finding("bpmn-root", "BPMN document root must be bpmn2:definitions")]
    return [finding for _, rule in _RULES for finding in rule(root)]
