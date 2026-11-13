import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const code = ts.transpileModule(
  readFileSync(new URL("../src/util/diff.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.ESNext } },
).outputText;
const { lineDiff, diffContext } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);

function roundtrip(before, after) {
  const diff = lineDiff(before, after);
  assert.equal(diff.filter((x) => x.kind !== "add").map((x) => x.text).join("\n"), before);
  assert.equal(diff.filter((x) => x.kind !== "del").map((x) => x.text).join("\n"), after);
  return diff;
}

test("a production-sized config and one edit fit a 128 MB heap", () => {
  const lines = Array.from({ length: 40_000 }, (_, i) => `  - https://example.test/document/${i}`);
  const before = lines.join("\n");
  assert.ok(roundtrip(before, before).every((x) => x.kind === "same"));
  lines[20_000] = "  - https://example.test/replacement";
  const diff = roundtrip(before, lines.join("\n"));
  assert.equal(diff.filter((x) => x.kind !== "same").length, 2);
  assert.ok(diffContext(diff).length <= 12);
  assert.deepEqual(diffContext(lineDiff(before, before)), [{ kind: "same", text: "No changes." }]);
});

test("large unrelated changes stay bounded and preserve both documents", () => {
  const before = Array.from({ length: 1500 }, (_, i) => `before ${i}`).join("\n");
  const after = Array.from({ length: 1500 }, (_, i) => `after ${i}`).join("\n");
  assert.equal(roundtrip(before, after).length, 3000);
});

test("small edits preserve common lines and insertion order", () => {
  const diff = roundtrip("a\nb\nc\nd\ne", "a\nx\nc\ny\ne");
  assert.deepEqual(diff.filter((x) => x.kind === "same").map((x) => x.text), ["a", "c", "e"]);
  roundtrip("", "line");
  roundtrip("line\n", "");
});
