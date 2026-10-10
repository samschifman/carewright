declare module "bpmn-auto-layout" {
  export function layoutProcess(
    xml: string,
  ): Promise<{ xml: string; warnings: unknown[] }>;
}

declare module "*.css";
