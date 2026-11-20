import { setUploadPath } from "../api/client";

/**
 * Every file in a drop, folders walked, each remembering its path inside the
 * folder it came from (`setUploadPath`). `DataTransfer.files` lists only the
 * top-level items and gives a dropped folder no contents at all, so a bundle
 * dragged in as a folder arrived as nothing; the entry API is what reaches
 * inside it.
 */
export async function droppedFiles(transfer: DataTransfer): Promise<File[]> {
  const entries = Array.from(transfer.items ?? [])
    .map((item) => (item.kind === "file" ? item.webkitGetAsEntry?.() : null))
    .filter((entry): entry is FileSystemEntry => Boolean(entry));
  if (!entries.length || entries.every((entry) => entry.isFile)) {
    return Array.from(transfer.files ?? []);
  }
  const files: File[] = [];
  await Promise.all(entries.map((entry) => walk(entry, "", files)));
  return files;
}

async function walk(entry: FileSystemEntry, prefix: string, into: File[]): Promise<void> {
  const path = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) =>
      (entry as FileSystemFileEntry).file(resolve, reject),
    );
    setUploadPath(file, path);
    into.push(file);
    return;
  }
  if (!entry.isDirectory) return;
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  // readEntries answers in batches (about 100 in Chromium) until it returns none.
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
      reader.readEntries(resolve, reject),
    );
    if (!batch.length) break;
    await Promise.all(batch.map((child) => walk(child, path, into)));
  }
}
