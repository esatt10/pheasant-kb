import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
const code = ts.transpileModule(readFileSync(new URL("../src/artifacts/folders.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { artifactFolder } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);

test("typed knowledge browses its real scope folder", () => {
  const folder = artifactFolder({ source: "agent-memory", folder: "org" });
  assert.equal(folder.label, "Shared knowledge");
  assert.equal(folder.path, "org");
  assert.equal(folder.source, "agent-memory");
  assert.equal(folder.browsable, true);
  assert.notEqual(folder.key, artifactFolder({ source: "another", folder: "org" }).key);
});
test("saved reports group by day and owner", () => {
  const folder = artifactFolder({ source: "artifacts", folder: "2026/11/13/shared/title--123" });
  assert.equal(folder.label, "2026-11-13");
  assert.equal(folder.path, "2026/11/13/shared");
  assert.equal(folder.owner, "shared");
});
test("unindexed run outputs do not offer a nonexistent source folder", () => {
  const folder = artifactFolder({});
  assert.equal(folder.label, "Published outputs");
  assert.equal(folder.browsable, false);
});
