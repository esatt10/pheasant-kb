"""L0 write-path normalization for memory records (Phase 1/6).

The governing rule for the whole compaction design: **L0 is an exact
equivalence relation over surface form, not a similarity judgement.**
Everything fuzzy (near-duplicate clustering, medoid selection, LLM
synthesis) happens offline in `memory.compaction` / `memory.synthesis`,
where the output is a reversible re-tier. This module runs on the write
path and is irreversible from the caller's point of view — a write that
matches here never becomes a file — so it may only fold genuinely
identical assertions: different casing, different whitespace, a
different framing sentence around the *same* claim.

Two things it deliberately does **not** do, because both would make it a
lossy similarity judgement rather than an equivalence:

* **No token-sort.** "A supersedes B" and "B supersedes A" would become
  identical under a sorted-token key, and they assert opposite things.
  Anything with argument structure — precedence, ownership, direction,
  before/after — breaks under token-sort. Reordering only ever collapses
  whitespace, never token order.
* **No stopword removal.** A quantifier like "not", "all", "any", "every"
  changes what a *fact* means; stripping it (as `search.sqlite_store`'s
  query-framing stopword list does, correctly, for ranking) would invert
  the assertion's truth value. Nothing here reads a stopword list.

`NORMALIZER_VERSION` is stamped into every key this module produces, for
the same reason `_digest_input` in `memory.store` is versioned: a future
change to the normalization rules must start a new equivalence class
rather than silently reshuffle which existing writes reinforce which
existing records.
"""

from __future__ import annotations

import hashlib
import unicodedata

NORMALIZER_VERSION = 1

#: A closed, deliberately small list. Each is a *framing* clause — it says
#: something about the act of asserting, not about the fact itself — so
#: stripping it cannot change what the sentence claims. Matched only at the
#: very start of the (already casefolded, whitespace-collapsed) text.
_FRAMING_PREFIXES = (
    "note that",
    "please note",
    "remember that",
    "just so you know",
    "for the record",
    "for reference",
    "heads up",
    "fyi",
    "reminder",
)

#: Reuses the shape of `memory.bridge._LEADING_ARTICLES` (a prefix strip),
#: not the function itself — `normalize_label` there uses `.lower()` with
#: no NFKC, which is fine for ASCII entity labels and wrong for content
#: identity (see `normalized_text` below).
_LEADING_ARTICLES = ("the ", "a ", "an ")

_TRAILING_PUNCTUATION = ".,;:!? \t\r\n"
_FRAMING_SEPARATORS = " \t:,-–—"  # includes en dash, em dash


def _strip_leading_framing(text: str) -> str:
    for prefix in _FRAMING_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            return rest.lstrip(_FRAMING_SEPARATORS)
    return text


def _strip_leading_article(text: str) -> str:
    for article in _LEADING_ARTICLES:
        if text.startswith(article):
            return text[len(article) :]
    return text


def normalized_text(text: str) -> str:
    """The comparable form of a memory's text. Order-preserving throughout.

    NFKC (so visually/semantically identical Unicode forms compare equal)
    -> casefold (stronger than `.lower()` across scripts) -> whitespace
    collapse -> strip trailing punctuation -> strip one leading framing
    clause -> strip one leading article. Each step only removes
    presentation, never reorders or drops content words.
    """
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.casefold()
    value = " ".join(value.split())
    value = value.rstrip(_TRAILING_PUNCTUATION)
    value = _strip_leading_framing(value)
    value = _strip_leading_article(value)
    return value.strip()


def acl_class_for(scope: str, written_by: str | None) -> str:
    """The ACL-equivalence partition a reinforcement bucket must respect.

    Mirrors `security.acl.normalize_acl`'s ``connector_type == "memory"``
    branch exactly, because that function is what actually decides who can
    read a record: ``org`` is a shared assertion (one partition, ``"*"``);
    ``user``/``session`` are readable only by their writer (one partition
    per ``written_by`` value); an unattributed ``user``/``session`` record
    (no recorded writer) falls through to the region default there, so it
    is bucketed under the empty string here too — it can only ever match
    another equally-unattributed record, never a written one, so this
    never widens what `normalize_acl` already exposes to a reader.

    Without this partition, two principals asserting the same sentence at
    `user` scope would collapse into one record carrying only one writer's
    ACL — a cross-principal read through the write path. See
    `tests/test_memory_hardening.py`'s reinforcement-ACL case.
    """
    if scope == "org":
        return "*"
    return str(written_by or "")


def normalized_key(*, scope: str, subject: str | None, kind: str, acl_class: str, text: str) -> str:
    """The full L0 equivalence key: everything two records must share to
    fold into one, plus the versioned, normalized text."""
    return (
        f"v{NORMALIZER_VERSION}|{scope}|{subject or ''}|{kind}|{acl_class}|{normalized_text(text)}"
    )


def normalized_digest(
    *, scope: str, subject: str | None, kind: str, acl_class: str, text: str
) -> str:
    """blake2b(8 bytes) of `normalized_key`, matching `_digest_input`'s
    digest size in `memory.store` — same construction, different key space."""
    key = normalized_key(scope=scope, subject=subject, kind=kind, acl_class=acl_class, text=text)
    return hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest()
