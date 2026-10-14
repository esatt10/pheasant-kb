import { GlobalWorkerOptions, TextLayer, getDocument } from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?worker";

/**
 * pdf.js, loaded only when a PDF is opened in the rendered view.
 *
 * The worker is bundled by Vite as an ordinary `.js` asset rather than
 * referenced as pdf.js's `.mjs` file, because a module worker is refused
 * unless it is served with a JavaScript MIME type and not every server this
 * bundle is behind knows `.mjs`. One worker serves every document.
 */
let worker: Worker | null = null;

export function loadPdf(data: ArrayBuffer): Promise<PDFDocumentProxy> {
  if (!worker) {
    worker = new PdfWorker();
    GlobalWorkerOptions.workerPort = worker;
  }
  // No `eval`: a PDF's fonts are compiled without it, at some cost in speed.
  return getDocument({ data, isEvalSupported: false }).promise;
}

export { TextLayer };
export type { PDFDocumentProxy };
