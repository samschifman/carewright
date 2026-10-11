import { useEffect, useRef, useState } from "react";
import { Alert } from "@patternfly/react-core";
import NavigatedViewer from "bpmn-js/lib/NavigatedViewer";
import { layoutProcess } from "bpmn-auto-layout";
import { mergeExpandedSubprocess } from "../utils/mergeExpandedSubprocess.js";

import "bpmn-js/dist/assets/diagram-js.css";
import "bpmn-js/dist/assets/bpmn-js.css";
import "bpmn-js/dist/assets/bpmn-font/css/bpmn.css";

const BPMNDI_NAMESPACE = "http://www.omg.org/spec/BPMN/20100524/DI";
const DI_NAMESPACE = "http://www.omg.org/spec/DD/20100524/DI";
const DC_NAMESPACE = "http://www.omg.org/spec/DD/20100524/DC";

interface Viewbox {
  x: number;
  y: number;
  width: number;
  height: number;
}

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

function viewboxForDiagram(xml: string, viewportWidth: number, viewportHeight: number): Viewbox | null {
  if (!(viewportWidth > 0) || !(viewportHeight > 0)) return null;

  const document = new DOMParser().parseFromString(xml, "application/xml");
  const diagram = document.getElementsByTagNameNS(BPMNDI_NAMESPACE, "BPMNDiagram")[0];
  const plane = diagram?.getElementsByTagNameNS(BPMNDI_NAMESPACE, "BPMNPlane")[0];
  if (!plane) return null;

  const coordinates: Array<{ x: number; y: number }> = [];
  for (const bounds of Array.from(plane.getElementsByTagNameNS(DC_NAMESPACE, "Bounds"))) {
    if (["x", "y", "width", "height"].some((attribute) => bounds.getAttribute(attribute) === null)) {
      continue;
    }
    const x = Number(bounds.getAttribute("x"));
    const y = Number(bounds.getAttribute("y"));
    const width = Number(bounds.getAttribute("width"));
    const height = Number(bounds.getAttribute("height"));
    if ([x, y, width, height].every(Number.isFinite)) {
      coordinates.push({ x, y }, { x: x + width, y: y + height });
    }
  }
  for (const waypoint of Array.from(plane.getElementsByTagNameNS(DI_NAMESPACE, "waypoint"))) {
    if (["x", "y"].some((attribute) => waypoint.getAttribute(attribute) === null)) continue;
    const x = Number(waypoint.getAttribute("x"));
    const y = Number(waypoint.getAttribute("y"));
    if (Number.isFinite(x) && Number.isFinite(y)) coordinates.push({ x, y });
  }
  if (coordinates.length === 0) return null;

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const point of coordinates) {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    maxX = Math.max(maxX, point.x);
    maxY = Math.max(maxY, point.y);
  }
  const padding = 40;
  let width = maxX - minX + padding * 2;
  let height = maxY - minY + padding * 2;
  if (!(width > 0) || !(height > 0)) return null;

  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  const aspectRatio = viewportWidth / viewportHeight;
  if (width / height < aspectRatio) {
    width = height * aspectRatio;
  } else {
    height = width / aspectRatio;
  }

  return {
    x: centerX - width / 2,
    y: centerY - height / 2,
    width,
    height,
  };
}

function waitForVisibleContainer(container: HTMLElement): {
  promise: Promise<boolean>;
  cancel: () => void;
} {
  if (typeof ResizeObserver === "undefined") {
    return { promise: Promise.resolve(true), cancel: () => undefined };
  }

  let observer: ResizeObserver | undefined;
  let interval: number | undefined;
  let finish: ((visible: boolean) => void) | undefined;
  const promise = new Promise<boolean>((resolve) => {
    finish = (visible) => {
      observer?.disconnect();
      if (interval !== undefined) window.clearInterval(interval);
      resolve(visible);
    };

    const check = () => {
      const { width, height } = container.getBoundingClientRect();
      if (width > 0 && height > 0) finish?.(true);
    };

    observer = new ResizeObserver(check);
    observer.observe(container);
    interval = window.setInterval(check, 100);
    check();
  });

  return { promise, cancel: () => finish?.(false) };
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
    let cancelVisibilityWait: (() => void) | undefined;
    setImportError(null);

    const renderDiagram = async () => {
      try {
        let xmlWithDiagram = xml;
        if (!hasDiagramInterchange(xml)) {
          const { xml: laidOutXml, warnings: layoutWarnings } = await layoutProcess(xml);
          const merged = await mergeExpandedSubprocess(laidOutXml);
          xmlWithDiagram = merged.xml;
          if (layoutWarnings.length > 0 || merged.warnings.length > 0) {
            console.warn("BPMN layout warnings:", [...layoutWarnings, ...merged.warnings]);
          }
        }
        if (cancelled) return;

        const { warnings } = await viewer.importXML(xmlWithDiagram);
        if (cancelled) return;

        const container = containerRef.current;
        if (!container) return;
        const bounds = container.getBoundingClientRect();
        if (bounds.width === 0 || bounds.height === 0) {
          const visibilityWait = waitForVisibleContainer(container);
          cancelVisibilityWait = visibilityWait.cancel;
          const becameVisible = await visibilityWait.promise;
          cancelVisibilityWait = undefined;
          if (!becameVisible || cancelled) return;
        }

        const canvas = viewer.get("canvas") as {
          zoom: (scale: string) => void;
          viewbox: (box: Viewbox) => void;
        };
        const viewport = container.getBoundingClientRect();
        const viewbox = viewboxForDiagram(xmlWithDiagram, viewport.width, viewport.height);
        if (viewbox) {
          canvas.viewbox(viewbox);
        } else {
          canvas.zoom("fit-viewport");
        }
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
      cancelVisibilityWait?.();
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
