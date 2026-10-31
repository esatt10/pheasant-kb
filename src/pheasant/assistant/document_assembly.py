"""How a cited file is read back from its chunks: whole, excerpted, or cut.

Split out of ``assistant.retrieval`` so the retriever stays reviewable as a
unit; ``retrieval`` imports every name here and ``PheasantRetriever.documents``
is the one caller. The rules themselves are documented on that method.
"""

from __future__ import annotations

from typing import Any

# Extensions whose files are read whole or not at all. Source and config
# files are *structurally* meaningful: an excerpt of a Python module with the
# imports cut off, or of a YAML file with half the keys, is not a smaller
# answer — it is a misleading one, and it is what makes a model invent the
# import it cannot see. They are also small: the threshold below exists for
# the pathological vendored bundle, not for anything a human wrote.
CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".cs",
        ".cpp",
        ".cc",
        ".c",
        ".h",
        ".hpp",
        ".swift",
        ".kt",
        ".scala",
        ".php",
        ".sh",
        ".bash",
        ".ps1",
        ".sql",
        ".r",
        ".m",
        ".lua",
        ".pl",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".ini",
        ".cfg",
        ".env",
        ".tf",
        ".dockerfile",
        ".gradle",
        ".proto",
        ".graphql",
    }
)

#: Original-file size above which a *prose* document is excerpted rather than
#: read whole. Deliberately measured against ``artifacts.size_bytes`` — what
#: the file actually is — not against the reassembled text, so the policy is a
#: property of the corpus rather than of whatever the budget happened to be.
LARGE_FILE_BYTES = 40_000

#: Chunks kept either side of a match when excerpting a large document.
ADJACENT_CHUNKS = 1


def _is_code(relative_path: str | None, language: str | None) -> bool:
    """Whether a file is source/config, and so must never be excerpted."""
    if language:  # the pipeline extracted symbols: something parsed it as code
        return True
    if not relative_path:
        return False
    suffix = relative_path[relative_path.rfind(".") :].lower() if "." in relative_path else ""
    name = relative_path.rsplit("/", 1)[-1].lower()
    return suffix in CODE_EXTENSIONS or name in ("dockerfile", "makefile")


def _reassemble(
    rows: list,
    anchor_ids: set[str],
    allowance: int,
    *,
    focused: bool = False,
) -> tuple[str, int, bool]:
    """Join a file's chunks back together, inside ``allowance`` characters.

    Returns ``(text, chunks_included, truncated)``.

    ``focused`` is the large-document policy: keep the chunks search matched
    plus their immediate neighbours and stop, **even when the allowance would
    permit more**. Filling the remaining budget with unrelated chunks of a
    400 KB document is not free — it dilutes the evidence the answer is
    supposed to be grounded in, and buries the matched region in the middle
    of the prompt. Small files and every code/config file are assembled whole
    instead (``focused=False``): there, the surrounding lines *are* the
    context.
    """
    blocks: list[str] = []
    for row in rows:
        label_bits = []
        if row["start_line"] is not None and row["end_line"] is not None:
            label_bits.append(f"lines {row['start_line']}-{row['end_line']}")
        heading = str(row["heading_path"] or "").strip()
        if heading:
            label_bits.append(heading)
        label = f"--- {' · '.join(label_bits)} ---\n" if label_bits else ""
        blocks.append(label + str(row["text"] or ""))

    anchors = [i for i, row in enumerate(rows) if str(row["id"]) in anchor_ids]

    if focused:
        # Only the matched neighbourhood, plus chunk 0 so the reader can still
        # tell which document this is (title, frontmatter, opening).
        wanted = {0}
        for anchor in anchors:
            for offset in range(-ADJACENT_CHUNKS, ADJACENT_CHUNKS + 1):
                if 0 <= anchor + offset < len(rows):
                    wanted.add(anchor + offset)
        order = sorted(wanted)
    else:
        total = sum(len(block) + 2 for block in blocks)
        if total <= allowance:
            return "\n\n".join(blocks), len(blocks), False
        # Over budget: claim positions by value rather than lopping off the
        # tail. Head first (what this file is), then what search matched (why
        # it came back), then outward from those.
        order = [0, *anchors]
        for anchor in anchors:
            for offset in (1, -1, 2, -2):
                if 0 <= anchor + offset < len(rows):
                    order.append(anchor + offset)
        order.extend(range(len(rows)))

    chosen: set[int] = set()
    spent = 0
    for index in order:
        if index in chosen:
            continue
        cost = len(blocks[index]) + 2
        if spent + cost > allowance:
            continue
        chosen.add(index)
        spent += cost
    if not chosen:  # a single chunk larger than the whole allowance
        return blocks[order[0]][:allowance], 1, True

    parts: list[str] = []
    previous = -1
    for index in sorted(chosen):
        gap = index - previous - 1
        if gap > 0 and previous >= 0:
            parts.append(f"--- … {gap} chunk(s) omitted … ---")
        parts.append(blocks[index])
        previous = index
    if previous < len(rows) - 1:
        parts.append(f"--- … {len(rows) - 1 - previous} chunk(s) omitted … ---")
    return "\n\n".join(parts), len(chosen), len(chosen) < len(rows)


def _chunk_label(row: Any) -> str:
    label_bits = []
    if row["start_line"] is not None and row["end_line"] is not None:
        label_bits.append(f"lines {row['start_line']}-{row['end_line']}")
    heading = str(row["heading_path"] or "").strip()
    if heading:
        label_bits.append(heading)
    return f"--- {' · '.join(label_bits)} ---\n" if label_bits else ""


def _descriptor_selection(
    rows: list[Any], anchor_ids: set[str], allowance: int, *, focused: bool
) -> tuple[list[int], bool, bool]:
    """Choose chunk positions from metadata without transferring their text.

    Returns ``(positions, truncated, all_fit)`` with the same priority and
    character accounting as :func:`_reassemble`.
    """
    lengths = [len(_chunk_label(row)) + int(row["text_length"] or 0) for row in rows]
    anchors = [i for i, row in enumerate(rows) if str(row["id"]) in anchor_ids]
    if focused:
        wanted = {0}
        for anchor in anchors:
            for offset in range(-ADJACENT_CHUNKS, ADJACENT_CHUNKS + 1):
                if 0 <= anchor + offset < len(rows):
                    wanted.add(anchor + offset)
        order = sorted(wanted)
    else:
        if sum(length + 2 for length in lengths) <= allowance:
            return list(range(len(rows))), False, True
        order = [0, *anchors]
        for anchor in anchors:
            for offset in (1, -1, 2, -2):
                if 0 <= anchor + offset < len(rows):
                    order.append(anchor + offset)
        order.extend(range(len(rows)))

    chosen: list[int] = []
    spent = 0
    for index in order:
        if index in chosen:
            continue
        cost = lengths[index] + 2
        if spent + cost <= allowance:
            chosen.append(index)
            spent += cost
    if not chosen and order:
        return [order[0]], True, False
    return sorted(chosen), len(chosen) < len(rows), False


def _selected_output_length(
    rows: list[Any],
    selected: list[int],
    text_limits: dict[str, int],
    allowance: int,
    *,
    truncated: bool,
    all_fit: bool,
) -> int:
    """Predict rendered characters from descriptors before fetching bodies."""
    if not selected:
        return 0
    block_lengths = {
        index: len(_chunk_label(rows[index]))
        + min(int(rows[index]["text_length"] or 0), text_limits[str(rows[index]["id"])])
        for index in selected
    }
    if all_fit:
        return sum(block_lengths.values()) + 2 * max(0, len(selected) - 1)
    if (
        truncated
        and len(selected) == 1
        and len(_chunk_label(rows[selected[0]])) + int(rows[selected[0]]["text_length"] or 0)
        > allowance
    ):
        return allowance
    parts: list[int] = []
    previous = -1
    for index in selected:
        gap = index - previous - 1
        if gap > 0 and previous >= 0:
            parts.append(len(f"--- … {gap} chunk(s) omitted … ---"))
        parts.append(block_lengths[index])
        previous = index
    if previous < len(rows) - 1:
        parts.append(len(f"--- … {len(rows) - 1 - previous} chunk(s) omitted … ---"))
    return sum(parts) + 2 * max(0, len(parts) - 1)


def _reassemble_selected(
    rows: list[Any],
    selected: list[int],
    texts: dict[str, str],
    allowance: int,
    *,
    truncated: bool,
    all_fit: bool,
) -> tuple[str, int, bool]:
    """Render selected chunks and omission markers after the bounded text read."""
    blocks = [_chunk_label(row) + str(texts.get(str(row["id"]), "")) for row in rows]
    if all_fit:
        return "\n\n".join(blocks), len(blocks), False
    if truncated and len(selected) == 1 and len(blocks[selected[0]]) > allowance:
        return blocks[selected[0]][:allowance], 1, True
    parts: list[str] = []
    previous = -1
    for index in selected:
        gap = index - previous - 1
        if gap > 0 and previous >= 0:
            parts.append(f"--- … {gap} chunk(s) omitted … ---")
        parts.append(blocks[index])
        previous = index
    if previous < len(rows) - 1:
        parts.append(f"--- … {len(rows) - 1 - previous} chunk(s) omitted … ---")
    return "\n\n".join(parts), len(selected), truncated
