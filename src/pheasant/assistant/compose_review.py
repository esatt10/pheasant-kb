"""Check a composed artifact against the passages it was written from.

A compose answer (``chat.COMPOSE_SYSTEM``) is assembled from parts the
passages document, and the way it goes wrong is specific: an identifier the
documentation never had (a removed argument, a plausible invention), or a real
one carried onto the wrong component — an argument of one resource written on
another. Neither is visible to a reader who has not got the documentation open,
and neither shows in a grounded/responsive score. Measured on one corpus: three
of four 18-resource configurations passed the target tool's own validator, and
the fourth failed on exactly these two shapes, once each.

So the artifact is checked before it is returned, in two passes:

1. **Suspects, deterministically.** Every assignment key in the artifact's code
   blocks that appears in *no* passage, and is not a name the artifact declares
   or reaches through a ``var.`` / ``local.`` / ``each.`` chain, is a suspect.
   No model, no format knowledge beyond ``key = value`` / ``key: value``.
2. **Reviewed.** One JSON call reads the artifact beside the passages, is told
   the suspects, and lists what is really wrong — a confirmed suspect, or an
   identifier documented for a different component than the one it is written
   on, which a presence check cannot see — with the passage for the right form.

Suspects are not acted on while a reviewer answers: an artifact's own map keys
look exactly like undocumented arguments, and 16 of them sent one correct
configuration to a rewrite that broke it. Only with no reviewer do they go to
the writer unconfirmed. What is confirmed goes back as a list — one rewrite of
the whole artifact with exactly those identifiers corrected. Nothing confirmed:
nothing is rewritten, and the step says the check ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pheasant.assistant.replies import json_object

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
#: ``key = value`` / ``key: value`` at the start of a line. Snake-case keys of
#: three characters or more only: single words (``name``, ``type``) are too
#: common in prose to be evidence either way.
_KEY = re.compile(r"^\s*([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*[=:](?!=)", re.M)
#: Names the artifact declares itself: quoted labels, and every segment of a
#: ``var.`` / ``local.`` / ``module.`` / ``each.`` chain (``local.names.key_vault``
#: makes ``key_vault`` a key of the artifact's own map). Its own vocabulary,
#: which no passage is expected to contain.
_LABEL = re.compile(r"\"([A-Za-z0-9_\-]+)\"")
_CHAIN = re.compile(r"\b(?:var|local|module|each)((?:\.[A-Za-z0-9_]+)+)")
#: A labelled block: ``kind "type" "name" {``. The first label is the
#: component type its keys are checked against; a block with one label
#: (``variable "x" {``) declares the artifact's own name and has no type.
_HEADER = re.compile(r'^\s*([A-Za-z_][\w-]*)\s+"([^"]+)"\s+"[^"]+"\s*\{')

REVIEW_SYSTEM = """You review a configuration or program that was assembled \
from documentation passages. Check every identifier it uses — resource or \
component types, block names, argument and attribute names — against the \
passages, component by component.

Reply with JSON only, no prose:
{"issues": [{"component": "the component it is written on", \
"identifier": "the identifier", "problem": "why it is wrong", \
"fix": "the documented identifier to use instead, or 'remove'", \
"passage": "the passage number documenting the right form, or none"}]}

Report an identifier ONLY when:
- the passages document it for a DIFFERENT component than the one it is \
written on, or
- the passages document a different spelling of it for this component (for \
example a newer name), or
- no passage documents it at all.
Names the artifact declares itself (its own variables, outputs, labels, the \
keys of its own maps and locals) and the syntax of the language are not \
identifiers to check. Every issue names the passage that documents the right \
form, or "none" when no passage documents it. Report nothing you are unsure \
of; an empty list is the expected answer for a correct artifact.

When SUSPECTS are listed, decide each one: report it as an issue, or put it \
in "cleared" — {"issues": [...], "cleared": ["identifier", ...]} — when it is \
the artifact's own name, the language's own syntax, or a passage does document \
it for that component. A suspect on a component that you neither report nor \
clear is corrected as if reported.

List in "omitted" every component the QUESTION asks for by name that \
the artifact does not contain at all — {"omitted": ["what was asked for", ...]}. \
Leave it empty when everything asked for is there.

Finally, where the QUESTION says what a component is for or how it must \
behave, check that the artifact's configuration of it would do that. List in \
"purpose" each one that would not — {"purpose": [{"component": "...", \
"problem": "what it would do instead"}]} — and leave it empty when every \
stated purpose is met. A component whose kind or mode field (a type, kind or \
mode the passages document) is unset, or set to something other than what its \
stated purpose needs, does not meet it. Judge only against what the question \
states, not against preferences of your own."""


@dataclass
class Review:
    """What the check found, and how it found it."""

    issues: list[dict[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    #: The component each unknown key is written on, when it has one.
    where: dict[str, str] = field(default_factory=dict)
    #: Components the question asked for that the artifact leaves out.
    omitted: list[str] = field(default_factory=list)
    #: ``var.x`` references with no ``variable "x"`` declaration.
    unresolved: list[str] = field(default_factory=list)
    #: ``(component, argument)`` its reference page requires and it leaves out.
    required: list[tuple[str, str]] = field(default_factory=list)
    #: Components whose configuration would not do what the question says
    #: they are for: ``{"component", "problem"}``.
    purpose: list[dict[str, str]] = field(default_factory=list)
    #: The answer to a request for an artifact contains no code at all.
    no_artifact: bool = False
    #: Component types the artifact declares that no passage mentions at all.
    types: list[str] = field(default_factory=list)
    reviewed: bool = False
    error: str | None = None

    def found(self) -> bool:
        return bool(
            self.issues
            or self.unknown
            or self.omitted
            or self.unresolved
            or self.required
            or self.purpose
            or self.no_artifact
            or self.types
        )

    def revision(self) -> str:
        """The rewrite instruction: exactly what to correct, nothing else."""

        lines = []
        if self.no_artifact:
            lines.append(
                "- The answer contains no artifact. Write it: a value the passages leave "
                "open becomes a placeholder variable, and what they do not document is "
                "listed after the artifact, never instead of it."
            )
        for name in self.unknown:
            on = self.where.get(name)
            lines.append(
                f"- `{name}` on {on} is not documented for that component in any passage."
                if on
                else f"- `{name}` appears in none of the passages."
            )
        for issue in self.issues:
            fix = issue.get("fix") or "use the documented identifier"
            lines.append(
                f"- `{issue.get('identifier')}` on {issue.get('component') or 'its component'}: "
                f"{issue.get('problem') or 'not documented there'} (fix: {fix})."
            )
        for component, argument in self.required:
            lines.append(
                f"- {component} leaves out `{argument}`, which its reference page marks "
                "Required: add it as that page documents it."
            )
        for name in self.unresolved:
            lines.append(
                f"- `var.{name}` is referenced but never declared: declare it as a variable "
                "with a placeholder default."
            )
        for kind in self.types:
            lines.append(
                f"- `{kind}` is a component type no passage documents: use the type the "
                "passages document for that component, or remove it."
            )
        for component in self.omitted:
            lines.append(
                f"- {component} was asked for and is missing: add it, using only what the "
                "passages document for it."
            )
        for entry in self.purpose:
            lines.append(
                f"- {entry.get('component') or 'A component'} would not do what the question "
                f"asks of it ({entry.get('problem') or 'its configuration contradicts it'}): "
                "change its design so it does, using only what the passages document."
            )
        if self.no_artifact:
            closing = (
                "\nWrite the WHOLE artifact now, complete, each file in its own fenced code block."
            )
        else:
            closing = (
                "\nThe draft follows the passages as DRAFT TO CORRECT. Return the WHOLE draft "
                "again, complete, with the same files each in its own fenced code block, "
                "making exactly these corrections — use the documented identifier for each "
                "component, remove a line the passages document nothing for, and add what is "
                "missing. Keep every other line of the draft as it is."
            )
        return (
            "\n\nREVISION: the artifact was checked against the question and, identifier by "
            "identifier, against the passages. Correct exactly these:\n"
            + "\n".join(lines)
            + closing
        )

    def summary(self) -> str:
        if self.error and not self.found():
            return f"review unavailable ({self.error}); returned as written"
        if not self.found():
            how = "checked against the passages and reviewed" if self.reviewed else "checked"
            return f"every identifier {how}; nothing to correct"
        parts = []
        named = [*self.unknown, *(str(i.get("identifier")) for i in self.issues)]
        if named:
            shown = ", ".join(f"`{n}`" for n in named[:6]) + ("…" if len(named) > 6 else "")
            parts.append(f"{len(named)} identifier(s) not documented where used ({shown})")
        if self.unresolved:
            parts.append(f"{len(self.unresolved)} undeclared reference(s)")
        if self.required:
            shown = ", ".join(f"`{a}` on {c}" for c, a in self.required[:3])
            parts.append(f"{len(self.required)} required argument(s) missing ({shown})")
        if self.omitted:
            shown = ", ".join(self.omitted[:4]) + ("…" if len(self.omitted) > 4 else "")
            parts.append(f"{len(self.omitted)} requested component(s) missing ({shown})")
        if self.purpose:
            shown = ", ".join(str(p.get("component")) for p in self.purpose[:3])
            parts.append(
                f"{len(self.purpose)} component(s) not fit for their stated purpose ({shown})"
            )
        if self.no_artifact:
            parts.append("no artifact in the answer")
        if self.types:
            shown = ", ".join(f"`{t}`" for t in self.types[:4])
            parts.append(f"{len(self.types)} component type(s) no passage documents ({shown})")
        return "; ".join(parts)


def code_blocks(answer: str) -> list[str]:
    return _FENCE.findall(answer or "")


def _present(key: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9_]){re.escape(key)}(?![a-z0-9_])", haystack))


def _declared(code: str) -> set[str]:
    declared = set(_LABEL.findall(code))
    for chain in _CHAIN.findall(code):
        declared.update(part for part in chain.split(".") if part)
    return declared


def _blocks(code: str) -> list[tuple[str | None, str | None, str]]:
    """``(keyword, type, text)`` for each top-level block; untyped text has
    ``None`` for both.

    Brace depth is counted per line, which is all a labelled-block format
    needs; a format without labelled blocks comes back as untyped text and is
    checked against every passage.
    """
    out: list[tuple[str | None, str | None, str]] = []
    depth, keyword, current, buffer = 0, None, None, []
    for line in code.split("\n"):
        if depth == 0:
            match = _HEADER.match(line)
            keyword, current = (match.group(1), match.group(2)) if match else (None, None)
            buffer = [line]
        else:
            buffer.append(line)
        depth = max(0, depth + line.count("{") - line.count("}"))
        if depth == 0:
            out.append((keyword, current, "\n".join(buffer)))
            keyword, current, buffer = None, None, []
    if buffer:
        out.append((keyword, current, "\n".join(buffer)))
    return out


def components(code: str) -> list[tuple[str | None, str]]:
    """``(type, text)`` for each top-level block of any kind."""

    return [(kind, body) for _, kind, body in _blocks(code)]


def declared_components(code: str) -> list[tuple[str | None, str]]:
    """``(type, text)`` with only the artifact's primary declaration kind typed.

    One type name can head blocks of different kinds — a declaration and a
    lookup of the same thing — and they are documented apart, so checking a
    lookup against the declaration's page flags arguments it was never meant
    to have. The kind the artifact uses most is the one it declares with; any
    other kind is checked like untyped text.
    """
    blocks = _blocks(code)
    counts: dict[str, int] = {}
    for keyword, kind, _ in blocks:
        if keyword and kind:
            counts[keyword] = counts.get(keyword, 0) + 1
    primary = max(counts, key=lambda k: counts[k]) if counts else None
    return [(kind if keyword == primary else None, body) for keyword, kind, body in blocks]


def page_name(document: Any) -> str:
    """What a document is called: its title, else its path.

    Hydrated evidence (``retrieval.Document``) has a ``relative_path`` and no
    ``title``. Reading ``title`` alone found no component's own page in a real
    answer, so every per-page check fell back silently, while every test,
    whose documents carried a title, passed.
    """
    return str(getattr(document, "title", None) or getattr(document, "relative_path", None) or "")


def names_page(component: str, title: str) -> bool:
    """Whether ``title`` is ``component``'s own page: a file named after it.

    The file stem is the component's name or its trailing segment —
    ``azurerm_subnet`` and ``subnet.html.markdown``,
    ``Microsoft.ApiManagement/service/backends`` and ``backends``.
    """
    wanted = component.lower()
    stem = title.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0].lower()
    return len(stem) >= 4 and (
        wanted == stem or wanted.endswith(("_" + stem, "." + stem, "/" + stem))
    )


def reference_pages(component: str, documents: dict) -> list[str]:
    """The passages that are ``component``'s own page, named after it."""

    return [
        document.text or ""
        for document in documents.values()
        if names_page(component, page_name(document))
    ]


def component_evidence(component: str, documents: dict) -> str | None:
    """What documents ``component``: its own reference page when there is one,
    else every example block of that type in any passage. ``None`` when nothing
    does, so the key is checked against every passage instead.

    The page outranks other pages' examples: an example inside another
    component's page is maintained with that component and goes stale — the
    private-endpoint page's subnet still sets an argument the subnet's own page
    no longer has, and counting it let the removed argument through.
    """

    pages = reference_pages(component, documents)
    if pages:
        return "\n".join(pages).lower()
    wanted = component.lower()
    examples = [
        body
        for document in documents.values()
        for kind, body in components(document.text or "")
        if kind and kind.lower() == wanted
    ]
    return "\n".join(examples).lower() if examples else None


_ARGUMENTS = re.compile(r"^#+\s*arguments?\s+reference", re.I | re.M)
_REQUIRED = re.compile(r"^\*\s+`([a-z][a-z0-9_]*)`\s+-\s+\(Required\)", re.M)


def required_arguments(page: str) -> list[str]:
    """Top-level arguments a reference page marks ``(Required)``.

    Read from its arguments section up to the first nested block's own list
    ("An `x` block supports…") or the next heading, so a field that is required
    only inside an optional nested block is never demanded at the top.
    """
    start = _ARGUMENTS.search(page)
    if not start:
        return []
    section = page[start.end() :]
    stop = re.search(r"^#+\s|block supports", section, re.M | re.I)
    return list(dict.fromkeys(_REQUIRED.findall(section[: stop.start()] if stop else section)))


def missing_required(answer: str, documents: dict) -> list[tuple[str, str]]:
    """``(component, argument)`` for each required argument a block leaves out."""

    code = "\n".join(code_blocks(answer))
    missing: list[tuple[str, str]] = []
    for component, body in declared_components(code):
        if not component:
            continue
        for page in reference_pages(component, documents):
            for argument in required_arguments(page):
                if not re.search(rf"^\s*{re.escape(argument)}\s*[={{]", body, re.M):
                    if (component, argument) not in missing:
                        missing.append((component, argument))
    return missing


def undocumented_types(answer: str, documents: dict) -> list[str]:
    """Component types the artifact declares that appear in no passage at all.

    An invented type is the one identifier a key check cannot see, because it
    has no page to check keys against. Measured: told to prefer one provider,
    the writer declared a resource type that provider does not have, and the
    target tool refused the artifact.
    """
    code = "\n".join(code_blocks(answer))
    everything = "\n".join(document.text or "" for document in documents.values()).lower()
    kinds = dict.fromkeys(kind for kind, _ in declared_components(code) if kind)
    return [kind for kind in kinds if not _present(kind.lower(), everything)]


def suspects(answer: str, documents: dict) -> dict[str, str | None]:
    """Assignment keys the passages do not document where the artifact uses them.

    A key inside a labelled block must be documented for that block's type;
    any other key must appear in some passage. The artifact's own names are
    never suspects. Returns ``{key: component or None}`` in artifact order.

    A component whose own page was read only in part (an excerpt) cannot
    show that a key is absent from it, so there a key is a suspect only when
    no passage has it at all. Measured: eleven arguments of excerpted pages,
    every one valid, were sent to a rewrite as undocumented.
    """
    code = "\n".join(code_blocks(answer))
    if not code:
        return {}
    declared = _declared(code)
    everything = "\n".join(document.text or "" for document in documents.values()).lower()
    scopes: dict[str, str | None] = {}
    partial: dict[str, bool] = {}
    found: dict[str, str | None] = {}
    for component, body in declared_components(code):
        scope = everything
        if component:
            if component not in scopes:
                scopes[component] = component_evidence(component, documents)
                own = [d for d in documents.values() if names_page(component, page_name(d))]
                partial[component] = bool(own) and all(getattr(d, "truncated", False) for d in own)
            scope = everything if partial[component] else scopes[component] or everything
        for key in _KEY.findall(body):
            if key not in declared and key not in found and not _present(key, scope):
                found[key] = component
    return found


_VARIABLE = re.compile(r'^\s*variable\s+"([A-Za-z0-9_\-]+)"', re.M)
_VAR_REF = re.compile(r"\bvar\.([A-Za-z0-9_\-]+)")


def unresolved(answer: str) -> list[str]:
    """Variables the artifact references and never declares, in order.

    Only where the artifact declares variables at all, so a format without
    ``variable`` blocks is never held to the convention. Deterministic: an
    undeclared reference is a defect whatever any reviewer says about it.
    """
    code = "\n".join(code_blocks(answer))
    declared = set(_VARIABLE.findall(code))
    if not declared:
        return []
    return [name for name in dict.fromkeys(_VAR_REF.findall(code)) if name not in declared]


def documented_where_used(identifier: str, code: str, documents: dict) -> bool:
    """True when every labelled block using ``identifier`` as a key has it in its
    own component's evidence. Untyped uses decide nothing (``False``)."""

    key = re.compile(rf"^\s*{re.escape(identifier)}\s*[=:{{]", re.M)
    uses = [kind for kind, body in declared_components(code) if kind and key.search(body)]
    if not uses:
        return False
    for kind in uses:
        scope = component_evidence(kind, documents)
        if scope is None or not _present(identifier.lower(), scope):
            return False
    return True


def undocumented_keys(answer: str, evidence_text: str) -> list[str]:
    """Assignment keys in the artifact's code that no passage contains."""

    code = "\n".join(code_blocks(answer))
    if not code:
        return []
    declared, haystack = _declared(code), evidence_text.lower()
    seen: list[str] = []
    for key in _KEY.findall(code):
        if key not in declared and key not in seen and not _present(key, haystack):
            seen.append(key)
    return seen


def review(
    llm: Any,
    question: str,
    answer: str,
    documents: dict,
    *,
    max_chars: int,
    max_output_tokens: int = 2000,
    reference: dict | None = None,
) -> Review:
    """Run both passes. Never raises: an unavailable reviewer is a note.

    ``reference`` holds components' own pages read for the deterministic
    checks only; the model reviewer sees the passages the answer was written
    from, so what it is asked about is what the writer had.
    """

    texts = []
    for index in sorted(documents):
        document = documents[index]
        texts.append(f"[{index}] {page_name(document)}\n{document.text or ''}")
    evidence = "\n\n".join(texts)
    pages = {**documents, **(reference or {})}
    suspected = suspects(answer, pages)
    where = {key: component for key, component in suspected.items() if component}
    result = Review(
        unknown=list(suspected),
        where=where,
        unresolved=unresolved(answer),
        required=missing_required(answer, pages),
        types=undocumented_types(answer, pages),
    )
    if not code_blocks(answer):
        # A request for an artifact answered with none: measured, a writer told
        # every component must work declined the whole request in prose, and
        # with nothing to check the review used to report "nothing to correct".
        result.no_artifact = True
        return result
    if llm is None:
        return result
    listed = ", ".join(f"{k} (on {c})" if c else k for k, c in suspected.items())
    hint = (
        "SUSPECTS (not documented for the component they are written on; report or clear "
        f"each): {listed}\n\n"
        if suspected
        else ""
    )
    prompt = (
        f"QUESTION: {question}\n\n{hint}ARTIFACT:\n{answer}\n\nPASSAGES:\n{evidence[:max_chars]}"
    )
    try:
        raw = llm.complete(
            REVIEW_SYSTEM, prompt, max_output_tokens=max_output_tokens, json_mode=True
        )
    except Exception as exc:  # noqa: BLE001 - a review never fails the answer
        result.error = str(exc).splitlines()[0][:80] if str(exc) else type(exc).__name__
        return result
    parsed = json_object(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("issues"), list):
        result.error = "reply was not the expected JSON"
        return result
    result.reviewed = True
    cleared = {
        str(item).strip().strip("`") for item in parsed.get("cleared") or [] if str(item).strip()
    }
    code = "\n".join(code_blocks(answer))
    for entry in parsed["issues"]:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("identifier") or "").strip().strip("`")
        # Only what the artifact really contains: a reviewer citing an
        # identifier the code does not use would have a rewrite chase nothing.
        if not identifier or identifier not in code:
            continue
        if any(issue["identifier"] == identifier for issue in result.issues):
            continue
        # The passages outrank the reviewer: an identifier the component's own
        # evidence documents is not wrong there, however the reviewer read a
        # deprecation note elsewhere (measured: a correct subnet argument
        # "fixed" back to the name the provider had removed).
        if documented_where_used(identifier, code, pages):
            continue
        result.issues.append(
            {
                "component": str(entry.get("component") or "").strip(),
                "identifier": identifier,
                "problem": str(entry.get("problem") or "").strip()[:200],
                "fix": str(entry.get("fix") or "").strip()[:120],
            }
        )
    # A suspect on a component that the reviewer neither reported nor cleared is
    # still a suspect: silence is not evidence that a key the scoped check could
    # not find on that component is fine. An untyped one (outside any labelled
    # block) is mostly the language's own syntax, so it needs a confirmation.
    reported = {issue["identifier"] for issue in result.issues}
    result.unknown = [
        key
        for key, component in suspected.items()
        if component and key not in cleared and key not in reported
    ]
    result.omitted = [
        " ".join(str(item).split())[:120]
        for item in parsed.get("omitted") or []
        if isinstance(item, str) and item.strip()
    ][:12]
    result.purpose = [
        {
            "component": " ".join(str(entry.get("component") or "").split())[:120],
            "problem": " ".join(str(entry.get("problem") or "").split())[:200],
        }
        for entry in parsed.get("purpose") or []
        if isinstance(entry, dict) and str(entry.get("component") or "").strip()
    ][:8]
    return result
