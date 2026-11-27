import type { UiExtension } from "@pheasant/ui/extensions";
import { HarnessPage } from "./HarnessPage";
import "./styles.css";

export default {
  id: "harness",
  apiVersion: 1,
  routes: [{ path: "/harness", label: "Harness", Page: HarnessPage }],
} satisfies UiExtension;
