// Minimal line-level diff for previewing config changes before applying.
export interface DiffLine {
  kind: "same" | "add" | "del";
  text: string;
}

/** Show nearby context without rendering thousands of unchanged URL rows. */
export function diffContext(lines: DiffLine[], context = 3): DiffLine[] {
  const visible = new Set<number>();
  lines.forEach((line, index) => {
    if (line.kind !== "same") {
      for (let i = Math.max(0, index - context); i <= Math.min(lines.length - 1, index + context); i++) visible.add(i);
    }
  });
  if (!visible.size) return [{ kind: "same", text: "No changes." }];
  const out: DiffLine[] = [];
  let skipped = 0;
  lines.forEach((line, index) => {
    if (!visible.has(index)) { skipped++; return; }
    if (skipped) out.push({ kind: "same", text: `… ${skipped} unchanged lines …` });
    skipped = 0;
    out.push(line);
  });
  if (skipped) out.push({ kind: "same", text: `… ${skipped} unchanged lines …` });
  return out;
}

export function lineDiff(before: string, after: string): DiffLine[] {
  const original = before.split("\n");
  const updated = after.split("\n");
  // Large source URL lists are normal. Trim unchanged context before the
  // quadratic step, and cap that step when both sides really changed.
  let start = 0;
  while (start < original.length && start < updated.length && original[start] === updated[start]) start++;
  let end = 0;
  while (end < original.length - start && end < updated.length - start &&
    original[original.length - 1 - end] === updated[updated.length - 1 - end]) end++;
  const prefix: DiffLine[] = original.slice(0, start).map((text) => ({ kind: "same", text }));
  const suffix: DiffLine[] = end ? original.slice(-end).map((text) => ({ kind: "same", text })) : [];
  const a = original.slice(start, original.length - end);
  const b = updated.slice(start, updated.length - end);
  if (a.length * b.length > 1_000_000) {
    return [...prefix, ...a.map((text): DiffLine => ({ kind: "del", text })),
      ...b.map((text): DiffLine => ({ kind: "add", text })), ...suffix];
  }
  // Longest common subsequence table.
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "del", text: a[i] });
      i++;
    } else {
      out.push({ kind: "add", text: b[j] });
      j++;
    }
  }
  while (i < a.length) out.push({ kind: "del", text: a[i++] });
  while (j < b.length) out.push({ kind: "add", text: b[j++] });
  return [...prefix, ...out, ...suffix];
}
