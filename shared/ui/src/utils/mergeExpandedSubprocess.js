import * as BpmnModdleModule from "bpmn-moddle";

const SHAPE = "bpmndi:BPMNShape";
const EDGE = "bpmndi:BPMNEdge";
const PADDING = 80;
// bpmn-moddle 9 exposes only a default export; 10 adds the named `BpmnModdle`.
// The lockfile and a hoisted install can disagree, so accept either shape.
const BpmnModdle =
  BpmnModdleModule.BpmnModdle ?? BpmnModdleModule.default ?? BpmnModdleModule;
if (typeof BpmnModdle !== "function") {
  throw new Error("bpmn-moddle did not export a constructor");
}

function elementId(reference) {
  return typeof reference === "string" ? reference : reference?.id;
}

function boundsForElements(elements) {
  const points = [];
  for (const element of elements) {
    if (element.$type === SHAPE && element.bounds) {
      const { x, y, width, height } = element.bounds;
      points.push({ x, y }, { x: x + width, y: y + height });
    } else if (element.$type === EDGE) {
      points.push(...(element.waypoint ?? []));
    }
  }
  if (points.length === 0) throw new Error("main subprocess layout contains no visual elements");
  return {
    minX: Math.min(...points.map(({ x }) => x)),
    minY: Math.min(...points.map(({ y }) => y)),
    maxX: Math.max(...points.map(({ x }) => x)),
    maxY: Math.max(...points.map(({ y }) => y)),
  };
}

function translate(element, dx, dy) {
  if (element.$type === SHAPE && element.bounds) {
    element.bounds.x += dx;
    element.bounds.y += dy;
  } else if (element.$type === EDGE) {
    for (const point of element.waypoint ?? []) {
      point.x += dx;
      point.y += dy;
    }
  }
}

function downstreamOfMain(process, mainId) {
  const flows = process.flowElements.filter((item) => item.$type === "bpmn:SequenceFlow");
  const pending = flows
    .filter((flow) => elementId(flow.sourceRef) === mainId)
    .map((flow) => elementId(flow.targetRef));
  const downstream = new Set();
  while (pending.length > 0) {
    const id = pending.pop();
    if (!id || downstream.has(id)) continue;
    downstream.add(id);
    for (const flow of flows) {
      if (elementId(flow.sourceRef) === id) pending.push(elementId(flow.targetRef));
    }
  }
  return downstream;
}

function point(x, y, moddle) {
  return moddle.create("dc:Point", { x, y });
}

/**
 * Merge bpmn-auto-layout's expanded subprocess plane into its containing process plane.
 * The input and output are BPMN XML strings; no source model or external state is changed.
 */
export async function mergeExpandedSubprocess(xml) {
  const moddle = new BpmnModdle();
  const { rootElement, warnings = [] } = await moddle.fromXML(xml);
  const diagrams = rootElement.diagrams ?? [];
  const main = rootElement.rootElements
    .flatMap((root) => root.flowElements ?? [])
    .find((element) => element.$type === "bpmn:SubProcess" && element.id === "main");
  if (!main) return { xml, warnings };

  const childDiagram = diagrams.find((diagram) => elementId(diagram.plane.bpmnElement) === main.id);
  const parentDiagram = diagrams.find((diagram) => diagram !== childDiagram
    && diagram.plane.bpmnElement.$type === "bpmn:Process");
  if (!childDiagram || !parentDiagram) {
    throw new Error("expected bpmn-auto-layout planes for main and its containing process");
  }

  const process = parentDiagram.plane.bpmnElement;
  const parentElements = parentDiagram.plane.planeElement;
  const childElements = childDiagram.plane.planeElement;
  const mainShape = parentElements.find((item) => item.$type === SHAPE && elementId(item.bpmnElement) === main.id);
  if (!mainShape?.bounds) throw new Error("expected a BPMNShape with bounds for main");

  const mainBoundary = process.flowElements.find((item) => item.$type === "bpmn:BoundaryEvent"
    && elementId(item.attachedToRef) === main.id);
  const boundaryShape = mainBoundary && parentElements.find((item) => item.$type === SHAPE
    && elementId(item.bpmnElement) === mainBoundary.id);
  const boundaryFlow = mainBoundary && process.flowElements.find((item) => item.$type === "bpmn:SequenceFlow"
    && elementId(item.sourceRef) === mainBoundary.id);
  const boundaryEnd = boundaryFlow && boundaryFlow.targetRef;
  const boundaryEndShape = boundaryEnd && parentElements.find((item) => item.$type === SHAPE
    && elementId(item.bpmnElement) === elementId(boundaryEnd));
  if (!mainBoundary || !boundaryShape?.bounds || !boundaryFlow || !boundaryEnd || !boundaryEndShape?.bounds) {
    throw new Error("expected main's boundary event, its outgoing sequence flow, and target shape");
  }

  const originalRight = mainShape.bounds.x + mainShape.bounds.width;
  const originalWidth = mainShape.bounds.width;
  const childBounds = boundsForElements(childElements);
  const dx = mainShape.bounds.x + PADDING - childBounds.minX;
  const dy = mainShape.bounds.y + PADDING - childBounds.minY;
  const width = childBounds.maxX - childBounds.minX + PADDING * 2;
  const height = childBounds.maxY - childBounds.minY + PADDING * 2;
  const growth = width - originalWidth;

  for (const item of childElements) translate(item, dx, dy);
  mainShape.isExpanded = true;
  mainShape.bounds.width = width;
  mainShape.bounds.height = height;

  const downstream = downstreamOfMain(process, main.id);
  for (const item of parentElements) {
    if (item.$type !== SHAPE || item === mainShape || item === boundaryShape || item === boundaryEndShape) continue;
    if (item.bounds && item.bounds.x >= originalRight) {
      item.bounds.x += growth;
      downstream.add(elementId(item.bpmnElement));
    }
  }

  for (const edge of parentElements) {
    if (edge.$type !== EDGE || edge.bpmnElement === boundaryFlow) continue;
    const sourceId = elementId(edge.bpmnElement.sourceRef);
    const targetId = elementId(edge.bpmnElement.targetRef);
    const sourceShifted = sourceId === main.id || downstream.has(sourceId);
    const targetShifted = downstream.has(targetId);
    const waypoints = edge.waypoint ?? [];
    if (sourceShifted && targetShifted) {
      for (const waypoint of waypoints) waypoint.x += growth;
    } else if (sourceShifted && waypoints.length > 0) {
      waypoints[0].x += growth;
    } else if (targetShifted && waypoints.length > 0) {
      waypoints[waypoints.length - 1].x += growth;
    }
  }

  const boundaryCenterX = mainShape.bounds.x + PADDING + boundaryShape.bounds.width / 2;
  const mainBottom = mainShape.bounds.y + height;
  boundaryShape.bounds.x = boundaryCenterX - boundaryShape.bounds.width / 2;
  boundaryShape.bounds.y = mainBottom - boundaryShape.bounds.height / 2;
  const endCenterX = boundaryCenterX + 150;
  boundaryEndShape.bounds.x = endCenterX - boundaryEndShape.bounds.width / 2;
  boundaryEndShape.bounds.y = mainBottom + 100;

  const boundaryBottom = boundaryShape.bounds.y + boundaryShape.bounds.height;
  const endTop = boundaryEndShape.bounds.y;
  const routeY = endTop - 48;
  boundaryFlow.waypoint = [
    point(boundaryCenterX, boundaryBottom, moddle),
    point(boundaryCenterX, routeY, moddle),
    point(endCenterX, routeY, moddle),
    point(endCenterX, endTop, moddle),
  ];

  parentElements.push(...childElements);
  rootElement.diagrams = diagrams.filter((diagram) => diagram !== childDiagram);
  const { xml: mergedXml } = await moddle.toXML(rootElement, { format: true });
  return { xml: mergedXml, warnings };
}
