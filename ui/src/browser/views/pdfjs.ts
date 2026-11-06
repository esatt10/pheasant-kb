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

/** Bytes per range request: a page of a typical book is well under this. */
const RANGE_CHUNK = 256 * 1024;

export function loadPdf(
  source: ArrayBuffer | { url: string; headers: Record<string, string> },
): Promise<PDFDocumentProxy> {
  if (!worker) {
    worker = new PdfWorker();
    GlobalWorkerOptions.workerPort = worker;
  }
  // From a URL, pdf.js streams the file and, when the server answers
  // `Accept-Ranges: bytes`, fetches the ranges it needs next (the
  // cross-reference table, then each page drawn or searched) while the stream
  // is still arriving -- so a page shows at whichever comes first. Ranges
  // alone were slower on a fast link: a book that is not linearized took 17
  // serial rounds before page 1 (7.2 s for 58 MB from Azure, against 2.0 s
  // for both), while a slow link is where the stream alone was the wait.
  const params =
    source instanceof ArrayBuffer
      ? { data: source }
      : {
          url: source.url,
          httpHeaders: source.headers,
          rangeChunkSize: RANGE_CHUNK,
          disableAutoFetch: true,
        };
  // No `eval`: a PDF's fonts are compiled without it, at some cost in speed.
  return getDocument({ ...params, isEvalSupported: false }).promise;
}

export { TextLayer };
export type { PDFDocumentProxy };
