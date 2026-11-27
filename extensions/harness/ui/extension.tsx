import type { UiExtension } from "@pheasant/ui/extensions";
import { HarnessPage } from "./HarnessPage";
import { TelemetryPanel } from "./TelemetryPanel";
import "./styles.css";

export default {
  id: "harness",
  apiVersion: 1,
  routes: [{ path: "/harness", label: "Harness", Page: HarnessPage }],
  // The Admin page's Harness tab: runs, spend, checks, connection calls and
  // every telemetry destination, gated as `GET /harness/telemetry` is.
  adminTabs: [{ id: "telemetry", label: "Harness", permission: "security.manage", Panel: TelemetryPanel }],
} satisfies UiExtension;
