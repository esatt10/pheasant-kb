"""What the Decisions API is asked: the miss kinds, the predicate texts, and
the follow-up searches a choice question offers.

The judgement wording lives apart from the gates that act on the answers, so
``decision_gates`` stays reviewable as a unit; it re-exports every name here.
"""

from __future__ import annotations

import re

#: Kinds of miss, as the sufficiency request offers them. The description is
#: what the trace says is missing, since the API writes no prose of its own.
MISS_KINDS: dict[str, str] = {
    "none": "Nothing the question needs is missing.",
    "content": "The passages are about the right thing but lack the specific facts, steps "
    "or examples the answer needs.",
    "location": "The passages are the right kind of file from the wrong package, directory "
    "or component.",
    "kind": "The passages are the wrong kind of file: documentation where source code or a "
    "usage example is needed, or tests instead of the implementation.",
    "absent": "Nothing in the passages bears on the question; the knowledge base may not cover it.",
}
#: Insufficient, yet the miss choice said "none": the two judgements are
#: independent questions and can disagree. Said as it is rather than reported
#: as "missing: none" beside "insufficient".
UNCLEAR = "Below the sufficiency threshold, with no specific gap named."

#: A question can rest on a premise the corpus contradicts ("What replaced
#: PDFium?" of a region where PDFium is what replaced MuPDF). The correct
#: answer is then the correction, and the passages that state it are enough;
#: judged as "nothing answers what replaced it", the loop spent its rounds
#: searching for a successor that does not exist and stamped a correct answer
#: with an "absent" evidence gap.
PREMISE = (
    " If the passages show that the question rests on a mistaken premise (it assumes "
    "something the passages contradict), that is enough: the correct answer is the "
    "correction."
)
SUFFICIENCY = (
    "Taken together, do the passages contain enough evidence to answer the question "
    "correctly and specifically? Judge the substance, not the wording, and use the file "
    "metadata: a result set from the wrong place or of the wrong kind is not enough even "
    "when each passage reads plausibly." + PREMISE
)
CRITERIA = {
    "knowledge": " This is a knowledge-summary question: enough means one could say what the "
    "thing is, what its main parts are and what each is for. Exhaustive coverage is not "
    "required.",
    "procedural": " This is a procedural question: enough means a followable sequence AND at "
    "least one real example with the actual identifiers, arguments or config keys. Naming the "
    "right file, or describing a capability in prose, is not enough.",
}
#: Compose replaces the question rather than extending it: "enough evidence to
#: answer the question" reads as "a passage shows the finished artifact", which
#: no corpus holds — measured p=0.09 on passages documenting every one of the 18
#: components asked for, and the round it bought searched the question verbatim.
SUFFICIENCY_COMPOSE = (
    "The question asks for a NEW artifact assembled from parts, so no passage is expected to "
    "contain the finished artifact and its absence is not a gap. For each component the "
    "question names, is there a passage documenting it — its actual type or API and its "
    "arguments? Say yes when every named component is documented, even though no passage "
    "shows them combined; say no when a named component has no such passage."
)

GROUNDED = (
    "Is the ANSWER supported by the passages it cites with [n] markers? Say no if it states "
    "something a cited passage contradicts, attributes a claim to a passage that does not "
    "support it, or makes a substantive factual claim that none of the passages supports. "
    "Summaries, paraphrases and connecting sentences that follow from the passages count as "
    "supported, and saying that evidence is missing is not a claim."
)
#: The grounding question read for an artifact. Measured: the plain question
#: scored a composed configuration grounded p=0.37 — wiring documented parts
#: together reads as "a claim no passage states" — and the rewrite it asked for
#: deleted most of the artifact (responsive 0.86 -> 0.02).
GROUNDED_COMPOSE = (
    " This ANSWER is a new artifact assembled from documented parts, so no passage contains it "
    "whole. Count as supported: connecting documented components by reference, names and "
    "placeholder values the author chose, and variables standing for values the reader must "
    "supply. Say no only if it uses a type, block, argument, attribute or API that the "
    "passages do not document, or uses one in a way a passage contradicts."
)
RESPONSIVE = (
    "Does the ANSWER respond to the QUESTION that was asked? An answer that says plainly what "
    "the knowledge base does not cover counts as responding."
)
#: Asked beside the acceptance pair only when the pre-writing sufficiency
#: judgement left an evidence gap to report. That judgement read 700-character
#: previews; the writer read whole documents, and an answer it wrote can settle
#: what the previews did not, most often by correcting the question's premise.
#: The gap line is added only when this says the answer leaves part unsettled.
SETTLED = (
    "Does the ANSWER settle the QUESTION from the passages: answering every part of it, or "
    "showing from the passages that the question rests on a mistaken premise and saying what "
    "is true instead? Say no if part of what was asked is left unanswered for lack of "
    "evidence."
)

_STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from how i in is it its me of on or "
    "should show tell that the their there these this to use using was what when where which "
    "who why will with would you your about into does explain describe".split()
)


def followup_candidates(
    question: str, intent: str, passages: list, shapes: dict, searched: list[str]
) -> list[str]:
    """Follow-up searches built from what the evidence already names.

    The Decisions API picks; it cannot write. So the candidates come from
    the hits' own directories and symbols and from the question's terms —
    the structural vocabulary the model planner is told to use, read off the
    index rather than invented. Deterministic: the same evidence offers the
    same candidates.
    """

    from pheasant.assistant.workflows.agentic import _dedupe, _query_key

    terms = [
        word
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-]*", question)
        if word.lower() not in _STOPWORDS and len(word) > 2
    ]
    base = " ".join(dict.fromkeys(terms))[:80].strip()
    if not base:
        return []
    short = " ".join(list(dict.fromkeys(terms))[:3])
    candidates: list[str] = []
    directories: list[str] = []
    for passage in passages[:8]:
        path = str(passage.relative_path or "")
        if "/" in path:
            directory = path.rsplit("/", 1)[0]
            if directory not in directories:
                directories.append(directory)
    candidates += [f"{directory} {short}" for directory in directories[:3]]
    lowered = question.lower()
    symbols: list[str] = []
    for meta in shapes.values():
        for symbol in meta.get("symbols") or []:
            if symbol.lower() not in lowered and symbol not in symbols:
                symbols.append(str(symbol))
    wants_examples = intent in ("procedural", "compose")
    suffix = "example usage" if wants_examples else ""
    candidates += [f"{symbol} {suffix}".strip() for symbol in symbols[:3]]
    if wants_examples:
        candidates.append(f"{base} example configuration")
    else:
        candidates.append(f"{base} overview")
    done = {_query_key(query) for query in searched}
    return [query for query in _dedupe(candidates) if _query_key(query) not in done][:6]
