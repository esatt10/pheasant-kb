"""The Decisions API's questions and answers, spoken as System One.

Microsoft-Decision-1, Microsoft's decision model on Azure AI Foundry, does not
answer OpenAI's ``/decisions`` shape. It answers the System One API at
``{resource}/providers/microsoft/v1/systemone``: one ``state`` to evaluate and
a *map* of typed questions, each answered with a probability. The judgements
are the same kind, so :class:`~pheasant.assistant.decisions.Decider` keeps
asking OpenAI-shaped questions and reading OpenAI-shaped answers, and this
module is the translation either side of the wire — which is what keeps the
gates in ``workflows/decision_gates`` unaware of which model judged.

=================  =============================  ==========================
Decisions          System One question            System One answer
=================  =============================  ==========================
``predicate``      ``noul``                       ``noul`` (a probability)
``choice``         ``choice``; ``criteria`` maps  ``choice`` + ``probabilities``
                   each value to its description  keyed by value
``score``          ``score``; ``criteria`` lists  ``score`` + ``probabilities``
                   the levels, lowest first       keyed by level index
=================  =============================  ==========================

Order is meaning, not presentation: the model reads choice options in the
order sent, so they go as a JSON object built in the caller's order (Python's
``dict`` keeps it). A question System One leaves unanswered, or answers as
another type, comes back as a refusal — the one answer every gate already
handles by falling back.

What does not carry over is calibration. The thresholds in
``assistant.decisions`` were set against ``gpt-6-luna``'s probabilities, and a
different model's probabilities are a different scale until
``scripts/calibrate_decisions.py --api systemone`` says otherwise.
"""

from __future__ import annotations

from typing import Any


def request(model: str, input: Any, questions: list[dict[str, Any]]) -> dict[str, Any]:
    """The System One body for one Decisions request."""

    return {
        "model": model,
        "state": _state(input),
        "questions": {question["name"]: _question(question) for question in questions},
    }


def _state(input: Any) -> Any:
    """A string as it is; messages as their text parts, joined by blank lines."""

    if isinstance(input, str) or not isinstance(input, list):
        return input
    texts: list[str] = []
    for message in input:
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
            ]
    return "\n\n".join(texts)


def _question(question: dict[str, Any]) -> dict[str, Any]:
    kind = question.get("type")
    instructions = question.get("instructions")
    if kind == "predicate":
        return {"type": "noul", "instructions": instructions}
    if kind == "choice":
        # A null description is System One's spelling for an option whose
        # name says it all.
        criteria = {
            str(option["value"]): option.get("description") or None
            for option in question.get("choices") or []
        }
        return {"type": "choice", "instructions": instructions, "criteria": criteria}
    if kind == "score":
        levels = [
            {"label": level["label"], "description": level["description"]}
            if level.get("description")
            else level["label"]
            for level in question.get("levels") or []
        ]
        return {"type": "score", "instructions": instructions, "criteria": levels}
    raise ValueError(f"System One has no question type for {kind!r}")


def answers(data: Any, questions: list[dict[str, Any]]) -> dict[str, Any]:
    """A System One answer in the Decisions response shape.

    Answers come back in question order under the names they were asked
    with, and usage under the Decisions spelling, so
    :func:`pheasant.assistant.decisions._parse` reads either wire.
    """

    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return {"answers": None}
    raw = data["answers"]
    out = [_answer(question, raw.get(question["name"])) for question in questions]
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "answers": out,
        "model": data.get("model"),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    }


def _answer(question: dict[str, Any], raw: Any) -> dict[str, Any]:
    name, kind = question["name"], question.get("type")
    refusal = {"name": name, "type": "refusal"}
    if not isinstance(raw, dict):
        return refusal
    probabilities = raw.get("probabilities") if isinstance(raw.get("probabilities"), dict) else {}
    if kind == "predicate" and raw.get("type") == "noul" and raw.get("noul") is not None:
        return {"name": name, "type": "predicate", "probability": raw["noul"]}
    if kind == "choice" and raw.get("type") == "choice":
        values = [str(option["value"]) for option in question.get("choices") or []]
        if str(raw.get("choice")) not in values:
            return refusal
        return {
            "name": name,
            "type": "choice",
            "choice": str(raw["choice"]),
            "confidence": raw.get("confidence"),
            "probabilities": [
                {"value": value, "probability": probabilities.get(value, 0.0)} for value in values
            ],
        }
    if kind == "score" and raw.get("type") == "score" and raw.get("score") is not None:
        return {
            "name": name,
            "type": "score",
            "score": raw["score"],
            "confidence": raw.get("confidence"),
            "probabilities": [
                {"label": level["label"], "probability": probabilities.get(str(index), 0.0)}
                for index, level in enumerate(question.get("levels") or [])
            ],
        }
    return refusal
