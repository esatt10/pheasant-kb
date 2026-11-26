import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, normalizePath, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function extensions(): Plugin {
  const id = "virtual:pheasant-extensions";
  const virtual = `\0${id}`;
  const selected = [...new Set((process.env.PHEASANT_BUILD_EXTENSIONS ?? "").split(",").map((name) => name.trim()).filter(Boolean))];
  return {
    name: "pheasant-extensions",
    resolveId: (name) => name === id ? virtual : undefined,
    load: (name) => {
      if (name !== virtual) return;
      const imports = selected.map((extension, index) => {
        if (!/^[a-z][a-z0-9-]{0,39}$/.test(extension)) throw new Error(`Invalid extension ID ${extension}`);
        const entry = fileURLToPath(new URL(`../extensions/${extension}/ui/extension.tsx`, import.meta.url));
        if (!existsSync(entry)) throw new Error(`Missing UI extension ${extension}`);
        return `import extension${index} from ${JSON.stringify(normalizePath(entry))};`;
      });
      return `${imports.join("\n")}\nexport default [${selected.map((_, index) => `extension${index}`).join(", ")}];`;
    },
  };
}

// The pheasant indexing container stays its own workload; this app is built to a
// static bundle and talks to it purely over HTTP. In dev we proxy API calls to a
// locally running pheasant so the bundle can use same-origin paths.
export default defineConfig({
  plugins: [react(), extensions()],
  resolve: {
    alias: { "@pheasant/ui": fileURLToPath(new URL("./src", import.meta.url)) },
    dedupe: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
  },
  server: {
    port: 5173,
    fs: { allow: [".", "../extensions"] },
    proxy: {
      // Forward the pheasant HTTP API to the running container during dev.
      "/api": {
        target: process.env.PHEASANT_API_BASE ?? "http://localhost:8765",
        changeOrigin: true,
        // The dictation relay (`/assistant/voice/relay`) is a WebSocket.
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
