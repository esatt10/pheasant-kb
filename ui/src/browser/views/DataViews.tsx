import yaml from "js-yaml";
import { useMemo, useState } from "react";
import { locatePassage, normalizeForMatch } from "../passage";

/* ------------------------------------------------------------- JSON / YAML */

/** Parse JSON (comments and trailing commas tolerated, for `.jsonc`) or YAML. */
export function parseStructured(
  text: string,
  kind: "json" | "yaml",
): { value: unknown; error: string | null } {
  try {
    if (kind === "yaml") {
      const documents = yaml.loadAll(text);
      return { value: documents.length === 1 ? documents[0] : documents, error: null };
    }
    try {
      return { value: JSON.parse(text), error: null };
    } catch {
      const relaxed = text
        .replace(/("(?:\\.|[^"\\])*")|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, (_m, str) => str ?? "")
        .replace(/,(\s*[}\]])/g, "$1");
      return { value: JSON.parse(relaxed), error: null };
    }
  } catch (error) {
    return { value: undefined, error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * A collapsible tree of a parsed document. Opens two levels deep; "Expand
 * all" and "Collapse all" re-key the tree so every node takes the new default.
 */
export function StructuredTree({ value, find }: { value: unknown; find?: string }) {
  const [depth, setDepth] = useState(2);
  const [generation, setGeneration] = useState(0);
  const needle = (find ?? "").trim().toLowerCase();
  return (
    <div className="tree-view">
      <div className="tree-view__bar">
        <button
          type="button"
          className="btn btn--small btn--ghost"
          onClick={() => {
            setDepth(Infinity);
            setGeneration((g) => g + 1);
          }}
        >
          Expand all
        </button>
        <button
          type="button"
          className="btn btn--small btn--ghost"
          onClick={() => {
            setDepth(1);
            setGeneration((g) => g + 1);
          }}
        >
          Collapse all
        </button>
      </div>
      <TreeNode key={generation} label={null} value={value} level={0} openDepth={depth} needle={needle} />
    </div>
  );
}

function TreeNode({
  label,
  value,
  level,
  openDepth,
  needle,
}: {
  label: string | null;
  value: unknown;
  level: number;
  openDepth: number;
  needle: string;
}) {
  const container = value !== null && typeof value === "object";
  const entries = useMemo(
    () =>
      container
        ? Array.isArray(value)
          ? value.map((item, index) => [String(index), item] as const)
          : Object.entries(value as Record<string, unknown>)
        : [],
    [container, value],
  );
  const [open, setOpen] = useState(level < openDepth);
  const hit = needle && label !== null && label.toLowerCase().includes(needle);
  const key =
    label === null ? null : (
      <span className={`tree-key${hit ? " tree-key--hit" : ""}`}>{label}</span>
    );
  if (!container) {
    const literal = scalar(value);
    const valueHit = needle && literal.text.toLowerCase().includes(needle);
    return (
      <div className="tree-row" style={{ paddingLeft: level * 14 }}>
        {key}
        {key ? <span className="tree-colon">: </span> : null}
        <span className={`tree-value tree-value--${literal.kind}${valueHit ? " tree-value--hit" : ""}`}>
          {literal.text}
        </span>
      </div>
    );
  }
  const isArray = Array.isArray(value);
  const summary = isArray ? `[${entries.length}]` : `{${entries.length}}`;
  return (
    <div className="tree-branch">
      <button
        type="button"
        className="tree-row tree-row--toggle"
        style={{ paddingLeft: level * 14 }}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="tree-caret">{open ? "▾" : "▸"}</span>
        {key}
        {key ? <span className="tree-colon">: </span> : null}
        <span className="tree-summary">{summary}</span>
      </button>
      {open
        ? entries.slice(0, 2000).map(([childKey, child]) => (
            <TreeNode
              key={childKey}
              label={childKey}
              value={child}
              level={level + 1}
              openDepth={openDepth}
              needle={needle}
            />
          ))
        : null}
      {open && entries.length > 2000 ? (
        <div className="tree-row muted small" style={{ paddingLeft: (level + 1) * 14 }}>
          …{(entries.length - 2000).toLocaleString()} more
        </div>
      ) : null}
    </div>
  );
}

function scalar(value: unknown): { kind: string; text: string } {
  if (value === null || value === undefined) return { kind: "null", text: "null" };
  if (typeof value === "string") return { kind: "string", text: JSON.stringify(value) };
  if (typeof value === "number") return { kind: "number", text: String(value) };
  if (typeof value === "boolean") return { kind: "boolean", text: String(value) };
  if (value instanceof Date) return { kind: "string", text: value.toISOString() };
  return { kind: "string", text: String(value) };
}

/* ------------------------------------------------------------------ tables */

/** RFC 4180 CSV (quoted fields, doubled quotes, newlines inside quotes). */
export function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else quoted = false;
      } else field += ch;
      continue;
    }
    if (ch === '"' && field === "") quoted = true;
    else if (ch === delimiter) {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += ch;
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((cell) => cell !== ""));
}

const ROW_WINDOW = 1000;

/**
 * A table with a sticky header, click-to-sort columns (numbers sort as
 * numbers), and a row filter. The first row is the header.
 */
export function DataTable({ rows, find, passage }: { rows: string[][]; find?: string; passage?: string | null }) {
  const [sort, setSort] = useState<{ column: number; descending: boolean } | null>(null);
  // A cited passage further down than the first window opens with its row drawn.
  const [limit, setLimit] = useState(() => {
    const row = passageRow(rows, passage ?? null);
    return row === null ? ROW_WINDOW : Math.max(ROW_WINDOW, row + 50);
  });
  const header = rows[0] ?? [];
  const width = Math.max(header.length, ...rows.slice(1, 200).map((r) => r.length));
  const needle = (find ?? "").trim().toLowerCase();
  const body = useMemo(() => {
    let out = rows.slice(1);
    if (needle) out = out.filter((r) => r.some((cell) => cell.toLowerCase().includes(needle)));
    if (sort) {
      const { column, descending } = sort;
      const numeric = out.every((r) => r[column] === undefined || r[column] === "" || isNumber(r[column]));
      out = [...out].sort((a, b) => {
        const x = a[column] ?? "";
        const y = b[column] ?? "";
        const order = numeric ? Number(x || 0) - Number(y || 0) : x.localeCompare(y, undefined, { numeric: true });
        return descending ? -order : order;
      });
    }
    return out;
  }, [rows, needle, sort]);

  if (!rows.length) return <p className="muted small viewer-pad">This table is empty.</p>;
  return (
    <div className="grid-table">
      <div className="grid-table__meta muted small" data-passage-skip>
        {body.length.toLocaleString()} {needle ? "matching " : ""}rows · {width} columns
      </div>
      <div className="grid-table__scroll">
        <table>
          <thead>
            <tr>
              <th className="grid-table__index" data-passage-skip>#</th>
              {Array.from({ length: width }, (_, column) => (
                <th key={column}>
                  <button
                    type="button"
                    onClick={() =>
                      setSort((current) =>
                        current?.column === column
                          ? current.descending
                            ? null
                            : { column, descending: true }
                          : { column, descending: false },
                      )
                    }
                    title="Sort by this column"
                  >
                    {header[column] || `column ${column + 1}`}
                    <span className="grid-table__sort">
                      {sort?.column === column ? (sort.descending ? "▾" : "▴") : ""}
                    </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.slice(0, limit).map((row, index) => (
              <tr key={index}>
                <td className="grid-table__index" data-passage-skip>{index + 1}</td>
                {Array.from({ length: width }, (_, column) => (
                  <td key={column} className={isNumber(row[column] ?? "") ? "grid-table__number" : undefined}>
                    {row[column] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {body.length > limit ? (
        <button type="button" className="btn btn--small" onClick={() => setLimit((l) => l + ROW_WINDOW * 5)}>
          Show more rows ({(body.length - limit).toLocaleString()} left)
        </button>
      ) : null}
    </div>
  );
}

/** The row a cited passage starts in, or null when it is not in this table. */
function passageRow(rows: string[][], passage: string | null): number | null {
  if (!passage) return null;
  const starts: number[] = [];
  let joined = "";
  for (const row of rows) {
    starts.push(joined.length);
    joined += normalizeForMatch(row.join(" "));
  }
  const span = locatePassage(joined, normalizeForMatch(passage));
  if (!span) return null;
  let index = 0;
  while (index + 1 < starts.length && starts[index + 1] <= span[0]) index += 1;
  return index;
}

function isNumber(value: string): boolean {
  return value.trim() !== "" && /^-?[\d,]*\.?\d+(e[+-]?\d+)?%?$/i.test(value.trim()) && !/^0\d/.test(value.trim());
}

/**
 * A workbook from its extracted text: sheets are blocks separated by a blank
 * line, each led by its name, rows tab-separated (`ingestion.office`).
 */
export function parseSheets(text: string): { name: string; rows: string[][] }[] {
  return text
    .split(/\n\s*\n/)
    .map((block, index) => {
      const lines = block.split("\n").filter((line) => line.trim());
      const tabbed = lines.some((line) => line.includes("\t"));
      const width = (line: string) => line.trim().split(/\s+/).length;
      const named =
        lines.length > 1 &&
        (tabbed ? !lines[0].includes("\t") : width(lines[0]) !== width(lines[1]));
      const body = named ? lines.slice(1) : lines;
      return {
        name: named ? lines[0] : `Sheet ${index + 1}`,
        rows: tabbed ? body.map((line) => line.split("\t")) : splitOnSpaces(body),
      };
    })
    .filter((sheet) => sheet.rows.length);
}

/**
 * Rows whose cells were joined by single spaces (the indexed text of a
 * workbook whose original is not readable here). Split on whitespace only when
 * nearly every row has the header's width; otherwise a cell holding a space
 * would shift every column after it, so the rows stay whole.
 */
function splitOnSpaces(lines: string[]): string[][] {
  const split = lines.map((line) => line.trim().split(/\s+/));
  const width = split[0]?.length ?? 0;
  const regular = split.filter((cells) => cells.length === width).length;
  return width > 1 && regular >= split.length * 0.8 ? split : lines.map((line) => [line]);
}

export function SpreadsheetView({ text, find, passage }: { text: string; find?: string; passage?: string | null }) {
  const sheets = useMemo(() => parseSheets(text), [text]);
  // Open on the sheet the cited passage is in.
  const [active, setActive] = useState(() => {
    if (!passage) return 0;
    const needle = normalizeForMatch(passage);
    const index = sheets.findIndex((sheet) =>
      locatePassage(normalizeForMatch(sheet.rows.map((row) => row.join(" ")).join(" ")), needle),
    );
    return Math.max(0, index);
  });
  if (!sheets.length) return <p className="muted small viewer-pad">No cells were extracted.</p>;
  const sheet = sheets[Math.min(active, sheets.length - 1)];
  return (
    <div className="sheet-view">
      {sheets.length > 1 ? (
        <div className="sheet-view__tabs" role="tablist" data-passage-skip>
          {sheets.map((candidate, index) => (
            <button
              type="button"
              role="tab"
              aria-selected={index === active}
              key={`${candidate.name}-${index}`}
              className={`chip${index === active ? " chip--active" : ""}`}
              onClick={() => setActive(index)}
            >
              {candidate.name}
            </button>
          ))}
        </div>
      ) : null}
      <DataTable key={active} rows={sheet.rows} find={find} passage={passage} />
      <p className="muted small viewer-pad">
        Cell values as extracted for indexing: empty cells are dropped, so a sparse row can shift
        left.
      </p>
    </div>
  );
}
