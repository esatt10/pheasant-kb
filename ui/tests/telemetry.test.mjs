import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import ts from "typescript";
const source = fs.readFileSync(new URL("../src/telemetry.ts", import.meta.url), "utf8");
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2021, module: ts.ModuleKind.ESNext } }).outputText;
const telemetry = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));

test("disabled telemetry creates no trace or buffered data", () => {
  telemetry.configureBrowserTelemetry(false);
  telemetry.recordUI("ask", "/");
  assert.equal(telemetry.browserTraceparent(), null);
  assert.deepEqual(telemetry.drainUIEvents(), []);
});
test("only allowed routes/actions are buffered and content is absent", () => {
  telemetry.configureBrowserTelemetry(true);
  telemetry.recordUI("form secret", "/");
  telemetry.recordUI("navigate", "/sources?token=secret");
  assert.deepEqual(telemetry.drainUIEvents(), []);
  telemetry.recordUI("ask", "/");
  const [event] = telemetry.drainUIEvents();
  assert.deepEqual(Object.keys(event).sort(), ["action", "duration_ms", "route", "span_id", "started_at", "trace_id"]);
  assert.equal(telemetry.browserTraceparent(), `00-${event.trace_id}-${event.span_id}-01`);
});
test("sampling zero propagates an unsampled context", () => {
  telemetry.configureBrowserTelemetry(true, 0);
  telemetry.recordUI("ask", "/");
  assert.equal(telemetry.browserTraceparent().slice(-3), "-00");
  assert.deepEqual(telemetry.drainUIEvents(), []);
});
test("buffer is bounded and disabling clears pending state", () => {
  telemetry.configureBrowserTelemetry(true, 1);
  for (let index = 0; index < 1000; index++) telemetry.recordUI("source.select", "/sources");
  assert.equal(telemetry.drainUIEvents().length, 50);
  telemetry.recordUI("navigate", "/");
  telemetry.configureBrowserTelemetry(false);
  assert.deepEqual(telemetry.drainUIEvents(), []);
  assert.equal(telemetry.browserTraceparent(), null);
});
