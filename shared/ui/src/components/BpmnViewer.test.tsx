import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const zoom = vi.fn();
  const importXML = vi.fn();
  const destroy = vi.fn();
  const get = vi.fn(() => ({ zoom }));
  const viewer = { destroy, get, importXML };
  const layoutProcess = vi.fn();

  return { destroy, get, importXML, layoutProcess, viewer, zoom };
});

vi.mock("bpmn-js/lib/NavigatedViewer", () => ({
  default: vi.fn(function MockNavigatedViewer() {
    return mocks.viewer;
  }),
}));

vi.mock("bpmn-auto-layout", () => ({
  layoutProcess: mocks.layoutProcess,
}));

import { BpmnViewer } from "./BpmnViewer";

const BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL";
const BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI";
const noDiagramXml = `<bpmn:definitions xmlns:bpmn="${BPMN_NS}"/>`;
const diagramXml = `<bpmn:definitions xmlns:bpmn="${BPMN_NS}" xmlns:bpmndi="${BPMNDI_NS}"><bpmndi:BPMNDiagram id="diagram"/></bpmn:definitions>`;

describe("BpmnViewer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.importXML.mockResolvedValue({ warnings: [] });
    mocks.layoutProcess.mockResolvedValue({ xml: "laid-out-bpmn", warnings: [] });
  });

  afterEach(() => cleanup());

  it("lays out BPMN XML without DI before importing it", async () => {
    render(<BpmnViewer xml={noDiagramXml} />);

    await waitFor(() => expect(mocks.importXML).toHaveBeenCalledWith("laid-out-bpmn"));
    expect(mocks.layoutProcess).toHaveBeenCalledWith(noDiagramXml);
    expect(mocks.zoom).toHaveBeenCalledWith("fit-viewport");
  });

  it("imports XML with DI without running auto-layout", async () => {
    render(<BpmnViewer xml={diagramXml} />);

    await waitFor(() => expect(mocks.importXML).toHaveBeenCalledWith(diagramXml));
    expect(mocks.layoutProcess).not.toHaveBeenCalled();
    expect(mocks.zoom).toHaveBeenCalledWith("fit-viewport");
  });

  it("shows an alert for malformed XML without importing it", async () => {
    render(<BpmnViewer xml="<bpmn:definitions" />);

    expect(
      await screen.findByText("BPMN diagram could not be rendered"),
    ).toBeTruthy();
    expect(mocks.importXML).not.toHaveBeenCalled();
  });

  it("destroys the viewer when unmounted", () => {
    const { unmount } = render(<BpmnViewer xml={diagramXml} />);

    unmount();

    expect(mocks.destroy).toHaveBeenCalledOnce();
  });
});
