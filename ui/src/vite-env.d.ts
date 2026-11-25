/// <reference types="vite/client" />

declare module "virtual:pheasant-extensions" {
  const extensions: readonly import("./extensions").UiExtension[];
  export default extensions;
}

declare module "react-cytoscapejs" {
  import type { Core } from "cytoscape";
  import type { CSSProperties } from "react";

  export interface CytoscapeComponentProps {
    elements: unknown[];
    style?: CSSProperties;
    stylesheet?: unknown[];
    layout?: unknown;
    cy?: (cy: Core) => void;
    className?: string;
    minZoom?: number;
    maxZoom?: number;
    wheelSensitivity?: number;
  }

  const CytoscapeComponent: (props: CytoscapeComponentProps) => JSX.Element;
  export default CytoscapeComponent;
}

interface ImportMetaEnv {
  readonly VITE_PHEASANT_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
