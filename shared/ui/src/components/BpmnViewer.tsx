import { useEffect, useRef, useState } from "react";
import { Alert } from "@patternfly/react-core";
import { layoutProcess } from "bpmn-auto-layout";
import NavigatedViewer from "bpmn-js/lib/NavigatedViewer";

import "bpmn-js/dist/assets/diagram-js.css";
import "bpmn-js/dist/assets/bpmn-js.css";
import "bpmn-js/dist/assets/bpmn-font/css/bpmn.css";

const BPMNDI_NAMESPACE = "http://www.omg.org/spec/BPMN/20100524/DI";

export interface BpmnViewerProps {
  xml: string;
  height?: number;
  title?: string;
}

function hasDiagramInterchange(xml: string): boolean {
  const document = new DOMParser().parseFromString(xml, "application/xml");
  const parserError = document.querySelector("parsererror");
  if (parserError) {
    throw new Error(parserError.textContent || "BPMN XML is malformed");
  }

  return document.getElementsByTagNameNS(BPMNDI_NAMESPACE, "BPMNDiagram").length > 0;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function BpmnViewer({
  xml,
  height = 320,
  title = "BPMN process diagram",
}: BpmnViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<NavigatedViewer | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const viewer = new NavigatedViewer({ container: containerRef.current });
    viewerRef.current = viewer;

    return () => {
      viewer.destroy();
      viewerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !xml) return;

    let cancelled = false;
    setImportError(null);

    const renderDiagram = async () => {
      try {
        const xmlWithDiagram = hasDiagramInterchange(xml)
          ? xml
          : (await layoutProcess(xml)).xml;
        if (cancelled) return;

        const { warnings } = await viewer.importXML(xmlWithDiagram);
        if (cancelled) return;

        (viewer.get("canvas") as { zoom: (scale: string) => void }).zoom(
          "fit-viewport",
        );
        if (warnings.length > 0) {
          console.warn("BPMN import warnings:", warnings);
        }
      } catch (error) {
        if (!cancelled) setImportError(errorMessage(error));
      }
    };

    void renderDiagram();
    return () => {
      cancelled = true;
    };
  }, [xml]);

  return (
    <>
      {importError && (
        <Alert
          variant="danger"
          title="BPMN diagram could not be rendered"
          isInline
          style={{ marginBottom: 8 }}
        >
          {importError}
        </Alert>
      )}
      <div
        ref={containerRef}
        role="figure"
        aria-label={title}
        style={{ width: "100%", height, minHeight: 200, overflow: "hidden" }}
      />
    </>
  );
}
