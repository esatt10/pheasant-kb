"""What the Decisions API is asked: the miss kinds and the predicate texts.

The judgement wording lives apart from the gates that act on the answers, so
``decision_gates`` stays reviewable as a unit; it re-exports every name here.
"""

from __future__ import annotations

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

SUFFICIENCY = (
    "Taken together, do the passages contain enough evidence to answer the question "
    "correctly and specifically? Judge the substance, not the wording, and use the file "
    "metadata: a result set from the wrong place or of the wrong kind is not enough even "
    "when each passage reads plausibly."
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
