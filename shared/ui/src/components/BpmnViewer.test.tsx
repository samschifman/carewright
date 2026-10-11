import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const zoom = vi.fn();
  const viewbox = vi.fn();
  const importXML = vi.fn();
  const layoutProcess = vi.fn();
  const mergeExpandedSubprocess = vi.fn();
  const destroy = vi.fn();
  const get = vi.fn(() => ({ zoom, viewbox }));
  const viewer = { destroy, get, importXML };
  return { destroy, get, importXML, layoutProcess, mergeExpandedSubprocess, viewer, zoom, viewbox };
});

vi.mock("bpmn-js/lib/NavigatedViewer", () => ({
  default: vi.fn(function MockNavigatedViewer() {
    return mocks.viewer;
  }),
}));
vi.mock("bpmn-auto-layout", () => ({ layoutProcess: mocks.layoutProcess }));
vi.mock("../utils/mergeExpandedSubprocess.js", () => ({
  mergeExpandedSubprocess: mocks.mergeExpandedSubprocess,
}));

import { BpmnViewer } from "./BpmnViewer";

const BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL";
const BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI";
const DC_NS = "http://www.omg.org/spec/DD/20100524/DC";
const noDiagramXml = `<bpmn:definitions xmlns:bpmn="${BPMN_NS}"/>`;
const diagramXml = `<bpmn:definitions xmlns:bpmn="${BPMN_NS}" xmlns:bpmndi="${BPMNDI_NS}"><bpmndi:BPMNDiagram id="diagram"/></bpmn:definitions>`;
const diagramWithBoundsXml = `<bpmn:definitions xmlns:bpmn="${BPMN_NS}" xmlns:bpmndi="${BPMNDI_NS}" xmlns:dc="${DC_NS}"><bpmndi:BPMNDiagram id="diagram"><bpmndi:BPMNPlane id="plane"><bpmndi:BPMNShape id="shape"><dc:Bounds x="100" y="40" width="100" height="80"/></bpmndi:BPMNShape></bpmndi:BPMNPlane></bpmndi:BPMNDiagram></bpmn:definitions>`;

describe("BpmnViewer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.importXML.mockResolvedValue({ warnings: [] });
    mocks.layoutProcess.mockResolvedValue({ xml: "layout-output", warnings: [] });
    mocks.mergeExpandedSubprocess.mockResolvedValue({ xml: "laid-out-bpmn", warnings: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("runs browser layout and merges main before importing XML without DI", async () => {
    render(<BpmnViewer xml={noDiagramXml} />);

    await waitFor(() => expect(mocks.importXML).toHaveBeenCalledWith("laid-out-bpmn"));
    expect(mocks.layoutProcess).toHaveBeenCalledWith(noDiagramXml);
    expect(mocks.mergeExpandedSubprocess).toHaveBeenCalledWith("layout-output");
    expect(mocks.zoom).toHaveBeenCalledWith("fit-viewport");
  });

  it("imports XML with existing DI without running auto-layout", async () => {
    render(<BpmnViewer xml={diagramXml} />);

    await waitFor(() => expect(mocks.importXML).toHaveBeenCalledWith(diagramXml));
    expect(mocks.layoutProcess).not.toHaveBeenCalled();
    expect(mocks.zoom).toHaveBeenCalledWith("fit-viewport");
  });

  it("fits diagrams from finite BPMN-DI bounds", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, top: 0, right: 800, bottom: 320, left: 0,
      width: 800, height: 320, toJSON: () => ({}),
    } as DOMRect);
    render(<BpmnViewer xml={diagramWithBoundsXml} />);

    await waitFor(() => expect(mocks.viewbox).toHaveBeenCalled());
    expect(mocks.viewbox).toHaveBeenCalledWith({ x: -50, y: 0, width: 400, height: 160 });
    expect(mocks.zoom).not.toHaveBeenCalled();
  });

  it("shows an alert for malformed XML without importing or laying it out", async () => {
    render(<BpmnViewer xml="<bpmn:definitions" />);

    expect(await screen.findByText("BPMN diagram could not be rendered")).toBeTruthy();
    expect(mocks.importXML).not.toHaveBeenCalled();
    expect(mocks.layoutProcess).not.toHaveBeenCalled();
  });

  it("destroys the viewer when unmounted", () => {
    const { unmount } = render(<BpmnViewer xml={diagramXml} />);

    unmount();

    expect(mocks.destroy).toHaveBeenCalledOnce();
  });
});
