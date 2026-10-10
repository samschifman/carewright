#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const uiRequire = createRequire(resolve(scriptDirectory, "../shared/ui/package.json"));
const { layoutProcess } = uiRequire("bpmn-auto-layout");
const inputPath = process.argv[2];

if (!inputPath) {
  process.stderr.write("Usage: node scripts/bpmn-layout.mjs in.bpmn > out.bpmn\n");
  process.exit(2);
}

try {
  const xml = await readFile(resolve(inputPath), "utf8");
  const { xml: laidOutXml, warnings } = await layoutProcess(xml);
  process.stdout.write(laidOutXml);
  for (const warning of warnings) {
    process.stderr.write(`${warning.message ?? warning}\n`);
  }
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`BPMN layout failed: ${message}\n`);
  process.exitCode = 1;
}
