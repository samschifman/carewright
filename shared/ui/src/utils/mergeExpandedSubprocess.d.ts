export interface MergeExpandedSubprocessResult {
  xml: string;
  warnings: Array<{ message?: string; [key: string]: unknown }>;
}

export function mergeExpandedSubprocess(xml: string): Promise<MergeExpandedSubprocessResult>;
