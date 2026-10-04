"""A Jupyter notebook's indexable text: its cells, not its JSON.

An ``.ipynb`` file is JSON whose bulk is usually *outputs*: base64 images,
rendered HTML, stack traces. Indexing the raw file would put all of that in the
full-text index and leave the code and prose that a reader is looking for
diluted across chunks of escaped quotes. So the cells are read in order:
Markdown and raw cells as they are, code cells fenced with the notebook's
language. Outputs are dropped. Deterministic, stdlib only; a file that is not
valid notebook JSON is returned unchanged, so it still indexes as text.
"""

from __future__ import annotations

import json
from typing import Any


def notebook_text(raw: str) -> str:
    try:
        notebook = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        return raw
    language = _language(notebook)
    parts: list[str] = []
    for cell in notebook["cells"]:
        if not isinstance(cell, dict):
            continue
        source = _source(cell.get("source"))
        if not source.strip():
            continue
        if cell.get("cell_type") == "code":
            parts.append(f"```{language}\n{source.rstrip()}\n```")
        else:
            parts.append(source.rstrip())
    return "\n\n".join(parts) + ("\n" if parts else "")


def _source(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(line) for line in value)
    return str(value or "")


def _language(notebook: dict[str, Any]) -> str:
    metadata = notebook.get("metadata")
    if not isinstance(metadata, dict):
        return "python"
    info = metadata.get("language_info")
    if isinstance(info, dict) and isinstance(info.get("name"), str):
        return info["name"]
    kernel = metadata.get("kernelspec")
    if isinstance(kernel, dict) and isinstance(kernel.get("language"), str):
        return kernel["language"]
    return "python"
