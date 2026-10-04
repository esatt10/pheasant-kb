"""Which text files a source reads, by default and when asked.

The default ``include`` is derived from ``content_types`` so a format cannot be
accepted by the parser and silently never reached by a source (or the reverse).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.schema import DEFAULT_EXCLUDES, DEFAULT_INCLUDES, SourceConfig, SourceType
from pheasant.ingestion.content_types import TEXT_EXTENSIONS, TEXT_FILENAMES, is_text_file
from pheasant.ingestion.pipeline import parse_file
from pheasant.ingestion.walk import walk_source

SAMPLES = {
    "main.go": "package main\nfunc Main() {}\n",
    "lib.rs": "pub fn run() {}\n",
    "App.java": "class App {}\n",
    "app.kt": "fun main() {}\n",
    "util.c": "int main(void) { return 0; }\n",
    "util.hpp": "#pragma once\n",
    "Program.cs": "class Program {}\n",
    "page.vue": "<template><p>hi</p></template>\n",
    "schema.proto": 'syntax = "proto3";\n',
    "query.sql": "select 1;\n",
    "infra.tf": 'resource "x" "y" {}\n',
    "deploy.bash": "echo hi\n",
    "Dockerfile": "FROM python:3.12\n",
    "Makefile": "all:\n\techo hi\n",
    ".gitignore": "*.pyc\n",
    "data.csv": "id,name\n1,ada\n",
    "data.tsv": "id\tname\n1\tada\n",
    "server.log": "2026-10-04 INFO started\n",
    "header.hrl": "-define(X, 1).\n",
}


def _source(root: Path, **overrides) -> SourceConfig:
    return SourceConfig(name="s", type=SourceType.repository, path=root, **overrides)


def test_default_includes_cover_every_default_on_text_format():
    on_by_default = {
        pattern.removeprefix("**/*") for pattern in DEFAULT_INCLUDES if pattern.startswith("**/*")
    }
    assert on_by_default <= TEXT_EXTENSIONS
    # Only the markup/machine-output formats are opt-in.
    assert TEXT_EXTENSIONS - on_by_default == {".html", ".xml", ".patch", ".diff"}
    named = {p.removeprefix("**/") for p in DEFAULT_INCLUDES if not p.startswith("**/*")}
    assert {n.lower() for n in named} == set(TEXT_FILENAMES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_sample_is_a_text_file_and_parses(tmp_path, name):
    (tmp_path / name).write_text(SAMPLES[name])
    assert is_text_file(name)
    artifact = parse_file(_source(tmp_path), tmp_path / name)
    assert artifact is not None
    assert artifact.relative_path == name


def test_default_walk_reaches_the_samples_and_skips_generated_text(tmp_path):
    for name, body in SAMPLES.items():
        (tmp_path / name).write_text(body)
    (tmp_path / "bundle.min.js").write_text("var a=1;")
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / "page.html").write_text("<p>x</p>")
    source = _source(tmp_path)
    report = walk_source(tmp_path, include=list(DEFAULT_INCLUDES), exclude=list(DEFAULT_EXCLUDES))
    found = {p.relative_to(tmp_path).as_posix() for p in report.files}
    assert set(SAMPLES) <= found
    assert not found & {"bundle.min.js", "package-lock.json", "page.html"}
    assert source.include == list(DEFAULT_INCLUDES)


def test_unknown_extension_is_still_refused(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
    assert not is_text_file("blob.bin")
    assert parse_file(_source(tmp_path, include=["**/*"]), tmp_path / "blob.bin") is None


def test_a_notebook_indexes_its_cells_not_its_json(tmp_path):
    import json

    notebook = {
        "metadata": {"kernelspec": {"language": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": "# Churn model"},
            {
                "cell_type": "code",
                "source": ["model = fit(train)\n"],
                "outputs": [{"output_type": "stream", "text": ["noise " * 200]}],
            },
        ],
    }
    (tmp_path / "churn.ipynb").write_text(json.dumps(notebook))
    assert "**/*.ipynb" in DEFAULT_INCLUDES
    artifact = parse_file(_source(tmp_path), tmp_path / "churn.ipynb")
    text = "\n".join(chunk.text for chunk in artifact.chunks)
    assert "# Churn model" in text and "model = fit(train)" in text
    assert "noise" not in text and '"cell_type"' not in text
