import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, it } from "vitest";
import * as BpmnModdleModule from "bpmn-moddle";
import { layoutProcess } from "bpmn-auto-layout";
import { mergeExpandedSubprocess } from "./mergeExpandedSubprocess.js";

const BpmnModdle = BpmnModdleModule.BpmnModdle;

const fixtureDirectory = resolve(process.cwd(), "../../cpg-ingester/data/golden/automation");
const goldenNames = [
  "home-bp-monitoring.bpmn",
  "acei-lab-follow-up.bpmn",
  "follow-up-visit.bpmn",
  "lifestyle-reassessment.bpmn",
];

async function layoutAndMerge(xml) {
  const { xml: laidOutXml } = await layoutProcess(xml);
  const { xml: mergedXml } = await mergeExpandedSubprocess(laidOutXml);
  return mergedXml;
}

async function parse(xml) {
  const { rootElement } = await new BpmnModdle().fromXML(xml);
  return rootElement;
}

function visualElement(plane, type, id) {
  return plane.planeElement.find((item) => item.$type === type && item.bpmnElement.id === id);
}

describe("mergeExpandedSubprocess", () => {
  for (const name of goldenNames) {
    it(`renders ${name} as one plane with main expanded`, async () => {
      const source = await readFile(resolve(fixtureDirectory, name), "utf8");
      const output = await layoutAndMerge(source);
      const definitions = await parse(output);

      assert.equal(definitions.diagrams.length, 1);
      const plane = definitions.diagrams[0].plane;
      const process = plane.bpmnElement;
      const main = process.flowElements.find((item) => item.$type === "bpmn:SubProcess" && item.id === "main");
      const mainShape = visualElement(plane, "bpmndi:BPMNShape", "main");
      assert.ok(main && mainShape?.isExpanded, "main is shown as an expanded subprocess");

      const mainBounds = mainShape.bounds;
      for (const child of main.flowElements.filter((item) => item.$type !== "bpmn:SequenceFlow")) {
        const shape = visualElement(plane, "bpmndi:BPMNShape", child.id);
        if (!shape) continue;
        assert.ok(shape.bounds.x >= mainBounds.x, `${child.id} is inside main horizontally`);
        assert.ok(shape.bounds.y >= mainBounds.y, `${child.id} is inside main vertically`);
        assert.ok(shape.bounds.x + shape.bounds.width <= mainBounds.x + mainBounds.width, `${child.id} fits main width`);
        assert.ok(shape.bounds.y + shape.bounds.height <= mainBounds.y + mainBounds.height, `${child.id} fits main height`);
      }

      const boundary = process.flowElements.find((item) => item.$type === "bpmn:BoundaryEvent"
        && item.attachedToRef.id === "main");
      const boundaryShape = visualElement(plane, "bpmndi:BPMNShape", boundary.id);
      assert.equal(boundaryShape.bounds.y + boundaryShape.bounds.height / 2, mainBounds.y + mainBounds.height);
    });
  }

  it("finds the main boundary target through the flow graph rather than its identifier", async () => {
    const source = await readFile(resolve(fixtureDirectory, goldenNames[0]), "utf8");
    const renamed = source.replaceAll("end_period", "period_result_end");
    const output = await layoutAndMerge(renamed);
    const definitions = await parse(output);
    const process = definitions.diagrams[0].plane.bpmnElement;
    const boundary = process.flowElements.find((item) => item.$type === "bpmn:BoundaryEvent"
      && item.attachedToRef.id === "main");
    const route = process.flowElements.find((item) => item.$type === "bpmn:SequenceFlow"
      && item.sourceRef.id === boundary.id);
    assert.equal(route.targetRef.id, "period_result_end");
    assert.ok(visualElement(definitions.diagrams[0].plane, "bpmndi:BPMNShape", route.targetRef.id));
  });

  it("keeps and lays out a boundary attached to a task inside main", async () => {
    const source = await readFile(resolve(fixtureDirectory, goldenNames[0]), "utf8");
    const taskBoundary = `
      <bpmn2:boundaryEvent id="task_timeout" name="Task timeout" attachedToRef="request_readings">
        <bpmn2:outgoing>f_task_timeout</bpmn2:outgoing>
        <bpmn2:timerEventDefinition><bpmn2:timeDuration>PT1H</bpmn2:timeDuration></bpmn2:timerEventDefinition>
      </bpmn2:boundaryEvent>
      <bpmn2:endEvent id="task_timeout_end"><bpmn2:incoming>f_task_timeout</bpmn2:incoming></bpmn2:endEvent>
      <bpmn2:sequenceFlow id="f_task_timeout" sourceRef="task_timeout" targetRef="task_timeout_end"/>`;
    const withTaskBoundary = source.replace("</bpmn2:subProcess>", `${taskBoundary}\n    </bpmn2:subProcess>`);
    assert.notEqual(withTaskBoundary, source, "fixture contains main subprocess");

    const output = await layoutAndMerge(withTaskBoundary);
    const definitions = await parse(output);
    const plane = definitions.diagrams[0].plane;
    const process = plane.bpmnElement;
    const main = process.flowElements.find((item) => item.$type === "bpmn:SubProcess" && item.id === "main");
    const boundary = main.flowElements.find((item) => item.$type === "bpmn:BoundaryEvent" && item.id === "task_timeout");
    const shape = visualElement(plane, "bpmndi:BPMNShape", boundary.id);
    assert.ok(shape, "task-attached boundary is present in merged plane");
    assert.equal(boundary.attachedToRef.id, "request_readings");
  });
});
