#!/usr/bin/env node
// Build `frontend/src/lib/agent-wire.ts` from the agent-service's
// per-model JSON Schema files.
//
// Walks `agent-service/src/agent_service/wire/schemas-agent/*.json`,
// merges them into a single combined schema with all models under
// `$defs`, then runs `json-schema-to-typescript` once. Single-pass
// generation lets json2ts deduplicate nested types via $ref instead
// of producing per-file collisions (e.g., `State`, `Verdict`).
//
// Auto-generated; never hand-edit `agent-wire.ts`. Re-run via
// `just regen-wire-types`.

import { compile } from "json-schema-to-typescript";
import { readFile, readdir, writeFile, mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "..", "..");
const SCHEMAS_DIR = resolve(
  REPO_ROOT,
  "agent-service",
  "src",
  "agent_service",
  "wire",
  "schemas-agent",
);
const OUT_FILE = resolve(REPO_ROOT, "frontend", "src", "lib", "agent-wire.ts");

const BANNER = `/* eslint-disable */
/**
 * AUTO-GENERATED FILE -- DO NOT EDIT.
 *
 * Source of truth: agent-service/src/agent_service/wire/agent.py
 * Generator: frontend/scripts/build-agent-wire.mjs (json-schema-to-typescript)
 * Re-run via: \`just regen-wire-types\`
 *
 * Drift between this file and the pydantic source fails CI via
 * agent-service/tests/integration/test_codegen_drift.py.
 */`;

async function main() {
  const files = (await readdir(SCHEMAS_DIR))
    .filter((f) => f.endsWith(".json"))
    .sort();

  if (files.length === 0) {
    console.error(`no schemas in ${SCHEMAS_DIR}`);
    process.exit(1);
  }

  // Combine all per-model schemas into one master schema. Each model
  // becomes a top-level $defs entry; pydantic's nested $defs (for
  // discriminator children, etc.) get merged in too with the same
  // names, so duplicate-name collisions resolve to the same shape.
  const masterDefs = {};
  const modelNames = [];

  for (const file of files) {
    const schemaPath = join(SCHEMAS_DIR, file);
    const schema = JSON.parse(await readFile(schemaPath, "utf-8"));
    const name = schema.title || file.replace(/\.json$/, "");
    modelNames.push(name);

    // Lift the model itself into $defs.
    const { $defs: nestedDefs, ...modelBody } = schema;
    masterDefs[name] = modelBody;

    // Merge nested $defs. If two schemas declare the same nested
    // type (e.g., shared enum), the second wins. Pydantic emits
    // identical schemas for identical types, so this is safe.
    if (nestedDefs) {
      Object.assign(masterDefs, nestedDefs);
    }
  }

  // The master schema has no top-level "type"; it's just a $defs
  // bundle. json2ts compiles every $defs entry as a top-level
  // interface/type. We pass `unreachableDefinitions: true` to force
  // emission of all definitions (otherwise json2ts only emits
  // referenced ones).
  const masterSchema = {
    $schema: "http://json-schema.org/draft-07/schema#",
    title: "AgentWire",
    type: "object",
    properties: {},
    additionalProperties: false,
    $defs: masterDefs,
  };

  const ts = await compile(masterSchema, "AgentWire", {
    bannerComment: "",
    additionalProperties: false,
    unreachableDefinitions: true,
    style: { singleQuote: false },
  });

  // Strip the master `AgentWire` placeholder interface (it was a
  // compile-time host for the $defs bundle, not a real wire type).
  const stripped = ts
    .split(/\n(?=export (?:interface|type|enum) )/g)
    .filter(
      (block) =>
        !block.trimStart().startsWith("export interface AgentWire {"),
    )
    .join("\n")
    .replace(/^\s+/, "");

  const out = `${BANNER}\n\n${stripped}`;
  await mkdir(dirname(OUT_FILE), { recursive: true });
  await writeFile(OUT_FILE, out, "utf-8");
  console.error(
    `wrote ${OUT_FILE} (${modelNames.length} models, ${Object.keys(masterDefs).length} $defs)`,
  );
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
